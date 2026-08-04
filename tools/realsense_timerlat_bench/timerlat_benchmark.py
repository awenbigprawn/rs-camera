"""Benchkit adapter for the RTNS Timerlat platform-characterization matrix."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from benchkit.benchmark import Benchmark

from noise_workloads import CpuBusyLoopNoise, CpuNoiseConfig
from realsense_bench_common.artifacts import resolve_selected_attempt
from realsense_bench_common.attempts import AttemptDecision, run_attempt_loop
from realsense_bench_common.memory import DropCachesBeforeRun, memory_cleanup_result_fields
from realsense_bench_common.recovery import CameraRecoveryConfig, MultiCameraFullReset
from realsense_bench_common.results import common_attempt_result_fields
from realsense_bench_common.system_controls import SystemControlConfig, SystemControls
from timerlat_parser import parse_timerlat_histogram
from timerlat_settings import (
    CAMPAIGN_CAMERA_GUARD_SECONDS,
    CAMPAIGN_CPU_FREQUENCY_MHZ,
    CAMPAIGN_CPU_NOISE_WORKERS,
    CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    CPU_LOCK_SCRIPT,
    CPU_RESTORE_SCRIPT,
    REPO_ROOT,
    STEADY_TOOL_DIR,
    TOOL_DIR,
)
from timerlat_workload import ManagedCameraLoad


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def preempt_rt_enabled() -> bool:
    try:
        if Path("/sys/kernel/realtime").read_text(encoding="utf-8").strip() == "1":
            return True
    except OSError:
        pass
    return "PREEMPT_RT" in platform.version()


def validate_kernel_label(label: str) -> Dict[str, Any]:
    detected_rt = preempt_rt_enabled()
    expected_rt = label == "linux_preempt_rt"
    if detected_rt != expected_rt:
        raise RuntimeError(
            f"kernel label {label!r} expects PREEMPT_RT={expected_rt}, "
            f"but the running kernel reports PREEMPT_RT={detected_rt}"
        )
    return {
        "label": label,
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "preempt_rt": detected_rt,
    }


def _camera_descriptors(
    case: Mapping[str, Any],
    serials: Sequence[str],
    camera_summary: Mapping[str, Any],
) -> list[Dict[str, Any]]:
    cameras = camera_summary.get("cameras", [])
    if isinstance(cameras, list) and cameras:
        return [dict(camera) for camera in cameras if isinstance(camera, dict)]
    count = int(case.get("camera", {}).get("count", 0))
    return [{"serial": serial} for serial in serials[:count]]


class RealSenseTimerlatBench(Benchmark):
    def __init__(
        self,
        *,
        cases: Iterable[Dict[str, Any]],
        timerlat_config: Mapping[str, Any],
        kernel_label: str,
        serials: Sequence[str],
        build_dir: Path,
        rtla: str,
        use_sudo: bool,
        recover_on_failure: str,
        recovery_reset_timeout_ms: int,
        recovery_wait_seconds: float,
        recovery_settle_seconds: float,
        max_attempts_per_run: int,
        build_jobs: int,
    ) -> None:
        super().__init__(
            command_wrappers=(),
            command_attachments=(),
            shared_libs=(),
            pre_run_hooks=(),
            post_run_hooks=(),
        )
        self._cases = {case["case_id"]: case for case in cases}
        self._timerlat = dict(timerlat_config)
        self._kernel = validate_kernel_label(kernel_label)
        self._serials = tuple(serials)
        self._build_dir = build_dir.resolve()
        self._probe = self._build_dir / "realsense_steady_probe"
        self._reset_probe = self._build_dir / "d435_sensor_probe"
        self._rtla = rtla
        self._use_sudo = use_sudo
        self._recover_on_failure = recover_on_failure
        self._recovery_settle_seconds = recovery_settle_seconds
        self._max_attempts_per_run = max_attempts_per_run
        self._build_jobs = build_jobs
        self._drop_caches = CAMPAIGN_DROP_CACHES_BEFORE_RUN
        self._memory_cleanup = DropCachesBeforeRun(use_sudo=use_sudo)
        self._active_camera: ManagedCameraLoad | None = None
        self._cpu_noise = CpuBusyLoopNoise(
            CpuNoiseConfig(
                executable=self._build_dir / "realsense_cpu_noise",
                modes=("none", "busy_loop"),
                workers=CAMPAIGN_CPU_NOISE_WORKERS,
                warmup_seconds=10.0,
                ready_timeout_seconds=30.0,
                cpu_affinity=None,
            ),
            REPO_ROOT,
        )
        self._recovery = MultiCameraFullReset(
            CameraRecoveryConfig(
                repo_root=REPO_ROOT,
                reset_probe=self._reset_probe,
                use_sudo=use_sudo,
                reset_timeout_ms=recovery_reset_timeout_ms,
                enumeration_timeout_seconds=recovery_wait_seconds,
            )
        )
        self._system_controls = SystemControls(
            SystemControlConfig(
                repo_root=REPO_ROOT,
                tool_dir=STEADY_TOOL_DIR,
                use_sudo=use_sudo,
                cpu_frequency_mhz=CAMPAIGN_CPU_FREQUENCY_MHZ,
                cpu_lock_script=CPU_LOCK_SCRIPT,
                cpu_restore_script=CPU_RESTORE_SCRIPT,
                rsusb_backend=False,
                rsusb_usb_devices=(),
                rsusb_helper=Path("/unused"),
                disable_realsense_autosuspend=True,
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
        return ["load_case"]

    def _rtla_executable(self) -> str:
        resolved = shutil.which(self._rtla)
        if resolved:
            return resolved
        candidate = Path(self._rtla)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise RuntimeError(f"rtla executable not found: {self._rtla}")

    def _timerlat_tracer_available(self) -> bool:
        paths = (
            Path("/sys/kernel/tracing/available_tracers"),
            Path("/sys/kernel/debug/tracing/available_tracers"),
        )
        for path in paths:
            try:
                if "timerlat" in path.read_text(encoding="utf-8").split():
                    return True
            except OSError:
                if not self._use_sudo:
                    continue
                completed = subprocess.run(
                    ["sudo", "--non-interactive", "cat", str(path)],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if (
                    completed.returncode == 0
                    and "timerlat" in completed.stdout.split()
                ):
                    return True
        return False

    def prebuild_bench(self, **_kwargs: Any) -> int:
        self._rtla = self._rtla_executable()
        if not self._timerlat_tracer_available():
            raise RuntimeError(
                "The running kernel does not expose timerlat. Mount tracefs and "
                "enable CONFIG_OSNOISE_TRACER and CONFIG_TIMERLAT_TRACER."
            )
        self._system_controls.validate_environment()
        if self._drop_caches:
            self._memory_cleanup.validate()

        camera_enabled = any(
            bool(case.get("camera", {}).get("enabled"))
            for case in self._cases.values()
        )
        cpu_noise_enabled = any(
            case.get("noise", {}).get("cpu") == "busy_loop"
            for case in self._cases.values()
        )
        if camera_enabled and self._recover_on_failure == "full-reset":
            self._recovery.validate_environment()
        targets: list[str] = []
        if camera_enabled:
            targets += ["realsense_steady_probe", "d435_sensor_probe"]
        if cpu_noise_enabled:
            self._cpu_noise.validate_environment()
            targets.append("realsense_cpu_noise")
        if targets:
            subprocess.check_call(
                [
                    "cmake",
                    "-S",
                    str(REPO_ROOT),
                    "-B",
                    str(self._build_dir),
                    "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                    "-DFORCE_RSUSB_BACKEND=OFF",
                    "-DRS_CAMERA_BUILD_GPU_NOISE=OFF",
                ]
            )
            subprocess.check_call(
                [
                    "cmake",
                    "--build",
                    str(self._build_dir),
                    "--target",
                    *targets,
                    "--parallel",
                    str(self._build_jobs),
                ]
            )
        return 0

    def build_bench(self, **_kwargs: Any) -> None:
        return None

    def _timerlat_command(self) -> list[str]:
        command = [
            self._rtla,
            "timerlat",
            "hist",
            "-k",
            "-c",
            str(self._timerlat["cpu_list"]),
            "-p",
            str(self._timerlat["period_us"]),
            "-P",
            str(self._timerlat["policy"]),
            "--warm-up",
            str(self._timerlat["warmup_seconds"]),
            "-d",
            f"{self._timerlat['duration_seconds']}s",
            "-b",
            str(self._timerlat["bucket_us"]),
            "-E",
            str(self._timerlat["entries"]),
            "--no-aa",
        ]
        return ["sudo", "--non-interactive", *command] if self._use_sudo else command

    def _prepare_attempt(
        self, attempt: int, attempt_dir: Path, load_case: str
    ) -> None:
        attempt_dir.mkdir(parents=True, exist_ok=False)
        self._system_controls.prepare_attempt(attempt, attempt_dir)
        if self._drop_caches:
            self._memory_cleanup(
                build_variables={},
                run_variables={"load_case": load_case},
                other_variables={},
                record_data_dir=attempt_dir,
            )

    def _start_camera(
        self, case: Mapping[str, Any], attempt_dir: Path
    ) -> tuple[ManagedCameraLoad | None, Dict[str, Any]]:
        camera = case.get("camera", {})
        if not camera.get("enabled"):
            return None, {"ready": True, "enabled": False}
        count = int(camera["count"])
        load = ManagedCameraLoad(
            probe=self._probe,
            repo_root=REPO_ROOT,
            serials=self._serials[:count],
            duration_seconds=int(self._timerlat["duration_seconds"]),
            timerlat_warmup_seconds=int(self._timerlat["warmup_seconds"]),
            guard_seconds=CAMPAIGN_CAMERA_GUARD_SECONDS,
        )
        self._active_camera = load
        load.start(case, attempt_dir)
        ready = load.wait_until_steady(
            float(camera["startup_timeout_ms"]) / 1000.0 + 10.0
        )
        _write_json(attempt_dir / "camera_ready.json", ready)
        return load, ready

    def _run_timerlat(self, attempt_dir: Path) -> tuple[str, Dict[str, Any]]:
        command = self._timerlat_command()
        _write_json(attempt_dir / "timerlat_command.json", command)
        started_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        try:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=(
                    int(self._timerlat["duration_seconds"])
                    + int(self._timerlat["warmup_seconds"])
                    + 60
                ),
            )
            stdout, stderr, returncode = (
                completed.stdout,
                completed.stderr,
                completed.returncode,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _process_text(error.stdout)
            stderr = _process_text(error.stderr) + "\nrtla process timed out\n"
            returncode = 124
        process = {
            "command": command,
            "returncode": returncode,
            "started_boottime_ns": started_ns,
            "ended_boottime_ns": time.clock_gettime_ns(time.CLOCK_BOOTTIME),
        }
        (attempt_dir / "timerlat_hist.txt").write_text(stdout, encoding="utf-8")
        (attempt_dir / "timerlat_stderr.txt").write_text(stderr, encoding="utf-8")
        _write_json(attempt_dir / "timerlat_process.json", process)
        return stdout, process

    def _run_attempt(
        self, *, case: Mapping[str, Any], attempt_dir: Path
    ) -> tuple[str, Dict[str, Any]]:
        self._system_controls.snapshot_topology(attempt_dir / "topology_before.json")
        kernel_before, kernel_error = self._system_controls.kernel_log()
        noise_mode = str(case.get("noise", {}).get("cpu", "none"))
        output = ""
        camera_load: ManagedCameraLoad | None = None
        ready: Dict[str, Any] = {"ready": True, "enabled": False}
        timerlat_process: Dict[str, Any] = {}
        timerlat_data: Dict[str, Any] = {}
        camera_process: Dict[str, Any] = {}
        parse_error = ""
        try:
            noise_ready = self._cpu_noise.start(noise_mode, attempt_dir)
            _write_json(attempt_dir / "cpu_noise_configuration.json", noise_ready)
            camera_load, ready = self._start_camera(case, attempt_dir)
            if ready.get("ready"):
                output, timerlat_process = self._run_timerlat(attempt_dir)
                if camera_load is not None:
                    camera_process = camera_load.finish(
                        timeout_seconds=(
                            int(self._timerlat["warmup_seconds"])
                            + CAMPAIGN_CAMERA_GUARD_SECONDS
                            + 30
                        )
                    )
                    self._active_camera = None
                try:
                    timerlat_data = parse_timerlat_histogram(output)
                    _write_json(
                        attempt_dir / "timerlat_histogram.json", timerlat_data
                    )
                except ValueError as error:
                    parse_error = str(error)
            elif camera_load is not None:
                camera_process = camera_load.stop()
                self._active_camera = None
        finally:
            if self._active_camera is not None:
                camera_process = self._active_camera.stop()
                self._active_camera = None
            self._cpu_noise.stop(attempt_dir)
            self._system_controls.capture_kernel_delta(
                attempt_dir, kernel_before, kernel_error
            )
            self._system_controls.snapshot_topology(
                attempt_dir / "topology_after.json"
            )

        _write_json(attempt_dir / "camera_process.json", camera_process)
        camera_summary = camera_load.read_summary() if camera_load else {}
        camera_enabled = bool(case.get("camera", {}).get("enabled"))
        if not ready.get("ready"):
            phase, error = "startup", str(
                camera_summary.get(
                    "error",
                    ready.get("error", "camera did not reach steady state"),
                )
            )
        elif timerlat_process.get("returncode", 0) != 0:
            phase, error = (
                "measurement",
                f"rtla exited with {timerlat_process.get('returncode')}",
            )
        elif parse_error:
            phase, error = "measurement", parse_error
        elif camera_enabled and not camera_summary.get("success", False):
            phase, error = "measurement", str(
                camera_summary.get("error", "camera load failed")
            )
        elif camera_enabled and camera_process.get("returncode") != 0:
            phase, error = (
                "measurement",
                f"camera probe exited with {camera_process.get('returncode')}",
            )
        else:
            phase, error = "none", ""
        summary: Dict[str, Any] = {
            "schema_version": 1,
            "success": phase == "none",
            "error": error,
            "failure_phase": phase,
            "kernel": self._kernel,
            "load_case": case["case_id"],
            "timerlat": timerlat_data,
            "timerlat_process": timerlat_process,
            "camera_enabled": camera_enabled,
            "camera_ready": ready,
            "camera_process": camera_process,
            "camera": camera_summary,
            "cameras": _camera_descriptors(case, self._serials, camera_summary),
            "cpu_noise_mode": noise_mode,
        }
        _write_json(attempt_dir / "timerlat_run_summary.json", summary)
        return output, summary

    def single_run(
        self, load_case: str, record_data_dir: Path, **_kwargs: Any
    ) -> str:
        case = self._cases[load_case]
        record_dir = Path(record_data_dir).resolve()
        record_dir.mkdir(parents=True, exist_ok=True)
        if any(record_dir.glob("attempt-*")):
            raise RuntimeError(f"Attempt directories already exist in {record_dir}")
        cpu_state = self._system_controls.prepare_campaign(record_dir)
        self._system_controls.write_cpu_state(
            record_dir / "cpu_frequency_state.json", cpu_state
        )
        _write_json(record_dir / "case.json", case)
        base_manifest = {
            "schema_version": 1,
            "load_case": load_case,
            "kernel": self._kernel,
            "timerlat": self._timerlat,
            "camera_policy": "SCHED_OTHER",
            "camera_serials": list(
                self._serials[: int(case.get("camera", {}).get("count", 0))]
            ),
            "cpu_frequency_mhz": CAMPAIGN_CPU_FREQUENCY_MHZ,
            "drop_caches_before_attempt": self._drop_caches,
            "cpu_noise": self._cpu_noise.manifest(
                str(case.get("noise", {}).get("cpu", "none"))
            ),
            "recover_on_failure": self._recover_on_failure,
            "max_attempts_per_run": self._max_attempts_per_run,
        }
        _write_json(record_dir / "run_manifest.json", base_manifest)
        camera_enabled = bool(case.get("camera", {}).get("enabled"))

        def run_attempt(
            attempt: int, attempt_dir: Path
        ) -> tuple[str, Dict[str, Any]]:
            self._prepare_attempt(attempt, attempt_dir, load_case)
            return self._run_attempt(case=case, attempt_dir=attempt_dir)

        def classify(summary: Mapping[str, Any]) -> AttemptDecision:
            success = bool(summary.get("success", False))
            phase = str(summary.get("failure_phase", "measurement"))
            return AttemptDecision(
                success=success,
                failure_phase=phase,
                retry=not success and phase == "startup",
                error=str(summary.get("error", "")),
            )

        def recover(
            summary: Mapping[str, Any],
            attempt_dir: Path,
            _decision: AttemptDecision,
        ) -> Dict[str, Any]:
            return self._recovery.recover(summary.get("cameras", []), attempt_dir)

        recovery_method = self._recover_on_failure if camera_enabled else "none"
        result = run_attempt_loop(
            record_dir=record_dir,
            max_attempts=(self._max_attempts_per_run if camera_enabled else 1),
            recovery_method=recovery_method,
            recovery_settle_seconds=self._recovery_settle_seconds,
            run_attempt=run_attempt,
            classify_attempt=classify,
            recover_attempt=(recover if recovery_method != "none" else None),
        )
        _write_json(record_dir / "timerlat_run_summary.json", result.summary)
        _write_json(record_dir / "attempts.json", result.attempts)
        (record_dir / "selected_attempt.txt").write_text(
            f"{result.selected_attempt}\n", encoding="utf-8"
        )
        base_manifest.update(
            {
                "selected_attempt": result.selected_attempt,
                "attempt_count": len(result.attempts),
                "eventual_success": result.summary["eventual_success"],
            }
        )
        _write_json(record_dir / "run_manifest.json", base_manifest)
        return result.output

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
        summary = json.loads(
            (record_dir / "timerlat_run_summary.json").read_text(encoding="utf-8")
        )
        selected = resolve_selected_attempt(record_dir)
        timerlat_global = summary.get("timerlat", {}).get("global", {})
        camera = summary.get("camera", {})
        aggregate = camera.get("aggregate", {})
        noise = self._cpu_noise.artifacts(
            str(summary.get("cpu_noise_mode", "none")), selected.data_dir
        )
        success = bool(summary.get("success", False)) and noise.valid
        error = str(summary.get("error", ""))
        if not noise.valid:
            error = " | ".join(part for part in (error, noise.error) if part)
        return {
            **memory_cleanup_result_fields(
                selected.data_dir, configured=self._drop_caches
            ),
            **common_attempt_result_fields(summary),
            "success": success,
            "error": error,
            "load_case": run_variables["load_case"],
            "kernel_label": self._kernel["label"],
            "kernel_release": self._kernel["release"],
            "preempt_rt": self._kernel["preempt_rt"],
            "timerlat_period_us": self._timerlat["period_us"],
            "timerlat_duration_seconds": self._timerlat["duration_seconds"],
            "timerlat_irq_max_us": timerlat_global.get("irq_max_us", 0),
            "timerlat_thread_max_us": timerlat_global.get("thread_max_us", 0),
            "timerlat_irq_p999_us_max_cpu": timerlat_global.get(
                "irq_p999_us_max_cpu", 0
            ),
            "timerlat_thread_p999_us_max_cpu": timerlat_global.get(
                "thread_p999_us_max_cpu", 0
            ),
            "timerlat_overflow_samples": timerlat_global.get("overflow_samples", 0),
            "camera_count": len(summary.get("cameras", [])),
            "camera_success": camera.get(
                "success", not summary.get("camera_enabled")
            ),
            "camera_frames": aggregate.get("frames", 0),
            "camera_drops": aggregate.get("drops", 0),
            "camera_timeouts": aggregate.get("timeouts", 0),
            "cpu_noise_mode": summary.get("cpu_noise_mode", "none"),
            "cpu_noise_valid": noise.valid,
            "record_data_dir": str(record_dir),
            "selected_attempt_data_dir": str(selected.data_dir),
        }

    def cleanup(self) -> None:
        errors = []
        if self._active_camera is not None:
            try:
                self._active_camera.stop()
            except Exception as error:
                errors.append(f"stop camera load: {error}")
            self._active_camera = None
        try:
            self._cpu_noise.stop()
        except Exception as error:
            errors.append(f"stop CPU noise: {error}")
        try:
            self._system_controls.cleanup()
        except Exception as error:
            errors.append(f"restore system controls: {error}")
        if errors:
            raise RuntimeError("Timerlat campaign cleanup failed: " + " | ".join(errors))
