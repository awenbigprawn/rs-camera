#!/usr/bin/env python3
"""Benchkit campaign for RealSense steady-state frame acquisition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
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
NCNN_MODEL_PARAM = (
    REPO_ROOT / "deps" / "ncnn" / "benchmark" / "models" / "mobilenet_v2.param"
)
DEFAULT_BROADCOM_VULKAN_ICD = Path("/usr/share/vulkan/icd.d/broadcom_icd.json")

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
GPU_NOISE_MODES = ("none", "mobilenet_v2_vulkan")
USB_STORAGE_NOISE_MODES = ("none", "sequential_read")


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        self._build_dir = build_dir.resolve()
        self._lime = lime.resolve()
        self._priority = priority
        self._use_lime = use_lime
        self._use_sudo = use_sudo
        self._rsusb_backend = rsusb_backend
        self._rsusb_usb_devices = rsusb_usb_devices
        self._cpu_frequency_mhz = cpu_frequency_mhz
        self._gpu_noise_modes = gpu_noise_modes
        self._gpu_noise_enabled = any(mode != "none" for mode in gpu_noise_modes)
        self._gpu_noise_device = gpu_noise_device
        self._gpu_noise_warmup_iterations = gpu_noise_warmup_iterations
        self._gpu_noise_ready_timeout_seconds = gpu_noise_ready_timeout_seconds
        self._gpu_noise_cpu_affinity = gpu_noise_cpu_affinity
        self._gpu_noise_vulkan_icd = (
            gpu_noise_vulkan_icd.resolve() if gpu_noise_vulkan_icd else None
        )
        self._usb_storage_noise_modes = usb_storage_noise_modes
        self._usb_storage_noise_enabled = any(
            mode != "none" for mode in usb_storage_noise_modes
        )
        self._usb_storage_device = usb_storage_device
        self._usb_storage_warmup_seconds = usb_storage_warmup_seconds
        self._usb_storage_block_size_kib = usb_storage_block_size_kib
        self._usb_storage_ready_timeout_seconds = usb_storage_ready_timeout_seconds
        self._usb_storage_identity: Dict[str, Any] = {}
        self._build_jobs = build_jobs
        self._gpu_noise_process: subprocess.Popen[str] | None = None
        self._usb_storage_noise_process: subprocess.Popen[str] | None = None
        self._cpu_locked = False
        self._cpu_restore_needed = False
        self._rsusb_unbound = False
        self._probe = self._build_dir / "realsense_steady_probe"
        self._gpu_noise = self._build_dir / "realsense_gpu_noise"
        self._usb_storage_noise = self._build_dir / "realsense_usb_storage_noise"
        self._tracer = self._build_dir / "libtrace_pthreads.so"

    @property
    def bench_src_path(self) -> Path:
        return TOOL_DIR

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return ["case_id", "policy", "gpu_noise", "usb_storage_noise"]

    def _privileged(self, command: List[str]) -> List[str]:
        return ["sudo", "--non-interactive", *command] if self._use_sudo else command

    def _validate_usb_storage_device(self) -> Dict[str, Any]:
        if self._usb_storage_device is None:
            raise RuntimeError(
                "--usb-storage-device is required when sequential-read noise is enabled"
            )
        for utility in ("lsblk", "udevadm"):
            if shutil.which(utility) is None:
                raise RuntimeError(f"{utility} is required for USB storage safety checks")

        requested = str(self._usb_storage_device)
        try:
            resolved = self._usb_storage_device.resolve(strict=True)
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

    def prebuild_bench(self, **_kwargs: Any) -> int:
        if self._use_lime and not self._lime.is_file():
            raise RuntimeError(
                f"LiME was not found at {self._lime}. Build the unmodified dependency with: "
                "cargo build --release --manifest-path deps/lime-rtw/Cargo.toml"
            )
        if shutil.which("chrt") is None:
            raise RuntimeError("chrt is required (normally provided by util-linux).")
        if self._usb_storage_noise_enabled:
            self._usb_storage_identity = self._validate_usb_storage_device()
        if self._gpu_noise_enabled:
            if not NCNN_MODEL_PARAM.is_file():
                raise RuntimeError(
                    "The pinned ncnn MobileNetV2 graph is missing; initialize deps/ncnn recursively"
                )
            if self._gpu_noise_vulkan_icd and not self._gpu_noise_vulkan_icd.is_file():
                raise RuntimeError(
                    f"Vulkan ICD file does not exist: {self._gpu_noise_vulkan_icd}"
                )
            if self._gpu_noise_cpu_affinity and shutil.which("taskset") is None:
                raise RuntimeError("taskset is required for --gpu-noise-cpu-affinity")
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
                f"-DRS_CAMERA_BUILD_GPU_NOISE={'ON' if self._gpu_noise_enabled else 'OFF'}",
            ]
        )
        build_targets = ["realsense_steady_probe"]
        if self._gpu_noise_enabled:
            build_targets.append("realsense_gpu_noise")
        if self._usb_storage_noise_enabled:
            build_targets.append("realsense_usb_storage_noise")
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

    def _usb_storage_noise_command(
        self,
        ready_path: Path,
        summary_path: Path,
    ) -> List[str]:
        if self._usb_storage_device is None:
            raise RuntimeError("USB storage device is not configured")
        command = [
            "chrt",
            "--other",
            "0",
            str(self._usb_storage_noise),
            "--device",
            str(self._usb_storage_device),
            "--ready-file",
            str(ready_path),
            "--summary-output",
            str(summary_path),
            "--block-size-kib",
            str(self._usb_storage_block_size_kib),
            "--warmup-seconds",
            str(self._usb_storage_warmup_seconds),
        ]
        if self._use_sudo:
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

    def _start_usb_storage_noise(self, mode: str, record_dir: Path) -> Dict[str, Any]:
        if mode == "none":
            return {"mode": "none", "enabled": False}
        if mode != "sequential_read":
            raise ValueError(f"Unsupported USB storage noise mode: {mode}")
        self._usb_storage_identity = self._validate_usb_storage_device()
        if self._usb_storage_noise_process is not None:
            raise RuntimeError("A USB storage noise process is already running")

        ready_path = record_dir / "usb_storage_noise_ready.json"
        summary_path = record_dir / "usb_storage_noise_summary.json"
        stdout_path = record_dir / "usb_storage_noise_stdout.txt"
        stderr_path = record_dir / "usb_storage_noise_stderr.txt"
        process_path = record_dir / "usb_storage_noise_process.json"
        command = self._usb_storage_noise_command(ready_path, summary_path)
        started = time.monotonic()
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_file:
            self._usb_storage_noise_process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
            )

        deadline = started + self._usb_storage_ready_timeout_seconds
        while time.monotonic() < deadline:
            returncode = self._usb_storage_noise_process.poll()
            if returncode is not None:
                self._usb_storage_noise_process = None
                process_path.write_text(
                    json.dumps(
                        {"returncode": returncode, "forced_kill": False},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"USB storage noise exited before ready with code {returncode}: {detail}"
                )
            if ready_path.is_file():
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(0.01)
                    continue
                if ready.get("ready"):
                    ready["enabled"] = True
                    ready["command"] = command
                    ready["identity"] = self._usb_storage_identity
                    print(
                        f"[USB-STORAGE-NOISE] ready at "
                        f"{ready.get('warmup_mib_per_second', 0):.1f} MiB/s"
                    )
                    return ready
            time.sleep(0.05)

        self._stop_usb_storage_noise(record_dir)
        raise RuntimeError(
            "USB storage noise did not become ready within "
            f"{self._usb_storage_ready_timeout_seconds:.1f} seconds"
        )

    def _stop_usb_storage_noise(self, record_dir: Path | None = None) -> None:
        process = self._usb_storage_noise_process
        if process is None:
            return
        self._usb_storage_noise_process = None
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
            (record_dir / "usb_storage_noise_process.json").write_text(
                json.dumps(
                    {"returncode": process.returncode, "forced_kill": forced},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        print(f"[USB-STORAGE-NOISE] stopped with code {process.returncode}")

    def _gpu_noise_command(
        self,
        ready_path: Path,
        summary_path: Path,
    ) -> List[str]:
        command = ["chrt", "--other", "0"]
        if self._gpu_noise_cpu_affinity:
            command += ["taskset", "--cpu-list", self._gpu_noise_cpu_affinity]
        command += [
            str(self._gpu_noise),
            "--model-param",
            str(NCNN_MODEL_PARAM),
            "--ready-file",
            str(ready_path),
            "--summary-output",
            str(summary_path),
            "--gpu-device",
            str(self._gpu_noise_device),
            "--warmup-iterations",
            str(self._gpu_noise_warmup_iterations),
            "--num-threads",
            "1",
        ]
        return command

    def _start_gpu_noise(self, mode: str, record_dir: Path) -> Dict[str, Any]:
        if mode == "none":
            return {"mode": "none", "enabled": False}
        if mode != "mobilenet_v2_vulkan":
            raise ValueError(f"Unsupported GPU-noise mode: {mode}")
        if self._gpu_noise_process is not None:
            raise RuntimeError("A GPU-noise process is already running")

        ready_path = record_dir / "gpu_noise_ready.json"
        summary_path = record_dir / "gpu_noise_summary.json"
        stdout_path = record_dir / "gpu_noise_stdout.txt"
        stderr_path = record_dir / "gpu_noise_stderr.txt"
        command = self._gpu_noise_command(ready_path, summary_path)
        environment = os.environ.copy()
        if self._gpu_noise_vulkan_icd:
            environment["VK_DRIVER_FILES"] = str(self._gpu_noise_vulkan_icd)

        started = time.monotonic()
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_file:
            self._gpu_noise_process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                env=environment,
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
            )

        deadline = started + self._gpu_noise_ready_timeout_seconds
        while time.monotonic() < deadline:
            returncode = self._gpu_noise_process.poll()
            if returncode is not None:
                self._gpu_noise_process = None
                detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"GPU noise exited before ready with code {returncode}: {detail}"
                )
            if ready_path.is_file():
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(0.01)
                    continue
                if ready.get("ready"):
                    ready["enabled"] = True
                    ready["command"] = command
                    ready["model_param_sha256"] = sha256_file(NCNN_MODEL_PARAM)
                    ready["vulkan_icd"] = (
                        str(self._gpu_noise_vulkan_icd) if self._gpu_noise_vulkan_icd else ""
                    )
                    ready["cpu_affinity"] = self._gpu_noise_cpu_affinity or ""
                    print(
                        f"[GPU-NOISE] ready on {ready.get('gpu_name', 'unknown')} "
                        f"after {ready.get('startup_ms', 0):.1f} ms"
                    )
                    return ready
            time.sleep(0.05)

        self._stop_gpu_noise(record_dir)
        raise RuntimeError(
            f"GPU noise did not become ready within "
            f"{self._gpu_noise_ready_timeout_seconds:.1f} seconds"
        )

    def _stop_gpu_noise(self, record_dir: Path | None = None) -> None:
        process = self._gpu_noise_process
        if process is None:
            return
        self._gpu_noise_process = None
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
            (record_dir / "gpu_noise_process.json").write_text(
                json.dumps(
                    {"returncode": process.returncode, "forced_kill": forced},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        print(f"[GPU-NOISE] stopped with code {process.returncode}")

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
        gpu_noise: str,
        usb_storage_noise: str,
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
        gpu_noise_ready: Dict[str, Any] = {"mode": gpu_noise, "enabled": False}
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
            "usb_storage_noise": {
                "mode": usb_storage_noise,
                "device": (
                    str(self._usb_storage_device) if self._usb_storage_device else None
                ),
                "warmup_seconds": self._usb_storage_warmup_seconds,
                "block_size_kib": self._usb_storage_block_size_kib,
                "identity": self._usb_storage_identity,
                "access": "read_only",
                "direct_io": True,
            },
            "gpu_noise": {
                "mode": gpu_noise,
                "gpu_device": self._gpu_noise_device,
                "warmup_iterations": self._gpu_noise_warmup_iterations,
                "cpu_affinity": self._gpu_noise_cpu_affinity,
                "vulkan_icd": (
                    str(self._gpu_noise_vulkan_icd)
                    if self._gpu_noise_vulkan_icd
                    else None
                ),
                "model_param": str(NCNN_MODEL_PARAM),
                "model_param_sha256": (
                    sha256_file(NCNN_MODEL_PARAM) if gpu_noise != "none" else None
                ),
            },
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
            usb_storage_ready = self._start_usb_storage_noise(
                usb_storage_noise, record_dir
            )
            (record_dir / "usb_storage_noise_configuration.json").write_text(
                json.dumps(usb_storage_ready, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            gpu_noise_ready = self._start_gpu_noise(gpu_noise, record_dir)
            (record_dir / "gpu_noise_configuration.json").write_text(
                json.dumps(gpu_noise_ready, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
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
            self._stop_gpu_noise(record_dir)
            self._stop_usb_storage_noise(record_dir)
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
            "gpu_noise_mode": run_variables["gpu_noise"],
            "gpu_noise_enabled": run_variables["gpu_noise"] != "none",
            "usb_storage_noise_mode": run_variables["usb_storage_noise"],
            "usb_storage_noise_enabled": run_variables["usb_storage_noise"] != "none",
            "camera_count": data.get("run", {}).get("camera_count", 0),
            "deliveries": aggregate.get("deliveries", 0),
            "frames": aggregate.get("frames", 0),
            "drops": aggregate.get("drops", 0),
            "timeouts": aggregate.get("timeouts", 0),
            "measurement_duration_ms": data.get("measurement", {}).get("duration_ms", 0),
            "record_data_dir": str(record_dir),
        }
        usb_summary_path = record_dir / "usb_storage_noise_summary.json"
        usb_ready_path = record_dir / "usb_storage_noise_ready.json"
        usb_process_path = record_dir / "usb_storage_noise_process.json"
        usb_summary = (
            json.loads(usb_summary_path.read_text(encoding="utf-8"))
            if usb_summary_path.is_file()
            else {}
        )
        usb_ready = (
            json.loads(usb_ready_path.read_text(encoding="utf-8"))
            if usb_ready_path.is_file()
            else {}
        )
        usb_process = (
            json.loads(usb_process_path.read_text(encoding="utf-8"))
            if usb_process_path.is_file()
            else {}
        )
        result["usb_storage_noise_ready"] = bool(usb_ready.get("ready", False))
        result["usb_storage_noise_process_returncode"] = usb_process.get(
            "returncode", ""
        )
        result["usb_storage_noise_forced_kill"] = usb_process.get(
            "forced_kill", False
        )
        if usb_summary:
            flatten("usb_storage_noise", usb_summary, result)
        if run_variables["usb_storage_noise"] != "none":
            usb_valid = (
                bool(usb_ready.get("ready"))
                and bool(usb_summary.get("success"))
                and usb_summary.get("access") == "read_only"
                and bool(usb_summary.get("direct_io"))
                and usb_process.get("returncode") == 0
                and not usb_process.get("forced_kill", False)
            )
            result["usb_storage_noise_valid"] = usb_valid
            if not usb_valid:
                result["success"] = False
                result["error"] = (
                    str(result.get("error", "")) + " | invalid USB storage noise process"
                ).strip(" |")
        else:
            result["usb_storage_noise_valid"] = True

        gpu_summary_path = record_dir / "gpu_noise_summary.json"
        gpu_ready_path = record_dir / "gpu_noise_ready.json"
        gpu_summary = (
            json.loads(gpu_summary_path.read_text(encoding="utf-8"))
            if gpu_summary_path.is_file()
            else {}
        )
        gpu_ready = (
            json.loads(gpu_ready_path.read_text(encoding="utf-8"))
            if gpu_ready_path.is_file()
            else {}
        )
        result["gpu_noise_ready"] = bool(gpu_ready.get("ready", False))
        result["gpu_noise_model_param_sha256"] = (
            sha256_file(NCNN_MODEL_PARAM) if run_variables["gpu_noise"] != "none" else ""
        )
        gpu_process_path = record_dir / "gpu_noise_process.json"
        gpu_process = (
            json.loads(gpu_process_path.read_text(encoding="utf-8"))
            if gpu_process_path.is_file()
            else {}
        )
        result["gpu_noise_process_returncode"] = gpu_process.get("returncode", "")
        result["gpu_noise_forced_kill"] = gpu_process.get("forced_kill", False)
        if gpu_summary:
            flatten("gpu_noise", gpu_summary, result)
        if run_variables["gpu_noise"] != "none":
            gpu_valid = (
                bool(gpu_ready.get("ready"))
                and bool(gpu_summary.get("success"))
                and gpu_process.get("returncode") == 0
                and not gpu_process.get("forced_kill", False)
            )
            result["gpu_noise_valid"] = gpu_valid
            if not gpu_valid:
                result["success"] = False
                result["error"] = (
                    str(result.get("error", "")) + " | invalid GPU-noise process"
                ).strip(" |")
        else:
            result["gpu_noise_valid"] = True
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
            ("stop GPU noise", benchmark._stop_gpu_noise),
            ("stop USB storage noise", benchmark._stop_usb_storage_noise),
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
    parser.add_argument("--rsusb-backend", action="store_true")
    parser.add_argument("--rsusb-usb-device", action="append", default=[])
    parser.add_argument(
        "--cpu-frequency-mhz",
        type=int,
        default=1500,
        help="Lock once before the first run; use 0 to disable",
    )
    args = parser.parse_args()
    if args.build_jobs < 1:
        raise SystemExit("--build-jobs must be positive")
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
        build_jobs=args.build_jobs,
    )
    campaign = CampaignCartesianProduct(
        name="realsense_steady",
        benchmark=benchmark,
        nb_runs=args.nb_runs,
        variables={
            "case_id": [case["case_id"] for case in cases],
            "policy": args.policies,
            "gpu_noise": args.gpu_noise_modes,
            "usb_storage_noise": args.usb_storage_noise_modes,
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
