#!/usr/bin/env python3
"""Validate one P1 v2 cell and retain a compact, fail-closed audit."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


EXPECTED_POLICIES = {
    "other": "SCHED_OTHER",
    "rr-rm": "SCHED_RR",
    "fifo-rm": "SCHED_FIFO",
    "deadline": "SCHED_DEADLINE",
}


def truth(value: str | None) -> bool:
    return str(value or "").lower() == "true"


def integer(row: dict[str, str], key: str) -> int:
    return int(row.get(key) or 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell", type=Path)
    parser.add_argument("policy", choices=tuple(EXPECTED_POLICIES))
    parser.add_argument("noise", choices=("none", "cpu4", "memory2000", "gpu"))
    parser.add_argument("uvc_policy", choices=("keep", "fifo", "rr"))
    args = parser.parse_args()

    csvs = sorted((args.cell / "results").glob("benchmark_*.csv"))
    if len(csvs) != 1:
        raise SystemExit(f"expected one benchmark CSV, found {len(csvs)}")
    lines = [
        line
        for line in csvs[0].read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    rows = list(csv.DictReader(lines, delimiter=";"))
    if len(rows) != 1:
        raise SystemExit(f"expected one result row, found {len(rows)}")
    row = rows[0]
    expected = EXPECTED_POLICIES[args.policy]
    effective = row.get("steady_worker_policy_effective", "")
    noise_valid = {
        "none": True,
        "cpu4": truth(row.get("cpu_noise_valid")),
        "memory2000": truth(row.get("memory_noise_valid")),
        "gpu": truth(row.get("gpu_noise_valid")),
    }[args.noise]

    monitor_logs = sorted(args.cell.glob("uvc_worker_monitor-*.jsonl"))
    if not monitor_logs:
        raise SystemExit("no UVC monitor log was recorded")
    monitor_text = monitor_logs[-1].read_text(encoding="utf-8")
    expected_action = "observed" if args.uvc_policy == "keep" else "promoted"
    monitor_valid = (
        f'"action":"{expected_action}"' in monitor_text
        and '"action":"promote-failed"' not in monitor_text
        and '"action":"restore-failed"' not in monitor_text
    )
    camera_count = integer(row, "camera_count")
    camera_deliveries = [
        integer(row, f"camera_{index}_deliveries")
        for index in range(camera_count)
    ]
    measurement_duration_ms = float(row.get("measurement_duration_ms") or 0.0)
    expected_fps = 60 if "stress60" in str(args.cell) else 30
    minimum_camera_deliveries = 0.95 * expected_fps * measurement_duration_ms / 1000.0

    result = {
        "benchmark_csv": str(csvs[0]),
        "success": truth(row.get("success")),
        "eventual_success": truth(row.get("eventual_success")),
        "attempt_count": integer(row, "attempt_count"),
        "expected_steady_policy": expected,
        "effective_steady_policy": effective,
        "noise": args.noise,
        "selected_noise_valid": noise_valid,
        "uvc_worker_policy": args.uvc_policy,
        "uvc_monitor_valid": monitor_valid,
        "camera_count": camera_count,
        "camera_deliveries": camera_deliveries,
        "minimum_camera_deliveries": minimum_camera_deliveries,
        "deliveries": integer(row, "deliveries"),
        "unique_frames": integer(row, "unique_frames"),
        "duplicate_frames": integer(row, "duplicate_frames"),
        "sequence_gaps": integer(row, "sequence_gaps"),
        "partially_stale_framesets": integer(row, "partially_stale_framesets"),
        "fully_stale_framesets": integer(row, "fully_stale_framesets"),
        "stale_framesets": integer(row, "stale_framesets"),
        "timeouts": integer(row, "timeouts"),
        "measurement_duration_ms": measurement_duration_ms,
        "fixed_cpu_isolation": truth(row.get("fixed_cpu_isolation")),
        "fixed_cmake_build_type": row.get("fixed_cmake_build_type", ""),
    }
    (args.cell / "cell_validation.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not result["success"] or not result["eventual_success"]:
        raise SystemExit(f"benchmark failed: {result}")
    if effective != expected:
        raise SystemExit(f"policy mismatch: expected {expected}, got {effective}")
    if not noise_valid:
        raise SystemExit(f"selected noise did not complete cleanly: {result}")
    if not monitor_valid:
        raise SystemExit(f"UVC worker monitor audit failed: {result}")
    if result["timeouts"]:
        raise SystemExit(f"measurement timeouts are not allowed: {result}")
    if camera_count != 2:
        raise SystemExit(f"expected two cameras: {result}")
    if any(deliveries < minimum_camera_deliveries for deliveries in camera_deliveries):
        raise SystemExit(f"insufficient per-camera deliveries: {result}")
    if result["fixed_cpu_isolation"]:
        raise SystemExit("CPU isolation was unexpectedly enabled")
    if result["fixed_cmake_build_type"] != "RelWithDebInfo":
        raise SystemExit(f"unexpected build type: {result['fixed_cmake_build_type']}")


if __name__ == "__main__":
    main()
