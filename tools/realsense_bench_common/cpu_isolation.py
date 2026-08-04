"""Reversible CPU partitioning for camera benchmark campaigns."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Set


CGROUP_ROOT = Path("/sys/fs/cgroup")
CPU_ONLINE_PATH = Path("/sys/devices/system/cpu/online")
PROC_ROOT = Path("/proc")
USB_SYSFS_BASE = Path("/sys/bus/usb/devices")


def parse_cpu_list(value: str) -> Set[int]:
    """Parse the Linux CPU-list syntax used by sysfs and procfs."""
    cpus: Set[int] = set()
    text = value.strip()
    if not text:
        return cpus
    for token in text.split(","):
        token = token.strip()
        if not token:
            raise ValueError(f"invalid empty CPU-list element in {value!r}")
        if "-" in token:
            fields = token.split("-", 1)
            if not all(field.isdigit() for field in fields):
                raise ValueError(f"invalid CPU range {token!r}")
            first, last = (int(field) for field in fields)
            if first > last:
                raise ValueError(f"descending CPU range {token!r}")
            cpus.update(range(first, last + 1))
        else:
            if not token.isdigit():
                raise ValueError(f"invalid CPU number {token!r}")
            cpus.add(int(token))
    return cpus


def format_cpu_list(cpus: Set[int]) -> str:
    """Return a canonical compact Linux CPU list."""
    if not cpus:
        return ""
    values = sorted(cpus)
    ranges: List[str] = []
    first = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(first) if first == previous else f"{first}-{previous}")
        first = previous = value
    ranges.append(str(first) if first == previous else f"{first}-{previous}")
    return ",".join(ranges)


@dataclass(frozen=True)
class CpuIsolationConfig:
    enabled: bool
    housekeeping_cpus: str
    benchmark_cpus: str
    use_sudo: bool
    repo_root: Path
    cgroup_root: Path = CGROUP_ROOT
    cpu_online_path: Path = CPU_ONLINE_PATH
    proc_root: Path = PROC_ROOT
    usb_sysfs_base: Path = USB_SYSFS_BASE


class CpuIsolation:
    """Place the campaign in an isolated cgroup and pin camera xHCI IRQs."""

    def __init__(self, config: CpuIsolationConfig) -> None:
        self.config = config
        self._active = False
        self._controller_enabled_by_us = False
        self._cgroup_created = False
        self._process_moved = False
        self._cgroup_path: Path | None = None
        self._original_cgroup_path: Path | None = None
        self._original_irq_affinities: Dict[int, str] = {}
        self._state: Dict[str, Any] = {
            "enabled": config.enabled,
            "active": False,
            "housekeeping_cpus": config.housekeeping_cpus,
            "benchmark_cpus": config.benchmark_cpus,
            "xhci_irqs": [],
        }

    def _privileged(self, command: List[str]) -> List[str]:
        return (
            ["sudo", "--non-interactive", *command]
            if self.config.use_sudo
            else command
        )

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip()

    def _run_privileged(self, command: List[str]) -> None:
        completed = subprocess.run(
            self._privileged(command),
            cwd=self.config.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"{' '.join(command)} failed: "
                f"{detail or f'status {completed.returncode}'}"
            )

    def _write(self, path: Path, value: str) -> None:
        completed = subprocess.run(
            self._privileged(["tee", str(path)]),
            input=value + "\n",
            cwd=self.config.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"failed to write {path}={value}: "
                f"{detail or f'status {completed.returncode}'}"
            )

    def _current_process_cgroup(self) -> Path:
        cgroup_text = self._read(self.config.proc_root / "self" / "cgroup")
        for line in cgroup_text.splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
                relative = fields[2].lstrip("/")
                return self.config.cgroup_root / relative
        raise RuntimeError("the current process is not in a cgroup-v2 hierarchy")

    def _connected_d435_devices(self) -> List[Path]:
        devices: List[Path] = []
        for path in sorted(self.config.usb_sysfs_base.glob("*")):
            if not path.is_dir():
                continue
            vendor = path / "idVendor"
            product = path / "idProduct"
            if not vendor.is_file() or not product.is_file():
                continue
            if self._read(vendor).lower() != "8086":
                continue
            if self._read(product).lower() != "0b07":
                continue
            devices.append(path)
        return devices

    def _interrupt_actions(self) -> Dict[str, int]:
        actions: Dict[str, int] = {}
        text = self._read(self.config.proc_root / "interrupts")
        for line in text.splitlines():
            match = re.match(r"^\s*(\d+):", line)
            if match is None:
                continue
            irq = int(match.group(1))
            for action in re.findall(r"xhci-hcd:(usb\d+)", line):
                if action in actions and actions[action] != irq:
                    raise RuntimeError(
                        f"xHCI action {action} maps to multiple IRQs"
                    )
                actions[action] = irq
        return actions

    def discover_camera_xhci_irqs(self) -> List[Dict[str, Any]]:
        devices = self._connected_d435_devices()
        if not devices:
            raise RuntimeError(
                "CPU isolation requires at least one connected D435 (8086:0b07)"
            )
        actions = self._interrupt_actions()
        by_irq: Dict[int, Dict[str, Any]] = {}
        for device in devices:
            resolved = device.resolve()
            controller = next(
                (
                    path
                    for path in (resolved, *resolved.parents)
                    if path.name.startswith("xhci-hcd.")
                ),
                None,
            )
            if controller is None:
                raise RuntimeError(
                    f"cannot find the xHCI controller for {device.name}: {resolved}"
                )
            root_hubs = sorted(
                child.name
                for child in controller.glob("usb*")
                if child.is_dir() and re.fullmatch(r"usb\d+", child.name)
            )
            matches = [(bus, actions[bus]) for bus in root_hubs if bus in actions]
            if len(matches) != 1:
                raise RuntimeError(
                    f"expected one xHCI IRQ for {device.name} via {controller}, "
                    f"found {matches!r}"
                )
            bus, irq = matches[0]
            record = by_irq.setdefault(
                irq,
                {
                    "irq": irq,
                    "action": f"xhci-hcd:{bus}",
                    "controller": str(controller),
                    "usb_devices": [],
                },
            )
            record["usb_devices"].append(device.name)
        return [by_irq[irq] for irq in sorted(by_irq)]

    def _irq_thread_state(self, irq: int) -> Dict[str, Any]:
        prefix = f"irq/{irq}-"
        for process in self.config.proc_root.glob("[0-9]*"):
            if not process.name.isdigit():
                continue
            comm = process / "comm"
            try:
                name = self._read(comm)
            except OSError:
                continue
            if not name.startswith(prefix):
                continue
            pid = int(process.name)
            try:
                policy_id = os.sched_getscheduler(pid)
                priority = os.sched_getparam(pid).sched_priority
                affinity = set(os.sched_getaffinity(pid))
            except (OSError, PermissionError):
                policy_id = -1
                priority = -1
                affinity = set()
            policy_names = {
                getattr(os, "SCHED_OTHER", 0): "SCHED_OTHER",
                getattr(os, "SCHED_FIFO", 1): "SCHED_FIFO",
                getattr(os, "SCHED_RR", 2): "SCHED_RR",
                getattr(os, "SCHED_BATCH", 3): "SCHED_BATCH",
                getattr(os, "SCHED_IDLE", 5): "SCHED_IDLE",
                getattr(os, "SCHED_DEADLINE", 6): "SCHED_DEADLINE",
            }
            return {
                "pid": pid,
                "name": name,
                "policy": policy_names.get(policy_id, str(policy_id)),
                "priority": priority,
                "affinity": format_cpu_list(affinity),
            }
        return {}

    def validate_environment(self) -> None:
        if not self.config.enabled:
            return
        housekeeping = parse_cpu_list(self.config.housekeeping_cpus)
        benchmark = parse_cpu_list(self.config.benchmark_cpus)
        online = parse_cpu_list(self._read(self.config.cpu_online_path))
        if not housekeeping or not benchmark:
            raise RuntimeError("CPU isolation requires two non-empty CPU sets")
        if housekeeping & benchmark:
            raise RuntimeError("housekeeping and benchmark CPU sets overlap")
        if housekeeping | benchmark != online:
            raise RuntimeError(
                "housekeeping and benchmark CPU sets must exactly partition "
                f"online CPUs {format_cpu_list(online)}"
            )
        root = self.config.cgroup_root
        required = (
            root / "cgroup.controllers",
            root / "cgroup.subtree_control",
            root / "cgroup.procs",
            root / "cpuset.cpus.effective",
            root / "cpuset.cpus.isolated",
            root / "cpuset.mems.effective",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(
                "cgroup-v2 cpuset support is unavailable: " + ", ".join(missing)
            )
        controllers = self._read(root / "cgroup.controllers").split()
        if "cpuset" not in controllers:
            raise RuntimeError("the cgroup-v2 cpuset controller is unavailable")
        if self._read(root / "cpuset.cpus.isolated"):
            raise RuntimeError(
                "an existing isolated cpuset partition is already active"
            )
        self._current_process_cgroup()
        self.discover_camera_xhci_irqs()

    def state(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._state))

    def verify(self, output: Path | None = None) -> Dict[str, Any]:
        if not self.config.enabled:
            return self.state()
        if not self._active or self._cgroup_path is None:
            raise RuntimeError("the configured CPU isolation is not active")

        root = self.config.cgroup_root
        benchmark = format_cpu_list(parse_cpu_list(self.config.benchmark_cpus))
        housekeeping = format_cpu_list(
            parse_cpu_list(self.config.housekeeping_cpus)
        )
        actual = {
            "root_effective_cpus": self._read(root / "cpuset.cpus.effective"),
            "benchmark_effective_cpus": self._read(
                self._cgroup_path / "cpuset.cpus.effective"
            ),
            "isolated_cpus": self._read(root / "cpuset.cpus.isolated"),
            "process_affinity": format_cpu_list(set(os.sched_getaffinity(0))),
        }
        errors: List[str] = []
        expected = {
            "root_effective_cpus": housekeeping,
            "benchmark_effective_cpus": benchmark,
            "isolated_cpus": benchmark,
            "process_affinity": benchmark,
        }
        for key, expected_value in expected.items():
            if parse_cpu_list(actual[key]) != parse_cpu_list(expected_value):
                errors.append(f"{key}={actual[key]}, expected {expected_value}")

        irq_records = self._state.get("xhci_irqs", [])
        for record in irq_records:
            irq = int(record["irq"])
            affinity = self._read(
                self.config.proc_root
                / "irq"
                / str(irq)
                / "smp_affinity_list"
            )
            record["effective_affinity"] = affinity
            record["thread"] = self._irq_thread_state(irq)
            if parse_cpu_list(affinity) != parse_cpu_list(housekeeping):
                errors.append(
                    f"IRQ {irq} affinity={affinity}, expected {housekeeping}"
                )

        self._state.update(actual)
        self._state["verified"] = not errors
        if output is not None:
            output.write_text(
                json.dumps(self._state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if errors:
            raise RuntimeError(
                "CPU isolation verification failed: " + " | ".join(errors)
            )
        return self.state()

    def prepare(self, record_dir: Path) -> Dict[str, Any]:
        if not self.config.enabled:
            return self.state()
        if self._active:
            return self.state()

        self.validate_environment()
        root = self.config.cgroup_root
        cgroup_path = root / f"rs-camera-benchmark-{os.getpid()}"
        if cgroup_path.exists():
            raise RuntimeError(f"benchmark cpuset already exists: {cgroup_path}")
        self._cgroup_path = cgroup_path
        self._original_cgroup_path = self._current_process_cgroup()
        subtree = self._read(root / "cgroup.subtree_control").split()
        irq_records = self.discover_camera_xhci_irqs()
        housekeeping = format_cpu_list(
            parse_cpu_list(self.config.housekeeping_cpus)
        )
        benchmark = format_cpu_list(parse_cpu_list(self.config.benchmark_cpus))

        try:
            if "cpuset" not in subtree:
                self._write(root / "cgroup.subtree_control", "+cpuset")
                self._controller_enabled_by_us = True
            self._run_privileged(["mkdir", str(cgroup_path)])
            self._cgroup_created = True
            self._write(
                cgroup_path / "cpuset.mems",
                self._read(root / "cpuset.mems.effective"),
            )
            self._write(cgroup_path / "cpuset.cpus", benchmark)
            self._write(cgroup_path / "cpuset.cpus.partition", "isolated")

            for record in irq_records:
                irq = int(record["irq"])
                affinity_path = (
                    self.config.proc_root
                    / "irq"
                    / str(irq)
                    / "smp_affinity_list"
                )
                original = self._read(affinity_path)
                self._original_irq_affinities[irq] = original
                self._write(affinity_path, housekeeping)
                record["original_affinity"] = original
                record["effective_affinity"] = self._read(affinity_path)
                record["thread"] = self._irq_thread_state(irq)

            self._write(cgroup_path / "cgroup.procs", str(os.getpid()))
            self._process_moved = True
            effective_benchmark = self._read(
                cgroup_path / "cpuset.cpus.effective"
            )
            effective_root = self._read(root / "cpuset.cpus.effective")
            isolated = self._read(root / "cpuset.cpus.isolated")
            process_affinity = format_cpu_list(set(os.sched_getaffinity(0)))
            errors = []
            if parse_cpu_list(effective_benchmark) != parse_cpu_list(benchmark):
                errors.append(f"benchmark effective CPUs={effective_benchmark}")
            if parse_cpu_list(effective_root) != parse_cpu_list(housekeeping):
                errors.append(f"root effective CPUs={effective_root}")
            if parse_cpu_list(isolated) != parse_cpu_list(benchmark):
                errors.append(f"isolated CPUs={isolated}")
            if parse_cpu_list(process_affinity) != parse_cpu_list(benchmark):
                errors.append(f"process affinity={process_affinity}")
            if errors:
                raise RuntimeError(
                    "CPU isolation verification failed: " + " | ".join(errors)
                )

            self._active = True
            self._state = {
                "enabled": True,
                "active": True,
                "cgroup": str(cgroup_path),
                "original_cgroup": str(self._original_cgroup_path),
                "housekeeping_cpus": housekeeping,
                "benchmark_cpus": benchmark,
                "root_effective_cpus": effective_root,
                "benchmark_effective_cpus": effective_benchmark,
                "isolated_cpus": isolated,
                "process_affinity": process_affinity,
                "xhci_irqs": irq_records,
            }
            self.verify()
            record_dir.mkdir(parents=True, exist_ok=True)
            (record_dir / "cpu_isolation_campaign.json").write_text(
                json.dumps(self._state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                f"[CPU-ISOLATION] IRQ/housekeeping CPUs={housekeeping}; "
                f"benchmark CPUs={benchmark}; xHCI IRQs="
                + ",".join(str(record["irq"]) for record in irq_records)
            )
            return self.state()
        except Exception:
            try:
                self.restore()
            except Exception as cleanup_error:
                raise RuntimeError(
                    "CPU isolation setup failed and cleanup also failed: "
                    f"{cleanup_error}"
                )
            raise

    def restore(self) -> None:
        if not any(
            (
                self._active,
                self._controller_enabled_by_us,
                self._cgroup_created,
                self._process_moved,
                self._original_irq_affinities,
            )
        ):
            return
        errors: List[str] = []

        if self._process_moved:
            target = self._original_cgroup_path or self.config.cgroup_root
            try:
                self._write(target / "cgroup.procs", str(os.getpid()))
                self._process_moved = False
            except Exception as error:
                errors.append(f"restore process cgroup: {error}")

        for irq, affinity in self._original_irq_affinities.items():
            try:
                self._write(
                    self.config.proc_root
                    / "irq"
                    / str(irq)
                    / "smp_affinity_list",
                    affinity,
                )
            except Exception as error:
                errors.append(f"restore IRQ {irq} affinity: {error}")
        self._original_irq_affinities.clear()

        if self._cgroup_created and self._cgroup_path is not None:
            try:
                self._write(
                    self._cgroup_path / "cpuset.cpus.partition", "member"
                )
                self._run_privileged(["rmdir", str(self._cgroup_path)])
                self._cgroup_created = False
            except Exception as error:
                errors.append(f"remove benchmark cpuset: {error}")

        if self._controller_enabled_by_us:
            try:
                self._write(
                    self.config.cgroup_root / "cgroup.subtree_control",
                    "-cpuset",
                )
                self._controller_enabled_by_us = False
            except Exception as error:
                errors.append(f"disable cpuset controller: {error}")

        self._active = False
        self._state["active"] = False
        if not errors:
            print(
                "[CPU-ISOLATION] restored xHCI IRQ affinity and the original "
                "process cgroup"
            )
        if errors:
            raise RuntimeError(" | ".join(errors))
