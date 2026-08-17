#!/usr/bin/env python3
"""Summarize one short kernel+librealsense full receive-path trace."""

from __future__ import annotations

import argparse
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
    by_delivery: dict[int, list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            stream = f"{row['stream']}#{row['stream_index']}"
            backend_by_stream[stream].append(float(row["backend_to_return_ms"]) / 1000.0)
            arrival_by_stream[stream].append(float(row["arrival_to_return_ms"]) / 1000.0)
            by_delivery[int(row["delivery"])].append(row)

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


def analyze_kernel(trace: Path, begin: float, end: float) -> dict[str, object]:
    report = subprocess.Popen(
        ["trace-cmd", "report", "-i", str(trace)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    starts: dict[tuple[str, int], list[float]] = defaultdict(list)
    irq_starts: dict[tuple[int, int], list[float]] = defaultdict(list)
    irq_wakeup: dict[int, list[float]] = defaultdict(list)
    irq_run: dict[int, list[float]] = defaultdict(list)
    hcd_by_urb: dict[str, list[float]] = defaultdict(list)
    xhci_by_urb: dict[str, list[float]] = defaultdict(list)
    durations: dict[str, list[float]] = defaultdict(list)

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
        elif event == "irq_handler_exit":
            irq = re.search(r"irq=(\d+)", body)
            if irq and irq_starts[(cpu, int(irq.group(1)))]:
                durations["xhci_irq_handler"].append(
                    timestamp - irq_starts[(cpu, int(irq.group(1)))].pop()
                )

        if event == "sched_wakeup" and "irq/" in body and "xhci" in body:
            target = re.search(r"irq/[^:]+:(\d+)", body)
            if target:
                irq_wakeup[int(target.group(1))].append(timestamp)
        elif event == "sched_switch" and "==> irq/" in body and "xhci" in body:
            target = re.search(r"==> irq/[^:]+:(\d+)", body)
            if target and irq_wakeup[int(target.group(1))]:
                target_pid = int(target.group(1))
                durations["xhci_irq_wakeup_to_run"].append(
                    timestamp - irq_wakeup[target_pid][-1]
                )
                irq_wakeup[target_pid].clear()
                irq_run[target_pid].append(timestamp)

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
            if irq_run[pid]:
                durations["xhci_irq_run_to_first_hcd"].append(
                    timestamp - irq_run[pid][-1]
                )
                irq_run[pid].clear()
        elif event == "uvc_complete_begin":
            urb = re.search(r"urb=(0x[0-9a-f]+)", body)
            if urb and hcd_by_urb[urb.group(1)]:
                durations["hcd_to_uvc_callback"].append(
                    timestamp - hcd_by_urb[urb.group(1)].pop(0)
                )
        elif event == "uvc_buffer_complete":
            backend = re.search(r"backend_ns=(-?\d+)", body)
            if backend and int(backend.group(1)) > 0:
                durations["uvc_frame_assembly_wall"].append(
                    timestamp - int(backend.group(1)) / 1_000_000_000.0
                )

    if report.wait() != 0:
        raise RuntimeError("trace-cmd report failed")
    return {stage: distribution(values) for stage, values in sorted(durations.items())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir
    summary = json.loads((run_dir / "steady_summary.json").read_text(encoding="utf-8"))
    measurement = summary["measurement"]
    begin = int(measurement["start_boottime_ns"]) / 1_000_000_000.0
    end = int(measurement["end_boottime_ns"]) / 1_000_000_000.0
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
        "kernel_stages": analyze_kernel(run_dir / "kernel_trace.dat", begin, end),
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
