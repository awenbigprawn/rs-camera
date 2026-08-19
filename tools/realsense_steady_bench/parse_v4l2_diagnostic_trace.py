#!/usr/bin/env python3
"""Decode the opt-in low-overhead librealsense V4L2 stage trace."""

from __future__ import annotations

import argparse
import bisect
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics
import struct
from typing import Any, Dict, Iterable, List, Tuple


HEADER = struct.Struct("<8sIIIIQQQ")
EVENT = struct.Struct("<QIHHiiII")
HEADER_BYTES = 4096
MAGIC = b"RSV4L2D"
STAGES = {
    1: "select_return",
    2: "metadata_begin",
    3: "metadata_end",
    4: "video_dqbuf_begin",
    5: "video_dqbuf_end",
    6: "metadata_dqbuf_begin",
    7: "metadata_dqbuf_end",
    8: "callback_begin",
    9: "callback_end",
    10: "requeue_begin",
    11: "requeue_end",
    12: "sensor_timestamp_begin",
    13: "sensor_timestamp_end",
    14: "sensor_allocate_begin",
    15: "sensor_allocate_end",
    16: "sensor_copy_begin",
    17: "sensor_copy_end",
    18: "sensor_continue_begin",
    19: "sensor_continue_end",
    20: "sensor_invoke_begin",
    21: "sensor_invoke_end",
    22: "syncer_match_begin",
    23: "syncer_match_end",
    24: "syncer_emit_begin",
    25: "syncer_emit_end",
    26: "aggregator_enqueue_begin",
    27: "aggregator_enqueue_end",
    28: "pipeline_wait_begin",
    29: "pipeline_wait_end",
    30: "format_convert_begin",
    31: "format_convert_end",
}
PAIRS = {
    stage: stage.removesuffix("_end") + "_begin"
    for stage in STAGES.values()
    if stage.endswith("_end")
}


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = quantile * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def distribution(values: Iterable[float]) -> Dict[str, float | int]:
    samples = list(values)
    if not samples:
        return {key: 0 for key in ("n", "min", "mean", "p50", "p90", "p99", "max")}
    return {
        "n": len(samples),
        "min": min(samples),
        "mean": statistics.fmean(samples),
        "p50": percentile(samples, 0.50),
        "p90": percentile(samples, 0.90),
        "p99": percentile(samples, 0.99),
        "max": max(samples),
    }


def phase_window(lifecycle_path: Path) -> Tuple[int, int]:
    markers: Dict[str, List[int]] = defaultdict(list)
    with lifecycle_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") == "phase_marker":
                markers[str(row.get("name"))].append(int(row["timestamp_ns"]))
    begin = markers["steady_state_begin"]
    end = markers["steady_state_end"]
    if len(begin) != 1 or len(end) != 1 or end[0] <= begin[0]:
        raise ValueError("lifecycle must contain one valid steady-state window")
    return begin[0], end[0]


ActivationIndex = Dict[int, Tuple[List[int], List[Dict[str, int]]]]


def load_activations(path: Path) -> ActivationIndex:
    rows_by_tid: Dict[int, List[Dict[str, int]]] = defaultdict(list)
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows_by_tid[int(row["tid"])].append(
                {
                    "activation": int(row["activation"]),
                    "release_ns": int(row["release_ns"]),
                    "completion_ns": int(row["completion_ns"]),
                }
            )
    result: ActivationIndex = {}
    for tid, rows in rows_by_tid.items():
        rows.sort(key=lambda row: row["release_ns"])
        result[tid] = ([row["release_ns"] for row in rows], rows)
    return result


def activation_at(
    activations: ActivationIndex, tid: int, timestamp_ns: int
) -> int | str:
    indexed = activations.get(tid)
    if indexed is None:
        return ""
    releases, rows = indexed
    position = bisect.bisect_right(releases, timestamp_ns) - 1
    if position >= 0 and timestamp_ns <= rows[position]["completion_ns"]:
        return rows[position]["activation"]
    return ""


