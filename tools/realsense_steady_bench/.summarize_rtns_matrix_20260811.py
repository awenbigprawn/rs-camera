#!/usr/bin/env python3
"""Summarize progress and results for the temporary RTNS matrix."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys


def main() -> None:
    root = Path(sys.argv[1])
    records = []
    for path in sorted(root.rglob("cell_validation.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        relative = path.relative_to(root).parts
        if len(relative) < 7:
            continue
        kernel, kind, round_part, case_id, noise_part, policy_part = relative[:6]
        record.update(
            {
                "kernel": kernel,
                "kind": kind,
                "round": round_part.removeprefix("round-"),
                "case_id": case_id,
                "noise": noise_part.removeprefix("noise-"),
                "policy": policy_part.removeprefix("policy-"),
                "path": str(path.parent),
            }
        )
        records.append(record)

    expected = {"preflight": 2, "formal": 192, "scaling": 30}
    by_kind = Counter(record["kind"] for record in records)
    by_kernel = Counter(record["kernel"] for record in records)
    failures = [record for record in records if not record["success"]]
    freshness = {
        key: sum(int(record.get(key, 0)) for record in records)
        for key in (
            "duplicate_frames",
            "sequence_gaps",
            "stale_framesets",
            "timeouts",
        )
    }
    grouped = defaultdict(list)
    for record in records:
        if record["kind"] != "formal":
            continue
        grouped[
            (
                record["kernel"],
                record["case_id"],
                record["noise"],
                record["policy"],
            )
        ].append(record)

    summary = {
        "root": str(root),
        "validated_cells": len(records),
        "expected_cells": sum(expected.values()),
        "expected_by_kind": expected,
        "completed_by_kind": dict(sorted(by_kind.items())),
        "completed_by_kernel": dict(sorted(by_kernel.items())),
        "failed_validated_cells": len(failures),
        "freshness_totals": freshness,
        "formal_groups_complete": sum(len(group) == 3 for group in grouped.values()),
        "formal_groups_expected": 64,
        "formal_group_attempt_counts": {
            "mean": statistics.fmean(
                record["attempt_count"]
                for record in records
                if record["kind"] == "formal"
            )
            if any(record["kind"] == "formal" for record in records)
            else 0.0,
            "max": max(
                (
                    record["attempt_count"]
                    for record in records
                    if record["kind"] == "formal"
                ),
                default=0,
            ),
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
