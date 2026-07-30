"""Managed interference workloads for the RealSense steady-state campaign."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Any, Dict, Iterable, List, Mapping


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class CpuNoiseConfig:
    executable: Path
    modes: tuple[str, ...]
    workers: int
    warmup_seconds: float
    ready_timeout_seconds: float
    cpu_affinity: str | None


@dataclass(frozen=True)
class MemoryNoiseConfig:
    executable: Path
    modes: tuple[str, ...]
    workers: int
    buffer_size_mib: int
    warmup_seconds: float
    ready_timeout_seconds: float
    cpu_affinity: str | None


@dataclass(frozen=True)
class UsbStorageNoiseConfig:
    executable: Path
    modes: tuple[str, ...]
    device: Path | None
    warmup_seconds: float
    block_size_kib: int
    ready_timeout_seconds: float
    use_sudo: bool


@dataclass(frozen=True)
class GpuNoiseConfig:
    executable: Path
    modes: tuple[str, ...]
    model_param: Path
    gpu_device: int
    warmup_iterations: int
    ready_timeout_seconds: float
    cpu_affinity: str | None
    vulkan_icd: Path | None


@dataclass(frozen=True)
class NoiseArtifacts:
    prefix: str
    mode: str
    ready: Dict[str, Any]
    summary: Dict[str, Any]
    process: Dict[str, Any]
    valid: bool
    error: str
    extra_result_fields: Dict[str, Any] = field(default_factory=dict)


class ManagedNoiseProcess:
    """Own one noise subprocess and its common ready/stop artifact lifecycle."""

    prefix = ""
    label = ""
    supported_mode = ""
    build_target = ""

    def __init__(
        self,
        *,
        modes: Iterable[str],
        ready_timeout_seconds: float,
        repo_root: Path,
    ) -> None:
        self._modes = tuple(modes)
        self._ready_timeout_seconds = ready_timeout_seconds
        self._repo_root = repo_root
        self._process: subprocess.Popen[str] | None = None

    @property
    def enabled(self) -> bool:
        return any(mode != "none" for mode in self._modes)

    def validate_environment(self) -> None:
        return None

    def manifest(self, mode: str) -> Dict[str, Any]:
        raise NotImplementedError

    def _command(self, ready_path: Path, summary_path: Path) -> List[str]:
        raise NotImplementedError

    def _environment(self) -> Dict[str, str] | None:
        return None

    def _before_start(self) -> None:
        return None

    def _decorate_ready(
        self,
        ready: Dict[str, Any],
        command: List[str],
    ) -> Dict[str, Any]:
        ready["enabled"] = True
        ready["command"] = command
        return ready

    def _ready_message(self, ready: Mapping[str, Any]) -> str:
        return "ready"

    def _summary_valid(self, summary: Mapping[str, Any]) -> bool:
        return bool(summary.get("success"))

    def _extra_result_fields(self, mode: str) -> Dict[str, Any]:
        del mode
        return {}

    def _path(self, record_dir: Path, suffix: str) -> Path:
        return record_dir / f"{self.prefix}_{suffix}"

    def _write_process(
        self,
        record_dir: Path,
        *,
        returncode: int,
        forced_kill: bool,
    ) -> None:
        self._path(record_dir, "process.json").write_text(
            json.dumps(
                {"returncode": returncode, "forced_kill": forced_kill},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def start(self, mode: str, record_dir: Path) -> Dict[str, Any]:
        if mode == "none":
            return {"mode": "none", "enabled": False}
        if mode != self.supported_mode:
            raise ValueError(f"Unsupported {self.label} mode: {mode}")
        if self._process is not None:
            raise RuntimeError(f"A {self.label} process is already running")

        self._before_start()
        ready_path = self._path(record_dir, "ready.json")
        summary_path = self._path(record_dir, "summary.json")
        stdout_path = self._path(record_dir, "stdout.txt")
        stderr_path = self._path(record_dir, "stderr.txt")
        command = self._command(ready_path, summary_path)
        started = time.monotonic()
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_file:
            self._process = subprocess.Popen(
                command,
                cwd=self._repo_root,
                env=self._environment(),
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
            )

        deadline = started + self._ready_timeout_seconds
        while time.monotonic() < deadline:
            assert self._process is not None
            returncode = self._process.poll()
            if returncode is not None:
                self._process = None
                self._write_process(
                    record_dir,
                    returncode=returncode,
                    forced_kill=False,
                )
                detail = stderr_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                raise RuntimeError(
                    f"{self.label} exited before ready with code {returncode}: {detail}"
                )
            if ready_path.is_file():
                try:
                    ready = _read_json(ready_path)
                except json.JSONDecodeError:
                    time.sleep(0.01)
                    continue
                if ready.get("ready"):
                    ready = self._decorate_ready(ready, command)
                    print(f"[{self.label.upper()}] {self._ready_message(ready)}")
                    return ready
            time.sleep(0.05)

        self.stop(record_dir)
        raise RuntimeError(
            f"{self.label} did not become ready within "
            f"{self._ready_timeout_seconds:.1f} seconds"
        )

    def stop(self, record_dir: Path | None = None) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        forced = False
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forced = True
                process.kill()
                process.wait(timeout=5)
        if record_dir is not None:
            self._write_process(
                record_dir,
                returncode=process.returncode,
                forced_kill=forced,
            )
        print(f"[{self.label.upper()}] stopped with code {process.returncode}")

    def artifacts(self, mode: str, record_dir: Path) -> NoiseArtifacts:
        ready = _read_json(self._path(record_dir, "ready.json"))
        summary = _read_json(self._path(record_dir, "summary.json"))
        process = _read_json(self._path(record_dir, "process.json"))
        valid = mode == "none" or (
            bool(ready.get("ready"))
            and self._summary_valid(summary)
            and process.get("returncode") == 0
            and not process.get("forced_kill", False)
        )
        return NoiseArtifacts(
            prefix=self.prefix,
            mode=mode,
            ready=ready,
            summary=summary,
            process=process,
            valid=valid,
            error=f"invalid {self.label} process",
            extra_result_fields=self._extra_result_fields(mode),
        )


class CpuBusyLoopNoise(ManagedNoiseProcess):
    prefix = "cpu_noise"
    label = "CPU-NOISE"
    supported_mode = "busy_loop"
    build_target = "realsense_cpu_noise"

    def __init__(self, config: CpuNoiseConfig, repo_root: Path) -> None:
        super().__init__(
            modes=config.modes,
            ready_timeout_seconds=config.ready_timeout_seconds,
            repo_root=repo_root,
        )
        self.config = config

    def validate_environment(self) -> None:
        if self.enabled and self.config.cpu_affinity and shutil.which("taskset") is None:
            raise RuntimeError("taskset is required for --cpu-noise-cpu-affinity")

    def manifest(self, mode: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "workers": self.config.workers,
            "warmup_seconds": self.config.warmup_seconds,
            "cpu_affinity": self.config.cpu_affinity,
            "working_set": "register_only",
            "process_policy": "SCHED_OTHER",
        }

    def _command(self, ready_path: Path, summary_path: Path) -> List[str]:
        command = ["chrt", "--other", "0"]
        if self.config.cpu_affinity:
            command += ["taskset", "--cpu-list", self.config.cpu_affinity]
        command += [
            str(self.config.executable),
            "--ready-file",
            str(ready_path),
            "--summary-output",
            str(summary_path),
            "--workers",
            str(self.config.workers),
            "--warmup-seconds",
            str(self.config.warmup_seconds),
        ]
        return command

    def _decorate_ready(
        self,
        ready: Dict[str, Any],
        command: List[str],
    ) -> Dict[str, Any]:
        ready = super()._decorate_ready(ready, command)
        ready["cpu_affinity"] = self.config.cpu_affinity or ""
        return ready

    def _ready_message(self, ready: Mapping[str, Any]) -> str:
        return (
            f"{ready.get('workers', 0)} workers ready at "
            f"{ready.get('warmup_cpu_equivalents', 0):.2f} CPU equivalents"
        )

    def _summary_valid(self, summary: Mapping[str, Any]) -> bool:
        return (
            bool(summary.get("success"))
            and summary.get("working_set") == "register_only"
            and summary.get("workers") == self.config.workers
            and float(summary.get("measurement_cpu_equivalents", 0)) > 0.0
        )


class FixedCopyMemoryNoise(ManagedNoiseProcess):
    prefix = "memory_noise"
    label = "MEMORY-NOISE"
    supported_mode = "fixed_copy"
    build_target = "realsense_memory_noise"

    def __init__(self, config: MemoryNoiseConfig, repo_root: Path) -> None:
        super().__init__(
            modes=config.modes,
            ready_timeout_seconds=config.ready_timeout_seconds,
            repo_root=repo_root,
        )
        self.config = config

    def validate_environment(self) -> None:
        if self.enabled and self.config.cpu_affinity and shutil.which("taskset") is None:
            raise RuntimeError("taskset is required for --memory-noise-cpu-affinity")

    def manifest(self, mode: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "workers": self.config.workers,
            "buffer_size_mib": self.config.buffer_size_mib,
            "buffers_per_worker": 2,
            "warmup_seconds": self.config.warmup_seconds,
            "cpu_affinity": self.config.cpu_affinity,
            "memory_access": "thread_private_memcpy_read_write",
            "process_policy": "SCHED_OTHER",
        }

    def _command(self, ready_path: Path, summary_path: Path) -> List[str]:
        command = ["chrt", "--other", "0"]
        if self.config.cpu_affinity:
            command += ["taskset", "--cpu-list", self.config.cpu_affinity]
        command += [
            str(self.config.executable),
            "--ready-file",
            str(ready_path),
            "--summary-output",
            str(summary_path),
            "--workers",
            str(self.config.workers),
            "--buffer-size-mib",
            str(self.config.buffer_size_mib),
            "--warmup-seconds",
            str(self.config.warmup_seconds),
        ]
        return command

    def _decorate_ready(
        self,
        ready: Dict[str, Any],
        command: List[str],
    ) -> Dict[str, Any]:
        ready = super()._decorate_ready(ready, command)
        ready["cpu_affinity"] = self.config.cpu_affinity or ""
        return ready

    def _ready_message(self, ready: Mapping[str, Any]) -> str:
        return (
            f"{ready.get('workers', 0)} workers ready at "
            f"{ready.get('warmup_estimated_memory_mib_per_second', 0):.1f} "
            "MiB/s estimated read+write traffic"
        )

    def _summary_valid(self, summary: Mapping[str, Any]) -> bool:
        return (
            bool(summary.get("success"))
            and summary.get("memory_access")
            == "thread_private_memcpy_read_write"
            and summary.get("workers") == self.config.workers
            and summary.get("buffer_size_mib") == self.config.buffer_size_mib
            and int(summary.get("payload_bytes_copied", 0)) > 0
            and float(summary.get("estimated_memory_mib_per_second", 0)) > 0.0
        )


class UsbStorageReadNoise(ManagedNoiseProcess):
    prefix = "usb_storage_noise"
    label = "USB-STORAGE-NOISE"
    supported_mode = "sequential_read"
    build_target = "realsense_usb_storage_noise"

    def __init__(self, config: UsbStorageNoiseConfig, repo_root: Path) -> None:
        super().__init__(
            modes=config.modes,
            ready_timeout_seconds=config.ready_timeout_seconds,
            repo_root=repo_root,
        )
        self.config = config
        self.identity: Dict[str, Any] = {}

    def validate_environment(self) -> None:
        if self.enabled:
            self.identity = self._validate_device()

    def manifest(self, mode: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "device": str(self.config.device) if self.config.device else None,
            "warmup_seconds": self.config.warmup_seconds,
            "block_size_kib": self.config.block_size_kib,
            "identity": self.identity,
            "access": "read_only",
            "direct_io": True,
        }

    def _before_start(self) -> None:
        self.identity = self._validate_device()

    def _validate_device(self) -> Dict[str, Any]:
        if self.config.device is None:
            raise RuntimeError(
                "--usb-storage-device is required when sequential-read noise is enabled"
            )
        for utility in ("lsblk", "udevadm"):
            if shutil.which(utility) is None:
                raise RuntimeError(f"{utility} is required for USB storage safety checks")

        requested = str(self.config.device)
        try:
            resolved = self.config.device.resolve(strict=True)
        except FileNotFoundError as error:
            raise RuntimeError(f"USB storage device does not exist: {requested}") from error
        device_stat = os.stat(resolved)
        if not stat.S_ISBLK(device_stat.st_mode):
            raise RuntimeError(f"USB storage noise requires a block device: {resolved}")

        completed = subprocess.run(
            [
                "lsblk",
                "--json",
                "--bytes",
                "--paths",
                "--output",
                "NAME,TYPE,TRAN,SIZE,RO,RM,MOUNTPOINTS,MODEL,SERIAL,VENDOR",
                str(resolved),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "lsblk failed for USB storage device: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        block_devices = json.loads(completed.stdout).get("blockdevices", [])
        if len(block_devices) != 1 or block_devices[0].get("type") != "disk":
            raise RuntimeError(
                "USB storage noise requires a whole disk, preferably /dev/disk/by-id/..."
            )
        root = block_devices[0]
        all_nodes = []
        pending = [root]
        while pending:
            node = pending.pop()
            all_nodes.append(node)
            pending.extend(node.get("children", []))
        mounted = []
        for node in all_nodes:
            mountpoints = node.get("mountpoints") or []
            if isinstance(mountpoints, str):
                mountpoints = [mountpoints]
            for mountpoint in mountpoints:
                if mountpoint:
                    mounted.append(f"{node.get('name')}:{mountpoint}")
        if mounted:
            raise RuntimeError(
                "refusing mounted USB storage device: " + ", ".join(mounted)
            )

        udev = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={resolved}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if udev.returncode != 0:
            raise RuntimeError(
                "udevadm failed for USB storage device: "
                + (udev.stderr.strip() or udev.stdout.strip())
            )
        properties = {
            key: value
            for line in udev.stdout.splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
        if root.get("tran") != "usb" and properties.get("ID_BUS") != "usb":
            raise RuntimeError(f"refusing non-USB block device: {resolved}")

        major = os.major(device_stat.st_rdev)
        minor = os.minor(device_stat.st_rdev)
        sysfs_path = Path(f"/sys/dev/block/{major}:{minor}").resolve()
        return {
            "requested_device": requested,
            "resolved_device": str(resolved),
            "major": major,
            "minor": minor,
            "size_bytes": root.get("size", 0),
            "transport": root.get("tran", ""),
            "model": root.get("model", ""),
            "serial": root.get("serial", ""),
            "vendor": root.get("vendor", ""),
            "removable": root.get("rm", False),
            "read_only_device": root.get("ro", False),
            "id_bus": properties.get("ID_BUS", ""),
            "id_path": properties.get("ID_PATH", ""),
            "id_serial": properties.get("ID_SERIAL", ""),
            "sysfs_path": str(sysfs_path),
        }

    def _command(self, ready_path: Path, summary_path: Path) -> List[str]:
        assert self.config.device is not None
        command = [
            "chrt",
            "--other",
            "0",
            str(self.config.executable),
            "--device",
            str(self.config.device),
            "--ready-file",
            str(ready_path),
            "--summary-output",
            str(summary_path),
            "--block-size-kib",
            str(self.config.block_size_kib),
            "--warmup-seconds",
            str(self.config.warmup_seconds),
        ]
        if self.config.use_sudo:
            command = [
                "sudo",
                "--non-interactive",
                *command,
                "--drop-uid",
                str(os.getuid()),
                "--drop-gid",
                str(os.getgid()),
            ]
        return command

    def _decorate_ready(
        self,
        ready: Dict[str, Any],
        command: List[str],
    ) -> Dict[str, Any]:
        ready = super()._decorate_ready(ready, command)
        ready["identity"] = self.identity
        return ready

    def _ready_message(self, ready: Mapping[str, Any]) -> str:
        return f"ready at {ready.get('warmup_mib_per_second', 0):.1f} MiB/s"

    def _summary_valid(self, summary: Mapping[str, Any]) -> bool:
        return (
            bool(summary.get("success"))
            and summary.get("access") == "read_only"
            and bool(summary.get("direct_io"))
        )


class GpuVulkanNoise(ManagedNoiseProcess):
    prefix = "gpu_noise"
    label = "GPU-NOISE"
    supported_mode = "mobilenet_v2_vulkan"
    build_target = "realsense_gpu_noise"

    def __init__(self, config: GpuNoiseConfig, repo_root: Path) -> None:
        super().__init__(
            modes=config.modes,
            ready_timeout_seconds=config.ready_timeout_seconds,
            repo_root=repo_root,
        )
        self.config = config

    def validate_environment(self) -> None:
        if not self.enabled:
            return
        if not self.config.model_param.is_file():
            raise RuntimeError(
                "The pinned ncnn MobileNetV2 graph is missing; initialize deps/ncnn recursively"
            )
        if self.config.vulkan_icd and not self.config.vulkan_icd.is_file():
            raise RuntimeError(
                f"Vulkan ICD file does not exist: {self.config.vulkan_icd}"
            )
        if self.config.cpu_affinity and shutil.which("taskset") is None:
            raise RuntimeError("taskset is required for --gpu-noise-cpu-affinity")

    def manifest(self, mode: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "gpu_device": self.config.gpu_device,
            "warmup_iterations": self.config.warmup_iterations,
            "cpu_affinity": self.config.cpu_affinity,
            "vulkan_icd": (
                str(self.config.vulkan_icd) if self.config.vulkan_icd else None
            ),
            "model_param": str(self.config.model_param),
            "model_param_sha256": (
                sha256_file(self.config.model_param) if mode != "none" else None
            ),
        }

    def _command(self, ready_path: Path, summary_path: Path) -> List[str]:
        command = ["chrt", "--other", "0"]
        if self.config.cpu_affinity:
            command += ["taskset", "--cpu-list", self.config.cpu_affinity]
        command += [
            str(self.config.executable),
            "--model-param",
            str(self.config.model_param),
            "--ready-file",
            str(ready_path),
            "--summary-output",
            str(summary_path),
            "--gpu-device",
            str(self.config.gpu_device),
            "--warmup-iterations",
            str(self.config.warmup_iterations),
            "--num-threads",
            "1",
        ]
        return command

    def _environment(self) -> Dict[str, str] | None:
        environment = os.environ.copy()
        if self.config.vulkan_icd:
            environment["VK_DRIVER_FILES"] = str(self.config.vulkan_icd)
        return environment

    def _decorate_ready(
        self,
        ready: Dict[str, Any],
        command: List[str],
    ) -> Dict[str, Any]:
        ready = super()._decorate_ready(ready, command)
        ready["model_param_sha256"] = sha256_file(self.config.model_param)
        ready["vulkan_icd"] = (
            str(self.config.vulkan_icd) if self.config.vulkan_icd else ""
        )
        ready["cpu_affinity"] = self.config.cpu_affinity or ""
        return ready

    def _ready_message(self, ready: Mapping[str, Any]) -> str:
        return (
            f"ready on {ready.get('gpu_name', 'unknown')} "
            f"after {ready.get('startup_ms', 0):.1f} ms"
        )

    def _extra_result_fields(self, mode: str) -> Dict[str, Any]:
        return {
            "gpu_noise_model_param_sha256": (
                sha256_file(self.config.model_param) if mode != "none" else ""
            )
        }


class NoiseSuite:
    """Compose all noise workloads and preserve their start/stop ordering."""

    mode_keys = ("cpu_noise", "memory_noise", "usb_storage_noise", "gpu_noise")

    def __init__(
        self,
        *,
        cpu: CpuBusyLoopNoise,
        memory: FixedCopyMemoryNoise,
        usb_storage: UsbStorageReadNoise,
        gpu: GpuVulkanNoise,
    ) -> None:
        self.cpu = cpu
        self.memory = memory
        self.usb_storage = usb_storage
        self.gpu = gpu
        self._by_key: Dict[str, ManagedNoiseProcess] = {
            "cpu_noise": cpu,
            "memory_noise": memory,
            "usb_storage_noise": usb_storage,
            "gpu_noise": gpu,
        }

    def validate_environment(self) -> None:
        for workload in self._by_key.values():
            workload.validate_environment()

    @property
    def gpu_enabled(self) -> bool:
        return self.gpu.enabled

    def build_targets(self) -> List[str]:
        return [
            workload.build_target
            for workload in self._by_key.values()
            if workload.enabled
        ]

    def manifest(self, modes: Mapping[str, str]) -> Dict[str, Any]:
        return {
            key: workload.manifest(modes[key])
            for key, workload in self._by_key.items()
        }

    def start_all(
        self,
        modes: Mapping[str, str],
        record_dir: Path,
    ) -> Dict[str, Dict[str, Any]]:
        ready_values: Dict[str, Dict[str, Any]] = {}
        try:
            for key, workload in self._by_key.items():
                ready = workload.start(modes[key], record_dir)
                ready_values[key] = ready
                (record_dir / f"{workload.prefix}_configuration.json").write_text(
                    json.dumps(ready, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        except Exception:
            self.stop_all(record_dir)
            raise
        return ready_values

    def stop_all(self, record_dir: Path | None = None) -> None:
        for workload in reversed(tuple(self._by_key.values())):
            workload.stop(record_dir)

    def artifacts(
        self,
        modes: Mapping[str, str],
        record_dir: Path,
    ) -> List[NoiseArtifacts]:
        return [
            workload.artifacts(modes[key], record_dir)
            for key, workload in self._by_key.items()
        ]
