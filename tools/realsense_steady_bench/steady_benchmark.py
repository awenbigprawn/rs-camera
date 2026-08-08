"""Benchkit adapter for one RealSense steady-state campaign run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, Iterable, List

from benchkit.benchmark import Benchmark

from noise_workloads import (
    CpuBusyLoopNoise,
    CpuNoiseConfig,
    FixedCopyMemoryNoise,
    GpuNoiseConfig,
    GpuVulkanNoise,
    MemoryNoiseConfig,
    NoiseSuite,
    UsbStorageNoiseConfig,
    UsbStorageReadNoise,
)
from noise_transition import NoiseTransition
from parse_steady_trace import parse_steady_trace
from parse_v4l2_diagnostic_trace import parse_trace as parse_v4l2_diagnostic_trace
from parse_overrun_kernel_trace import parse_trace as parse_overrun_kernel_trace
from parse_freshness_kernel_trace import (
    parse_trace as parse_freshness_kernel_trace,
)
from realsense_bench_common.commands import (
    build_pthread_tracer,
    scheduler_prefix,
    traced_command,
    validate_trace_environment,
)
from realsense_bench_common.recovery import (
    CameraRecoveryConfig,
    MultiCameraFullReset,
)
from realsense_bench_common.system_controls import (
    SystemControlConfig,
    SystemControls,
)
from realsense_bench_common.memory import DropCachesBeforeRun
from steady_attempts import camera_descriptors, run_steady_attempts
from steady_results import parse_steady_results
from steady_settings import (
    CAMPAIGN_BACKEND,
    CAMPAIGN_CPU_FREQUENCY_MHZ,
    CAMPAIGN_DROP_CACHES_BEFORE_RUN,
    CAMPAIGN_RSUSB_USB_DEVICES,
    CAMPAIGN_RT_PRIORITY,
    CAMPAIGN_USB_KERNEL_DRIVER,
    CPU_LOCK,
    CPU_RESTORE,
    NCNN_MODEL_PARAM,
    MODELED_POLICIES,
    POLICY_NAMES,
    REPO_ROOT,
    RSUSB_HELPER,
    TOOL_DIR,
    TRACER_SOURCE,
)


OVERRUN_KERNEL_TRACE = TOOL_DIR / "record_overrun_kernel_trace.sh"
FRESHNESS_KERNEL_TRACE = TOOL_DIR / "record_freshness_kernel_trace.sh"
V4L2_DIAGNOSTIC_TRACE_CAPACITY = 12_000_000


class RealSenseSteadyBench(Benchmark):
    def __init__(
        self,
        cases: Iterable[Dict[str, Any]],
        build_dir: Path,
        lime: Path,
        use_lime: bool,
        v4l2_diagnostics: bool,
        overrun_kernel_trace: bool,
        freshness_kernel_trace: bool,
        use_sudo: bool,
        cpu_isolation_enabled: bool,
        housekeeping_cpus: str,
        benchmark_cpus: str,
        cpu_noise_modes: List[str],
        cpu_noise_workers: int,
        cpu_noise_warmup_seconds: float,
        cpu_noise_ready_timeout_seconds: float,
        cpu_noise_cpu_affinity: str | None,
        memory_noise_modes: List[str],
        memory_noise_workers: int,
        memory_noise_buffer_size_mib: int,
        memory_noise_copy_chunk_kib: int,
        memory_noise_target_mib_per_second: float,
        memory_noise_warmup_seconds: float,
        memory_noise_ready_timeout_seconds: float,
        memory_noise_cpu_affinity: str | None,
        gpu_noise_modes: List[str],
        gpu_noise_device: int,
        gpu_noise_warmup_iterations: int,
        gpu_noise_ready_timeout_seconds: float,
        gpu_noise_cpu_affinity: str | None,
        gpu_noise_vulkan_icd: Path | None,
        usb_storage_noise_modes: List[str],
        usb_storage_device: Path | None,
        usb_storage_warmup_seconds: float,
        usb_storage_block_size_kib: int,
        usb_storage_ready_timeout_seconds: float,
        recover_on_failure: str,
        recovery_reset_timeout_ms: int,
        recovery_wait_seconds: float,
        recovery_settle_seconds: float,
        max_attempts_per_run: int,
        build_jobs: int,
        v4l2_diagnostics_build_only: bool = False,
    ) -> None:
        memory_cleanup_hook = DropCachesBeforeRun(use_sudo=use_sudo)
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
        self._priority = CAMPAIGN_RT_PRIORITY
        self._use_lime = use_lime
        self._v4l2_diagnostics = v4l2_diagnostics
        self._v4l2_diagnostics_build = (
            v4l2_diagnostics or v4l2_diagnostics_build_only
        )
        self._overrun_kernel_trace = overrun_kernel_trace
        self._freshness_kernel_trace = freshness_kernel_trace
        self._use_sudo = use_sudo
        self._drop_caches_before_run = CAMPAIGN_DROP_CACHES_BEFORE_RUN
        self._memory_cleanup_hook = memory_cleanup_hook
        self._build_jobs = build_jobs
        self._logical_failures: List[str] = []
        self._probe = self._build_dir / "realsense_steady_probe"
        self._reset_probe = self._build_dir / "d435_sensor_probe"
        self._tracer = self._build_dir / "libtrace_pthreads.so"
        self._recover_on_failure = recover_on_failure
        self._recovery_settle_seconds = recovery_settle_seconds
        self._max_attempts_per_run = max_attempts_per_run
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
                tool_dir=TOOL_DIR,
                use_sudo=use_sudo,
                cpu_frequency_mhz=CAMPAIGN_CPU_FREQUENCY_MHZ,
                cpu_lock_script=CPU_LOCK,
                cpu_restore_script=CPU_RESTORE,
                rsusb_backend=CAMPAIGN_BACKEND == "rsusb",
                rsusb_usb_devices=CAMPAIGN_RSUSB_USB_DEVICES,
                rsusb_helper=RSUSB_HELPER,
                disable_realsense_autosuspend=(CAMPAIGN_BACKEND == "v4l2"),
                cpu_isolation_enabled=cpu_isolation_enabled,
                housekeeping_cpus=housekeeping_cpus,
                benchmark_cpus=benchmark_cpus,
            )
        )
        vulkan_icd = gpu_noise_vulkan_icd.resolve() if gpu_noise_vulkan_icd else None
        self._noise_suite = NoiseSuite(
            cpu=CpuBusyLoopNoise(
                CpuNoiseConfig(
                    executable=self._build_dir / "realsense_cpu_noise",
                    modes=tuple(cpu_noise_modes),
                    workers=cpu_noise_workers,
                    warmup_seconds=cpu_noise_warmup_seconds,
                    ready_timeout_seconds=cpu_noise_ready_timeout_seconds,
                    cpu_affinity=cpu_noise_cpu_affinity,
                ),
                REPO_ROOT,
            ),
            memory=FixedCopyMemoryNoise(
                MemoryNoiseConfig(
                    executable=self._build_dir / "realsense_memory_noise",
                    modes=tuple(memory_noise_modes),
                    workers=memory_noise_workers,
                    buffer_size_mib=memory_noise_buffer_size_mib,
                    copy_chunk_kib=memory_noise_copy_chunk_kib,
                    target_memory_mib_per_second=(
                        memory_noise_target_mib_per_second
                    ),
                    warmup_seconds=memory_noise_warmup_seconds,
                    ready_timeout_seconds=memory_noise_ready_timeout_seconds,
                    cpu_affinity=memory_noise_cpu_affinity,
                ),
                REPO_ROOT,
            ),
            usb_storage=UsbStorageReadNoise(
                UsbStorageNoiseConfig(
                    executable=self._build_dir / "realsense_usb_storage_noise",
                    modes=tuple(usb_storage_noise_modes),
                    device=usb_storage_device,
                    warmup_seconds=usb_storage_warmup_seconds,
                    block_size_kib=usb_storage_block_size_kib,
                    ready_timeout_seconds=usb_storage_ready_timeout_seconds,
                    use_sudo=use_sudo,
                ),
                REPO_ROOT,
            ),
            gpu=GpuVulkanNoise(
                GpuNoiseConfig(
                    executable=self._build_dir / "realsense_gpu_noise",
                    modes=tuple(gpu_noise_modes),
                    model_param=NCNN_MODEL_PARAM,
                    gpu_device=gpu_noise_device,
                    warmup_iterations=gpu_noise_warmup_iterations,
                    ready_timeout_seconds=gpu_noise_ready_timeout_seconds,
                    cpu_affinity=gpu_noise_cpu_affinity,
                    vulkan_icd=vulkan_icd,
                ),
                REPO_ROOT,
            ),
        )

    @property
    def bench_src_path(self) -> Path:
        return TOOL_DIR

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return [
            "case_id",
            "policy",
            "cpu_noise",
            "memory_noise",
            "gpu_noise",
            "usb_storage_noise",
        ]

    def prebuild_bench(self, **_kwargs: Any) -> int:
        validate_trace_environment(lime=self._lime, use_lime=self._use_lime)
        if self._drop_caches_before_run:
            self._memory_cleanup_hook.validate()
        self._noise_suite.validate_environment()
        if self._recover_on_failure == "full-reset":
            self._recovery.validate_environment()
        self._system_controls.validate_environment()
        if self._overrun_kernel_trace:
            if not self._use_sudo:
                raise RuntimeError("--overrun-kernel-trace requires sudo")
            if shutil.which("trace-cmd") is None:
                raise RuntimeError("--overrun-kernel-trace requires trace-cmd")
        if self._freshness_kernel_trace:
            if not self._use_sudo:
                raise RuntimeError("--freshness-kernel-trace requires sudo")
            if shutil.which("trace-cmd") is None:
                raise RuntimeError("--freshness-kernel-trace requires trace-cmd")
        if self._overrun_kernel_trace and self._freshness_kernel_trace:
            raise RuntimeError(
                "--overrun-kernel-trace and --freshness-kernel-trace "
                "cannot wrap the same process simultaneously"
            )

        subprocess.check_call(
            [
                "cmake",
                "-S",
                str(REPO_ROOT),
                "-B",
                str(self._build_dir),
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                f"-DFORCE_RSUSB_BACKEND={'ON' if self._system_controls.config.rsusb_backend else 'OFF'}",
                f"-DRS_CAMERA_BUILD_GPU_NOISE={'ON' if self._noise_suite.gpu_enabled else 'OFF'}",
                f"-DRS_CAMERA_V4L2_DIAGNOSTICS={'ON' if self._v4l2_diagnostics_build else 'OFF'}",
            ]
        )
        build_targets = [
            "realsense_steady_probe",
            "d435_sensor_probe",
            *self._noise_suite.build_targets(),
        ]
        subprocess.check_call(
            [
                "cmake",
                "--build",
                str(self._build_dir),
                "--target",
                *build_targets,
                "--parallel",
                str(self._build_jobs),
            ]
        )
        build_pthread_tracer(output=self._tracer, source=TRACER_SOURCE)
        return 0

    def build_bench(self, **_kwargs: Any) -> None:
        return None

    def _scheduled_probe(
        self,
        case: Dict[str, Any],
        policy: str,
        summary_path: Path,
        events_path: Path,
        scheduler_profile_path: Path | None = None,
        transition_arguments: List[str] | None = None,
    ) -> List[str]:
        command = scheduler_prefix(policy, self._priority)

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
            ("measurement_duration_ms", "--measurement-duration-ms"),
            ("warmup_frames", "--warmup-frames"),
            ("deadline_apply_after_frames", "--deadline-apply-after-frames"),
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
        if policy in MODELED_POLICIES:
            profile = scheduler_profile_path or Path(str(probe["deadline_profile"]))
        if policy == "deadline":
            command += ["--deadline-profile", str(profile)]
            if probe.get("deadline_allow_partial_profile", False):
                command.append("--deadline-allow-partial-profile")
        elif policy in {"rr-rm", "fifo-rm"}:
            command += [
                "--rate-monotonic-profile",
                str(profile),
                "--rate-monotonic-policy",
                "rr" if policy == "rr-rm" else "fifo",
                "--rate-monotonic-highest-priority",
                str(self._priority),
            ]
        command += transition_arguments or []
        command += [
            "--summary-output",
            str(summary_path),
            "--events-output",
            str(events_path),
        ]
        return command

    def _drop_caches_for_attempt(
        self,
        attempt_dir: Path,
        case_id: str,
        policy: str,
        noise_modes: Dict[str, str],
    ) -> None:
        if not self._drop_caches_before_run:
            return
        self._memory_cleanup_hook(
            build_variables={},
            run_variables={
                "case_id": case_id,
                "policy": policy,
                **noise_modes,
            },
            other_variables={},
            record_data_dir=attempt_dir,
        )

    def _run_attempt(
        self,
        *,
        case_id: str,
        case: Dict[str, Any],
        policy: str,
        attempt: int,
        attempt_dir: Path,
        base_manifest: Dict[str, Any],
        noise_modes: Dict[str, str],
        kwargs: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        attempt_dir.mkdir(parents=True, exist_ok=False)
        self._system_controls.prepare_attempt(attempt, attempt_dir)
        self._drop_caches_for_attempt(attempt_dir, case_id, policy, noise_modes)

        summary_path = attempt_dir / "steady_summary.json"
        events_path = attempt_dir / "frame_events.csv"
        lifecycle_path = attempt_dir / "thread_lifecycle.jsonl"
        v4l2_diagnostic_path = attempt_dir / "v4l2_diagnostic_trace.bin"
        kernel_trace_path = attempt_dir / "overrun_kernel_trace.dat"
        freshness_kernel_trace_path = (
            attempt_dir / "freshness_kernel_trace.dat"
        )
        lime_dir = attempt_dir / "lime_trace"
        stdout_path = attempt_dir / "probe_stdout.txt"
        before = attempt_dir / "topology_before.json"
        after = attempt_dir / "topology_after.json"
        self._system_controls.snapshot_topology(before)
        noise_transition = NoiseTransition(
            noise_suite=self._noise_suite,
            modes=noise_modes,
            record_dir=attempt_dir,
        )

        scheduler_profile_copy = None
        scheduler_profile_sha256 = ""
        if policy in MODELED_POLICIES:
            scheduler_profile_source = Path(
                str(case.get("probe", {})["deadline_profile"])
            ).resolve()
            profile_name = (
                "deadline_profile.csv"
                if policy == "deadline"
                else "rate_monotonic_profile.csv"
            )
            scheduler_profile_copy = attempt_dir / profile_name
            shutil.copy2(scheduler_profile_source, scheduler_profile_copy)
            scheduler_profile_sha256 = hashlib.sha256(
                scheduler_profile_copy.read_bytes()
            ).hexdigest()
            metadata_source = scheduler_profile_source.with_suffix(
                scheduler_profile_source.suffix + ".json"
            )
            if metadata_source.is_file():
                shutil.copy2(
                    metadata_source,
                    attempt_dir / f"{profile_name}.json",
                )

        command = traced_command(
            scheduled_command=self._scheduled_probe(
                case,
                policy,
                summary_path,
                events_path,
                scheduler_profile_path=scheduler_profile_copy,
                transition_arguments=noise_transition.probe_arguments(),
            ),
            tracer=self._tracer,
            lifecycle_path=lifecycle_path,
            lime=self._lime,
            lime_dir=lime_dir,
            use_lime=self._use_lime,
            use_sudo=self._use_sudo,
            target_environment=(
                {
                    "RS_V4L2_DIAGNOSTIC_TRACE_FILE": str(v4l2_diagnostic_path),
                    "RS_V4L2_DIAGNOSTIC_TRACE_CAPACITY": str(
                        V4L2_DIAGNOSTIC_TRACE_CAPACITY
                    ),
                }
                if self._v4l2_diagnostics
                else None
            ),
        )
        if self._overrun_kernel_trace:
            command = [
                "sudo",
                str(OVERRUN_KERNEL_TRACE),
                str(kernel_trace_path),
                *command,
            ]
        elif self._freshness_kernel_trace:
            command = [
                "sudo",
                str(FRESHNESS_KERNEL_TRACE),
                str(freshness_kernel_trace_path),
                *command,
            ]

        attempt_manifest = {
            **base_manifest,
            "attempt": attempt,
            "command": command,
            "record_data_dir": str(attempt_dir),
            "scheduler_profile_copy": (
                str(scheduler_profile_copy) if scheduler_profile_copy else ""
            ),
            "scheduler_profile_sha256": scheduler_profile_sha256,
            "deadline_profile_copy": (
                str(scheduler_profile_copy) if policy == "deadline" else ""
            ),
            "deadline_profile_sha256": (
                scheduler_profile_sha256 if policy == "deadline" else ""
            ),
            "noise_after_camera_warmup": noise_transition.enabled,
            "measurement_gate_timeout_ms": (
                noise_transition.gate_timeout_ms if noise_transition.enabled else 0
            ),
        }
        (attempt_dir / "attempt_manifest.json").write_text(
            json.dumps(attempt_manifest, indent=2, sort_keys=True) + "\n",
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
        measurement_duration = int(probe.get("measurement_duration_ms", 0)) / 1000
        if measurement_duration > 0:
            timeout = int(measurement_duration + 90)
        else:
            automatic_seconds = frames / max(1, fps) * 2 + 90
            measurement_timeout = int(probe.get("measurement_timeout_ms", 0)) / 1000
            timeout = max(int(automatic_seconds), int(measurement_timeout + 60))
        if noise_transition.enabled:
            timeout += int(noise_transition.gate_timeout_ms / 1000) + 5
        kernel_before, kernel_error = self._system_controls.kernel_log()
        output = ""
        try:
            noise_transition.start()
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
            noise_transition.finish()
            self._noise_suite.stop_all(attempt_dir)
            self._system_controls.capture_kernel_delta(
                attempt_dir, kernel_before, kernel_error
            )
            self._system_controls.snapshot_topology(after)
            self._system_controls.verify_cpu_isolation(
                attempt_dir / "cpu_isolation_after.json"
            )

        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        else:
            summary = {
                "schema_version": 1,
                "success": False,
                "error": "steady probe summary file missing",
                "measurement": {
                    "start_boottime_ns": 0,
                    "end_boottime_ns": 0,
                    "duration_ms": 0.0,
                },
                "cameras": camera_descriptors(case, {}),
                "aggregate": {},
            }
            summary_path.write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        if self._use_lime:
            try:
                parse_steady_trace(lifecycle_path, lime_dir, attempt_dir)
            except Exception as error:
                (attempt_dir / "trace_parse_error.txt").write_text(
                    f"{type(error).__name__}: {error}\n",
                    encoding="utf-8",
                )
                if summary.get("success"):
                    raise
        if self._v4l2_diagnostics and v4l2_diagnostic_path.is_file():
            try:
                parse_v4l2_diagnostic_trace(
                    v4l2_diagnostic_path,
                    lifecycle_path,
                    attempt_dir,
                    compact_output=self._freshness_kernel_trace,
                )
            except Exception as error:
                (attempt_dir / "v4l2_diagnostic_parse_error.txt").write_text(
                    f"{type(error).__name__}: {error}\n", encoding="utf-8"
                )
                if summary.get("success"):
                    raise
        activation_path = attempt_dir / "thread_steady_activations.csv"
        if (
            self._overrun_kernel_trace
            and kernel_trace_path.is_file()
            and activation_path.is_file()
        ):
            try:
                parse_overrun_kernel_trace(
                    kernel_trace_path,
                    lifecycle_path,
                    activation_path,
                    attempt_dir,
                )
            except Exception as error:
                (attempt_dir / "overrun_kernel_trace_parse_error.txt").write_text(
                    f"{type(error).__name__}: {error}\n", encoding="utf-8"
                )
                if summary.get("success"):
                    raise
        if (
            self._freshness_kernel_trace
            and freshness_kernel_trace_path.is_file()
        ):
            try:
                parse_freshness_kernel_trace(
                    freshness_kernel_trace_path,
                    lifecycle_path,
                    attempt_dir,
                )
            except Exception as error:
                (
                    attempt_dir / "freshness_kernel_trace_parse_error.txt"
                ).write_text(
                    f"{type(error).__name__}: {error}\n", encoding="utf-8"
                )
                if summary.get("success"):
                    raise
        return output, summary

    def single_run(
        self,
        case_id: str,
        policy: str,
        cpu_noise: str,
        memory_noise: str,
        gpu_noise: str,
        usb_storage_noise: str,
        record_data_dir: Path,
        **kwargs: Any,
    ) -> str:
        case = self._cases[case_id]
        record_dir = Path(record_data_dir).resolve()
        record_dir.mkdir(parents=True, exist_ok=True)
        if any(record_dir.glob("attempt-*")):
            raise RuntimeError(f"Attempt directories already exist in {record_dir}")
        self._system_controls.prepare_campaign(record_dir)

        noise_modes = {
            "cpu_noise": cpu_noise,
            "memory_noise": memory_noise,
            "usb_storage_noise": usb_storage_noise,
            "gpu_noise": gpu_noise,
        }
        (record_dir / "case.json").write_text(
            json.dumps(case, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        base_manifest: Dict[str, Any] = {
            "schema_version": 2,
            "case_id": case_id,
            "policy_requested": POLICY_NAMES[policy],
            "priority_requested": (
                0 if policy in {"other", "deadline"} else self._priority
            ),
            "backend": self._system_controls.backend_name,
            "usb_kernel_driver": CAMPAIGN_USB_KERNEL_DRIVER,
            "realsense_usb_autosuspend_disabled": (
                self._system_controls.config.disable_realsense_autosuspend
            ),
            "lime_enabled": self._use_lime,
            "cpu_frequency_mhz": self._system_controls.config.cpu_frequency_mhz,
            "cpu_isolation": self._system_controls.cpu_isolation_state(),
            "drop_caches_before_attempt": self._drop_caches_before_run,
            "recover_on_failure": self._recover_on_failure,
            "recovery_reset_timeout_ms": self._recovery.config.reset_timeout_ms,
            "recovery_wait_seconds": (
                self._recovery.config.enumeration_timeout_seconds
            ),
            "recovery_settle_seconds": self._recovery_settle_seconds,
            "max_attempts_per_run": self._max_attempts_per_run,
            **self._noise_suite.manifest(noise_modes),
            "clock": "CLOCK_BOOTTIME",
            "deadline_profile_source": (
                str(case.get("probe", {}).get("deadline_profile", ""))
                if policy == "deadline"
                else ""
            ),
            "scheduler_profile_source": (
                str(case.get("probe", {}).get("deadline_profile", ""))
                if policy in MODELED_POLICIES
                else ""
            ),
            "rate_monotonic_highest_priority": (
                self._priority if policy in {"rr-rm", "fifo-rm"} else 0
            ),
        }
        (record_dir / "run_manifest.json").write_text(
            json.dumps(base_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        def run_attempt(
            attempt: int, attempt_dir: Path
        ) -> tuple[str, Dict[str, Any]]:
            return self._run_attempt(
                case_id=case_id,
                case=case,
                policy=policy,
                attempt=attempt,
                attempt_dir=attempt_dir,
                base_manifest=base_manifest,
                noise_modes=noise_modes,
                kwargs=kwargs,
            )

        return run_steady_attempts(
            case=case,
            record_dir=record_dir,
            base_manifest=base_manifest,
            recover_on_failure=self._recover_on_failure,
            recovery_settle_seconds=self._recovery_settle_seconds,
            max_attempts_per_run=self._max_attempts_per_run,
            recovery=self._recovery,
            run_attempt=run_attempt,
        )

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
        result = parse_steady_results(
            record_dir=record_dir,
            case=self._cases[run_variables["case_id"]],
            run_variables=run_variables,
            backend_name=self._system_controls.backend_name,
            policy_names=POLICY_NAMES,
            drop_caches_configured=self._drop_caches_before_run,
            noise_suite=self._noise_suite,
            cpu_isolation_state=self._system_controls.cpu_isolation_state(),
        )
        if not bool(result.get("success", False)):
            self._logical_failures.append(
                f"{run_variables['case_id']}/{run_variables['policy']}/"
                f"cpu={run_variables['cpu_noise']}/"
                f"memory={run_variables['memory_noise']}/"
                f"gpu={run_variables['gpu_noise']}/"
                f"usb={run_variables['usb_storage_noise']}: "
                f"{result.get('error', 'unknown error')}"
            )
        return result

    def assert_all_runs_successful(self) -> None:
        if self._logical_failures:
            preview = " | ".join(self._logical_failures[:5])
            if len(self._logical_failures) > 5:
                preview += f" | ... {len(self._logical_failures) - 5} more"
            raise RuntimeError(
                f"{len(self._logical_failures)} logical benchmark run(s) failed: "
                + preview
            )

    def cleanup(self) -> None:
        """Stop any surviving workloads and restore host-wide controls."""
        errors = []
        for description, cleanup in (
            ("stop noise workloads", self._noise_suite.stop_all),
            ("restore system controls", self._system_controls.cleanup),
        ):
            try:
                cleanup()
            except Exception as error:
                errors.append(f"{description}: {error}")
        if errors:
            raise RuntimeError("Campaign cleanup failed: " + " | ".join(errors))
