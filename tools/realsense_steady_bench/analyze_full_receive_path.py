#!/usr/bin/env python3
"""Summarize one short kernel+librealsense full receive-path trace."""

from __future__ import annotations

import argparse
import bisect
from collections import defaultdict
import csv
import json
from pathlib import Path
import re
import statistics
import subprocess
from typing import Iterable


LINE = re.compile(
    r"^\s*(?P<comm>.*)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\].*?"
    r"(?P<ts>\d+\.\d+):\s+(?P<event>[^:]+):\s*(?P<body>.*)$"
)


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = quantile * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def distribution(values: Iterable[float]) -> dict[str, float | int]:
    samples = list(values)
    if not samples:
        return {key: 0 for key in ("n", "mean_us", "p50_us", "p99_us", "max_us")}
    return {
        "n": len(samples),
        "mean_us": statistics.fmean(samples) * 1_000_000.0,
        "p50_us": percentile(samples, 0.50) * 1_000_000.0,
        "p99_us": percentile(samples, 0.99) * 1_000_000.0,
        "max_us": max(samples) * 1_000_000.0,
    }


def frame_distributions(path: Path) -> dict[str, object]:
    backend_by_stream: dict[str, list[float]] = defaultdict(list)
    arrival_by_stream: dict[str, list[float]] = defaultdict(list)
    by_delivery: dict[tuple[int, str, int], list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            camera_index = int(row.get("camera_index", 0))
            serial = row.get("serial", "")
            stream = (
                f"camera{camera_index}:{serial}:{row['stream']}#{row['stream_index']}"
            )
            backend_by_stream[stream].append(float(row["backend_to_return_ms"]) / 1000.0)
            arrival_by_stream[stream].append(float(row["arrival_to_return_ms"]) / 1000.0)
            by_delivery[(camera_index, serial, int(row["delivery"]))].append(row)

    frameset_oldest: list[float] = []
    frameset_oldest_arrival: list[float] = []
    frameset_backend_skew: list[float] = []
    frameset_arrival_skew: list[float] = []
    for rows in by_delivery.values():
        ages = [float(row["backend_to_return_ms"]) / 1000.0 for row in rows]
        arrival_ages = [float(row["arrival_to_return_ms"]) / 1000.0 for row in rows]
        backend = [float(row["backend_timestamp_ms"]) for row in rows]
        arrival = [float(row["time_of_arrival_ms"]) for row in rows]
        frameset_oldest.append(max(ages))
        frameset_oldest_arrival.append(max(arrival_ages))
        frameset_backend_skew.append((max(backend) - min(backend)) / 1000.0)
        frameset_arrival_skew.append((max(arrival) - min(arrival)) / 1000.0)
    return {
        "backend_to_return": {
            stream: distribution(values) for stream, values in sorted(backend_by_stream.items())
        },
        "arrival_to_return": {
            stream: distribution(values) for stream, values in sorted(arrival_by_stream.items())
        },
        "frameset_oldest_backend_to_return": distribution(frameset_oldest),
        "frameset_oldest_arrival_to_return": distribution(frameset_oldest_arrival),
        "frameset_backend_timestamp_skew": distribution(frameset_backend_skew),
        "frameset_arrival_timestamp_skew": distribution(frameset_arrival_skew),
    }


def queue_handoff_distribution(path: Path) -> dict[str, float | int]:
    latest_enqueue: dict[int, int] = {}
    durations: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["in_steady_state"] != "True":
                continue
            sequence = int(row["sequence"])
            timestamp_ns = int(row["timestamp_ns"])
            if row["stage"] == "aggregator_enqueue_end":
                latest_enqueue[sequence] = timestamp_ns
            elif row["stage"] == "pipeline_wait_end" and int(row["result"]) == 1:
                enqueue_ns = latest_enqueue.get(sequence)
                if enqueue_ns is not None and enqueue_ns <= timestamp_ns:
                    durations.append((timestamp_ns - enqueue_ns) / 1_000_000_000.0)
    return distribution(durations)


def analyze_kernel(
    trace: Path, begin: float, end: float
) -> tuple[dict[str, object], list[dict[str, float | int]], str]:
    report = subprocess.Popen(
        ["trace-cmd", "report", "-t", "-i", str(trace)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    starts: dict[tuple[str, int], list[float]] = defaultdict(list)
    irq_starts: dict[tuple[int, int], list[float]] = defaultdict(list)
    irq_wakeup: dict[int, list[float]] = defaultdict(list)
    irq_run: dict[int, list[float]] = defaultdict(list)
    hcd_by_urb: dict[str, list[float]] = defaultdict(list)
    hcd_activation_by_urb: dict[
        str, list[tuple[float | None, int | None]]
    ] = defaultdict(list)
    xhci_by_urb: dict[str, list[float]] = defaultdict(list)
    durations: dict[str, list[float]] = defaultdict(list)
    pending_irq_wakeup: dict[int, float] = {}
    active_irq_release: dict[int, float] = {}
    running_irq: dict[int, tuple[float | None, float, int, int | None]] = {}
    threaded_intervals: list[
        tuple[float, float, float | None, int, int, int | None]
    ] = []
    hard_intervals: list[tuple[float, float, float]] = []
    hard_starts: dict[tuple[int, int], list[float]] = defaultdict(list)
    uvc_callback_starts: dict[
        int, list[tuple[float, int, float | None, int | None]]
    ] = defaultdict(list)
    # Each tuple is (callback start, callback end, xHCI activation start,
    # xHCI IRQ number).
    # The activation is captured while the callback is executing.  This is
    # more precise than searching a global list of IRQ slices because two USB
    # controllers can execute concurrently on different CPUs.
    uvc_xhci_callbacks: list[tuple[float, float, float, int | None]] = []
    video_buffers: list[dict[str, float | int]] = []

    pairs = {
        "usb_bh_begin": ("usb_bh", True),
        "usb_bh_end": ("usb_bh", False),
        "uvc_complete_begin": ("uvc_complete", True),
        "uvc_complete_end": ("uvc_complete", False),
        "uvc_copy_begin": ("uvc_copy", True),
        "uvc_copy_end": ("uvc_copy", False),
    }

    assert report.stdout is not None
    for raw in report.stdout:
        match = LINE.match(raw)
        if not match:
            continue
        timestamp = float(match.group("ts"))
        if timestamp < begin or timestamp > end:
            continue
        event = match.group("event").strip()
        body = match.group("body")
        pid = int(match.group("pid"))
        cpu = int(match.group("cpu"))

        if event in pairs:
            stage, is_begin = pairs[event]
            key = (stage, pid)
            if is_begin:
                starts[key].append(timestamp)
            elif starts[key]:
                durations[stage].append(timestamp - starts[key].pop())

        if event == "irq_handler_entry":
            irq = re.search(r"irq=(\d+)\s+name=(.*)$", body)
            if irq and "xhci" in irq.group(2):
                irq_starts[(cpu, int(irq.group(1)))].append(timestamp)
                hard_starts[(cpu, int(irq.group(1)))].append(timestamp)
        elif event == "irq_handler_exit":
            irq = re.search(r"irq=(\d+)", body)
            if irq and irq_starts[(cpu, int(irq.group(1)))]:
                durations["xhci_irq_handler"].append(
                    timestamp - irq_starts[(cpu, int(irq.group(1)))].pop()
                )
            if irq and hard_starts[(cpu, int(irq.group(1)))]:
                start = hard_starts[(cpu, int(irq.group(1)))].pop()
                hard_intervals.append((start, timestamp, start))

        if event == "sched_wakeup" and "irq/" in body and "xhci" in body:
            target = re.search(r"irq/[^:]+:(\d+)", body)
            if target:
                target_pid = int(target.group(1))
                # A wakeup emitted while the IRQ thread is already running
                # coalesces more controller work into the current activation.
                # It must not be paired with the next sched_switch into the
                # thread, or that later activation receives a false delay.
                if target_pid not in running_irq:
                    irq_wakeup[target_pid].append(timestamp)
                    pending_irq_wakeup.setdefault(target_pid, timestamp)
        elif event == "sched_switch" and "==> irq/" in body and "xhci" in body:
            target = re.search(r"==> irq/[^:]+:(\d+)", body)
            if target:
                target_pid = int(target.group(1))
                irq_number_match = re.search(r"==> irq/(\d+)-", body)
                irq_number = (
                    int(irq_number_match.group(1)) if irq_number_match else None
                )
                release = pending_irq_wakeup.pop(target_pid, None)
                if release is not None:
                    durations["xhci_irq_wakeup_to_run"].append(timestamp - release)
                    active_irq_release[target_pid] = release
                else:
                    release = active_irq_release.get(target_pid)
                irq_wakeup[target_pid].clear()
                irq_run[target_pid].append(timestamp)
                running_irq[target_pid] = (release, timestamp, cpu, irq_number)

        if event == "sched_switch" and "irq/" in match.group("comm") and "xhci" in match.group("comm"):
            active = running_irq.pop(pid, None)
            if active is not None:
                release, run, run_cpu, irq_number = active
                threaded_intervals.append(
                    (run, timestamp, release, pid, run_cpu, irq_number)
                )
            previous = re.search(r":%d\s+\[\d+\]\s+(\S+)\s+==>" % pid, body)
            if previous is not None and not previous.group(1).startswith("R"):
                active_irq_release.pop(pid, None)

        if event == "xhci_urb_giveback":
            urb = re.search(r"urb=(0x[0-9a-f]+)", body)
            if urb:
                xhci_by_urb[urb.group(1)].append(timestamp)
        elif event == "hcd_giveback":
            urb = re.search(r"urb=(0x[0-9a-f]+)", body)
            if urb:
                value = urb.group(1)
                if xhci_by_urb[value]:
                    durations["xhci_giveback_to_hcd"].append(
                        timestamp - xhci_by_urb[value].pop(0)
                    )
                hcd_by_urb[value].append(timestamp)
                activation: float | None = None
                activation_irq: int | None = None
                active = running_irq.get(pid)
                if active is not None and active[2] == cpu:
                    activation = active[0]
                    activation_irq = active[3]
                if activation is None:
                    hard_candidates = [
                        (values[-1], hard_irq)
                        for (hard_cpu, hard_irq), values in hard_starts.items()
                        if hard_cpu == cpu and values
                    ]
                    if hard_candidates:
                        activation, activation_irq = max(hard_candidates)
                hcd_activation_by_urb[value].append(
                    (activation, activation_irq)
                )
            if irq_run[pid]:
                durations["xhci_irq_run_to_first_hcd"].append(
                    timestamp - irq_run[pid][-1]
                )
                irq_run[pid].clear()
        elif event == "uvc_complete_begin":
            urb = re.search(r"urb=(0x[0-9a-f]+)", body)
            callback_activation: float | None = None
            callback_irq: int | None = None
            if urb and hcd_by_urb[urb.group(1)]:
                value = urb.group(1)
                durations["hcd_to_uvc_callback"].append(
                    timestamp - hcd_by_urb[value].pop(0)
                )
                if hcd_activation_by_urb[value]:
                    callback_activation, callback_irq = hcd_activation_by_urb[
                        value
                    ].pop(0)
            uvc_callback_starts[pid].append(
                (timestamp, cpu, callback_activation, callback_irq)
            )
        elif event == "uvc_complete_end" and uvc_callback_starts[pid]:
            callback_start, callback_cpu, activation, activation_irq = (
                uvc_callback_starts[pid].pop()
            )
            active = running_irq.get(pid)
            if activation is None and active is not None and active[2] == callback_cpu:
                activation = active[0]
                activation_irq = active[3]
            if activation is None:
                hard_candidates = [
                    (values[-1], hard_irq)
                    for (hard_cpu, hard_irq), values in hard_starts.items()
                    if hard_cpu == callback_cpu and values
                ]
                if hard_candidates:
                    activation, activation_irq = max(hard_candidates)
            if activation is not None:
                uvc_xhci_callbacks.append(
                    (callback_start, timestamp, activation, activation_irq)
                )
        elif event == "uvc_buffer_complete":
            backend = re.search(r"backend_ns=(-?\d+)", body)
            if backend and int(backend.group(1)) > 0:
                durations["uvc_frame_assembly_wall"].append(
                    timestamp - int(backend.group(1)) / 1_000_000_000.0
                )
                bytesused = re.search(r"bytesused=(\d+)", body)
                sequence = re.search(r"sequence=(\d+)", body)
                # Metadata buffers are only a few hundred bytes.  Retain the
                # video buffer whose backend timestamp marks its first
                # accepted UVC payload.
                if bytesused and int(bytesused.group(1)) >= 4096:
                    video_buffers.append(
                        {
                            "backend_s": int(backend.group(1)) / 1_000_000_000.0,
                            "complete_s": timestamp,
                            "bytesused": int(bytesused.group(1)),
                            "sequence": int(sequence.group(1)) if sequence else -1,
                        }
                    )

    if report.wait() != 0:
        raise RuntimeError("trace-cmd report failed")
    threaded_intervals.sort()
    hard_intervals.sort()
    mode = "threaded_irq" if threaded_intervals else "hard_irq"
    uvc_xhci_callbacks.sort()
    callback_starts = [item[0] for item in uvc_xhci_callbacks]
    max_callback_duration = max(
        (item[1] - item[0] for item in uvc_xhci_callbacks), default=0.0
    )
    for buffer in video_buffers:
        backend_s = float(buffer["backend_s"])
        position = bisect.bisect_right(callback_starts, backend_s) - 1
        candidates: list[tuple[float, float, float, int | None]] = []
        while position >= 0:
            callback = uvc_xhci_callbacks[position]
            if callback[0] < backend_s - max_callback_duration:
                break
            if backend_s <= callback[1]:
                candidates.append(callback)
            position -= 1
        if candidates:
            callback_start, _, release, irq_number = max(
                candidates, key=lambda item: item[0]
            )
            buffer["xhci_activation_s"] = release
            buffer["xhci_run_s"] = callback_start
            if irq_number is not None:
                buffer["xhci_irq"] = irq_number
    return (
        {stage: distribution(values) for stage, values in sorted(durations.items())},
        video_buffers,
        mode,
    )


def host_receive_latency(
    frame_events: Path,
    video_buffers: list[dict[str, float | int]],
    irq_mode: str,
    rows_output: Path,
    camera_irq_numbers: dict[int, int] | None = None,
) -> dict[str, object]:
    """Correlate selected inputs with their first-payload xHCI activation.

    The SDK timestamps have millisecond granularity.  They are used only to
    locate the matching kernel buffer-completion record.  Latency starts at
    the xHCI activation associated with that buffer's exact backend timestamp.
    """

    by_delivery: dict[tuple[int, str, int], list[dict[str, str]]] = defaultdict(list)
    with frame_events.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            camera_index = int(row.get("camera_index", 0))
            serial = row.get("serial", "")
            by_delivery[(camera_index, serial, int(row["delivery"]))].append(row)

    indexed = sorted(
        (
            (float(buffer["backend_s"]), index, buffer)
            for index, buffer in enumerate(video_buffers)
            if "xhci_activation_s" in buffer
        ),
        key=lambda item: item[0],
    )
    backend_times = [item[0] for item in indexed]
    backend_tolerance_s = 0.0025
    complete_tolerance_s = 0.0025
    latencies: list[float] = []
    latencies_by_camera: dict[tuple[int, str], list[float]] = defaultdict(list)
    matched_by_camera: dict[tuple[int, str], int] = defaultdict(int)
    totals_by_camera: dict[tuple[int, str], int] = defaultdict(int)
    output_rows: list[dict[str, object]] = []

    for (camera_index, serial, delivery), rows in sorted(by_delivery.items()):
        camera_key = (camera_index, serial)
        totals_by_camera[camera_key] += 1
        return_s = int(rows[0]["host_boottime_ns"]) / 1_000_000_000.0
        candidates: dict[int, dict[str, float | int]] = {}
        matched_components = 0
        expected_irq = (camera_irq_numbers or {}).get(camera_index)
        for row in rows:
            backend_target = return_s - float(row["backend_to_return_ms"]) / 1000.0
            complete_target = return_s - float(row["arrival_to_return_ms"]) / 1000.0
            first = bisect.bisect_left(backend_times, backend_target - backend_tolerance_s)
            last = bisect.bisect_right(backend_times, backend_target + backend_tolerance_s)
            component_candidates: list[
                tuple[float, int, dict[str, float | int]]
            ] = []
            for backend_s, index, buffer in indexed[first:last]:
                complete_error = abs(float(buffer["complete_s"]) - complete_target)
                if complete_error > complete_tolerance_s:
                    continue
                if expected_irq is not None and buffer.get("xhci_irq") != expected_irq:
                    continue
                score = abs(backend_s - backend_target) + complete_error
                component_candidates.append((score, index, buffer))
            if component_candidates:
                matched_components += 1
                best_score = min(item[0] for item in component_candidates)
                # Preserve simultaneous Depth/IR candidates while rejecting
                # a more distant camera whose millisecond SDK timestamps happen
                # to fall inside the same coarse correlation window.
                for score, index, buffer in component_candidates:
                    if score <= best_score + 0.00025:
                        candidates[index] = buffer

        if not candidates:
            continue
        activation_s = min(float(item["xhci_activation_s"]) for item in candidates.values())
        latency_s = return_s - activation_s
        if latency_s < 0.0:
            continue
        latencies.append(latency_s)
        latencies_by_camera[camera_key].append(latency_s)
        matched_by_camera[camera_key] += 1
        output_rows.append(
            {
                "camera_index": camera_index,
                "serial": serial,
                "delivery": delivery,
                "return_boottime_ns": int(rows[0]["host_boottime_ns"]),
                "xhci_activation_boottime_ns": round(activation_s * 1_000_000_000.0),
                "latency_ms": latency_s * 1000.0,
                "matched_video_buffers": len(candidates),
                "matched_components": matched_components,
                "required_components": len(rows),
                "expected_xhci_irq": expected_irq if expected_irq is not None else "",
                "irq_mode": irq_mode,
            }
        )

    with rows_output.open("w", newline="", encoding="utf-8") as handle:
        columns = [
            "camera_index",
            "serial",
            "delivery",
            "return_boottime_ns",
            "xhci_activation_boottime_ns",
            "latency_ms",
            "matched_video_buffers",
            "matched_components",
            "required_components",
            "expected_xhci_irq",
            "irq_mode",
        ]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(output_rows)

    result = distribution(latencies)
    per_camera = []
    for camera_key in sorted(totals_by_camera):
        camera_index, serial = camera_key
        camera_result = distribution(latencies_by_camera[camera_key])
        total = totals_by_camera[camera_key]
        matched = matched_by_camera[camera_key]
        camera_result.update(
            {
                "camera_index": camera_index,
                "serial": serial,
                "xhci_irq": (camera_irq_numbers or {}).get(camera_index),
                "matched_deliveries": matched,
                "total_deliveries": total,
                "coverage": matched / total if total else 0.0,
            }
        )
        per_camera.append(camera_result)
    result.update(
        {
            "name": "host receive-to-frameset latency",
            "symbol": "L_host_c_k",
            "irq_mode": irq_mode,
            "matched_deliveries": len(latencies),
            "total_deliveries": len(by_delivery),
            "coverage": len(latencies) / len(by_delivery) if by_delivery else 0.0,
            "per_camera": per_camera,
            "start": (
                "threaded xHCI activation sched_wakeup"
                if irq_mode == "threaded_irq"
                else "xHCI hard-IRQ handler entry"
            ),
            "end": "wait_for_frames return",
        }
    )
    return result


def camera_irq_numbers(
    summary: dict[str, object], topology: dict[str, object] | None
) -> dict[int, int]:
    """Map selected cameras to the xHCI IRQ serving their USB root hub."""

    irq_by_root_bus: dict[int, int] = {}
    if topology:
        parsed = topology.get("parsed_interrupts", {})
        if isinstance(parsed, dict):
            for line in parsed.get("lines", []):
                if not isinstance(line, dict):
                    continue
                match = re.search(r"xhci-hcd:usb(\d+)", str(line.get("description", "")))
                if match:
                    irq_by_root_bus[int(match.group(1))] = int(line["irq"])

    result: dict[int, int] = {}
    cameras = summary.get("cameras", [])
    if not isinstance(cameras, list):
        return result
    for fallback_index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            continue
        physical_port = str(camera.get("physical_port", ""))
        bus_match = re.search(r"/usb(\d+)/", physical_port)
        if not bus_match:
            continue
        superspeed_bus = int(bus_match.group(1))
        # Linux exposes the USB2 and USB3 roothubs of one xHCI controller as
        # adjacent buses.  The IRQ action is named after the USB2 (even) bus.
        irq_root_bus = superspeed_bus - 1 if superspeed_bus % 2 else superspeed_bus
        irq_number = irq_by_root_bus.get(irq_root_bus)
        if irq_number is not None:
            result[int(camera.get("index", fallback_index))] = irq_number
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    summary = json.loads((run_dir / "steady_summary.json").read_text(encoding="utf-8"))
    measurement = summary["measurement"]
    begin = int(measurement["start_boottime_ns"]) / 1_000_000_000.0
    end = int(measurement["end_boottime_ns"]) / 1_000_000_000.0
    topology_path = run_dir / "topology_before.json"
    topology = (
        json.loads(topology_path.read_text(encoding="utf-8"))
        if topology_path.is_file()
        else None
    )
    kernel_stages, video_buffers, irq_mode = analyze_kernel(
        run_dir / "kernel_trace.dat", begin, end
    )
    irq_numbers = camera_irq_numbers(summary, topology)
    result = {
        "schema_version": 1,
        "serial": summary["cameras"][0]["serial"],
        "camera_name": summary["cameras"][0]["name"],
        "measurement": measurement,
        "freshness": {
            key: summary["cameras"][0].get(key)
            for key in (
                "deliveries",
                "duplicate_frames",
                "sequence_gaps",
                "out_of_order_frames",
                "measurement_timeouts",
            )
        },
        "frame_age": frame_distributions(run_dir / "frame_events.csv"),
        "kernel_stages": kernel_stages,
        "host_receive_to_frameset_latency": host_receive_latency(
            run_dir / "frame_events.csv",
            video_buffers,
            irq_mode,
            run_dir / "host_receive_latency_frames.csv",
            irq_numbers,
        ),
        "frameset_queue_handoff": queue_handoff_distribution(
            run_dir / "v4l2_diagnostic_events.csv"
        ),
        "userspace_stages_ms": json.loads(
            (run_dir / "v4l2_diagnostic_summary.json").read_text(encoding="utf-8")
        )["stages_ms"],
    }
    destination = run_dir / "full_receive_path_summary.json"
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
