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
from parse_steady_trace import parse_steady_trace
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
    POLICY_NAMES,
    REPO_ROOT,
    RSUSB_HELPER,
    TOOL_DIR,
    TRACER_SOURCE,
)


class RealSenseSteadyBench(Benchmark):
    def __init__(
        self,
        cases: Iterable[Dict[str, Any]],
        build_dir: Path,
        lime: Path,
        use_lime: bool,
        use_sudo: bool,
        cpu_noise_modes: List[str],
        cpu_noise_workers: int,
        cpu_noise_warmup_seconds: float,
        cpu_noise_ready_timeout_seconds: float,
        cpu_noise_cpu_affinity: str | None,
        memory_noise_modes: List[str],
        memory_noise_workers: int,
        memory_noise_buffer_size_mib: int,
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
        self._use_sudo = use_sudo
        self._drop_caches_before_run = CAMPAIGN_DROP_CACHES_BEFORE_RUN
        self._memory_cleanup_hook = memory_cleanup_hook
        self._build_jobs = build_jobs
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
        deadline_profile_path: Path | None = None,
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
        if policy == "deadline":
            profile = deadline_profile_path or Path(str(probe["deadline_profile"]))
            command += ["--deadline-profile", str(profile)]
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
        self._drop_caches_for_attempt(attempt_dir, case_id, policy, noise_modes)

        summary_path = attempt_dir / "steady_summary.json"
        events_path = attempt_dir / "frame_events.csv"
        lifecycle_path = attempt_dir / "thread_lifecycle.jsonl"
        lime_dir = attempt_dir / "lime_trace"
        stdout_path = attempt_dir / "probe_stdout.txt"
        before = attempt_dir / "topology_before.json"
        after = attempt_dir / "topology_after.json"
        self._system_controls.snapshot_topology(before)

        deadline_profile_copy = None
        deadline_profile_sha256 = ""
        if policy == "deadline":
            deadline_profile_source = Path(
                str(case.get("probe", {})["deadline_profile"])
            ).resolve()
            deadline_profile_copy = attempt_dir / "deadline_profile.csv"
            shutil.copy2(deadline_profile_source, deadline_profile_copy)
            deadline_profile_sha256 = hashlib.sha256(
                deadline_profile_copy.read_bytes()
            ).hexdigest()
            metadata_source = deadline_profile_source.with_suffix(
                deadline_profile_source.suffix + ".json"
            )
            if metadata_source.is_file():
                shutil.copy2(
                    metadata_source,
                    attempt_dir / "deadline_profile.csv.json",
                )

        command = traced_command(
            scheduled_command=self._scheduled_probe(
                case,
                policy,
                summary_path,
                events_path,
                deadline_profile_path=deadline_profile_copy,
            ),
            tracer=self._tracer,
            lifecycle_path=lifecycle_path,
            lime=self._lime,
            lime_dir=lime_dir,
            use_lime=self._use_lime,
            use_sudo=self._use_sudo,
        )

        attempt_manifest = {
            **base_manifest,
            "attempt": attempt,
            "command": command,
            "record_data_dir": str(attempt_dir),
            "deadline_profile_copy": (
                str(deadline_profile_copy) if deadline_profile_copy else ""
            ),
            "deadline_profile_sha256": deadline_profile_sha256,
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
        automatic_seconds = frames / max(1, fps) * 2 + 90
        measurement_timeout = int(probe.get("measurement_timeout_ms", 0)) / 1000
        timeout = max(int(automatic_seconds), int(measurement_timeout + 60))
        kernel_before, kernel_error = self._system_controls.kernel_log()
        output = ""
        try:
            self._noise_suite.start_all(noise_modes, attempt_dir)
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
            self._noise_suite.stop_all(attempt_dir)
            self._system_controls.capture_kernel_delta(
                attempt_dir, kernel_before, kernel_error
            )
            self._system_controls.snapshot_topology(after)

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
            "lime_enabled": self._use_lime,
            "cpu_frequency_mhz": self._system_controls.config.cpu_frequency_mhz,
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
        return parse_steady_results(
            record_dir=record_dir,
            case=self._cases[run_variables["case_id"]],
            run_variables=run_variables,
            backend_name=self._system_controls.backend_name,
            policy_names=POLICY_NAMES,
            drop_caches_configured=self._drop_caches_before_run,
            noise_suite=self._noise_suite,
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
