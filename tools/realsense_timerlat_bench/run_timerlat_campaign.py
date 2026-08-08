#!/usr/bin/env python3
"""Run the minimal RTNS Timerlat matrix on the currently booted kernel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping


TOOL_DIR = Path(__file__).resolve().parent
TOOLS_DIR = TOOL_DIR.parent
REPO_ROOT = TOOLS_DIR.parent
BENCHKIT_PATH = REPO_ROOT / "deps" / "benchkit"
STEADY_TOOL_DIR = TOOLS_DIR / "realsense_steady_bench"
sys.path.insert(0, str(BENCHKIT_PATH))
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(STEADY_TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR))

from benchkit.campaign import CampaignCartesianProduct  # noqa: E402
from realsense_bench_common.settings import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS_PER_RUN,
    DEFAULT_RECOVER_ON_FAILURE,
    DEFAULT_RECOVERY_RESET_TIMEOUT_MS,
    DEFAULT_RECOVERY_SETTLE_SECONDS,
    DEFAULT_RECOVERY_WAIT_SECONDS,
    DEFAULT_RESET_BEFORE_RUN,
)
from timerlat_benchmark import RealSenseTimerlatBench  # noqa: E402
from timerlat_settings import (  # noqa: E402
    CAMPAIGN_CPU_NOISE_WORKERS,
    CAMPAIGN_REPETITIONS,
    DEFAULT_BUILD_DIR,
    DEFAULT_CONFIG,
    DEFAULT_RESULTS_DIR,
    KERNEL_LABELS,
)


def load_matrix(path: Path) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("timerlat"), dict):
        raise ValueError(f"{path} does not contain a timerlat object")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path} does not contain a non-empty case list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not case.get("case_id"):
            raise ValueError(f"Case without case_id: {case!r}")
        if case["case_id"] in seen:
            raise ValueError(f"Duplicate case_id: {case['case_id']}")
        seen.add(case["case_id"])
        camera = case.get("camera", {})
        if bool(camera.get("enabled")) != (int(camera.get("count", 0)) > 0):
            raise ValueError(
                f"Case {case['case_id']!r} has inconsistent camera enabled/count"
            )
        noise = case.get("noise", {})
        if noise.get("cpu") == "busy_loop" and int(
            noise.get("workers", -1)
        ) != CAMPAIGN_CPU_NOISE_WORKERS:
            raise ValueError(
                f"Case {case['case_id']!r} must use exactly "
                f"{CAMPAIGN_CPU_NOISE_WORKERS} CPU workers"
            )
    required = {
        "duration_seconds",
        "warmup_seconds",
        "period_us",
        "cpu_list",
        "policy",
        "bucket_us",
        "entries",
    }
    missing = required - set(value["timerlat"])
    if missing:
        raise ValueError("Timerlat configuration missing: " + ", ".join(sorted(missing)))
    return dict(value["timerlat"]), cases


def run_with_cleanup(
    campaign: CampaignCartesianProduct,
    benchmark: RealSenseTimerlatBench,
) -> None:
    try:
        campaign.run()
    finally:
        campaign_failed = sys.exc_info()[0] is not None
        try:
            benchmark.cleanup()
        except Exception as error:
            if not campaign_failed:
                raise
            print(f"[CLEANUP] {error}", file=sys.stderr)


def _select_cases(
    cases: List[Dict[str, Any]], selected: List[str] | None
) -> List[Dict[str, Any]]:
    if not selected:
        return cases
    wanted = set(selected)
    result = [case for case in cases if case["case_id"] in wanted]
    missing = wanted - {case["case_id"] for case in result}
    if missing:
        raise SystemExit("Unknown case_id(s): " + ", ".join(sorted(missing)))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-label", choices=KERNEL_LABELS, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--case", dest="case_ids", action="append")
    parser.add_argument("--serial", dest="serials", action="append", default=[])
    parser.add_argument("--nb-runs", type=int, default=CAMPAIGN_REPETITIONS)
    parser.add_argument(
        "--duration-seconds",
        type=int,
        help="Debug-only override; the formal matrix uses 300 seconds",
    )
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--rtla", default="rtla")
    parser.add_argument("--no-sudo", action="store_true")
    parser.add_argument(
        "--recover-on-failure",
        choices=("none", "full-reset"),
        default=DEFAULT_RECOVER_ON_FAILURE,
    )
    parser.add_argument(
        "--reset-before-run",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_RESET_BEFORE_RUN,
    )
    parser.add_argument(
        "--recovery-reset-timeout-ms",
        type=int,
        default=DEFAULT_RECOVERY_RESET_TIMEOUT_MS,
    )
    parser.add_argument(
        "--recovery-wait-seconds",
        type=float,
        default=DEFAULT_RECOVERY_WAIT_SECONDS,
    )
    parser.add_argument(
        "--recovery-settle-seconds",
        type=float,
        default=DEFAULT_RECOVERY_SETTLE_SECONDS,
    )
    parser.add_argument(
        "--max-attempts-per-run",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS_PER_RUN,
    )
    parser.add_argument(
        "--build-jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
    )
    args = parser.parse_args()
    if args.nb_runs < 1 or args.max_attempts_per_run < 1 or args.build_jobs < 1:
        raise SystemExit("run, attempt, and build-job counts must be positive")
    if args.duration_seconds is not None and args.duration_seconds < 1:
        raise SystemExit("--duration-seconds must be positive")
    if args.recovery_reset_timeout_ms < 1 or args.recovery_wait_seconds <= 0:
        raise SystemExit("recovery timeouts must be positive")
    if args.recovery_settle_seconds < 0:
        raise SystemExit("--recovery-settle-seconds must be non-negative")
    if args.max_attempts_per_run > 1 and args.recover_on_failure == "none":
        raise SystemExit("multiple attempts require --recover-on-failure full-reset")

    timerlat, cases = load_matrix(args.config)
    cases = _select_cases(cases, args.case_ids)
    if args.duration_seconds is not None:
        timerlat["duration_seconds"] = args.duration_seconds
    required_cameras = max(int(case["camera"]["count"]) for case in cases)
    if len(args.serials) < required_cameras:
        raise SystemExit(
            f"selected cases require {required_cameras} explicit --serial values; "
            f"received {len(args.serials)}"
        )

    args.results_dir.mkdir(parents=True, exist_ok=True)
    benchmark = RealSenseTimerlatBench(
        cases=cases,
        timerlat_config=timerlat,
        kernel_label=args.kernel_label,
        serials=args.serials,
        build_dir=args.build_dir,
        rtla=args.rtla,
        use_sudo=not args.no_sudo,
        recover_on_failure=args.recover_on_failure,
        reset_before_run=args.reset_before_run,
        recovery_reset_timeout_ms=args.recovery_reset_timeout_ms,
        recovery_wait_seconds=args.recovery_wait_seconds,
        recovery_settle_seconds=args.recovery_settle_seconds,
        max_attempts_per_run=args.max_attempts_per_run,
        build_jobs=args.build_jobs,
    )
    constants: Mapping[str, Any] = {
        "kernel_label": args.kernel_label,
        "timerlat_duration_seconds": timerlat["duration_seconds"],
        "timerlat_warmup_seconds": timerlat["warmup_seconds"],
        "timerlat_period_us": timerlat["period_us"],
        "timerlat_cpu_list": timerlat["cpu_list"],
        "timerlat_policy": timerlat["policy"],
        "cpu_frequency_mhz": 1500,
    }
    campaign = CampaignCartesianProduct(
        name="realsense_timerlat",
        benchmark=benchmark,
        nb_runs=args.nb_runs,
        variables={"load_case": [case["case_id"] for case in cases]},
        constants=dict(constants),
        debug=False,
        gdb=False,
        enable_data_dir=True,
        continuing=False,
        benchmark_duration_seconds=None,
        results_dir=str(args.results_dir),
    )
    run_with_cleanup(campaign, benchmark)


if __name__ == "__main__":
    main()
