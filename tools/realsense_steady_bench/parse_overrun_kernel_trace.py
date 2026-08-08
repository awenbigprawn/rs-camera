#!/usr/bin/env python3
"""Correlate IRQ/softirq and Deadline-throttle events with steady activations."""

from __future__ import annotations

import argparse
from array import array
import bisect
from collections import defaultdict
import csv
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, Iterator, List, Tuple

from parse_v4l2_diagnostic_trace import phase_window


LINE = re.compile(
    r"^\s*(?P<task>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\].*?"
    r"\s(?P<seconds>\d+\.\d+):\s+(?P<event>[\w-]+(?::[\w-]+)?):\s*(?P<fields>.*)$"
)


def field_int(text: str, name: str, default: int = 0) -> int:
    match = re.search(rf"(?:^|\s){re.escape(name)}=(-?(?:0x)?[0-9a-fA-F]+)", text)
    if not match:
        return default
    return int(match.group(1), 0)


def load_activations(path: Path) -> List[Dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def overlap_ns(left_begin: int, left_end: int, right_begin: int, right_end: int) -> int:
    return max(0, min(left_end, right_end) - max(left_begin, right_begin))


def topology_xhci_irq_ids(topology: Dict[str, Any]) -> set[int]:
    parsed = topology.get("parsed_interrupts", [])
    if isinstance(parsed, dict):
        parsed = parsed.get("lines", [])
    if not isinstance(parsed, list):
        return set()
    return {
        int(interrupt["irq"])
        for interrupt in parsed
        if isinstance(interrupt, dict)
        and "xhci-hcd" in str(interrupt.get("description", ""))
        and "irq" in interrupt
    }


def trace_report_lines(trace_path: Path) -> Iterator[str]:
    process = subprocess.Popen(
        ["trace-cmd", "report", "-i", str(trace_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    yield from process.stdout
    stderr = process.stderr.read() if process.stderr is not None else ""
    returncode = process.wait()
    if returncode:
        raise subprocess.CalledProcessError(
            returncode,
            process.args,
            stderr=stderr,
        )


def parse_trace(
    trace_path: Path,
    lifecycle_path: Path,
    activation_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    steady_begin, steady_end = phase_window(lifecycle_path)
    activations = load_activations(activation_path)
    application_tids = {int(row["tid"]) for row in activations}
    topology_path = output_dir / "topology_before.json"
    xhci_irq_ids: set[int] = set()
    if topology_path.is_file():
        topology = json.loads(topology_path.read_text(encoding="utf-8"))
        xhci_irq_ids = topology_xhci_irq_ids(topology)
    output_dir.mkdir(parents=True, exist_ok=True)
    interrupt_fields = [
        "kind",
        "identifier",
        "cpu",
        "context_tid",
        "begin_ns",
        "end_ns",
        "duration_ms",
        "in_steady_state",
    ]
    kernel_event_count = 0
    interrupt_interval_count = 0
    irq_stacks: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
    application_interrupt_rows: List[Dict[str, Any]] = []
    xhci_interrupt_begins = array("q")
    xhci_interrupt_durations_ns = array("q")
    throttle_rows: List[Dict[str, Any]] = []
    signal_rows: List[Dict[str, Any]] = []
    interrupt_path = output_dir / "kernel_interrupt_intervals.csv"
    with interrupt_path.open("w", newline="", encoding="utf-8") as handle:
        interrupt_writer = csv.DictWriter(handle, fieldnames=interrupt_fields)
        interrupt_writer.writeheader()
        for line in trace_report_lines(trace_path):
            match = LINE.match(line)
            if not match:
                continue
            kernel_event_count += 1
            timestamp_ns = round(float(match.group("seconds")) * 1_000_000_000)
            event = match.group("event")
            row = {
                "timestamp_ns": timestamp_ns,
                "time_from_steady_begin_ms": (timestamp_ns - steady_begin)
                / 1_000_000,
                "in_steady_state": steady_begin <= timestamp_ns <= steady_end,
                "context_tid": int(match.group("pid")),
                "task": match.group("task").strip(),
                "cpu": int(match.group("cpu")),
                "event": event,
                "fields": match.group("fields"),
            }
            short_event = event.rsplit(":", 1)[-1]
            if short_event in {"irq_handler_entry", "softirq_entry"}:
                kind = (
                    "hardirq" if short_event.startswith("irq_handler") else "softirq"
                )
                identifier = field_int(
                    row["fields"], "irq" if kind == "hardirq" else "vec"
                )
                irq_stacks[(row["cpu"], kind)].append(
                    {**row, "identifier": identifier}
                )
            elif short_event in {"irq_handler_exit", "softirq_exit"}:
                kind = (
                    "hardirq" if short_event.startswith("irq_handler") else "softirq"
                )
                stack = irq_stacks[(row["cpu"], kind)]
                if not stack:
                    continue
                begin = stack.pop()
                interrupt = {
                        "kind": kind,
                        "identifier": begin["identifier"],
                        "cpu": row["cpu"],
                        "context_tid": begin["context_tid"],
                        "begin_ns": begin["timestamp_ns"],
                        "end_ns": timestamp_ns,
                        "duration_ms": (timestamp_ns - begin["timestamp_ns"]) / 1_000_000,
                        "in_steady_state": bool(begin["in_steady_state"] and row["in_steady_state"]),
                }
                interrupt_writer.writerow(interrupt)
                interrupt_interval_count += 1
                if int(interrupt["context_tid"]) in application_tids:
                    application_interrupt_rows.append(interrupt)
                if (
                    kind == "hardirq"
                    and int(interrupt["identifier"]) in xhci_irq_ids
                ):
                    xhci_interrupt_begins.append(int(interrupt["begin_ns"]))
                    xhci_interrupt_durations_ns.append(
                        int(interrupt["end_ns"]) - int(interrupt["begin_ns"])
                    )
            elif short_event == "dl_runtime_exhausted":
                flags = field_int(row["fields"], "flags")
                throttle_rows.append(
                    {
                        **row,
                        "runtime_ns": field_int(row["fields"], "runtime"),
                        "flags": flags,
                        "dl_overrun_flag": bool(flags & 0x8),
                    }
                )
            elif short_event in {"signal_generate", "signal_deliver"}:
                signal_rows.append(
                    {**row, "signal": field_int(row["fields"], "sig")}
                )

    interrupts_by_tid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for interrupt in application_interrupt_rows:
        interrupts_by_tid[int(interrupt["context_tid"])].append(interrupt)
    interrupt_begins_by_tid: Dict[int, List[int]] = {}
    for tid, rows in interrupts_by_tid.items():
        rows.sort(key=lambda row: int(row["begin_ns"]))
        interrupt_begins_by_tid[tid] = [int(row["begin_ns"]) for row in rows]

    def xhci_windows(timestamp_ns: int) -> Dict[str, Any]:
        windows: Dict[str, Any] = {}
        for radius_ms in (1, 10, 100, 1000):
            radius_ns = radius_ms * 1_000_000
            first = bisect.bisect_left(xhci_interrupt_begins, timestamp_ns - radius_ns)
            past_last = bisect.bisect_right(
                xhci_interrupt_begins, timestamp_ns + radius_ns
            )
            durations = xhci_interrupt_durations_ns[first:past_last]
            windows[str(radius_ms)] = {
                "radius_ms": radius_ms,
                "interval_count": past_last - first,
                "service_ms": sum(durations) / 1_000_000,
                "maximum_service_ms": max(durations, default=0) / 1_000_000,
            }
        return windows
    corrected_rows: List[Dict[str, Any]] = []
    for activation in activations:
        tid = int(activation["tid"])
        begin = int(activation["release_ns"])
        end = int(activation["completion_ns"])
        hardirq_ns = 0
        softirq_ns = 0
        candidate_interrupts = interrupts_by_tid.get(tid, [])
        candidate_begins = interrupt_begins_by_tid.get(tid, [])
        first = max(0, bisect.bisect_left(candidate_begins, begin) - 1)
        past_last = bisect.bisect_left(candidate_begins, end)
        for interrupt in candidate_interrupts[first:past_last]:
            duration = overlap_ns(begin, end, int(interrupt["begin_ns"]), int(interrupt["end_ns"]))
            if interrupt["kind"] == "hardirq":
                hardirq_ns += duration
            else:
                softirq_ns += duration
        scheduler_residency_ms = float(activation["execution_ms"])
        corrected_rows.append(
            {
                **activation,
                "scheduler_residency_ms": scheduler_residency_ms,
                "hardirq_ms": hardirq_ns / 1_000_000,
                "softirq_ms": softirq_ns / 1_000_000,
                "task_execution_ms": max(
                    0.0,
                    scheduler_residency_ms - (hardirq_ns + softirq_ns) / 1_000_000,
                ),
            }
        )

    corrected_by_tid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    corrected_releases_by_tid: Dict[int, List[int]] = {}
    for row in corrected_rows:
        corrected_by_tid[int(row["tid"])].append(row)
    for tid, rows in corrected_by_tid.items():
        rows.sort(key=lambda row: int(row["release_ns"]))
        corrected_releases_by_tid[tid] = [int(row["release_ns"]) for row in rows]

    diagnostic_durations = load_csv(output_dir / "v4l2_diagnostic_durations.csv")
    diagnostic_by_tid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    diagnostic_begins_by_tid: Dict[int, List[int]] = {}
    for row in diagnostic_durations:
        diagnostic_by_tid[int(row["tid"])].append(row)
    for tid, rows in diagnostic_by_tid.items():
        rows.sort(key=lambda row: int(row["begin_ns"]))
        diagnostic_begins_by_tid[tid] = [int(row["begin_ns"]) for row in rows]

    raw_sequence_gaps = load_csv(output_dir / "v4l2_sequence_gaps.csv")
    raw_sequence_gaps.sort(key=lambda row: int(row["timestamp_ns"]))
    raw_sequence_gap_times = [int(row["timestamp_ns"]) for row in raw_sequence_gaps]

    correlation_rows: List[Dict[str, Any]] = []
    for throttle in throttle_rows:
        tid = int(throttle["context_tid"])
        timestamp_ns = int(throttle["timestamp_ns"])
        if not (
            tid in application_tids
            and throttle["dl_overrun_flag"]
            and throttle["in_steady_state"]
        ):
            continue

        activation: Dict[str, Any] | None = None
        rows = corrected_by_tid.get(tid, [])
        releases = corrected_releases_by_tid.get(tid, [])
        position = bisect.bisect_right(releases, timestamp_ns) - 1
        if position >= 0 and timestamp_ns <= int(rows[position]["completion_ns"]):
            activation = rows[position]

        active_stages: List[Dict[str, Any]] = []
        activation_stage_totals_ms: Dict[str, float] = defaultdict(float)
        last_stage_before_throttle: Dict[str, Any] | None = None
        duration_rows = diagnostic_by_tid.get(tid, [])
        duration_begins = diagnostic_begins_by_tid.get(tid, [])
        past_last = bisect.bisect_right(duration_begins, timestamp_ns)
        for duration in reversed(duration_rows[:past_last]):
            if int(duration["end_ns"]) >= timestamp_ns:
                active_stages.append(
                    {
                        "stage": duration["stage"],
                        "begin_ns": int(duration["begin_ns"]),
                        "end_ns": int(duration["end_ns"]),
                        "duration_ms": float(duration["duration_ms"]),
                        "fd": int(duration["fd"]),
                        "sequence": int(duration["sequence"]),
                    }
                )
        active_stages.sort(key=lambda row: int(row["begin_ns"]))
        nearby_sequence_gaps: List[Dict[str, Any]] = []
        gap_begin = bisect.bisect_left(
            raw_sequence_gap_times, timestamp_ns - 2_000_000_000
        )
        gap_end = bisect.bisect_right(
            raw_sequence_gap_times, timestamp_ns + 2_000_000_000
        )
        for gap in raw_sequence_gaps[gap_begin:gap_end]:
            nearby_sequence_gaps.append(
                {
                    "delta_from_throttle_ms": (
                        int(gap["timestamp_ns"]) - timestamp_ns
                    )
                    / 1_000_000,
                    "tid": int(gap["tid"]),
                    "fd": int(gap["fd"]),
                    "previous_sequence": int(gap["previous_sequence"]),
                    "sequence": int(gap["sequence"]),
                    "missing_frames": int(gap["missing_frames"]),
                }
            )
        if activation:
            activation_begin = int(activation["release_ns"])
            activation_end = int(activation["completion_ns"])
            activation_past_last = bisect.bisect_right(
                duration_begins, activation_end
            )
            for duration in duration_rows[:activation_past_last]:
                duration_begin = int(duration["begin_ns"])
                duration_end = int(duration["end_ns"])
                overlap = overlap_ns(
                    activation_begin,
                    activation_end,
                    duration_begin,
                    duration_end,
                )
                if overlap:
                    activation_stage_totals_ms[str(duration["stage"])] += (
                        overlap / 1_000_000
                    )
                if (
                    duration_end >= activation_begin
                    and duration_end <= timestamp_ns
                    and (
                        last_stage_before_throttle is None
                        or duration_end
                        > int(last_stage_before_throttle["end_ns"])
                    )
                ):
                    last_stage_before_throttle = {
                        "stage": duration["stage"],
                        "begin_ns": duration_begin,
                        "end_ns": duration_end,
                        "duration_ms": float(duration["duration_ms"]),
                        "fd": int(duration["fd"]),
                        "sequence": int(duration["sequence"]),
                    }

        correlation_rows.append(
            {
                "timestamp_ns": timestamp_ns,
                "time_from_steady_begin_ms": throttle["time_from_steady_begin_ms"],
                "tid": tid,
                "cpu": throttle["cpu"],
                "runtime_remaining_ns": throttle["runtime_ns"],
                "activation": activation["activation"] if activation else "",
                "activation_release_ns": activation["release_ns"] if activation else "",
                "activation_completion_ns": activation["completion_ns"] if activation else "",
                "scheduler_residency_ms": (
                    activation["scheduler_residency_ms"] if activation else ""
                ),
                "hardirq_ms": activation["hardirq_ms"] if activation else "",
                "softirq_ms": activation["softirq_ms"] if activation else "",
                "irq_corrected_task_execution_ms": (
                    activation["task_execution_ms"] if activation else ""
                ),
                "active_v4l2_stages": ",".join(
                    str(row["stage"]) for row in active_stages
                ),
                "active_v4l2_stage_details": json.dumps(
                    active_stages, separators=(",", ":")
                ),
                "activation_v4l2_stage_durations_ms": json.dumps(
                    dict(sorted(activation_stage_totals_ms.items())),
                    separators=(",", ":"),
                ),
                "last_v4l2_stage_before_throttle": json.dumps(
                    last_stage_before_throttle,
                    separators=(",", ":"),
                ),
                "raw_sequence_gaps_within_2s": json.dumps(
                    nearby_sequence_gaps,
                    separators=(",", ":"),
                ),
                "xhci_irq_windows": json.dumps(
                    xhci_windows(timestamp_ns),
                    separators=(",", ":"),
                ),
            }
        )

    outputs = (
        ("deadline_runtime_exhaustions.csv", throttle_rows),
        ("deadline_signal_events.csv", signal_rows),
        ("thread_steady_activations_irq_corrected.csv", corrected_rows),
        ("deadline_overrun_correlations.csv", correlation_rows),
    )
    for filename, rows in outputs:
        fields = list(rows[0]) if rows else ["timestamp_ns"]
        with (output_dir / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    application_throttles = [
        row
        for row in throttle_rows
        if int(row["context_tid"]) in application_tids and row["dl_overrun_flag"]
    ]
    steady_throttles = [
        row for row in application_throttles if row["in_steady_state"]
    ]
    steady_sigxcpu = [
        row for row in signal_rows if row["in_steady_state"] and row["signal"] == 24
    ]
    largest_raw = max(corrected_rows, key=lambda row: float(row["scheduler_residency_ms"]), default=None)
    largest_task = max(corrected_rows, key=lambda row: float(row["task_execution_ms"]), default=None)
    summary = {
        "schema_version": 1,
        "kernel_event_count": kernel_event_count,
        "interrupt_interval_count": interrupt_interval_count,
        "system_deadline_throttle_point_count": len(throttle_rows),
        "application_deadline_runtime_exhaustion_count": len(application_throttles),
        "steady_deadline_runtime_exhaustion_count": len(steady_throttles),
        "steady_deadline_runtime_exhaustion_tids": sorted(
            {int(row["context_tid"]) for row in steady_throttles}
        ),
        "steady_deadline_overrun_gate_passed": not steady_throttles,
        "steady_deadline_overrun_correlations": correlation_rows,
        "steady_sigxcpu_event_count": len(steady_sigxcpu),
        "post_steady_deadline_runtime_exhaustion_count": sum(
            int(row["timestamp_ns"]) > steady_end for row in application_throttles
        ),
        "largest_scheduler_residency_activation": largest_raw,
        "largest_irq_corrected_task_activation": largest_task,
        "largest_irq_corrected_task_activation_xhci_windows": (
            xhci_windows(int(largest_task["completion_ns"]))
            if largest_task
            else {}
        ),
        "xhci_irq_ids": sorted(xhci_irq_ids),
    }
    (output_dir / "overrun_kernel_trace_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--lifecycle", type=Path, required=True)
    parser.add_argument("--activations", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            parse_trace(args.trace, args.lifecycle, args.activations, args.output_dir),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
