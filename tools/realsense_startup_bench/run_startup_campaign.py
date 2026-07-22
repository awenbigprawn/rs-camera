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
BENCHKIT_PATH = REPO_ROOT / "deps" / "benchkit"
DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-startup"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results"
DEFAULT_LIME = REPO_ROOT / "deps" / "lime-rtw" / "target" / "release" / "lime-rtw"

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


POLICY_NAMES = {
    "other": "SCHED_OTHER",
    "rr": "SCHED_RR",
    "fifo": "SCHED_FIFO",
}


class RealSenseStartupBench(Benchmark):
    def __init__(
        self,
        build_dir: Path,
        lime: Path,
        cycles: int,
        frames: int,
        priority: int,
        serial: str,
        frame_timeout_ms: int,
        join_timeout_ms: int,
        recovery_reset_timeout_ms: int,
        recover_on_failure: str,
        recovery_wait_seconds: float,
        recovery_settle_seconds: float,
        max_attempts_per_run: int,
        use_sudo: bool,
    ) -> None:
        super().__init__(
            command_wrappers=(),
            command_attachments=(),
            shared_libs=(),
            pre_run_hooks=(),
            post_run_hooks=(),
        )
        self._build_dir = build_dir.resolve()
        self._lime = lime.resolve()
        self._cycles = cycles
        self._frames = frames
        self._priority = priority
        self._serial = serial
        self._frame_timeout_ms = frame_timeout_ms
        self._join_timeout_ms = join_timeout_ms
        self._recovery_reset_timeout_ms = recovery_reset_timeout_ms
        self._recover_on_failure = recover_on_failure
        self._recovery_wait_seconds = recovery_wait_seconds
        self._recovery_settle_seconds = recovery_settle_seconds
        self._max_attempts_per_run = max_attempts_per_run
        self._use_sudo = use_sudo
        self._probe = self._build_dir / "d435_sensor_probe"
        self._tracer = self._build_dir / "libtrace_pthreads.so"

    @property
    def bench_src_path(self) -> Path:
        return TOOL_DIR

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return ["policy", "cycle_delay_ms"]

    def prebuild_bench(self, **_kwargs: Any) -> int:
        if not self._lime.is_file():
            raise RuntimeError(
                f"LiME executable not found at {self._lime}. "
                "Build the unmodified dependency with: "
                "cargo build --release --manifest-path deps/lime-rtw/Cargo.toml"
            )
        if shutil.which("chrt") is None:
            raise RuntimeError("chrt is required (normally provided by util-linux).")
        if (
            self._recover_on_failure in ("usb", "full-reset")
            and shutil.which("usbreset") is None
        ):
            raise RuntimeError(
                "usbreset is required for --recover-on-failure usb/full-reset."
            )
        if self._recover_on_failure == "depth-prime" and shutil.which("v4l2-ctl") is None:
            raise RuntimeError("v4l2-ctl is required for --recover-on-failure depth-prime.")

        subprocess.check_call([
            "cmake", "-S", str(REPO_ROOT), "-B", str(self._build_dir),
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        ])
        subprocess.check_call([
            "cmake", "--build", str(self._build_dir),
            "--target", "d435_sensor_probe", "-j4",
        ])
        compiler = os.environ.get("CC", "cc")
        subprocess.check_call([
            compiler, "-shared", "-fPIC", "-g", "-O2", "-fno-omit-frame-pointer",
            "-Wall", "-Wextra", "-o", str(self._tracer),
            str(REPO_ROOT / "tools" / "realsense_thread_trace" / "trace_pthreads.c"),
            "-ldl", "-pthread",
        ])
        return 0

    def build_bench(self, **_kwargs: Any) -> None:
        return None

    def _scheduled_probe(self, policy: str, cycle_delay_ms: int) -> List[str]:
        if policy == "other":
            scheduled = ["chrt", "--other", "0"]
        elif policy == "rr":
            scheduled = ["chrt", "--rr", str(self._priority)]
        elif policy == "fifo":
            scheduled = ["chrt", "--fifo", str(self._priority)]
        else:
            raise ValueError(f"Unsupported policy: {policy}")

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

    def _read_kernel_log(self) -> tuple[str | None, str]:
        command = ["dmesg", "--raw"]
        if self._use_sudo:
            command = ["sudo", "--non-interactive", *command]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            message = completed.stderr.strip() or f"dmesg exited with status {completed.returncode}"
            return None, message
        return completed.stdout, ""

    @staticmethod
    def _kernel_log_delta(before: str, after: str) -> tuple[str, str]:
        if after.startswith(before):
            return after[len(before):], ""

        before_lines = before.splitlines()
        after_lines = after.splitlines()
        if not before_lines:
            return after, ""

        anchor = before_lines[-1]
        for index in range(len(after_lines) - 1, -1, -1):
            if after_lines[index] == anchor:
                delta = "\n".join(after_lines[index + 1:])
                return (delta + "\n" if delta else ""), ""
        return "", "dmesg ring buffer changed and the pre-run anchor was lost"

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
        reset_timeout_seconds = self._recovery_reset_timeout_ms / 1000.0
        command = [
            str(self._probe),
            "--hardware-reset",
            "--reset-timeout-ms",
            str(int(reset_timeout_seconds * 1000)),
        ]
        if self._serial:
            command += ["--serial", self._serial]
        if self._use_sudo:
            command = [
                "sudo", "--non-interactive", "--preserve-env=LD_LIBRARY_PATH", *command,
            ]

        result: Dict[str, Any] = {
            "attempted": True,
            "method": "full-reset",
            "success": False,
            "physical_port": str(device.get("physical_port", "")),
            "hardware_reset": {"command": command, "success": False},
        }
        errors: List[str] = []
        print("[RECOVERY] resetting D435 firmware and waiting for USB reconnect")
        try:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=reset_timeout_seconds + 5,
                check=False,
            )
            hardware_output = (completed.stdout + completed.stderr).strip()
            hardware_success = (
                completed.returncode == 0
                and '"state":"complete"' in hardware_output
            )
            result["hardware_reset"] = {
                "command": command,
                "returncode": completed.returncode,
                "output": hardware_output,
                "success": hardware_success,
            }
            if not hardware_success:
                errors.append("D435 firmware hardware reset did not complete")
        except (OSError, subprocess.TimeoutExpired) as error:
            result["hardware_reset"]["error"] = str(error)
            errors.append(f"D435 firmware hardware reset failed: {error}")

        # A D435 is one composite USB device. Resetting this parent device resets
        # the depth, RGB, and infrared UVC interfaces together.
        print(
            "[RECOVERY] firmware reset "
            f"success={result['hardware_reset'].get('success', False)}; "
            "resetting composite host USB device"
        )
        usb_result = self._recover_usb_device(device, record_dir)
        result["usb_reset"] = usb_result
        if not usb_result.get("success", False):
            errors.append(str(usb_result.get("error", "host USB reset failed")))

        result["success"] = bool(
            result["hardware_reset"].get("success", False)
            and usb_result.get("success", False)
        )
        if errors:
            result["error"] = " | ".join(errors)
        (record_dir / "recovery.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result

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

        target = [
            "env",
            f"LD_PRELOAD={self._tracer}",
            f"RS_THREAD_TRACE_FILE={lifecycle_path}",
            *self._scheduled_probe(policy, cycle_delay_ms),
        ]
        command = [
            str(self._lime), "trace", "--best-effort", "-o", str(lime_dir), "--", *target,
        ]
        if self._use_sudo:
            command = ["sudo", "--preserve-env=LD_LIBRARY_PATH", *command]

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
            30,
            self._cycles * (join_timeout_seconds + frame_timeout_seconds + 5)
            + delay_budget_seconds,
        )
        kernel_before, kernel_error = self._read_kernel_log()
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
            kernel_after, after_error = self._read_kernel_log()
            if kernel_before is not None and kernel_after is not None:
                kernel_delta, delta_error = self._kernel_log_delta(kernel_before, kernel_after)
                if not delta_error:
                    (attempt_dir / "kernel_log.txt").write_text(kernel_delta, encoding="utf-8")
                kernel_error = kernel_error or after_error or delta_error
            else:
                kernel_error = kernel_error or after_error
            if kernel_error:
                (attempt_dir / "kernel_log_capture_error.txt").write_text(
                    kernel_error + "\n", encoding="utf-8"
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
        cycle_delay_ms: int,
        record_data_dir: Path,
        **kwargs: Any,
    ) -> str:
        cycle_delay_ms = int(cycle_delay_ms)
        record_dir = Path(record_data_dir).resolve()
        record_dir.mkdir(parents=True, exist_ok=True)
        if any(record_dir.glob("attempt-*")):
            raise RuntimeError(f"Attempt directories already exist in {record_dir}")

        base_manifest: Dict[str, Any] = {
            "schema_version": 4,
            "policy_requested": POLICY_NAMES[policy],
            "priority_requested": 0 if policy == "other" else self._priority,
            "cycles": self._cycles,
            "frames_per_cycle": self._frames,
            "frame_timeout_ms": self._frame_timeout_ms,
            "serial": self._serial,
            "join_timeout_ms": self._join_timeout_ms,
            "recovery_reset_timeout_ms": self._recovery_reset_timeout_ms,
            "cycle_delay_ms": cycle_delay_ms,
            "recover_on_failure": self._recover_on_failure,
            "recovery_wait_seconds": self._recovery_wait_seconds,
            "recovery_settle_seconds": self._recovery_settle_seconds,
            "max_attempts_per_run": self._max_attempts_per_run,
            "probe": str(self._probe),
            "lime": str(self._lime),
            "clock": "CLOCK_BOOTTIME",
        }
        (record_dir / "run_manifest.json").write_text(
            json.dumps(base_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        attempt_records: List[Dict[str, Any]] = []
        recoveries: List[Dict[str, Any]] = []
        final_output = ""
        final_summary: Dict[str, Any] | None = None

        for attempt in range(1, self._max_attempts_per_run + 1):
            attempt_dir = record_dir / f"attempt-{attempt}"
            output, summary = self._run_attempt(
                policy=policy,
                cycle_delay_ms=cycle_delay_ms,
                attempt=attempt,
                attempt_dir=attempt_dir,
                base_manifest=base_manifest,
                kwargs=kwargs,
            )
            final_output = output
            final_summary = summary
            success = bool(summary.get("startup_result", {}).get("success", False))
            attempt_record: Dict[str, Any] = {
                "attempt": attempt,
                "success": success,
                "cycles_started": summary.get("cycles_started", 0),
                "cycles_completed": summary.get("startup_result", {}).get("completed_cycles", 0),
                "error_kind": summary.get("startup_error", {}).get("kind", ""),
                "error_message": summary.get("startup_error", {}).get("message", ""),
                "record_data_dir": str(attempt_dir),
            }

            if not success and self._recover_on_failure != "none":
                error_message = attempt_record["error_message"] or "workload failed"
                print(
                    f"[RECOVERY] attempt {attempt}/{self._max_attempts_per_run} "
                    f"failed: {error_message}"
                )
                if self._recover_on_failure == "usb":
                    recovery = self._recover_usb_device(summary.get("device", {}), attempt_dir)
                elif self._recover_on_failure == "depth-prime":
                    recovery = self._recover_depth_prime(summary.get("device", {}), attempt_dir)
                elif self._recover_on_failure == "full-reset":
                    recovery = self._recover_full_reset(summary.get("device", {}), attempt_dir)
                else:
                    recovery = {
                        "attempted": False,
                        "method": self._recover_on_failure,
                        "success": False,
                        "error": "unsupported recovery method",
                    }
                recoveries.append(recovery)
                attempt_record["recovery"] = recovery
                if self._recovery_settle_seconds > 0:
                    next_action = (
                        f"attempt {attempt + 1}/{self._max_attempts_per_run}"
                        if attempt < self._max_attempts_per_run
                        else "the next Benchkit point"
                    )
                    print(
                        f"[RETRY] recovery success={recovery.get('success', False)}; "
                        f"waiting {self._recovery_settle_seconds:g}s before {next_action}"
                    )
                    time.sleep(self._recovery_settle_seconds)

            attempt_records.append(attempt_record)
            if success:
                break

        if final_summary is None:
            raise RuntimeError("No workload attempt was executed")

        failed_attempts = sum(1 for item in attempt_records if not item["success"])
        recovery_errors = [
            str(item.get("error", "")) for item in recoveries if item.get("error")
        ]
        aggregate_recovery: Dict[str, Any] = {
            "attempted": bool(recoveries),
            "method": self._recover_on_failure,
            "count": len(recoveries),
        }
        if recoveries:
            aggregate_recovery["success"] = all(
                bool(item.get("success", False)) for item in recoveries
            )
        if recovery_errors:
            aggregate_recovery["error"] = " | ".join(recovery_errors)

        final_summary["attempt_count"] = len(attempt_records)
        final_summary["failed_attempt_count"] = failed_attempts
        final_summary["initial_attempt_success"] = bool(attempt_records[0]["success"])
        final_summary["eventual_success"] = bool(
            final_summary.get("startup_result", {}).get("success", False)
        )
        final_summary["attempts"] = attempt_records
        final_summary["recovery"] = aggregate_recovery
        (record_dir / "attempts.json").write_text(
            json.dumps(attempt_records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (record_dir / "summary.json").write_text(
            json.dumps(final_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (record_dir / "probe_stdout.txt").write_text(final_output, encoding="utf-8")
        (record_dir / "selected_attempt.txt").write_text(
            str(len(attempt_records)) + "\n", encoding="utf-8"
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
        requested = POLICY_NAMES[run_variables["policy"]]
        effective = scheduler.get("policy", "")
        recovery = summary.get("recovery", {})
        return {
            "policy_requested": requested,
            "priority_requested": 0 if run_variables["policy"] == "other" else self._priority,
            "policy_effective": effective,
            "priority_effective": scheduler.get("priority", ""),
            "policy_matches": effective == requested,
            "workload_success": bool(startup_result.get("success", False)),
            "attempt_count": summary.get("attempt_count", 1),
            "failed_attempt_count": summary.get("failed_attempt_count", 0),
            "initial_attempt_success": summary.get("initial_attempt_success", True),
            "eventual_success": summary.get("eventual_success", False),
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
            "recovery_attempted": recovery.get("attempted", False),
            "recovery_count": recovery.get("count", 0),
            "recovery_method": recovery.get("method", "none"),
            "recovery_success": recovery.get("success", ""),
            "recovery_error": recovery.get("error", ""),
            "record_data_dir": str(record_dir),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Trace repeated D435 startup/frame/shutdown cycles with LiME/eBPF, "
            "sweep the post-shutdown quiescence delay, and aggregate with Benchkit."
        )
    )
    parser.add_argument("--policies", nargs="+", choices=sorted(POLICY_NAMES), default=["other", "rr", "fifo"])
    parser.add_argument("--priority", type=int, default=80, help="SCHED_RR/FIFO priority (1-99).")
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
        "--recovery-reset-timeout-ms",
        type=int,
        default=5000,
        help=(
            "D435 firmware reconnect timeout during full-reset recovery "
            "(default: 5000 ms)."
        ),
    )
    parser.add_argument(
        "--cycle-delays-ms",
        nargs="+",
        type=int,
        default=[0],
        help="Post-destruction quiescence delays to sweep between cycles (default: 0).",
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

    if not 1 <= args.priority <= 99:
        parser.error("--priority must be in [1, 99]")
    if (
        args.cycles < 1
        or args.frames < 1
        or args.frame_timeout_ms < 1
        or args.recovery_reset_timeout_ms < 1
        or args.nb_runs < 1
    ):
        parser.error(
            "--cycles, --frames, --frame-timeout-ms, "
            "--recovery-reset-timeout-ms, and --nb-runs must be positive"
        )
    if args.join_timeout_ms < 0:
        parser.error("--join-timeout-ms must be non-negative")
    if any(delay < 0 for delay in args.cycle_delays_ms):
        parser.error("--cycle-delays-ms values must be non-negative")
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
        priority=args.priority,
        serial=args.serial,
        frame_timeout_ms=args.frame_timeout_ms,
        join_timeout_ms=args.join_timeout_ms,
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
        variables={"policy": args.policies, "cycle_delay_ms": args.cycle_delays_ms},
        constants=None,
        debug=False,
        gdb=False,
        enable_data_dir=True,
        continuing=False,
        benchmark_duration_seconds=None,
        results_dir=str(args.results_dir),
    )
    campaign.run()


if __name__ == "__main__":
    main()