def parse_trace(
    trace_path: Path,
    lifecycle_path: Path,
    output_dir: Path,
    compact_output: bool = False,
) -> Dict[str, Any]:
    steady_begin, steady_end = phase_window(lifecycle_path)
    activations = load_activations(output_dir / "thread_steady_activations.csv")
    with trace_path.open("rb") as handle:
        raw_header = handle.read(HEADER_BYTES)
        if len(raw_header) < HEADER.size:
            raise ValueError("V4L2 diagnostic trace header is truncated")
        magic, version, header_size, event_size, _, capacity, count, dropped = HEADER.unpack_from(raw_header)
        if magic[:7] != MAGIC or version != 1:
            raise ValueError("unsupported V4L2 diagnostic trace")
        if header_size != HEADER_BYTES or event_size != EVENT.size:
            raise ValueError("unexpected V4L2 diagnostic trace layout")
        stored_count = min(count, capacity)
        raw_events = handle.read(stored_count * EVENT.size)
    if len(raw_events) != stored_count * EVENT.size:
        raise ValueError("V4L2 diagnostic trace event array is truncated")

    event_rows: List[Dict[str, Any]] = []
    for offset in range(0, len(raw_events), EVENT.size):
        timestamp_ns, tid, cpu, stage_id, fd, result, sequence, _ = EVENT.unpack_from(raw_events, offset)
        stage = STAGES.get(stage_id, f"unknown_{stage_id}")
        row = {
            "timestamp_ns": timestamp_ns,
            "time_from_steady_begin_ms": (timestamp_ns - steady_begin) / 1_000_000,
            "in_steady_state": steady_begin <= timestamp_ns <= steady_end,
            "tid": tid,
            "cpu": cpu,
            "stage": stage,
            "fd": fd,
            "result": result,
            "sequence": sequence,
            "activation": activation_at(activations, tid, timestamp_ns),
        }
        event_rows.append(row)

    # The low-overhead producer stores events in per-CPU lanes so capture
    # threads do not contend on one global cache line. Re-establish the global
    # event order after the measured process has exited.
    event_rows.sort(key=lambda row: (int(row["timestamp_ns"]), int(row["tid"])))
    previous_video_sequence: Dict[Tuple[int, int], int] = {}
    sequence_gap_rows: List[Dict[str, Any]] = []
    video_frames_by_stream: Dict[Tuple[int, int], int] = defaultdict(int)
    stacks: Dict[Tuple[int, int, str], List[Dict[str, Any]]] = defaultdict(list)
    duration_rows: List[Dict[str, Any]] = []
    for row in event_rows:
        tid = int(row["tid"])
        fd = int(row["fd"])
        cpu = int(row["cpu"])
        stage = str(row["stage"])
        timestamp_ns = int(row["timestamp_ns"])
        sequence = int(row["sequence"])
        if stage == "video_dqbuf_end" and row["in_steady_state"]:
            stream_key = (tid, fd)
            video_frames_by_stream[stream_key] += 1
            previous = previous_video_sequence.get(stream_key)
            if previous is not None and sequence != previous + 1:
                sequence_gap_rows.append(
                    {
                        "timestamp_ns": timestamp_ns,
                        "time_from_steady_begin_ms": row["time_from_steady_begin_ms"],
                        "tid": tid,
                        "cpu": cpu,
                        "fd": fd,
                        "previous_sequence": previous,
                        "sequence": sequence,
                        "sequence_delta": sequence - previous,
                        "missing_frames": max(0, sequence - previous - 1),
                        "out_of_order": sequence <= previous,
                        "activation": row["activation"],
                    }
                )
            previous_video_sequence[stream_key] = sequence
        if stage.endswith("_begin"):
            stacks[(tid, fd, stage)].append(row)
        elif stage in PAIRS:
            begin_stage = PAIRS[stage]
            key = (tid, fd, begin_stage)
            if stacks[key]:
                begin_row = stacks[key].pop()
                duration_rows.append(
                    {
                        "tid": tid,
                        "cpu": cpu,
                        "fd": fd,
                        "stage": begin_stage.removesuffix("_begin"),
                        "begin_ns": begin_row["timestamp_ns"],
                        "end_ns": timestamp_ns,
                        "duration_ms": (timestamp_ns - int(begin_row["timestamp_ns"])) / 1_000_000,
                        "sequence": sequence,
                        "in_steady_state": bool(begin_row["in_steady_state"] and row["in_steady_state"]),
                        "activation": row["activation"],
                    }
                )

    output_dir.mkdir(parents=True, exist_ok=True)
    event_fields = list(event_rows[0]) if event_rows else ["timestamp_ns", "stage"]
    duration_fields = list(duration_rows[0]) if duration_rows else ["tid", "stage", "duration_ms"]
    sequence_gap_fields = (
        list(sequence_gap_rows[0])
        if sequence_gap_rows
        else ["timestamp_ns", "tid", "fd", "previous_sequence", "sequence"]
    )
    detailed_outputs = (
        (output_dir / "v4l2_diagnostic_events.csv", event_rows, event_fields),
        (output_dir / "v4l2_diagnostic_durations.csv", duration_rows, duration_fields),
    )
    if compact_output:
        for path, _, _ in detailed_outputs:
            path.unlink(missing_ok=True)
        outputs = (
            (output_dir / "v4l2_sequence_gaps.csv", sequence_gap_rows, sequence_gap_fields),
        )
    else:
        outputs = (
            *detailed_outputs,
            (output_dir / "v4l2_sequence_gaps.csv", sequence_gap_rows, sequence_gap_fields),
        )
    for path, rows, fields in outputs:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    steady_durations = [row for row in duration_rows if row["in_steady_state"]]
    summary = {
        "schema_version": 1,
        "event_count": count,
        "stored_event_count": stored_count,
        "dropped_event_count": dropped,
        "compact_output": compact_output,
        "steady_event_count": sum(bool(row["in_steady_state"]) for row in event_rows),
        "unmatched_begin_count": sum(len(rows) for rows in stacks.values()),
        "raw_video_sequence_discontinuity_count": len(sequence_gap_rows),
        "raw_video_missing_frame_count": sum(
            int(row["missing_frames"]) for row in sequence_gap_rows
        ),
        "raw_video_streams": [
            {
                "tid": tid,
                "fd": fd,
                "frames": frame_count,
                "sequence_discontinuities": sum(
                    1
                    for row in sequence_gap_rows
                    if int(row["tid"]) == tid and int(row["fd"]) == fd
                ),
                "missing_frames": sum(
                    int(row["missing_frames"])
                    for row in sequence_gap_rows
                    if int(row["tid"]) == tid and int(row["fd"]) == fd
                ),
            }
            for (tid, fd), frame_count in sorted(video_frames_by_stream.items())
        ],
        "stages_ms": {
            stage: distribution(
                float(row["duration_ms"])
                for row in steady_durations
                if row["stage"] == stage
            )
            for stage in sorted({str(row["stage"]) for row in steady_durations})
        },
        "longest_steady_stage": max(
            steady_durations,
            key=lambda row: float(row["duration_ms"]),
            default=None,
        ),
    }
    (output_dir / "v4l2_diagnostic_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--lifecycle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--compact-output",
        action="store_true",
        help="omit the full event and duration CSV files",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            parse_trace(
                args.trace,
                args.lifecycle,
                args.output_dir,
                compact_output=args.compact_output,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
