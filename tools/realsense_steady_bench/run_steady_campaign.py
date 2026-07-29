#!/usr/bin/env python3
"""Benchkit campaign for RealSense steady-state frame acquisition."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "realsense_steady_bench"
BENCHKIT_PATH = REPO_ROOT / "deps" / "benchkit"
DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-steady"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"
DEFAULT_LIME = REPO_ROOT / "deps" / "lime-rtw" / "target" / "release" / "lime-rtw"
TRACER_SOURCE = REPO_ROOT / "tools" / "realsense_thread_trace" / "trace_pthreads.c"
CPU_LOCK = REPO_ROOT / "scripts" / "lock_cpu_freq.sh"
CPU_RESTORE = REPO_ROOT / "scripts" / "restore_cpu_freq_default.sh"
RSUSB_HELPER = REPO_ROOT / "scripts" / "realsense_rsusb_uvc.sh"

if not BENCHKIT_PATH.exists():
    raise SystemExit("deps/benchkit is missing; initialize repository submodules first.")
sys.path.insert(0, str(BENCHKIT_PATH))
sys.path.insert(0, str(TOOL_DIR))

from benchkit.benchmark import Benchmark  # noqa: E402
from benchkit.campaign import CampaignCartesianProduct  # noqa: E402
from parse_steady_trace import parse_steady_trace  # noqa: E402


POLICY_NAMES = {
    "other": "SCHED_OTHER",
    "rr": "SCHED_RR",
    "fifo": "SCHED_FIFO",
}


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


def flatten(prefix: str, value: Any, output: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            flatten(f"{prefix}_{key}" if prefix else str(key), child, output)
    elif isinstance(value, list):
        output[prefix] = json.dumps(value, sort_keys=True)
    else:
        output[prefix] = value


def interrupt_totals(path: Path) -> Dict[str, int]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return {
        f"{line.get('irq', '')}:{line.get('description', '')}": sum(
            int(count) for count in line.get("counts", [])
        )
        for line in value.get("parsed_interrupts", {}).get("lines", [])
    }


class RealSenseSteadyBench(Benchmark):
    def __init__(
        self,
        cases: Iterable[Dict[str, Any]],
        build_dir: Path,
        lime: Path,
        priority: int,
        use_lime: bool,
        use_sudo: bool,
        rsusb_backend: bool,
        rsusb_usb_devices: List[str],
        cpu_frequency_mhz: int | None,
    ) -> None:
        super().__init__(
            command_wrappers=(),
            command_attachments=(),
            shared_libs=(),
            pre_run_hooks=(),
            post_run_hooks=(),
        )
        self._cases = {case["case_id"]: case for case in cases}
        self._build_dir = build_dir.resolve()
        self._lime = lime.resolve()
        self._priority = priority
        self._use_lime = use_lime
        self._use_sudo = use_sudo
        self._rsusb_backend = rsusb_backend
        self._rsusb_usb_devices = rsusb_usb_devices
        self._cpu_frequency_mhz = cpu_frequency_mhz
        self._cpu_locked = False
        self._cpu_restore_needed = False
        self._rsusb_unbound = False
        self._probe = self._build_dir / "realsense_steady_probe"
        self._tracer = self._build_dir / "libtrace_pthreads.so"

    @property
    def bench_src_path(self) -> Path:
        return TOOL_DIR

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return ["case_id", "policy"]

    def _privileged(self, command: List[str]) -> List[str]:
        return ["sudo", "--non-interactive", *command] if self._use_sudo else command

    def prebuild_bench(self, **_kwargs: Any) -> int:
        if self._use_lime and not self._lime.is_file():
            raise RuntimeError(
                f"LiME was not found at {self._lime}. Build the unmodified dependency with: "
                "cargo build --release --manifest-path deps/lime-rtw/Cargo.toml"
            )
        if shutil.which("chrt") is None:
            raise RuntimeError("chrt is required (normally provided by util-linux).")
        if self._rsusb_backend and not self._rsusb_usb_devices:
            raise RuntimeError(
                "--rsusb-backend requires one --rsusb-usb-device per connected camera"
            )
        helpers = []
        if self._cpu_frequency_mhz is not None:
            helpers.extend((CPU_LOCK, CPU_RESTORE))
        if self._rsusb_backend:
            helpers.append(RSUSB_HELPER)
        for helper in helpers:
            if not helper.is_file():
                raise RuntimeError(f"Required helper is missing: {helper}")

        subprocess.check_call(
            [
                "cmake",
                "-S",
                str(REPO_ROOT),
                "-B",
                str(self._build_dir),
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                f"-DFORCE_RSUSB_BACKEND={'ON' if self._rsusb_backend else 'OFF'}",
            ]
        )
        subprocess.check_call(
            [
                "cmake",
                "--build",
                str(self._build_dir),
                "--target",
                "realsense_steady_probe",
                "-j4",
            ]
        )
        compiler = os.environ.get("CC", "cc")
        subprocess.check_call(
            [
                compiler,
                "-shared",
                "-fPIC",
                "-g",
                "-O2",
                "-fno-omit-frame-pointer",
                "-Wall",
                "-Wextra",
                "-o",
                str(self._tracer),
                str(TRACER_SOURCE),
                "-ldl",
                "-pthread",
            ]
        )
        return 0

    def build_bench(self, **_kwargs: Any) -> None:
        return None

    def _lock_cpu_once(self, record_dir: Path) -> None:
        if self._cpu_frequency_mhz is None or self._cpu_locked:
            return
        command = self._privileged([str(CPU_LOCK), str(self._cpu_frequency_mhz * 1000)])
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        (record_dir / "cpu_frequency_lock.txt").write_text(
            completed.stdout + completed.stderr,
            encoding="utf-8",
        )
        self._cpu_restore_needed = True
        if completed.returncode != 0:
            raise RuntimeError("Failed to lock CPU frequency; see cpu_frequency_lock.txt")
        self._cpu_locked = True
        print(f"[CPU-FREQ] locked at {self._cpu_frequency_mhz} MHz")

    def restore_cpu_frequency(self) -> None:
        if not self._cpu_restore_needed:
            return
        completed = subprocess.run(
            self._privileged([str(CPU_RESTORE)]),
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self._cpu_restore_needed = False
        self._cpu_locked = False
        if completed.returncode != 0:
            raise RuntimeError(
                "Failed to restore CPU frequency: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        print("[CPU-FREQ] restored dynamic frequency scaling")

    def _run_rsusb_helper(self, action: str) -> None:
        for device in self._rsusb_usb_devices:
            completed = subprocess.run(
                self._privileged([str(RSUSB_HELPER), action, device]),
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Failed to {action} UVC for {device}: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                )

    def _prepare_rsusb_once(self) -> None:
        if not self._rsusb_backend or self._rsusb_unbound:
            return
        self._run_rsusb_helper("unbind")
        self._rsusb_unbound = True
        time.sleep(0.25)
        print("[RSUSB] kernel UVC interfaces unbound for the campaign")

    def restore_v4l2_binding(self) -> None:
        if not self._rsusb_unbound:
            return
        self._run_rsusb_helper("bind")
        self._rsusb_unbound = False
        print("[RSUSB] kernel UVC interfaces rebound")

    def _snapshot(self, output: Path) -> None:
        subprocess.run(
            [
                sys.executable,
                str(TOOL_DIR / "snapshot_topology.py"),
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            check=False,
        )

    def _kernel_log(self) -> tuple[str | None, str]:
        completed = subprocess.run(
            self._privileged(["dmesg", "--color=never"]),
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return None, detail or f"dmesg exited with {completed.returncode}"
        return completed.stdout, ""

    @staticmethod
    def _kernel_delta(before: str, after: str) -> str:
        if after.startswith(before):
            return after[len(before):]
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        common = 0
        for old, new in zip(before_lines, after_lines):
            if old != new:
                break
            common += 1
        return "\n".join(after_lines[common:]) + ("\n" if common < len(after_lines) else "")

    def _scheduled_probe(
        self,
        case: Dict[str, Any],
        policy: str,
        summary_path: Path,
        events_path: Path,
    ) -> List[str]:
        if policy == "other":
            command = ["chrt", "--other", "0"]
        elif policy == "rr":
            command = ["chrt", "--rr", str(self._priority)]
        elif policy == "fifo":
            command = ["chrt", "--fifo", str(self._priority)]
        else:
            raise ValueError(f"Unsupported policy: {policy}")

        probe = case.get("probe", {})
        command.append(str(self._probe))
        serials = probe.get("serials", probe.get("serial", []))
        if isinstance(serials, str):
            serials = [serials] if serials else []
        for serial in serials:
            command += ["--serial", str(serial)]
        camera_count = probe.get(
            "camera_count",
            case.get("physical", {}).get("camera_count", 1),
        )
        command += ["--camera-count", str(camera_count)]
        fields = [
            ("stream_mode", "--stream-mode"),
            ("delivery", "--delivery"),
            ("frames", "--frames"),
            ("warmup_frames", "--warmup-frames"),
            ("frame_timeout_ms", "--frame-timeout-ms"),
            ("startup_timeout_ms", "--startup-timeout-ms"),
            ("measurement_timeout_ms", "--measurement-timeout-ms"),
            ("fps", "--fps"),
            ("depth_width", "--depth-width"),
            ("depth_height", "--depth-height"),
            ("color_width", "--color-width"),
            ("color_height", "--color-height"),
        ]
        for key, flag in fields:
            if key in probe and probe[key] is not None:
                command += [flag, str(probe[key])]
        command += [
            "--summary-output",
            str(summary_path),
            "--events-output",
            str(events_path),
        ]
        return command

    def single_run(
        self,
        case_id: str,
        policy: str,
        record_data_dir: Path,
        **kwargs: Any,
    ) -> str:
        case = self._cases[case_id]
        record_dir = Path(record_data_dir).resolve()
        record_dir.mkdir(parents=True, exist_ok=True)
        self._lock_cpu_once(record_dir)
        self._prepare_rsusb_once()

        summary_path = record_dir / "steady_summary.json"
        events_path = record_dir / "frame_events.csv"
        lifecycle_path = record_dir / "thread_lifecycle.jsonl"
        lime_dir = record_dir / "lime_trace"
        stdout_path = record_dir / "probe_stdout.txt"
        before = record_dir / "topology_before.json"
        after = record_dir / "topology_after.json"
        (record_dir / "case.json").write_text(
            json.dumps(case, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._snapshot(before)

        target = [
            "env",
            f"LD_PRELOAD={self._tracer}",
            f"RS_THREAD_TRACE_FILE={lifecycle_path}",
            *self._scheduled_probe(case, policy, summary_path, events_path),
        ]
        command = target
        if self._use_lime:
            command = [
                str(self._lime),
                "trace",
                "--best-effort",
                "-o",
                str(lime_dir),
                "--",
                *target,
            ]
        if self._use_sudo:
            command = ["sudo", "--preserve-env=LD_LIBRARY_PATH", *command]

        manifest = {
            "schema_version": 1,
            "case_id": case_id,
            "policy_requested": POLICY_NAMES[policy],
            "priority_requested": 0 if policy == "other" else self._priority,
            "backend": "RSUSB" if self._rsusb_backend else "V4L2",
            "lime_enabled": self._use_lime,
            "cpu_frequency_mhz": self._cpu_frequency_mhz,
            "command": command,
            "clock": "CLOCK_BOOTTIME",
        }
        (record_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        environment = self._preload_env(**kwargs)
        wrapped_command, wrapped_environment = self._wrap_command(
            run_command=command,
            environment=environment,
            **kwargs,
        )
        probe = case.get("probe", {})
        frames = int(probe.get("frames", 10000))
        fps = int(probe.get("fps", 30))
        automatic_seconds = frames / max(1, fps) * 2 + 90
        measurement_timeout = int(probe.get("measurement_timeout_ms", 0)) / 1000
        timeout = max(int(automatic_seconds), int(measurement_timeout + 60))
        kernel_before, kernel_error = self._kernel_log()
        try:
            output = self.run_bench_command(
                run_command=command,
                wrapped_run_command=wrapped_command,
                current_dir=REPO_ROOT,
                environment=environment,
                wrapped_environment=wrapped_environment,
                print_output=False,
                timeout=timeout,
                ignore_ret_codes=(1, 2, 3),
            )
            stdout_path.write_text(output, encoding="utf-8")
        finally:
            kernel_after, after_error = self._kernel_log()
            kernel_error = kernel_error or after_error
            if kernel_before is not None and kernel_after is not None:
                (record_dir / "kernel_log.txt").write_text(
                    self._kernel_delta(kernel_before, kernel_after),
                    encoding="utf-8",
                )
            elif kernel_error:
                (record_dir / "kernel_log_capture_error.txt").write_text(
                    kernel_error + "\n",
                    encoding="utf-8",
                )
            self._snapshot(after)

        if self._use_lime and summary_path.is_file():
            try:
                parse_steady_trace(lifecycle_path, lime_dir, record_dir)
            except Exception as error:
                (record_dir / "trace_parse_error.txt").write_text(
                    f"{type(error).__name__}: {error}\n",
                    encoding="utf-8",
                )
                data = json.loads(summary_path.read_text(encoding="utf-8"))
                if data.get("success"):
                    raise
        return output

    def parse_output_to_results(
        self,
        command_output: str,
        build_variables: Dict[str, Any],
        run_variables: Dict[str, Any],
        benchmark_duration_seconds: int,
        record_data_dir: Path,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        del command_output, build_variables, benchmark_duration_seconds
        record_dir = Path(record_data_dir).resolve()
        case = self._cases[run_variables["case_id"]]
        path = record_dir / "steady_summary.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        aggregate = data.get("aggregate", {})
        result: Dict[str, Any] = {
            "success": data.get("success", False),
            "error": data.get("error", "summary file missing"),
            "backend": "RSUSB" if self._rsusb_backend else "V4L2",
            "policy_requested": POLICY_NAMES[run_variables["policy"]],
            "camera_count": data.get("run", {}).get("camera_count", 0),
            "deliveries": aggregate.get("deliveries", 0),
            "frames": aggregate.get("frames", 0),
            "drops": aggregate.get("drops", 0),
            "timeouts": aggregate.get("timeouts", 0),
            "measurement_duration_ms": data.get("measurement", {}).get("duration_ms", 0),
            "record_data_dir": str(record_dir),
        }
        flatten("workload", case.get("workload", {}), result)
        flatten("physical", case.get("physical", {}), result)
        flatten("delivery_interarrival_ms", aggregate.get("delivery_interarrival_ms", {}), result)
        flatten("wait_ms", aggregate.get("wait_ms", {}), result)
        for camera in data.get("cameras", []):
            index = camera.get("index", 0)
            prefix = f"camera_{index}"
            for key in (
                "serial",
                "usb_type",
                "start_call_ms",
                "stop_call_ms",
                "deliveries",
                "frames",
                "drops",
                "timeouts",
            ):
                result[f"{prefix}_{key}"] = camera.get(key, "")
            flatten(
                f"{prefix}_interarrival_ms",
                camera.get("delivery_interarrival_ms", {}),
                result,
            )

        thread_path = record_dir / "thread_steady_summary.json"
        if thread_path.is_file():
            thread_data = json.loads(thread_path.read_text(encoding="utf-8"))
            result["traced_thread_count"] = thread_data.get("thread_count", 0)
            result["traced_activation_count"] = thread_data.get("activation_count", 0)
        else:
            result["traced_thread_count"] = 0
            result["traced_activation_count"] = 0

        kernel_path = record_dir / "kernel_log.txt"
        kernel_text = (
            kernel_path.read_text(encoding="utf-8", errors="replace")
            if kernel_path.is_file()
            else ""
        )
        uvc_matches = re.findall(
            r"uvcvideo\s+(\S+):\s+Failed to resubmit video URB\s+\((-?\d+)\)",
            kernel_text,
        )
        result["kernel_log_captured"] = kernel_path.is_file()
        result["uvc_resubmit_errors"] = len(uvc_matches)
        result["uvc_resubmit_interfaces"] = ",".join(
            sorted({match[0] for match in uvc_matches})
        )
        result["uvc_resubmit_error_codes"] = ",".join(
            sorted({match[1] for match in uvc_matches})
        )

        before_totals = interrupt_totals(record_dir / "topology_before.json")
        after_totals = interrupt_totals(record_dir / "topology_after.json")
        result["irq_delta_json"] = json.dumps(
            {
                key: after_totals.get(key, 0) - before_totals.get(key, 0)
                for key in sorted(set(before_totals) | set(after_totals))
            },
            sort_keys=True,
        )
        return result


def run_with_cleanup(
    campaign: CampaignCartesianProduct,
    benchmark: RealSenseSteadyBench,
) -> None:
    try:
        campaign.run()
    finally:
        errors = []
        for description, cleanup in (
            ("restore V4L2 binding", benchmark.restore_v4l2_binding),
            ("restore CPU frequency", benchmark.restore_cpu_frequency),
        ):
            try:
                cleanup()
            except Exception as error:
                errors.append(f"{description}: {error}")
        if errors and sys.exc_info()[0] is None:
            raise RuntimeError("Campaign cleanup failed: " + " | ".join(errors))
        for error in errors:
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
    parser.add_argument("--priority", type=int, default=80)
    parser.add_argument("--nb-runs", type=int, default=1)
    parser.add_argument("--frames", type=int, help="Override measured frames for every case")
    parser.add_argument("--serial", dest="serials", action="append")
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--lime", type=Path, default=DEFAULT_LIME)
    parser.add_argument("--no-lime", action="store_true")
    parser.add_argument("--no-sudo", action="store_true")
    parser.add_argument("--rsusb-backend", action="store_true")
    parser.add_argument("--rsusb-usb-device", action="append", default=[])
    parser.add_argument(
        "--cpu-frequency-mhz",
        type=int,
        default=1500,
        help="Lock once before the first run; use 0 to disable",
    )
    args = parser.parse_args()

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

    args.results_dir.mkdir(parents=True, exist_ok=True)
    benchmark = RealSenseSteadyBench(
        cases=cases,
        build_dir=args.build_dir,
        lime=args.lime,
        priority=args.priority,
        use_lime=not args.no_lime,
        use_sudo=not args.no_sudo,
        rsusb_backend=args.rsusb_backend,
        rsusb_usb_devices=args.rsusb_usb_device,
        cpu_frequency_mhz=args.cpu_frequency_mhz or None,
    )
    campaign = CampaignCartesianProduct(
        name="realsense_steady",
        benchmark=benchmark,
        nb_runs=args.nb_runs,
        variables={
            "case_id": [case["case_id"] for case in cases],
            "policy": args.policies,
        },
        constants=None,
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
