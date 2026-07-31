#!/usr/bin/env python3
"""Generate a per-thread SCHED_DEADLINE profile from SCHED_OTHER LiME traces."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TOOL = REPO_ROOT / "tools" / "realsense_startup_bench"
sys.path.insert(0, str(STARTUP_TOOL))

from parse_startup_trace import lifecycle_records, read_jsonl  # noqa: E402
from parse_steady_trace import marker_time  # noqa: E402


PROFILE_HEADER = [
    "signature",
    "instance",
    "name",
    "runtime_ns",
    "deadline_ns",
    "period_ns",
]
def _read_limit(path: Path, fallback: int) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return fallback


def _selected_attempt(path: Path) -> Path:
    if (path / "thread_lifecycle.jsonl").is_file():
        return path
    selection = path / "selected_attempt.txt"
    if selection.is_file():
        attempt = int(selection.read_text(encoding="utf-8").strip())
        selected = path / f"attempt-{attempt}"
        if selected.is_dir():
            return selected
    raise ValueError(
        f"{path} is neither a trace attempt nor a logical run with selected_attempt.txt"
    )


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def _thread_identities(
    lifecycle_path: Path,
) -> Tuple[Dict[int, Tuple[str, int, str]], int, int]:
    events = read_jsonl(lifecycle_path)
    begin = marker_time(events, "steady_state_begin")
    end = marker_time(events, "steady_state_end")
    records, _ = lifecycle_records(events, {})
    counts: Dict[str, int] = defaultdict(int)
    identities: Dict[int, Tuple[str, int, str]] = {}
    for record in sorted(
        records,
        key=lambda item: int(item.get("started_ns") or item.get("created_ns") or 0),
    ):
        tid = record.get("tid")
        name = str(record.get("name") or "")
        if name == "main":
            continue
        signature = str(record.get("signature") or "")
        thread_begin = int(record.get("started_ns") or record.get("created_ns") or begin)
        thread_end = int(record.get("exited_ns") or end)
        if (
            tid is None
            or not signature
            or min(end, thread_end) <= max(begin, thread_begin)
        ):
            continue
        counts[signature] += 1
        identities[int(tid)] = (
            signature,
            counts[signature],
            name,
        )
    return identities, begin, end


def _split_logical_jobs(
    activations: Sequence[Mapping[str, Any]],
) -> Tuple[List[List[Mapping[str, Any]]], float | None]:
    ordered = sorted(activations, key=lambda row: int(row["release_ns"]))
    if not ordered:
        return [], None
    deltas = [
        int(ordered[index]["release_ns"]) - int(ordered[index - 1]["release_ns"])
        for index in range(1, len(ordered))
    ]
    positive = sorted(delta for delta in deltas if delta > 0)
    threshold: float | None = None
    if len(positive) >= 4:
        best_ratio = 1.0
        best_index = -1
        for index in range(len(positive) - 1):
            if positive[index] <= 0:
                continue
            ratio = positive[index + 1] / positive[index]
            if ratio > best_ratio:
                best_ratio = ratio
                best_index = index
        if best_ratio >= 4.0:
            upper_count = len(positive) - best_index - 1
            upper_fraction = upper_count / len(positive)
            if upper_count >= 2 and 0.05 <= upper_fraction <= 0.90:
                threshold = math.sqrt(
                    positive[best_index] * positive[best_index + 1]
                )

    groups: List[List[Mapping[str, Any]]] = [[ordered[0]]]
    for index in range(1, len(ordered)):
        delta = int(ordered[index]["release_ns"]) - int(
            ordered[index - 1]["release_ns"]
        )
        if threshold is None or delta >= threshold:
            groups.append([ordered[index]])
        else:
            groups[-1].append(ordered[index])
    return groups, threshold


def _minimum_stable_period_ns(groups: Sequence[Sequence[Mapping[str, Any]]]) -> int:
    releases = [int(group[0]["release_ns"]) for group in groups if group]
    periods = [
        releases[index] - releases[index - 1]
        for index in range(1, len(releases))
        if releases[index] > releases[index - 1]
    ]
    if len(periods) < 2:
        raise ValueError("fewer than three complete logical jobs")
    median = statistics.median(periods)
    stable = [period for period in periods if 0.80 * median <= period <= 1.20 * median]
    if len(stable) < max(2, int(math.ceil(len(periods) * 0.50))):
        raise ValueError("logical period is not stable enough to model")
    return min(stable)


def _maximum_job_execution_ns(
    groups: Sequence[Sequence[Mapping[str, Any]]],
) -> int:
    maxima = [
        sum(int(round(float(row["execution_ms"]) * 1_000_000)) for row in group)
        for group in groups
        if group
    ]
    if not maxima or max(maxima) <= 0:
        raise ValueError("no positive execution demand was observed")
    return max(maxima)


def _trace_threads(path: Path) -> Tuple[Dict[Tuple[str, int], Dict[str, Any]], Dict[str, Any]]:
    attempt = _selected_attempt(path.resolve())
    lifecycle = attempt / "thread_lifecycle.jsonl"
    activations_path = attempt / "thread_steady_activations.csv"
    summary_path = attempt / "thread_steady_summary.json"
    for required in (lifecycle, activations_path, summary_path):
        if not required.is_file():
            raise ValueError(f"required trace artifact is missing: {required}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    identities, begin, end = _thread_identities(lifecycle)
    summary_by_tid = {
        int(thread["tid"]): thread
        for thread in summary.get("threads", [])
        if thread.get("tid") is not None
    }
    rows_by_tid: Dict[int, List[Dict[str, str]]] = defaultdict(list)
    for row in _read_csv(activations_path):
        if _bool(row.get("partial_start", "")) or _bool(row.get("partial_end", "")):
            continue
        rows_by_tid[int(row["tid"])].append(row)

    result: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for tid, (signature, instance, lifecycle_name) in identities.items():
        thread_summary = summary_by_tid.get(tid)
        if not thread_summary:
            raise ValueError(f"TID {tid} has no LiME steady-state summary")
        policy = str(thread_summary.get("policy") or "")
        if "SCHED_OTHER" not in policy or "|" in policy:
            raise ValueError(
                f"TID {tid} source policy is {policy!r}, not a pure SCHED_OTHER trace"
            )
        groups, threshold = _split_logical_jobs(rows_by_tid.get(tid, []))
        try:
            period_ns = _minimum_stable_period_ns(groups)
            execution_ns = _maximum_job_execution_ns(groups)
        except ValueError as error:
            raise ValueError(
                f"cannot model TID {tid} ({lifecycle_name}, {signature}): {error}"
            ) from error
        key = (signature, instance)
        if key in result:
            raise ValueError(f"duplicate live-thread identity: {key}")
        result[key] = {
            "name": str(thread_summary.get("name") or lifecycle_name),
            "period_ns": period_ns,
            "execution_ns": execution_ns,
            "activation_count": len(rows_by_tid.get(tid, [])),
            "logical_job_count": len(groups),
            "burst_threshold_ns": threshold,
            "model_kind": "periodic_or_sporadic_worker",
        }

    digest = hashlib.sha256(lifecycle.read_bytes()).hexdigest()
    metadata = {
        "input": str(path.resolve()),
        "attempt": str(attempt),
        "steady_begin_ns": begin,
        "steady_end_ns": end,
        "duration_ms": (end - begin) / 1_000_000,
        "lifecycle_sha256": digest,
        "thread_count": len(result),
    }
    return result, metadata


def generate_profile(
    trace_runs: Iterable[Path],
    output: Path,
    runtime_margin: float,
    period_scale: float,
    minimum_runtime_us: int,
    maximum_period_us: int,
) -> Dict[str, Any]:
    if runtime_margin < 1.0:
        raise ValueError("runtime margin must be at least 1.0")
    if not 0.0 < period_scale <= 1.0:
        raise ValueError("period scale must be in (0, 1]")
    runs = list(trace_runs)
    if not runs:
        raise ValueError("at least one SCHED_OTHER trace run is required")

    per_run = []
    sources = []
    for run in runs:
        threads, source = _trace_threads(run)
        per_run.append(threads)
        sources.append(source)

    expected = set(per_run[0])
    for index, threads in enumerate(per_run[1:], 2):
        if set(threads) != expected:
            missing = sorted(expected - set(threads))
            extra = sorted(set(threads) - expected)
            raise ValueError(
                f"trace run {index} has a different live-thread set; "
                f"missing={missing}, extra={extra}"
            )

    minimum_runtime_ns = minimum_runtime_us * 1000
    maximum_period_ns = maximum_period_us * 1000
    rows = []
    detail = []
    for signature, instance in sorted(expected):
        observations = [threads[(signature, instance)] for threads in per_run]
        observed_execution_ns = max(item["execution_ns"] for item in observations)
        observed_period_ns = min(item["period_ns"] for item in observations)
        runtime_ns = max(
            minimum_runtime_ns,
            int(math.ceil(observed_execution_ns * runtime_margin)),
        )
        period_ns = min(
            maximum_period_ns,
            int(math.floor(observed_period_ns * period_scale)),
        )
        if runtime_ns > period_ns:
            raise ValueError(
                f"runtime exceeds deadline for {signature} instance {instance}: "
                f"runtime={runtime_ns}, period={period_ns}"
            )
        name = str(observations[0]["name"])
        rows.append(
            {
                "signature": signature,
                "instance": instance,
                "name": name,
                "runtime_ns": runtime_ns,
                "deadline_ns": period_ns,
                "period_ns": period_ns,
            }
        )
        detail.append(
            {
                **rows[-1],
                "observed_execution_max_ns": observed_execution_ns,
                "observed_logical_period_min_ns": observed_period_ns,
                "runtime_was_clamped_to_kernel_minimum": (
                    runtime_ns == minimum_runtime_ns
                    and observed_execution_ns * runtime_margin < minimum_runtime_ns
                ),
                "period_was_clamped_to_kernel_maximum": (
                    period_ns == maximum_period_ns
                    and observed_period_ns * period_scale > maximum_period_ns
                ),
                "utilization": runtime_ns / period_ns,
                "observations": observations,
            }
        )

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROFILE_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    metadata = {
        "schema_version": 1,
        "profile": str(output),
        "formula": {
            "runtime": "max(kernel_minimum, ceil(runtime_margin * observed_max_logical_job_execution))",
            "deadline": "min(kernel_maximum, floor(period_scale * observed_min_stable_logical_period))",
            "period": "deadline",
        },
        "runtime_margin": runtime_margin,
        "period_scale": period_scale,
        "minimum_runtime_us": minimum_runtime_us,
        "maximum_period_us": maximum_period_us,
        "thread_count": len(rows),
        "total_reserved_cpu_utilization": sum(
            row["runtime_ns"] / row["period_ns"] for row in rows
        ),
        "sources": sources,
        "threads": detail,
    }
    metadata_path = output.with_suffix(output.suffix + ".json")
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace-run",
        type=Path,
        action="append",
        required=True,
        help="SCHED_OTHER attempt/run directory; repeat for independent traces",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-margin", type=float, default=1.20)
    parser.add_argument("--period-scale", type=float, default=0.91)
    parser.add_argument(
        "--minimum-runtime-us",
        type=int,
        default=_read_limit(
            Path("/proc/sys/kernel/sched_deadline_period_min_us"), 100
        ),
    )
    parser.add_argument(
        "--maximum-period-us",
        type=int,
        default=_read_limit(
            Path("/proc/sys/kernel/sched_deadline_period_max_us"), 4_194_304
        ),
    )
    args = parser.parse_args()
    if args.minimum_runtime_us < 1 or args.maximum_period_us < 1:
        raise SystemExit("kernel runtime/period limits must be positive")
    metadata = generate_profile(
        trace_runs=args.trace_run,
        output=args.output,
        runtime_margin=args.runtime_margin,
        period_scale=args.period_scale,
        minimum_runtime_us=args.minimum_runtime_us,
        maximum_period_us=args.maximum_period_us,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
