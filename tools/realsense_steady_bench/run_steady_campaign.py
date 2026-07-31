#!/usr/bin/env python3
"""Benchkit campaign for RealSense steady-state frame acquisition."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List

_BOOTSTRAP_TOOL_DIR = Path(__file__).resolve().parent
_BOOTSTRAP_TOOLS_DIR = _BOOTSTRAP_TOOL_DIR.parent
sys.path.insert(0, str(_BOOTSTRAP_TOOLS_DIR))

from steady_settings import (
    BENCHKIT_PATH,
    CPU_NOISE_MODES,
    DEFAULT_BROADCOM_VULKAN_ICD,
    DEFAULT_BUILD_DIR,
    DEFAULT_LIME,
    DEFAULT_RESULTS_DIR,
    FIXED_CAMPAIGN_CONSTANTS,
    GPU_NOISE_MODES,
    MEMORY_NOISE_MODES,
    POLICY_NAMES,
    TOOL_DIR,
    TOOLS_DIR,
    USB_STORAGE_NOISE_MODES,
)

if not BENCHKIT_PATH.exists():
    raise SystemExit("deps/benchkit is missing; initialize repository submodules first.")
sys.path.insert(0, str(BENCHKIT_PATH))
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from benchkit.campaign import CampaignCartesianProduct  # noqa: E402
from steady_benchmark import RealSenseSteadyBench  # noqa: E402


def load_cases(path: Path) -> List[Dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    cases = value.get("cases", []) if isinstance(value, dict) else value
    if not isinstance(cases, list):
        raise ValueError(f"{path} does not contain a case list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not case.get("case_id"):
            raise ValueError(f"Case without case_id: {case!r}")
        if case["case_id"] in seen:
            raise ValueError(f"Duplicate case_id: {case['case_id']}")
        seen.add(case["case_id"])
    return cases


def run_with_cleanup(
    campaign: CampaignCartesianProduct,
    benchmark: RealSenseSteadyBench,
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=TOOL_DIR / "configs" / "smoke_matrix.json",
    )
    parser.add_argument("--case", dest="case_ids", action="append")
    parser.add_argument("--policies", nargs="+", choices=POLICY_NAMES, default=["other"])
    parser.add_argument("--nb-runs", type=int, default=1)
    parser.add_argument("--frames", type=int, help="Override measured frames for every case")
    parser.add_argument("--serial", dest="serials", action="append")
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--lime", type=Path, default=DEFAULT_LIME)
    parser.add_argument("--no-lime", action="store_true")
    parser.add_argument("--no-sudo", action="store_true")
    parser.add_argument(
        "--recover-on-failure",
        choices=("none", "full-reset"),
        default="full-reset",
        help=(
            "Recover failed camera startup before continuing (default: full-reset). "
            "A full reset covers every selected D435 composite USB device."
        ),
    )
    parser.add_argument(
        "--recovery-reset-timeout-ms",
        type=int,
        default=5000,
        help="Per-camera D435 firmware reset timeout (default: 5000 ms)",
    )
    parser.add_argument(
        "--recovery-wait-seconds",
        type=float,
        default=1.2,
        help="Per-camera USB re-enumeration timeout (default: 1.2 s)",
    )
    parser.add_argument(
        "--recovery-settle-seconds",
        type=float,
        default=0.0,
        help="Delay after resetting all cameras and before retry (default: 0 s)",
    )
    parser.add_argument(
        "--max-attempts-per-run",
        type=int,
        default=3,
        help="Maximum startup attempts for one logical Benchkit run (default: 3)",
    )
    parser.add_argument(
        "--cpu-noise-modes",
        nargs="+",
        choices=CPU_NOISE_MODES,
        default=["none"],
        help="Cartesian CPU-noise variable; busy_loop uses register-only arithmetic",
    )
    parser.add_argument(
        "--cpu-noise-workers",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="busy-loop worker count; defaults to the number of logical CPUs",
    )
    parser.add_argument("--cpu-noise-warmup-seconds", type=float, default=10.0)
    parser.add_argument("--cpu-noise-ready-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--cpu-noise-cpu-affinity",
        help="optional taskset CPU list inherited by every busy-loop worker",
    )
    parser.add_argument(
        "--memory-noise-modes",
        nargs="+",
        choices=MEMORY_NOISE_MODES,
        default=["none"],
        help="Cartesian memory-noise variable; fixed_copy streams between private buffers",
    )
    parser.add_argument(
        "--memory-noise-workers",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="fixed-copy worker count; defaults to the number of logical CPUs",
    )
    parser.add_argument(
        "--memory-noise-buffer-size-mib",
        type=int,
        default=64,
        help="size of each source and destination buffer per worker (default: 64 MiB)",
    )
    parser.add_argument("--memory-noise-warmup-seconds", type=float, default=10.0)
    parser.add_argument(
        "--memory-noise-ready-timeout-seconds", type=float, default=30.0
    )
    parser.add_argument(
        "--memory-noise-cpu-affinity",
        help="optional taskset CPU list inherited by every fixed-copy worker",
    )
    parser.add_argument(
        "--gpu-noise-modes",
        nargs="+",
        choices=GPU_NOISE_MODES,
        default=["none"],
        help="Cartesian GPU-noise variable; the only workload is pinned MobileNetV2+ncnn Vulkan",
    )
    parser.add_argument("--gpu-noise-device", type=int, default=0)
    parser.add_argument("--gpu-noise-warmup-iterations", type=int, default=10)
    parser.add_argument("--gpu-noise-ready-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--gpu-noise-cpu-affinity")
    parser.add_argument(
        "--usb-storage-noise-modes",
        nargs="+",
        choices=USB_STORAGE_NOISE_MODES,
        default=["none"],
        help="Cartesian USB storage noise variable; only read-only sequential I/O is supported",
    )
    parser.add_argument(
        "--usb-storage-device",
        type=Path,
        help="unmounted whole USB disk, preferably a stable /dev/disk/by-id path",
    )
    parser.add_argument("--usb-storage-warmup-seconds", type=float, default=10.0)
    parser.add_argument("--usb-storage-block-size-kib", type=int, default=1024)
    parser.add_argument(
        "--usb-storage-ready-timeout-seconds", type=float, default=30.0
    )
    parser.add_argument(
        "--gpu-noise-vulkan-icd",
        type=Path,
        help="Vulkan ICD JSON; auto-selects the Raspberry Pi Broadcom ICD when present",
    )
    parser.add_argument(
        "--build-jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
        help="Parallel build jobs; defaults to leaving one CPU free",
    )
    args = parser.parse_args()
    if args.build_jobs < 1:
        raise SystemExit("--build-jobs must be positive")
    if args.recovery_reset_timeout_ms < 1:
        raise SystemExit("--recovery-reset-timeout-ms must be positive")
    if args.recovery_wait_seconds <= 0:
        raise SystemExit("--recovery-wait-seconds must be positive")
    if args.recovery_settle_seconds < 0:
        raise SystemExit("--recovery-settle-seconds must be non-negative")
    if args.max_attempts_per_run < 1:
        raise SystemExit("--max-attempts-per-run must be positive")
    if args.max_attempts_per_run > 1 and args.recover_on_failure == "none":
        raise SystemExit("multiple attempts require --recover-on-failure")
    if args.cpu_noise_workers < 1:
        raise SystemExit("--cpu-noise-workers must be positive")
    if args.cpu_noise_warmup_seconds <= 0:
        raise SystemExit("--cpu-noise-warmup-seconds must be positive")
    if args.cpu_noise_ready_timeout_seconds <= args.cpu_noise_warmup_seconds:
        raise SystemExit(
            "--cpu-noise-ready-timeout-seconds must exceed the warm-up duration"
        )
    if args.memory_noise_workers < 1:
        raise SystemExit("--memory-noise-workers must be positive")
    if args.memory_noise_buffer_size_mib < 1:
        raise SystemExit("--memory-noise-buffer-size-mib must be positive")
    if args.memory_noise_warmup_seconds <= 0:
        raise SystemExit("--memory-noise-warmup-seconds must be positive")
    if args.memory_noise_ready_timeout_seconds <= args.memory_noise_warmup_seconds:
        raise SystemExit(
            "--memory-noise-ready-timeout-seconds must exceed the warm-up duration"
        )
    if args.gpu_noise_warmup_iterations < 1:
        raise SystemExit("--gpu-noise-warmup-iterations must be positive")
    if args.gpu_noise_ready_timeout_seconds <= 0:
        raise SystemExit("--gpu-noise-ready-timeout-seconds must be positive")
    if args.usb_storage_warmup_seconds <= 0:
        raise SystemExit("--usb-storage-warmup-seconds must be positive")
    if args.usb_storage_block_size_kib < 4:
        raise SystemExit("--usb-storage-block-size-kib must be at least 4")
    if args.usb_storage_ready_timeout_seconds <= args.usb_storage_warmup_seconds:
        raise SystemExit(
            "--usb-storage-ready-timeout-seconds must exceed the warm-up duration"
        )
    if (
        any(mode != "none" for mode in args.usb_storage_noise_modes)
        and args.usb_storage_device is None
    ):
        raise SystemExit(
            "--usb-storage-device is required for sequential-read USB noise"
        )
    if (
        args.gpu_noise_vulkan_icd is None
        and DEFAULT_BROADCOM_VULKAN_ICD.is_file()
        and any(mode != "none" for mode in args.gpu_noise_modes)
    ):
        args.gpu_noise_vulkan_icd = DEFAULT_BROADCOM_VULKAN_ICD

    cases = load_cases(args.config)
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case["case_id"] in wanted]
        missing = wanted - {case["case_id"] for case in cases}
        if missing:
            raise SystemExit("Unknown case_id(s): " + ", ".join(sorted(missing)))
    if args.frames is not None:
        for case in cases:
            case.setdefault("probe", {})["frames"] = args.frames
    if args.serials:
        for case in cases:
            case.setdefault("probe", {})["serials"] = args.serials
            case["probe"]["camera_count"] = len(args.serials)
            case.setdefault("physical", {})["camera_count"] = len(args.serials)
    if not cases:
        raise SystemExit("No cases selected")
    if args.recover_on_failure == "full-reset":
        for case in cases:
            probe = case.get("probe", {})
            serials = probe.get("serials", probe.get("serial", []))
            if isinstance(serials, str):
                serials = [serials] if serials else []
            camera_count = int(
                probe.get(
                    "camera_count",
                    case.get("physical", {}).get("camera_count", 1),
                )
            )
            if len(serials) != camera_count:
                raise SystemExit(
                    "full-reset recovery requires one explicit --serial per "
                    f"selected camera; case {case['case_id']!r} selects "
                    f"{camera_count} camera(s) but provides {len(serials)} serial(s)"
                )

    args.results_dir.mkdir(parents=True, exist_ok=True)
    benchmark = RealSenseSteadyBench(
        cases=cases,
        build_dir=args.build_dir,
        lime=args.lime,
        use_lime=not args.no_lime,
        use_sudo=not args.no_sudo,
        cpu_noise_modes=args.cpu_noise_modes,
        cpu_noise_workers=args.cpu_noise_workers,
        cpu_noise_warmup_seconds=args.cpu_noise_warmup_seconds,
        cpu_noise_ready_timeout_seconds=args.cpu_noise_ready_timeout_seconds,
        cpu_noise_cpu_affinity=args.cpu_noise_cpu_affinity,
        memory_noise_modes=args.memory_noise_modes,
        memory_noise_workers=args.memory_noise_workers,
        memory_noise_buffer_size_mib=args.memory_noise_buffer_size_mib,
        memory_noise_warmup_seconds=args.memory_noise_warmup_seconds,
        memory_noise_ready_timeout_seconds=args.memory_noise_ready_timeout_seconds,
        memory_noise_cpu_affinity=args.memory_noise_cpu_affinity,
        gpu_noise_modes=args.gpu_noise_modes,
        gpu_noise_device=args.gpu_noise_device,
        gpu_noise_warmup_iterations=args.gpu_noise_warmup_iterations,
        gpu_noise_ready_timeout_seconds=args.gpu_noise_ready_timeout_seconds,
        gpu_noise_cpu_affinity=args.gpu_noise_cpu_affinity,
        gpu_noise_vulkan_icd=args.gpu_noise_vulkan_icd,
        usb_storage_noise_modes=args.usb_storage_noise_modes,
        usb_storage_device=args.usb_storage_device,
        usb_storage_warmup_seconds=args.usb_storage_warmup_seconds,
        usb_storage_block_size_kib=args.usb_storage_block_size_kib,
        usb_storage_ready_timeout_seconds=args.usb_storage_ready_timeout_seconds,
        recover_on_failure=args.recover_on_failure,
        recovery_reset_timeout_ms=args.recovery_reset_timeout_ms,
        recovery_wait_seconds=args.recovery_wait_seconds,
        recovery_settle_seconds=args.recovery_settle_seconds,
        max_attempts_per_run=args.max_attempts_per_run,
        build_jobs=args.build_jobs,
    )
    campaign = CampaignCartesianProduct(
        name="realsense_steady",
        benchmark=benchmark,
        nb_runs=args.nb_runs,
        variables={
            "case_id": [case["case_id"] for case in cases],
            "policy": args.policies,
            "cpu_noise": args.cpu_noise_modes,
            "memory_noise": args.memory_noise_modes,
            "gpu_noise": args.gpu_noise_modes,
            "usb_storage_noise": args.usb_storage_noise_modes,
        },
        constants=FIXED_CAMPAIGN_CONSTANTS,
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
