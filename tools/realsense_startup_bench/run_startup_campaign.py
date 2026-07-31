#!/usr/bin/env python3
"""Benchkit campaign for repeated RealSense startup under Linux scheduling policies."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "realsense_startup_bench"
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.settings import (  # noqa: E402
    BENCHKIT_PATH,
    CPU_LOCK_SCRIPT as CPU_FREQ_LOCK_SCRIPT,
    CPU_RESTORE_SCRIPT as CPU_FREQ_RESTORE_SCRIPT,
    DEFAULT_LIME,
    PAPER_BACKEND as CAMPAIGN_BACKEND,
    PAPER_CPU_FREQUENCY_MHZ as CAMPAIGN_CPU_FREQUENCY_MHZ,
    PAPER_DROP_CACHES_BEFORE_RUN as CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    PAPER_RT_PRIORITY as CAMPAIGN_RT_PRIORITY,
    PAPER_USB_KERNEL_DRIVER as CAMPAIGN_USB_KERNEL_DRIVER,
    POLICY_NAMES,
)

DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-startup"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"
CAMPAIGN_CYCLE_DELAY_MS = 0
CAMPAIGN_RSUSB_USB_DEVICE = ""
CAMPAIGN_RSUSB_PREPARE_TIMEOUT_SECONDS = 10.0
CAMPAIGN_RSUSB_UNBIND_SETTLE_SECONDS = 1.0
FIXED_CAMPAIGN_CONSTANTS = {
    "fixed_librealsense_backend": CAMPAIGN_BACKEND,
    "fixed_usb_kernel_driver": CAMPAIGN_USB_KERNEL_DRIVER,
    "fixed_cpu_frequency_mhz": CAMPAIGN_CPU_FREQUENCY_MHZ,
    "fixed_drop_caches_before_run": CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    "fixed_rt_priority": CAMPAIGN_RT_PRIORITY,
    "fixed_cycle_delay_ms": CAMPAIGN_CYCLE_DELAY_MS,
}

if not BENCHKIT_PATH.exists():
    raise SystemExit("deps/benchkit is missing; initialize the repository submodules first.")
sys.path.insert(0, str(BENCHKIT_PATH))
sys.path.insert(0, str(TOOL_DIR))
_BENCHKIT_IMPORT_ERROR = None
try:
    from benchkit.benchmark import Benchmark
    from benchkit.campaign import CampaignCartesianProduct
except ModuleNotFoundError as error:
    _BENCHKIT_IMPORT_ERROR = error
    Benchmark = object
    CampaignCartesianProduct = None
from parse_startup_trace import parse_startup_trace
from realsense_bench_common.memory import (  # noqa: E402
    DropCachesBeforeRun,
    memory_cleanup_result_fields,
)
from realsense_bench_common.attempts import (  # noqa: E402
    AttemptDecision,
    run_attempt_loop,
)
from realsense_bench_common.commands import (  # noqa: E402
    build_pthread_tracer,
    scheduler_prefix,
    traced_command,
    validate_trace_environment,
)
from realsense_bench_common.recovery import (  # noqa: E402
    CameraRecoveryConfig,
    MultiCameraFullReset,
)
from realsense_bench_common.system_controls import (  # noqa: E402
    SystemControlConfig,
    SystemControls,
)
from realsense_bench_common.results import (  # noqa: E402
    common_attempt_result_fields,
)


class RealSenseStartupBench(Benchmark):
    def __init__(
        self,
        build_dir: Path,
        lime: Path,
        cycles: int,
        frames: int,
        serial: str,
        frame_timeout_ms: int,
        join_timeout_ms: int,
        process_timeout_seconds: int,
        recovery_reset_timeout_ms: int,
        recover_on_failure: str,
        recovery_wait_seconds: float,
        recovery_settle_seconds: float,
        max_attempts_per_run: int,
        use_sudo: bool,
    ) -> None:
        memory_cleanup_hook = DropCachesBeforeRun(use_sudo=use_sudo)
        super().__init__(
            command_wrappers=(),
            command_attachments=(),
            shared_libs=(),
            pre_run_hooks=(
                (memory_cleanup_hook,)
                if CAMPAIGN_DROP_CACHES_BEFORE_RUN
                else ()
            ),
            post_run_hooks=(),
        )
        self._build_dir = build_dir.resolve()
        self._lime = lime.resolve()
        self._cycles = cycles
        self._frames = frames
        self._priority = CAMPAIGN_RT_PRIORITY
        self._serial = serial
        self._frame_timeout_ms = frame_timeout_ms
        self._join_timeout_ms = join_timeout_ms
        self._process_timeout_seconds = process_timeout_seconds
        self._recovery_reset_timeout_ms = recovery_reset_timeout_ms
        self._recover_on_failure = recover_on_failure
        self._recovery_wait_seconds = recovery_wait_seconds
        self._recovery_settle_seconds = recovery_settle_seconds
        self._max_attempts_per_run = max_attempts_per_run
        self._use_sudo = use_sudo
        self._rsusb_backend = CAMPAIGN_BACKEND == "rsusb"
        self._rsusb_usb_device = CAMPAIGN_RSUSB_USB_DEVICE
        self._rsusb_prepare_timeout_seconds = CAMPAIGN_RSUSB_PREPARE_TIMEOUT_SECONDS
        self._rsusb_unbind_settle_seconds = CAMPAIGN_RSUSB_UNBIND_SETTLE_SECONDS
        self._cpu_frequency_mhz = CAMPAIGN_CPU_FREQUENCY_MHZ
        self._drop_caches_before_run = CAMPAIGN_DROP_CACHES_BEFORE_RUN
        self._memory_cleanup_hook = memory_cleanup_hook
        self._probe = self._build_dir / "d435_sensor_probe"
        self._tracer = self._build_dir / "libtrace_pthreads.so"
        self._system_controls = SystemControls(
            SystemControlConfig(
                repo_root=REPO_ROOT,
                tool_dir=TOOL_DIR,
                use_sudo=use_sudo,
                cpu_frequency_mhz=self._cpu_frequency_mhz,
                cpu_lock_script=CPU_FREQ_LOCK_SCRIPT,
                cpu_restore_script=CPU_FREQ_RESTORE_SCRIPT,
                rsusb_backend=self._rsusb_backend,
                rsusb_usb_devices=(
                    (self._rsusb_usb_device,)
                    if self._rsusb_usb_device
                    else ()
                ),
                rsusb_helper=(
                    REPO_ROOT / "scripts" / "realsense_rsusb_uvc.sh"
                ),
                rsusb_prepare_each_attempt=True,
                rsusb_prepare_timeout_seconds=(
                    self._rsusb_prepare_timeout_seconds
                ),
                rsusb_unbind_settle_seconds=(
                    self._rsusb_unbind_settle_seconds
                ),
            )
        )

    @property
    def bench_src_path(self) -> Path:
        return TOOL_DIR

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return ["policy"]

    def prebuild_bench(self, **_kwargs: Any) -> int:
        validate_trace_environment(lime=self._lime, use_lime=True)
        if self._drop_caches_before_run:
            self._memory_cleanup_hook.validate()
        if (
            self._recover_on_failure in ("usb", "full-reset")
            and shutil.which("usbreset") is None
        ):
            raise RuntimeError(
                "usbreset is required for --recover-on-failure usb/full-reset."
            )
        if self._recover_on_failure == "depth-prime" and shutil.which("v4l2-ctl") is None:
            raise RuntimeError("v4l2-ctl is required for --recover-on-failure depth-prime.")
        self._system_controls.validate_environment()

        subprocess.check_call([
            "cmake", "-S", str(REPO_ROOT), "-B", str(self._build_dir),
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            f"-DFORCE_RSUSB_BACKEND={'ON' if self._rsusb_backend else 'OFF'}",
        ])
        subprocess.check_call([
            "cmake", "--build", str(self._build_dir),
            "--target", "d435_sensor_probe", "-j4",
        ])
        build_pthread_tracer(
            output=self._tracer,
            source=(
                REPO_ROOT
                / "tools"
                / "realsense_thread_trace"
                / "trace_pthreads.c"
            ),
        )
        return 0

    def build_bench(self, **_kwargs: Any) -> None:
        return None

    def _scheduled_probe(self, policy: str, cycle_delay_ms: int) -> List[str]:
        scheduled = scheduler_prefix(policy, self._priority)

        probe = [
            str(self._probe),
            "--cycles", str(self._cycles),
            "--frames", str(self._frames),
            "--frame-timeout-ms", str(self._frame_timeout_ms),
            "--join-timeout-ms", str(self._join_timeout_ms),
            "--cycle-delay-ms", str(cycle_delay_ms),
            "--strict-streams",
        ]
        if self._serial:
            probe += ["--serial", self._serial]
        return scheduled + probe

    def restore_v4l2_binding(self) -> None:
        self._system_controls.restore_v4l2_binding()

    def restore_cpu_frequency(self) -> None:
        self._system_controls.restore_cpu_frequency()

    @staticmethod
    def _usb_device_from_physical_port(physical_port: str) -> Path | None:
        if not physical_port.startswith("/sys/"):
            return None
        path = Path(physical_port).resolve()
        for candidate in (path, *path.parents):
            if (candidate / "busnum").is_file() and (candidate / "devnum").is_file():
                return candidate
        return None

    def _probe_can_find_device(
        self, timeout_seconds: float = 10.0
    ) -> tuple[bool, str]:
        command = [str(self._probe), "--list-only"]
        if self._serial:
            command += ["--serial", self._serial]
        try:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "device enumeration probe timed out"
        output = completed.stdout + completed.stderr
        return completed.returncode == 0, output.strip()

    @staticmethod
    def _depth_video_node(physical_port: str) -> Path | None:
        name = Path(physical_port).name
        if not name.startswith("video") or not name[5:].isdigit():
            return None
        return Path("/dev") / name

    def _recover_depth_prime(
        self, device: Dict[str, Any], record_dir: Path
    ) -> Dict[str, Any]:
        physical_port = str(device.get("physical_port", ""))
        result: Dict[str, Any] = {
            "attempted": True,
            "method": "depth-prime",
            "success": False,
            "physical_port": physical_port,
            "expected_buffers": 10,
        }
        video_node = self._depth_video_node(physical_port)
        if video_node is None or not video_node.exists():
            result["error"] = "could not resolve the selected camera depth video node"
        else:
            command = [
                "v4l2-ctl",
                "--device", str(video_node),
                "--set-fmt-video=width=848,height=480,pixelformat=0x2036315a",
                "--set-parm=30",
                "--stream-mmap=4",
                "--stream-count=10",
                "--stream-to=/dev/null",
                "--verbose",
            ]
            result["video_node"] = str(video_node)
            result["command"] = command
            try:
                completed = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                output = completed.stdout + completed.stderr
                captured_buffers = output.count("cap dqbuf:")
                result["returncode"] = completed.returncode
                result["captured_buffers"] = captured_buffers
                result["output"] = output.strip()
                result["success"] = completed.returncode == 0 and captured_buffers >= 10
                if not result["success"]:
                    result["error"] = "depth priming did not capture 10 buffers"
            except (OSError, subprocess.TimeoutExpired) as error:
                result["error"] = str(error)
        (record_dir / "recovery.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result

    def _recover_usb_device(
        self, device: Dict[str, Any], record_dir: Path
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "attempted": True,
            "method": "usb",
            "success": False,
            "physical_port": str(device.get("physical_port", "")),
        }
        usb_device = self._usb_device_from_physical_port(result["physical_port"])
        if usb_device is None:
            result["error"] = "could not resolve the selected camera to a USB device"
        else:
            try:
                bus = int((usb_device / "busnum").read_text(encoding="utf-8").strip())
                device_number = int(
                    (usb_device / "devnum").read_text(encoding="utf-8").strip()
                )
                target = f"{bus:03d}/{device_number:03d}"
                command = ["usbreset", target]
                if self._use_sudo:
                    command = ["sudo", "--non-interactive", *command]
                result["usb_device"] = str(usb_device)
                result["target"] = target
                result["command"] = command
                completed = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                result["reset_returncode"] = completed.returncode
                result["reset_output"] = (completed.stdout + completed.stderr).strip()
                if completed.returncode != 0:
                    result["error"] = "usbreset failed"
                else:
                    deadline = time.monotonic() + self._recovery_wait_seconds
                    last_probe_output = ""
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        visible, last_probe_output = self._probe_can_find_device(
                            timeout_seconds=max(0.05, min(2.0, remaining))
                        )
                        if visible:
                            result["success"] = True
                            break
                        remaining = deadline - time.monotonic()
                        if remaining > 0:
                            time.sleep(min(0.25, remaining))
                    result["enumeration_output"] = last_probe_output
                    if not result["success"]:
                        result["error"] = "camera did not reappear before recovery timeout"
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                result["error"] = str(error)
        (record_dir / "recovery.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result

    def _recover_full_reset(
        self, device: Dict[str, Any], record_dir: Path
    ) -> Dict[str, Any]:
        descriptor = dict(device)
        if not descriptor.get("serial"):
            descriptor["serial"] = self._serial
        recovery = MultiCameraFullReset(
            CameraRecoveryConfig(
                repo_root=REPO_ROOT,
                reset_probe=self._probe,
                use_sudo=self._use_sudo,
                reset_timeout_ms=self._recovery_reset_timeout_ms,
                enumeration_timeout_seconds=self._recovery_wait_seconds,
            )
        )
        return recovery.recover([descriptor], record_dir)

    def _run_attempt(
        self,
        policy: str,
        cycle_delay_ms: int,
        attempt: int,
        attempt_dir: Path,
        base_manifest: Dict[str, Any],
        kwargs: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        attempt_dir.mkdir(parents=True, exist_ok=False)
        lifecycle_path = attempt_dir / "thread_lifecycle.jsonl"
        stdout_path = attempt_dir / "probe_stdout.txt"
        lime_dir = attempt_dir / "lime_trace"

        command = traced_command(
            scheduled_command=self._scheduled_probe(policy, cycle_delay_ms),
            tracer=self._tracer,
            lifecycle_path=lifecycle_path,
            lime=self._lime,
            lime_dir=lime_dir,
            use_lime=True,
            use_sudo=self._use_sudo,
        )

        attempt_manifest = dict(base_manifest)
        attempt_manifest.update({
            "attempt": attempt,
            "command": command,
            "record_data_dir": str(attempt_dir),
        })
        (attempt_dir / "run_manifest.json").write_text(
            json.dumps(attempt_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        environment = self._preload_env(**kwargs)
        wrapped_command, wrapped_environment = self._wrap_command(
            run_command=command,
            environment=environment,
            **kwargs,
        )
        delay_budget_seconds = (max(0, self._cycles - 1) * cycle_delay_ms + 999) // 1000
        frame_timeout_seconds = (self._frame_timeout_ms + 999) // 1000
        join_timeout_seconds = (self._join_timeout_ms + 999) // 1000
        timeout = max(
            self._process_timeout_seconds,
            self._cycles * (join_timeout_seconds + frame_timeout_seconds + 5)
            + delay_budget_seconds,
        )
        kernel_before, kernel_error = self._system_controls.kernel_log()
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
        finally:
            self._system_controls.capture_kernel_delta(
                attempt_dir,
                kernel_before,
                kernel_error,
            )

        stdout_path.write_text(output, encoding="utf-8")
        summary = parse_startup_trace(
            lifecycle_path=lifecycle_path,
            lime_dir=lime_dir,
            output_dir=attempt_dir,
            stdout_path=stdout_path,
        )
        return output, summary

    def single_run(
        self,
        policy: str,
        record_data_dir: Path,
        **kwargs: Any,
    ) -> str:
        cycle_delay_ms = CAMPAIGN_CYCLE_DELAY_MS
        record_dir = Path(record_data_dir).resolve()
        record_dir.mkdir(parents=True, exist_ok=True)
        if any(record_dir.glob("attempt-*")):
            raise RuntimeError(f"Attempt directories already exist in {record_dir}")

        cpu_frequency_before_run = self._system_controls.prepare_campaign(
            record_dir
        )
        (record_dir / "cpu_frequency_before_run.json").write_text(
            json.dumps(cpu_frequency_before_run, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        base_manifest: Dict[str, Any] = {
            "schema_version": 5,
            "policy_requested": POLICY_NAMES[policy],
            "priority_requested": 0 if policy == "other" else self._priority,
            "librealsense_backend": CAMPAIGN_BACKEND,
            "usb_kernel_driver": CAMPAIGN_USB_KERNEL_DRIVER,
            "cycles": self._cycles,
            "frames_per_cycle": self._frames,
            "frame_timeout_ms": self._frame_timeout_ms,
            "serial": self._serial,
            "join_timeout_ms": self._join_timeout_ms,
            "process_timeout_seconds": self._process_timeout_seconds,
            "recovery_reset_timeout_ms": self._recovery_reset_timeout_ms,
            "cycle_delay_ms": cycle_delay_ms,
            "recover_on_failure": self._recover_on_failure,
            "recovery_wait_seconds": self._recovery_wait_seconds,
            "recovery_settle_seconds": self._recovery_settle_seconds,
            "max_attempts_per_run": self._max_attempts_per_run,
            "rsusb_usb_device": self._rsusb_usb_device,
            "rsusb_prepare_timeout_seconds": self._rsusb_prepare_timeout_seconds,
            "rsusb_unbind_settle_seconds": self._rsusb_unbind_settle_seconds,
            "cpu_frequency": cpu_frequency_before_run,
            "drop_caches_before_run": getattr(
                self, "_drop_caches_before_run", False
            ),
            "probe": str(self._probe),
            "lime": str(self._lime),
            "clock": "CLOCK_BOOTTIME",
        }
        (record_dir / "run_manifest.json").write_text(
            json.dumps(base_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        def run_attempt(
            attempt: int, attempt_dir: Path
        ) -> tuple[str, Dict[str, Any]]:
            return self._run_attempt(
                policy=policy,
                cycle_delay_ms=cycle_delay_ms,
                attempt=attempt,
                attempt_dir=attempt_dir,
                base_manifest=base_manifest,
                kwargs=kwargs,
            )

        def classify_attempt(summary: Dict[str, Any]) -> AttemptDecision:
            startup_result = summary.get("startup_result", {})
            startup_error = summary.get("startup_error", {})
            success = bool(startup_result.get("success", False))
            error_message = str(startup_error.get("message", ""))
            return AttemptDecision(
                success=success,
                failure_phase="none" if success else "startup",
                retry=not success,
                error=error_message,
                metadata={
                    "cycles_started": summary.get("cycles_started", 0),
                    "cycles_completed": startup_result.get(
                        "completed_cycles", 0
                    ),
                    "error_kind": startup_error.get("kind", ""),
                    "error_message": error_message,
                },
            )

        def recover_attempt(
            summary: Dict[str, Any],
            attempt_dir: Path,
            _decision: AttemptDecision,
        ) -> Dict[str, Any]:
            device = summary.get("device", {})
            if self._recover_on_failure == "usb":
                return self._recover_usb_device(device, attempt_dir)
            if self._recover_on_failure == "depth-prime":
                return self._recover_depth_prime(device, attempt_dir)
            if self._recover_on_failure == "full-reset":
                return self._recover_full_reset(device, attempt_dir)
            return {
                "attempted": False,
                "method": self._recover_on_failure,
                "success": False,
                "error": "unsupported recovery method",
            }

        attempt_result = run_attempt_loop(
            record_dir=record_dir,
            max_attempts=self._max_attempts_per_run,
            recovery_method=self._recover_on_failure,
            recovery_settle_seconds=self._recovery_settle_seconds,
            run_attempt=run_attempt,
            classify_attempt=classify_attempt,
            recover_attempt=(
                recover_attempt
                if self._recover_on_failure != "none"
                else None
            ),
            before_attempt=self._system_controls.prepare_attempt,
        )
        attempt_records = attempt_result.attempts
        final_output = attempt_result.output
        final_summary = attempt_result.summary

        cpu_frequency_after_run = (
            self._system_controls.cpu_frequency_state()
            if self._cpu_frequency_mhz is not None
            else {"policies": []}
        )
        frequency_errors = self._system_controls.verify_cpu_frequency(
            cpu_frequency_after_run
        )
        cpu_frequency_after_run.update({
            "enabled": self._cpu_frequency_mhz is not None,
            "requested_mhz": self._cpu_frequency_mhz,
            "maintained": self._cpu_frequency_mhz is not None and not frequency_errors,
            "verification_errors": frequency_errors,
        })
        final_summary["cpu_frequency"] = {
            "before_run": cpu_frequency_before_run,
            "after_run": cpu_frequency_after_run,
        }
        (record_dir / "cpu_frequency_after_run.json").write_text(
            json.dumps(cpu_frequency_after_run, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (record_dir / "attempts.json").write_text(
            json.dumps(attempt_records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (record_dir / "summary.json").write_text(
            json.dumps(final_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (record_dir / "probe_stdout.txt").write_text(final_output, encoding="utf-8")
        (record_dir / "selected_attempt.txt").write_text(
            str(attempt_result.selected_attempt) + "\n", encoding="utf-8"
        )
        return final_output

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
        record_dir = Path(record_data_dir)
        summary = json.loads((record_dir / "summary.json").read_text(encoding="utf-8"))
        scheduler = summary.get("scheduler", {})
        startup_result = summary.get("startup_result", {})
        cpu_frequency = summary.get("cpu_frequency", {})
        frequency_before = cpu_frequency.get("before_run", {})
        frequency_after = cpu_frequency.get("after_run", {})
        frequency_policies = frequency_before.get("policies", [])
        memory_cleanup = memory_cleanup_result_fields(
            record_dir,
            configured=getattr(self, "_drop_caches_before_run", False),
        )
        requested = POLICY_NAMES[run_variables["policy"]]
        effective = scheduler.get("policy", "")
        return {
            **memory_cleanup,
            **common_attempt_result_fields(summary),
            "librealsense_backend": "rsusb" if self._rsusb_backend else "v4l2",
            "cpu_frequency_requested_mhz": frequency_before.get("requested_mhz", ""),
            "cpu_frequency_locked": frequency_before.get("locked", False),
            "cpu_frequency_maintained": frequency_after.get("maintained", False),
            "cpu_frequency_policy_count": len(frequency_policies),
            "cpu_frequency_governors": ",".join(
                str(item.get("governor", "")) for item in frequency_policies
            ),
            "cpu_frequency_min_khz": ",".join(
                str(item.get("scaling_min_khz", "")) for item in frequency_policies
            ),
            "cpu_frequency_max_khz": ",".join(
                str(item.get("scaling_max_khz", "")) for item in frequency_policies
            ),
            "cpu_frequency_current_khz": ",".join(
                str(item.get("scaling_current_khz", "")) for item in frequency_policies
            ),
            "temperature_before_run_millic": frequency_before.get(
                "temperature_millic", ""
            ),
            "temperature_after_run_millic": frequency_after.get(
                "temperature_millic", ""
            ),
            "policy_requested": requested,
            "priority_requested": 0 if run_variables["policy"] == "other" else self._priority,
            "policy_effective": effective,
            "priority_effective": scheduler.get("priority", ""),
            "policy_matches": effective == requested,
            "workload_success": bool(startup_result.get("success", False)),
            "process_error": summary.get("process_error", False),
            "startup_error_kind": summary.get("startup_error", {}).get("kind", ""),
            "startup_error_message": summary.get("startup_error", {}).get("message", ""),
            "cycles_started": summary.get("cycles_started", 0),
            "cycles_completed": startup_result.get("completed_cycles", 0),
            "cycles_successful": summary.get("successful_cycles", 0),
            "all_threads_terminated": summary.get("all_observed_threads_terminated", False),
            "observed_thread_instances": summary.get("thread_instances", 0),
            "start_call_ms_mean": summary.get("start_call_ms_mean", 0),
            "start_call_ms_max": summary.get("start_call_ms_max", 0),
            "first_frame_ms_mean": summary.get("first_frame_ms_mean", 0),
            "first_frame_ms_max": summary.get("first_frame_ms_max", 0),
            "first_frame_wait_ms_mean": summary.get("first_frame_wait_ms_mean", 0),
            "first_frame_wait_ms_max": summary.get("first_frame_wait_ms_max", 0),
            "join_wait_ms_mean": summary.get("join_wait_ms_mean", 0),
            "join_wait_ms_max": summary.get("join_wait_ms_max", 0),
            "execution_ms_total": summary.get("execution_ms_total", 0),
            "sleep_ms_total": summary.get("sleep_ms_total", 0),
            "ready_ms_total": summary.get("ready_ms_total", 0),
            "kernel_log_captured": summary.get("kernel_log_captured", False),
            "uvc_resubmit_errors": summary.get("uvc_resubmit_errors", 0),
            "uvc_resubmit_errors_startup": summary.get("uvc_resubmit_errors_startup", 0),
            "uvc_resubmit_errors_streaming": summary.get("uvc_resubmit_errors_streaming", 0),
            "uvc_resubmit_errors_teardown": summary.get("uvc_resubmit_errors_teardown", 0),
            "uvc_resubmit_errors_other": summary.get("uvc_resubmit_errors_other", 0),
            "uvc_resubmit_interfaces": summary.get("uvc_resubmit_interfaces", ""),
            "uvc_resubmit_error_codes": summary.get("uvc_resubmit_error_codes", ""),
            "record_data_dir": str(record_dir),
        }


def _run_campaign_with_cleanup(
    campaign: CampaignCartesianProduct,
    benchmark: RealSenseStartupBench,
) -> None:
    cleanup_actions = (
        ("restore V4L2 binding", benchmark.restore_v4l2_binding),
        ("restore CPU frequency", benchmark.restore_cpu_frequency),
    )
    try:
        campaign.run()
    except BaseException:
        for description, cleanup in cleanup_actions:
            try:
                cleanup()
            except Exception as cleanup_error:
                print(
                    f"[CLEANUP] failed to {description}: {cleanup_error}",
                    file=sys.stderr,
                )
        raise

    cleanup_errors: List[str] = []
    for description, cleanup in cleanup_actions:
        try:
            cleanup()
        except Exception as cleanup_error:
            cleanup_errors.append(f"{description}: {cleanup_error}")
    if cleanup_errors:
        raise RuntimeError("Campaign cleanup failed: " + " | ".join(cleanup_errors))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Trace repeated D435 startup/frame/shutdown cycles with LiME/eBPF, "
            "and aggregate scheduling-policy comparisons with Benchkit."
        )
    )
    parser.add_argument("--policies", nargs="+", choices=sorted(POLICY_NAMES), default=["other", "rr", "fifo"])
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--serial", default="")
    parser.add_argument(
        "--frame-timeout-ms",
        type=int,
        default=1500,
        help="Timeout for each frame wait (default: 1500 ms).",
    )
    parser.add_argument(
        "--join-timeout-ms",
        type=int,
        default=10,
        help="Wait for cycle-created threads to exit (default: 10 ms).",
    )
    parser.add_argument(
        "--process-timeout-seconds",
        type=int,
        default=30,
        help="Wall-clock timeout for one traced attempt (default: 30 seconds).",
    )
    parser.add_argument(
        "--recovery-reset-timeout-ms",
        type=int,
        default=5000,
        help=(
            "D435 firmware reconnect timeout during full-reset recovery "
            "(default: 5000 ms)."
        ),
    )
    parser.add_argument(
        "--recover-on-failure",
        choices=("none", "usb", "depth-prime", "full-reset"),
        default="none",
        help=(
            "After recording a failed run, recover before continuing; full-reset "
            "resets D435 firmware and the complete composite USB device."
        ),
    )
    parser.add_argument(
        "--recovery-wait-seconds",
        type=float,
        default=1.2,
        help=(
            "How long to wait for the reset camera to become enumerable "
            "(default: 1.2 secs)."
        ),
    )
    parser.add_argument(
        "--recovery-settle-seconds",
        type=float,
        default=0.0,
        help="Wait after recovery before retrying the same run (default: 0 secs).",
    )
    parser.add_argument(
        "--max-attempts-per-run",
        type=int,
        default=1,
        help="Maximum measured attempts for one Benchkit repetition (default: 1).",
    )
    parser.add_argument("--nb-runs", type=int, default=1)
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--lime", type=Path, default=DEFAULT_LIME)
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="Do not add sudo when not root (LiME/eBPF and RT policies will then require capabilities).",
    )
    args = parser.parse_args()

    if _BENCHKIT_IMPORT_ERROR is not None:
        raise SystemExit(
            f"Benchkit Python dependency missing: {_BENCHKIT_IMPORT_ERROR.name}. "
            "Install the Benchkit environment (this pinned revision also needs numpy)."
        )

    if (
        args.cycles < 1
        or args.frames < 1
        or args.frame_timeout_ms < 1
        or args.process_timeout_seconds < 1
        or args.recovery_reset_timeout_ms < 1
        or args.nb_runs < 1
    ):
        parser.error(
            "--cycles, --frames, --frame-timeout-ms, --process-timeout-seconds, "
            "--recovery-reset-timeout-ms, and --nb-runs must be positive"
        )
    if args.join_timeout_ms < 0:
        parser.error("--join-timeout-ms must be non-negative")
    if args.recovery_wait_seconds <= 0:
        parser.error("--recovery-wait-seconds must be positive")
    if args.recovery_settle_seconds < 0:
        parser.error("--recovery-settle-seconds must be non-negative")
    if args.max_attempts_per_run < 1:
        parser.error("--max-attempts-per-run must be positive")
    if args.max_attempts_per_run > 1 and args.recover_on_failure == "none":
        parser.error("multiple attempts require --recover-on-failure")

    use_sudo = os.geteuid() != 0 and not args.no_sudo
    args.results_dir.mkdir(parents=True, exist_ok=True)
    benchmark = RealSenseStartupBench(
        build_dir=args.build_dir,
        lime=args.lime,
        cycles=args.cycles,
        frames=args.frames,
        serial=args.serial,
        frame_timeout_ms=args.frame_timeout_ms,
        join_timeout_ms=args.join_timeout_ms,
        process_timeout_seconds=args.process_timeout_seconds,
        recovery_reset_timeout_ms=args.recovery_reset_timeout_ms,
        recover_on_failure=args.recover_on_failure,
        recovery_wait_seconds=args.recovery_wait_seconds,
        recovery_settle_seconds=args.recovery_settle_seconds,
        max_attempts_per_run=args.max_attempts_per_run,
        use_sudo=use_sudo,
    )
    campaign = CampaignCartesianProduct(
        name="realsense_startup",
        benchmark=benchmark,
        nb_runs=args.nb_runs,
        variables={"policy": args.policies},
        constants=FIXED_CAMPAIGN_CONSTANTS,
        debug=False,
        gdb=False,
        enable_data_dir=True,
        continuing=False,
        benchmark_duration_seconds=None,
        results_dir=str(args.results_dir),
    )
    _run_campaign_with_cleanup(campaign, benchmark)


if __name__ == "__main__":
    main()
