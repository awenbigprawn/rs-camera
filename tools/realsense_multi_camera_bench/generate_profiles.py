#!/usr/bin/env python3
"""Generate one modeled-scheduler profile per exact multi-camera case."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import Dict, List


TOOL_DIR = Path(__file__).resolve().parent
TOOLS_DIR = TOOL_DIR.parent
STEADY_TOOL = TOOLS_DIR / "realsense_steady_bench"
sys.path.insert(0, str(STEADY_TOOL))

from generate_deadline_profile import generate_profile, _read_limit  # noqa: E402


def successful_other_runs(root: Path) -> Dict[str, List[Path]]:
    grouped: Dict[str, List[Path]] = defaultdict(list)
    for case_path in root.rglob("case.json"):
        run_dir = case_path.parent
        summary_path = run_dir / "steady_summary.json"
        manifest_path = run_dir / "run_manifest.json"
        if not summary_path.is_file() or not manifest_path.is_file():
            continue
        try:
            case = json.loads(case_path.read_text(encoding="utf-8"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not summary.get("eventual_success", summary.get("success", False)):
            continue
        if manifest.get("policy_requested") != "SCHED_OTHER":
            continue
        case_id = str(case.get("case_id", ""))
        if case_id:
            grouped[case_id].append(run_dir)
    return {
        case_id: sorted(paths)
        for case_id, paths in sorted(grouped.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-runs", type=int, default=3)
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
    parser.add_argument("--maximum-modeled-period-us", type=int)
    args = parser.parse_args()
    if args.minimum_runs < 1:
        raise SystemExit("--minimum-runs must be positive")

    grouped = successful_other_runs(args.results_root.resolve())
    if not grouped:
        raise SystemExit("no successful SCHED_OTHER multi-camera trace runs found")
    insufficient = {
        case_id: len(paths)
        for case_id, paths in grouped.items()
        if len(paths) < args.minimum_runs
    }
    if insufficient:
        raise SystemExit(
            "insufficient traces: "
            + ", ".join(
                f"{case_id}={count}/{args.minimum_runs}"
                for case_id, count in insufficient.items()
            )
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    index = {"schema_version": 1, "profiles": []}
    for case_id, paths in grouped.items():
        output = args.output_dir / f"{case_id}.csv"
        print(f"[PROFILE] {case_id}: {len(paths)} trace run(s) -> {output}")
        metadata = generate_profile(
            trace_runs=paths,
            output=output,
            runtime_margin=args.runtime_margin,
            period_scale=args.period_scale,
            minimum_runtime_us=args.minimum_runtime_us,
            maximum_period_us=args.maximum_period_us,
            maximum_modeled_period_us=args.maximum_modeled_period_us,
        )
        index["profiles"].append(
            {
                "case_id": case_id,
                "profile": str(output.resolve()),
                "trace_runs": [str(path.resolve()) for path in paths],
                "metadata": metadata,
            }
        )
    (args.output_dir / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
