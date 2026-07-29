#!/usr/bin/env python3
"""Extract per-thread steady-state activations from pthread and LiME traces."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TOOL = REPO_ROOT / "tools" / "realsense_startup_bench"
sys.path.insert(0, str(STARTUP_TOOL))

from parse_startup_trace import (  # noqa: E402
    lifecycle_records,
    load_lime,
    read_jsonl,
    scheduler_intervals,
)


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def distribution(values: Iterable[float]) -> Dict[str, Any]:
    samples = [float(value) for value in values]
    if not samples:
        return {
            "n": 0,
            "min": 0.0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p99": 0.0,
            "p999": 0.0,
            "max": 0.0,
        }
    return {
        "n": len(samples),
        "min": min(samples),
        "mean": statistics.fmean(samples),
        "p50": percentile(samples, 0.50),
        "p90": percentile(samples, 0.90),
        "p99": percentile(samples, 0.99),
        "p999": percentile(samples, 0.999),
        "max": max(samples),
    }


def marker_time(events: List[Dict[str, Any]], name: str) -> int:
    matches = [
        int(event["timestamp_ns"])
        for event in events
        if event.get("event") == "phase_marker" and event.get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one {name!r} phase marker, found {len(matches)}")
    return matches[0]


def activations_from_intervals(
    intervals: List[Dict[str, Any]],
    tid: int,
    name: str,
    window_end: int,
) -> List[Dict[str, Any]]:
    """Group ready/running fragments separated by sleeping into activations."""
    rows: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    preceded_by_sleep = False

    def close(completion_ns: int, partial_end: bool) -> None:
        nonlocal current
        if current is None:
            return
        current["completion_ns"] = completion_ns
        current["response_ms"] = (
            completion_ns - int(current["release_ns"])
        ) / 1_000_000
        current["partial_end"] = partial_end
        rows.append(current)
        current = None

    for interval in sorted(intervals, key=lambda item: int(item["start_ns"])):
        state = str(interval["state"])
        start_ns = int(interval["start_ns"])
        if state == "sleeping":
            close(start_ns, partial_end=False)
            preceded_by_sleep = True
            continue
        if state not in {"ready", "running"}:
            continue
        if current is None:
            current = {
                "tid": tid,
                "name": name,
                "release_ns": start_ns,
                "first_run_ns": "",
                "execution_ms": 0.0,
                "ready_ms": 0.0,
                "partial_start": not preceded_by_sleep,
            }
            preceded_by_sleep = False
        metric = "execution_ms" if state == "running" else "ready_ms"
        current[metric] += float(interval["duration_ms"])
        if state == "running" and current["first_run_ns"] == "":
            current["first_run_ns"] = start_ns

    close(window_end, partial_end=True)
    for index, row in enumerate(rows, 1):
        row["activation"] = index
        row["period_ms"] = (
            ""
            if index == 1
            else (int(row["release_ns"]) - int(rows[index - 2]["release_ns"])) / 1_000_000
        )
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_steady_trace(
    lifecycle_path: Path,
    lime_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    lifecycle = read_jsonl(lifecycle_path)
    steady_begin = marker_time(lifecycle, "steady_state_begin")
    steady_end = marker_time(lifecycle, "steady_state_end")
    if steady_end <= steady_begin:
        raise ValueError("steady_state_end must be after steady_state_begin")

    records, _ = lifecycle_records(lifecycle, {})
    app_tgid = next(
        (int(record["tgid"]) for record in records if record.get("tgid") is not None),
        None,
    )
    lime_events, policies = load_lime(lime_dir, app_tgid)

    interval_rows: List[Dict[str, Any]] = []
    activation_rows: List[Dict[str, Any]] = []
    thread_rows: List[Dict[str, Any]] = []
    for record in records:
        if record.get("tid") is None:
            continue
        tid = int(record["tid"])
        thread_begin = int(record.get("started_ns") or record.get("created_ns") or steady_begin)
        thread_end = int(record.get("exited_ns") or steady_end)
        begin = max(steady_begin, thread_begin)
        end = min(steady_end, thread_end)
        if end <= begin:
            continue

        name = str(record.get("name") or "")
        intervals, _, _ = scheduler_intervals(
            lime_events.get(tid, []),
            begin,
            end,
            0,
            tid,
            name,
            steady_begin,
        )
        interval_rows.extend(intervals)
        activations = activations_from_intervals(intervals, tid, name, end)
        activation_rows.extend(activations)

        complete = [
            row
            for row in activations
            if not row["partial_start"] and not row["partial_end"]
        ]
        source = complete if complete else activations
        running_ms = sum(
            float(row["duration_ms"]) for row in intervals if row["state"] == "running"
        )
        ready_ms = sum(
            float(row["duration_ms"]) for row in intervals if row["state"] == "ready"
        )
        sleeping_ms = sum(
            float(row["duration_ms"]) for row in intervals if row["state"] == "sleeping"
        )
        lifetime_ms = (end - begin) / 1_000_000
        policy_values = policies.get(tid, [])
        thread_rows.append(
            {
                "tid": tid,
                "name": name,
                "policy": "|".join(value[0] for value in policy_values) or "UNKNOWN",
                "priority": "|".join(value[1] for value in policy_values if value[1]),
                "lifetime_in_window_ms": lifetime_ms,
                "running_ms": running_ms,
                "ready_ms": ready_ms,
                "sleeping_ms": sleeping_ms,
                "trace_coverage_ms": running_ms + ready_ms + sleeping_ms,
                "unobserved_ms": max(
                    0.0, lifetime_ms - running_ms - ready_ms - sleeping_ms
                ),
                "lime_event_count": len(lime_events.get(tid, [])),
                "activation_count": len(activations),
                "complete_activation_count": len(complete),
                "execution_ms": distribution(row["execution_ms"] for row in source),
                "ready_per_activation_ms": distribution(row["ready_ms"] for row in source),
                "response_ms": distribution(row["response_ms"] for row in source),
                "period_ms": distribution(
                    row["period_ms"] for row in source if row["period_ms"] != ""
                ),
                "cpus": sorted(
                    {
                        int(row["cpu"])
                        for row in intervals
                        if str(row.get("cpu", "")).isdigit()
                    }
                ),
            }
        )

    interval_fields = [
        "tid",
        "name",
        "state",
        "start_ns",
        "end_ns",
        "start_ms",
        "end_ms",
        "duration_ms",
        "cpu",
        "reason",
    ]
    activation_fields = [
        "tid",
        "name",
        "activation",
        "release_ns",
        "first_run_ns",
        "completion_ns",
        "execution_ms",
        "ready_ms",
        "response_ms",
        "period_ms",
        "partial_start",
        "partial_end",
    ]
    flat_thread_rows: List[Dict[str, Any]] = []
    for row in thread_rows:
        flat = {key: value for key, value in row.items() if not isinstance(value, dict)}
        for metric in ("execution_ms", "ready_per_activation_ms", "response_ms", "period_ms"):
            for key, value in row[metric].items():
                flat[f"{metric}_{key}"] = value
        flat["cpus"] = ",".join(str(cpu) for cpu in row["cpus"])
        flat_thread_rows.append(flat)
    thread_fields = list(flat_thread_rows[0].keys()) if flat_thread_rows else [
        "tid",
        "name",
    ]

    write_csv(output_dir / "thread_steady_intervals.csv", interval_rows, interval_fields)
    write_csv(output_dir / "thread_steady_activations.csv", activation_rows, activation_fields)
    write_csv(output_dir / "thread_steady_summary.csv", flat_thread_rows, thread_fields)

    summary = {
        "schema_version": 1,
        "steady_begin_ns": steady_begin,
        "steady_end_ns": steady_end,
        "duration_ms": (steady_end - steady_begin) / 1_000_000,
        "thread_count": len(thread_rows),
        "activation_count": len(activation_rows),
        "threads": thread_rows,
    }
    (output_dir / "thread_steady_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifecycle", type=Path, required=True)
    parser.add_argument("--lime-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = parse_steady_trace(args.lifecycle, args.lime_dir, args.output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
