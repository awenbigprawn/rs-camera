#!/usr/bin/env python3
"""Compare capture-worker PMU behavior between camera-count cases.

The PMU runner attaches ``perf stat --per-thread`` after steady workers have
been created.  This analyzer joins each perf PID/TID to the corresponding
LiME ``thread_steady_summary.csv`` and emits one row per capture worker.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path


CAPTURE_SIGNATURES = {
    "0x6c9348": "Color",
    "0x6c8358": "Color",
    "0x6c5938": "Color",
    "0x41fdc0": "Depth",
    "0x41edd0": "Depth",
    "0x41d980": "Depth",
}


def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter=delimiter))


def infer_case(path: Path) -> str:
    part = next(value for value in path.parts if value.startswith("case_id-"))
    return part.removeprefix("case_id-")


def infer_run(path: Path) -> int:
    part = next(value for value in path.parts if value.startswith("run-"))
    return int(part.removeprefix("run-"))


def camera_count(case_id: str) -> int:
    for count in (1, 2, 4):
        if f"_n{count}_" in case_id:
            return count
    raise ValueError(f"Cannot infer camera count from case id: {case_id}")


def family(signature: str) -> str | None:
    for marker, name in CAPTURE_SIGNATURES.items():
        if marker in signature:
            return name
    return None


def perf_counts(path: Path) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    counts: dict[int, dict[str, float]] = defaultdict(dict)
    coverage: dict[int, dict[str, float]] = defaultdict(dict)
    with path.open(newline="") as stream:
        for fields in csv.reader(stream, delimiter=";"):
            if len(fields) < 6 or not fields[0] or fields[0].startswith("#"):
                continue
            event = fields[3]
            value = fields[1]
            if not event or value.startswith("<"):
                continue
            try:
                tid = int(fields[0].rsplit("-", 1)[1])
                counts[tid][event] = float(value)
                coverage[tid][event] = float(fields[5])
            except (IndexError, ValueError):
                continue
    return counts, coverage


def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def collect(result_dir: Path) -> list[dict[str, object]]:
    summaries: dict[int, tuple[Path, list[dict[str, str]]]] = {}
    for path in result_dir.rglob("thread_steady_summary.csv"):
        rows = read_csv(path)
        main = next((row for row in rows if row["signature"] == "process-main"), None)
        if main is not None:
            summaries[int(main["tid"])] = (path, rows)

    output: list[dict[str, object]] = []
    for perf_path in sorted((result_dir / "perf").glob("perf-stat-pid-*.csv")):
        pid = int(perf_path.stem.removeprefix("perf-stat-pid-"))
        if pid not in summaries:
            raise RuntimeError(f"No thread summary matches perf PID {pid}")
        summary_path, rows = summaries[pid]
        case_id = infer_case(summary_path)
        run = infer_run(summary_path)
        counts, coverage = perf_counts(perf_path)
        meta_path = perf_path.with_suffix(".meta")
        metadata = dict(
            line.split("=", 1)
            for line in meta_path.read_text().splitlines()
            if "=" in line
        )
        seconds = float(metadata["measurement_seconds"])
        expected_frames = seconds * 30.0

        for row in rows:
            worker_family = family(row["signature"])
            tid = int(row["tid"])
            if worker_family is None or tid not in counts:
                continue
            values = counts[tid]
            instructions = values.get("instructions", 0.0)
            cycles = values.get("cycles", 0.0)
            l1_loads = values.get("L1-dcache-loads", 0.0)
            l1_misses = values.get("L1-dcache-load-misses", 0.0)
            llc_loads = values.get("LLC-loads", 0.0)
            llc_misses = values.get("LLC-load-misses", 0.0)
            cache_misses = values.get("cache-misses", 0.0)
            backend_stalls = values.get("stalled-cycles-backend", 0.0)
            event_coverage = [
                coverage[tid][event]
                for event in values
                if event in coverage[tid]
            ]
            output.append(
                {
                    "case_id": case_id,
                    "camera_count": camera_count(case_id),
                    "run": run,
                    "pid": pid,
                    "tid": tid,
                    "family": worker_family,
                    "instance": int(row["profile_instance"]),
                    "instructions": instructions,
                    "cycles": cycles,
                    "instructions_per_frame": safe_ratio(instructions, expected_frames),
                    "cycles_per_frame": safe_ratio(cycles, expected_frames),
                    "ipc": safe_ratio(instructions, cycles),
                    "cpi": safe_ratio(cycles, instructions),
                    "backend_stall_fraction": safe_ratio(backend_stalls, cycles),
                    "l1d_miss_fraction": safe_ratio(l1_misses, l1_loads),
                    "llc_miss_fraction": safe_ratio(llc_misses, llc_loads),
                    "cache_misses_per_kinstruction": 1000.0
                    * safe_ratio(cache_misses, instructions),
                    "minimum_event_coverage_percent": min(event_coverage)
                    if event_coverage
                    else 0.0,
                    "summary_path": str(summary_path),
                    "perf_path": str(perf_path),
                }
            )
    return sorted(
        output,
        key=lambda row: (
            int(row["camera_count"]),
            int(row["run"]),
            str(row["family"]),
            int(row["instance"]),
        ),
    )


def median_comparison(records: list[dict[str, object]]) -> list[dict[str, object]]:
    metrics = (
        "instructions_per_frame",
        "cycles_per_frame",
        "ipc",
        "cpi",
        "backend_stall_fraction",
        "l1d_miss_fraction",
        "llc_miss_fraction",
        "cache_misses_per_kinstruction",
        "minimum_event_coverage_percent",
    )
    grouped: dict[tuple[int, str, int], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        grouped[
            (
                int(record["camera_count"]),
                str(record["family"]),
                int(record["instance"]),
            )
        ].append(record)

    medians: dict[tuple[int, str, int], dict[str, float]] = {}
    for key, rows in grouped.items():
        medians[key] = {
            metric: statistics.median(float(row[metric]) for row in rows)
            for metric in metrics
        }

    comparison: list[dict[str, object]] = []
    for worker_family in ("Color", "Depth"):
        baseline = medians.get((1, worker_family, 1))
        scaled = medians.get((4, worker_family, 1))
        if baseline is None or scaled is None:
            continue
        row: dict[str, object] = {"family": worker_family, "instance": 1}
        for metric in metrics:
            before = baseline[metric]
            after = scaled[metric]
            row[f"n1_{metric}"] = before
            row[f"n4_{metric}"] = after
            row[f"change_percent_{metric}"] = (
                100.0 * (after - before) / before if before else 0.0
            )
        comparison.append(row)
    return comparison


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or args.result_dir / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    records = collect(args.result_dir)
    comparison = median_comparison(records)
    write_csv(output_dir / "capture_worker_pmu.csv", records)
    write_csv(output_dir / "capture_worker_pmu_n1_vs_n4.csv", comparison)
    (output_dir / "capture_worker_pmu_n1_vs_n4.json").write_text(
        json.dumps(comparison, indent=2) + "\n"
    )
    print(json.dumps(comparison, indent=2))


if __name__ == "__main__":
    main()
