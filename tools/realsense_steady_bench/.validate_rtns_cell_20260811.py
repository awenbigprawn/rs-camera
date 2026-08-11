#!/usr/bin/env python3
"""Validate one temporary RTNS matrix cell and save a compact result."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell", type=Path)
    parser.add_argument("policy", choices=tuple(EXPECTED_POLICIES))
    parser.add_argument("noise", choices=("none", "cpu4", "memory2000", "gpu"))
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
    effective = row.get("steady_worker_policy_effective", "")
    expected = EXPECTED_POLICIES[args.policy]
    selected_noise_valid = {
        "none": True,
        "cpu4": truth(row.get("cpu_noise_valid")),
        "memory2000": truth(row.get("memory_noise_valid")),
        "gpu": truth(row.get("gpu_noise_valid")),
    }[args.noise]
    result = {
        "benchmark_csv": str(csvs[0]),
        "success": truth(row.get("success")),
        "eventual_success": truth(row.get("eventual_success")),
        "attempt_count": int(row.get("attempt_count") or 0),
        "expected_steady_policy": expected,
        "effective_steady_policy": effective,
        "noise": args.noise,
        "selected_noise_valid": selected_noise_valid,
        "camera_count": int(row.get("camera_count") or 0),
        "deliveries": int(row.get("deliveries") or 0),
        "unique_frames": int(row.get("unique_frames") or 0),
        "duplicate_frames": int(row.get("duplicate_frames") or 0),
        "sequence_gaps": int(row.get("sequence_gaps") or 0),
        "stale_framesets": int(row.get("stale_framesets") or 0),
        "timeouts": int(row.get("timeouts") or 0),
        "measurement_duration_ms": float(
            row.get("measurement_duration_ms") or 0.0
        ),
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
    if not selected_noise_valid:
        raise SystemExit(f"selected noise did not complete cleanly: {result}")
    if result["fixed_cpu_isolation"]:
        raise SystemExit("CPU isolation was unexpectedly enabled")
    if result["fixed_cmake_build_type"] != "RelWithDebInfo":
        raise SystemExit(f"unexpected build type: {result['fixed_cmake_build_type']}")


if __name__ == "__main__":
    main()
