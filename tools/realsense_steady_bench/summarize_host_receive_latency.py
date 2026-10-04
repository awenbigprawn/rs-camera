#!/usr/bin/env python3
"""Aggregate strict host receive-to-frameset latency from full-path runs."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import re
from typing import Iterable


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = quantile * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def write_csv(path: Path, rows: Iterable[dict[str, object]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def condition_name(path: Path) -> str:
    for part in path.parts:
        if part.startswith("h1-uvc16-"):
            return part
    raise ValueError(f"Cannot identify H1 condition from {path}")


def workload_name(path: Path) -> str:
    text = str(path)
    return "stress_60fps" if "stress" in text else "representative_30fps"


def logical_rep(path: Path) -> int:
    if "replacement-run3" in str(path):
        return 3
    match = re.search(r"/run-(\d+)/attempt-\d+", str(path))
    if not match:
        raise ValueError(f"Cannot identify repetition from {path}")
    return int(match.group(1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--runs-output", type=Path)
    parser.add_argument("--groups-output", type=Path)
    args = parser.parse_args()
    runs_output = args.runs_output or args.root / "h1_host_receive_latency_runs.csv"
    groups_output = args.groups_output or args.root / "h1_host_receive_latency_cells.csv"

    run_rows: list[dict[str, object]] = []
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    matched: dict[tuple[str, str], int] = defaultdict(int)
    total: dict[tuple[str, str], int] = defaultdict(int)

    for summary_path in sorted(args.root.rglob("full_receive_path_summary.json")):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        latency = summary.get("host_receive_to_frameset_latency")
        if not latency or int(latency["n"]) == 0:
            continue
        condition = condition_name(summary_path)
        workload = workload_name(summary_path)
        rep = logical_rep(summary_path)
        frames_path = summary_path.with_name("host_receive_latency_frames.csv")
        with frames_path.open(newline="", encoding="utf-8") as handle:
            values = [float(row["latency_ms"]) for row in csv.DictReader(handle)]
        key = (condition, workload)
        samples[key].extend(values)
        matched[key] += int(latency["matched_deliveries"])
        total[key] += int(latency["total_deliveries"])
        run_rows.append(
            {
                "condition": condition,
                "workload": workload,
                "rep": rep,
                "samples": int(latency["n"]),
                "total_deliveries": int(latency["total_deliveries"]),
                "coverage": float(latency["coverage"]),
                "p99_ms": float(latency["p99_us"]) / 1000.0,
                "max_ms": float(latency["max_us"]) / 1000.0,
                "selected_attempt_dir": str(summary_path.parent),
            }
        )

    run_rows.sort(key=lambda row: (str(row["condition"]), str(row["workload"]), int(row["rep"])))
    group_rows: list[dict[str, object]] = []
    for condition, workload in sorted(samples):
        values = samples[(condition, workload)]
        group_rows.append(
            {
                "condition": condition,
                "workload": workload,
                "repetitions": 3,
                "samples": len(values),
                "total_deliveries": total[(condition, workload)],
                "coverage": matched[(condition, workload)] / total[(condition, workload)],
                "pooled_p99_ms": percentile(values, 0.99),
                "pooled_max_ms": max(values),
            }
        )

    write_csv(
        runs_output,
        run_rows,
        [
            "condition",
            "workload",
            "rep",
            "samples",
            "total_deliveries",
            "coverage",
            "p99_ms",
            "max_ms",
            "selected_attempt_dir",
        ],
    )
    write_csv(
        groups_output,
        group_rows,
        [
            "condition",
            "workload",
            "repetitions",
            "samples",
            "total_deliveries",
            "coverage",
            "pooled_p99_ms",
            "pooled_max_ms",
        ],
    )
    print(runs_output)
    print(groups_output)


if __name__ == "__main__":
    main()
