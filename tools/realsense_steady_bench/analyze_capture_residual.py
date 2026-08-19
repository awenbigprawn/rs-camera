#!/usr/bin/env python3
"""Decompose capture-worker scaling into traced V4L2/SDK stages.

The input is one steady-benchmark campaign directory.  Each logical run must
contain ``selected_attempt.txt`` and the selected attempt must contain LiME,
IRQ-corrected, and V4L2 diagnostic CSV files.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


CAPTURE_SIGNATURES = {
    "0x6c9348": "Color",
    "0x6c8358": "Color",
    "0x6c5938": "Color",
    "0x41fdc0": "Depth",
    "0x41edd0": "Depth",
    "0x41d980": "Depth",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def as_float(value: str | None) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def median(values: Iterable[float]) -> float:
    collected = list(values)
    return statistics.median(collected) if collected else 0.0


def camera_count(case_id: str) -> int:
    for count in (1, 2, 4):
        if f"_n{count}_" in case_id:
            return count
    raise ValueError(f"Cannot infer camera count from case id: {case_id}")


def capture_threads(attempt_dir: Path) -> dict[int, tuple[str, int]]:
    result: dict[int, tuple[str, int]] = {}
    for row in read_csv(attempt_dir / "thread_steady_summary.csv"):
        signature = row["signature"]
        for suffix, family in CAPTURE_SIGNATURES.items():
            if suffix in signature:
                result[int(row["tid"])] = (family, int(row["profile_instance"]))
                break
    return result


def running_intervals(
    attempt_dir: Path, tids: Iterable[int]
) -> dict[int, list[tuple[int, int, int]]]:
    """Return LiME scheduler-residency intervals as (begin, end, cpu)."""
    output: dict[int, list[tuple[int, int, int]]] = {}
    for tid in tids:
        events: list[dict[str, object]] = []
        for path in sorted((attempt_dir / "lime_trace").glob(f"{tid}-*.events.json")):
            events.extend(json.loads(path.read_text()))
        events.sort(key=lambda event: int(event["ts"]))
        intervals: list[tuple[int, int, int]] = []
        active: tuple[int, int] | None = None
        for event in events:
            kind = event.get("event")
            if kind == "sched_switched_in":
                active = (int(event["ts"]), int(event["cpu"]))
            elif kind == "sched_switched_out" and active is not None:
                begin_ns, cpu = active
                end_ns = int(event["ts"])
                if end_ns > begin_ns:
                    intervals.append((begin_ns, end_ns, cpu))
                active = None
        output[tid] = intervals
    return output


def interrupt_intervals(
    attempt_dir: Path,
) -> dict[tuple[str, int], list[tuple[int, int]]]:
    output: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    for row in read_csv(attempt_dir / "kernel_interrupt_intervals.csv"):
        if row["in_steady_state"] != "True":
            continue
        output[(row["kind"], int(row["cpu"]))].append(
            (int(row["begin_ns"]), int(row["end_ns"]))
        )
    for intervals in output.values():
        intervals.sort()
    return output


def interval_overlap_ns(
    intervals: list[tuple[int, int]],
    starts: list[int],
    begin_ns: int,
    end_ns: int,
) -> int:
    if not intervals or end_ns <= begin_ns:
        return 0
    index = max(0, bisect.bisect_right(starts, begin_ns) - 1)
    total = 0
    while index < len(intervals):
        interval_begin, interval_end = intervals[index]
        if interval_begin >= end_ns:
            break
        total += max(0, min(end_ns, interval_end) - max(begin_ns, interval_begin))
        index += 1
    return total


def stage_cpu_ns(
    intervals: list[tuple[int, int, int]],
    run_starts: list[int],
    interrupts: dict[tuple[str, int], list[tuple[int, int]]],
    interrupt_starts: dict[tuple[str, int], list[int]],
    begin_ns: int,
    end_ns: int,
) -> tuple[int, int, int, int]:
    """Return scheduler residency, hardirq, softirq, and task CPU time."""
    if not intervals or end_ns <= begin_ns:
        return (0, 0, 0, 0)
    index = max(0, bisect.bisect_right(run_starts, begin_ns) - 1)
    scheduler = hardirq = softirq = 0
    while index < len(intervals):
        run_begin, run_end, cpu = intervals[index]
        if run_begin >= end_ns:
            break
        overlap_begin = max(begin_ns, run_begin)
        overlap_end = min(end_ns, run_end)
        if overlap_end > overlap_begin:
            scheduler += overlap_end - overlap_begin
            hardirq_key = ("hardirq", cpu)
            softirq_key = ("softirq", cpu)
            hardirq += interval_overlap_ns(
                interrupts.get(hardirq_key, []),
                interrupt_starts.get(hardirq_key, []),
                overlap_begin,
                overlap_end,
            )
            softirq += interval_overlap_ns(
                interrupts.get(softirq_key, []),
                interrupt_starts.get(softirq_key, []),
                overlap_begin,
                overlap_end,
            )
        index += 1
    task = max(0, scheduler - hardirq - softirq)
    return (scheduler, hardirq, softirq, task)


def selected_attempts(campaign_dir: Path) -> list[tuple[str, int, Path]]:
    selected: list[tuple[str, int, Path]] = []
    for marker in campaign_dir.rglob("selected_attempt.txt"):
        run_dir = marker.parent
        attempt_name = marker.read_text().strip()
        if attempt_name.isdigit():
            attempt_name = f"attempt-{attempt_name}"
        attempt_dir = run_dir / attempt_name
        case_part = next(part for part in run_dir.parts if part.startswith("case_id-"))
        case_id = case_part.removeprefix("case_id-")
        run_number = int(run_dir.name.removeprefix("run-"))
        required = (
            "thread_steady_summary.csv",
            "thread_steady_activations_irq_corrected.csv",
            "v4l2_diagnostic_durations.csv",
        )
        if all((attempt_dir / name).is_file() for name in required):
            selected.append((case_id, run_number, attempt_dir))
    return sorted(selected, key=lambda item: (camera_count(item[0]), item[1]))


def analyze_attempt(case_id: str, run_number: int, attempt_dir: Path) -> list[dict[str, object]]:
    threads = capture_threads(attempt_dir)
    run_intervals = running_intervals(attempt_dir, threads)
    run_starts = {
        tid: [interval[0] for interval in intervals]
        for tid, intervals in run_intervals.items()
    }
    interrupts = interrupt_intervals(attempt_dir)
    interrupt_starts = {
        key: [interval[0] for interval in intervals]
        for key, intervals in interrupts.items()
    }
    activation_totals: dict[tuple[str, int], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    activation_counts: dict[tuple[str, int], int] = defaultdict(int)

    for row in read_csv(attempt_dir / "thread_steady_activations_irq_corrected.csv"):
        tid = int(row["tid"])
        key = threads.get(tid)
        if key is None or row["partial_start"] == "True" or row["partial_end"] == "True":
            continue
        activation_counts[key] += 1
        totals = activation_totals[key]
        totals["raw"] += as_float(row["scheduler_residency_ms"])
        totals["task"] += as_float(row["task_execution_ms"])
        totals["hardirq"] += as_float(row["hardirq_ms"])
        totals["softirq"] += as_float(row["softirq_ms"])

    stage_totals: dict[tuple[str, int], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    stage_counts: dict[tuple[str, int], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    stage_cpu_totals: dict[tuple[str, int], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for row in read_csv(attempt_dir / "v4l2_diagnostic_durations.csv"):
        if row["in_steady_state"] != "True":
            continue
        key = threads.get(int(row["tid"]))
        if key is None:
            continue
        stage = row["stage"]
        stage_totals[key][stage] += as_float(row["duration_ms"])
        stage_counts[key][stage] += 1
        scheduler_ns, hardirq_ns, softirq_ns, task_ns = stage_cpu_ns(
            run_intervals.get(int(row["tid"]), []),
            run_starts.get(int(row["tid"]), []),
            interrupts,
            interrupt_starts,
            int(row["begin_ns"]),
            int(row["end_ns"]),
        )
        cpu_totals = stage_cpu_totals[key]
        cpu_totals[f"{stage}:scheduler"] += scheduler_ns / 1_000_000.0
        cpu_totals[f"{stage}:hardirq"] += hardirq_ns / 1_000_000.0
        cpu_totals[f"{stage}:softirq"] += softirq_ns / 1_000_000.0
        cpu_totals[f"{stage}:task"] += task_ns / 1_000_000.0

    records: list[dict[str, object]] = []
    for key in sorted(stage_totals):
        family, instance = key
        stages = stage_totals[key]
        callbacks = stage_counts[key].get("callback", 0)
        if callbacks == 0:
            continue

        def per_frame(stage: str) -> float:
            return stages.get(stage, 0.0) / callbacks

        cpu_stages = stage_cpu_totals[key]

        def per_frame_task(stage: str) -> float:
            return cpu_stages.get(f"{stage}:task", 0.0) / callbacks

        # aggregator_enqueue is nested in syncer_emit.  Likewise,
        # metadata_dqbuf is nested in metadata.  Keep those substages for
        # localization, but do not add them twice to the path total.
        sync_total = per_frame("syncer_match") + per_frame("syncer_emit")
        sync_task = per_frame_task("syncer_match") + per_frame_task("syncer_emit")
        requeue = per_frame("requeue")
        continue_other = max(0.0, per_frame("sensor_continue") - requeue)
        converter = per_frame("format_convert")
        if converter:
            convert_other = max(0.0, converter - sync_total)
            invoke_other = max(0.0, per_frame("sensor_invoke") - converter)
            convert_other_task = max(
                0.0, per_frame_task("format_convert") - sync_task
            )
            invoke_other_task = max(
                0.0,
                per_frame_task("sensor_invoke")
                - per_frame_task("format_convert"),
            )
        else:
            # Compatibility with traces captured before the converter marker
            # was added.
            convert_other = 0.0
            invoke_other = max(0.0, per_frame("sensor_invoke") - sync_total)
            convert_other_task = 0.0
            invoke_other_task = max(
                0.0, per_frame_task("sensor_invoke") - sync_task
            )
        callback_other = max(
            0.0,
            per_frame("callback")
            - sum(
                per_frame(stage)
                for stage in (
                    "sensor_timestamp",
                    "sensor_allocate",
                    "sensor_copy",
                    "sensor_continue",
                    "sensor_invoke",
                )
            ),
        )
        before_callback = per_frame("metadata") + per_frame("video_dqbuf")
        callback_other_task = max(
            0.0,
            per_frame_task("callback")
            - sum(
                per_frame_task(stage)
                for stage in (
                    "sensor_timestamp",
                    "sensor_allocate",
                    "sensor_copy",
                    "sensor_continue",
                    "sensor_invoke",
                )
            ),
        )
        before_callback_task = (
            per_frame_task("metadata") + per_frame_task("video_dqbuf")
        )
        totals = activation_totals[key]
        raw = totals["raw"] / callbacks
        task = totals["task"] / callbacks
        marked_path = before_callback + per_frame("callback")

        records.append(
            {
                "case_id": case_id,
                "cameras": camera_count(case_id),
                "run": run_number,
                "family": family,
                "instance": instance,
                "callbacks": callbacks,
                "activations": activation_counts[key],
                "raw_ms": raw,
                "task_ms": task,
                "hardirq_ms": totals["hardirq"] / callbacks,
                "softirq_ms": totals["softirq"] / callbacks,
                "before_callback_ms": before_callback,
                "before_callback_task_ms": before_callback_task,
                "metadata_dqbuf_ms": per_frame("metadata_dqbuf"),
                "timestamp_ms": per_frame("sensor_timestamp"),
                "timestamp_task_ms": per_frame_task("sensor_timestamp"),
                "allocate_ms": per_frame("sensor_allocate"),
                "allocate_task_ms": per_frame_task("sensor_allocate"),
                "copy_ms": per_frame("sensor_copy"),
                "copy_task_ms": per_frame_task("sensor_copy"),
                "continue_other_ms": continue_other,
                "continue_task_ms": per_frame_task("sensor_continue"),
                "requeue_ms": requeue,
                "requeue_task_ms": per_frame_task("requeue"),
                "invoke_other_ms": invoke_other,
                "invoke_other_task_ms": invoke_other_task,
                "convert_other_ms": convert_other,
                "convert_other_task_ms": convert_other_task,
                "sync_join_ms": sync_total,
                "sync_join_task_ms": sync_task,
                "aggregator_enqueue_ms": per_frame("aggregator_enqueue"),
                "callback_other_ms": callback_other,
                "callback_other_task_ms": callback_other_task,
                "marked_path_ms": marked_path,
                "marked_path_task_ms": (
                    before_callback_task + per_frame_task("callback")
                ),
                # Stage durations are wall-clock spans.  This remainder is a
                # localization aid, not an exact CPU-accounting identity.
                "unmarked_or_span_ms": task - marked_path,
            }
        )
    return records


def aggregate(records: list[dict[str, object]], include_instance: bool) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for row in records:
        key: tuple[object, ...] = (row["cameras"], row["family"])
        if include_instance:
            key += (row["instance"],)
        groups[key].append(row)

    metrics = [
        "raw_ms",
        "task_ms",
        "hardirq_ms",
        "softirq_ms",
        "before_callback_ms",
        "before_callback_task_ms",
        "metadata_dqbuf_ms",
        "timestamp_ms",
        "timestamp_task_ms",
        "allocate_ms",
        "allocate_task_ms",
        "copy_ms",
        "copy_task_ms",
        "continue_other_ms",
        "continue_task_ms",
        "requeue_ms",
        "requeue_task_ms",
        "invoke_other_ms",
        "invoke_other_task_ms",
        "convert_other_ms",
        "convert_other_task_ms",
        "sync_join_ms",
        "sync_join_task_ms",
        "aggregator_enqueue_ms",
        "callback_other_ms",
        "callback_other_task_ms",
        "marked_path_ms",
        "marked_path_task_ms",
        "unmarked_or_span_ms",
    ]
    output: list[dict[str, object]] = []
    for key, rows in sorted(groups.items()):
        cameras, family, *instance = key
        item: dict[str, object] = {
            "cameras": cameras,
            "family": family,
            "instance": instance[0] if instance else "all",
            "run_records": len(rows),
        }
        for metric in metrics:
            item[metric] = median(float(row[metric]) for row in rows)
        output.append(item)
    return output


def print_table(title: str, rows: list[dict[str, object]]) -> None:
    print(f"\n{title}")
    print(
        "cams family inst  raw-ms task-ms irq-us copy-ms convert-ms "
        "invoke-other-ms sync-ms marked-ms remainder-ms"
    )
    for row in rows:
        irq_us = 1000.0 * (float(row["hardirq_ms"]) + float(row["softirq_ms"]))
        print(
            f"{int(row['cameras']):>4} {str(row['family']):<6} "
            f"{str(row['instance']):>4} "
            f"{float(row['raw_ms']):>7.4f} {float(row['task_ms']):>7.4f} "
            f"{irq_us:>6.2f} {float(row['copy_ms']):>7.4f} "
            f"{float(row['convert_other_ms']):>10.4f} "
            f"{float(row['invoke_other_ms']):>15.4f} "
            f"{float(row['sync_join_ms']):>7.4f} "
            f"{float(row['marked_path_ms']):>9.4f} "
            f"{float(row['unmarked_or_span_ms']):>12.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    records: list[dict[str, object]] = []
    attempts = selected_attempts(args.campaign_dir.resolve())
    for case_id, run_number, attempt_dir in attempts:
        records.extend(analyze_attempt(case_id, run_number, attempt_dir))
    if not records:
        raise SystemExit("No selected diagnostic attempts found")

    family_rows = aggregate(records, include_instance=False)
    instance_rows = aggregate(records, include_instance=True)
    print_table("Family medians across selected attempts", family_rows)
    print_table("Per-instance medians (nested D455 instances are 1 and 2)", instance_rows)

    if args.json_output:
        args.json_output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "selected_attempt_count": len(attempts),
                    "per_attempt": records,
                    "family_medians": family_rows,
                    "instance_medians": instance_rows,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
