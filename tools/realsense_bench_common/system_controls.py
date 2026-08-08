"""Machine-level controls shared by RealSense benchmark campaigns."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List

from realsense_bench_common.cpu_isolation import (
    CpuIsolation,
    CpuIsolationConfig,
)
from realsense_bench_common.realsense_devices import (
    discover_realsense_devices,
)


CPUFREQ_BASE = Path("/sys/devices/system/cpu/cpufreq")
NO_TURBO_PATH = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
THERMAL_ZONE0 = Path("/sys/class/thermal/thermal_zone0/temp")
USB_SYSFS_BASE = Path("/sys/bus/usb/devices")


@dataclass(frozen=True)
class SystemControlConfig:
    repo_root: Path
    tool_dir: Path
    use_sudo: bool
    cpu_frequency_mhz: int | None
    cpu_lock_script: Path
    cpu_restore_script: Path
    rsusb_backend: bool
    rsusb_usb_devices: tuple[str, ...]
    rsusb_helper: Path
    rsusb_prepare_each_attempt: bool = False
    rsusb_prepare_timeout_seconds: float = 10.0
    rsusb_unbind_settle_seconds: float = 0.25
    disable_realsense_autosuspend: bool = False
    usb_sysfs_base: Path = USB_SYSFS_BASE
    cpu_isolation_enabled: bool = False
    housekeeping_cpus: str = "0"
    benchmark_cpus: str = "1-3"


class SystemControls:
    """Own CPU-frequency, backend-binding, topology, and kernel-log state."""

    def __init__(self, config: SystemControlConfig) -> None:
        self.config = config
        self._cpu_locked = False
        self._cpu_restore_needed = False
        self._cpu_original_state: Dict[str, Any] | None = None
        self._rsusb_unbound = False
        self._cpu_isolation = CpuIsolation(
            CpuIsolationConfig(
                enabled=config.cpu_isolation_enabled,
                housekeeping_cpus=config.housekeeping_cpus,
                benchmark_cpus=config.benchmark_cpus,
                use_sudo=config.use_sudo,
                repo_root=config.repo_root,
                usb_sysfs_base=config.usb_sysfs_base,
            )
        )

    @property
    def backend_name(self) -> str:
        return "RSUSB" if self.config.rsusb_backend else "V4L2"

    def cpu_isolation_state(self) -> Dict[str, Any]:
        return self._cpu_isolation.state()

    def verify_cpu_isolation(self, output: Path | None = None) -> Dict[str, Any]:
        return self._cpu_isolation.verify(output)

    def _privileged(self, command: List[str]) -> List[str]:
        return (
            ["sudo", "--non-interactive", *command]
            if self.config.use_sudo
            else command
        )

    @staticmethod
    def _read_optional_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def cpu_frequency_state(self) -> Dict[str, Any]:
        policies: List[Dict[str, Any]] = []
        for policy_dir in sorted(CPUFREQ_BASE.glob("policy*")):
            if not policy_dir.is_dir():
                continue
            policies.append(
                {
                    "name": policy_dir.name,
                    "path": str(policy_dir),
                    "driver": self._read_optional_text(
                        policy_dir / "scaling_driver"
                    ),
                    "affected_cpus": self._read_optional_text(
                        policy_dir / "affected_cpus"
                    ),
                    "governor": self._read_optional_text(
                        policy_dir / "scaling_governor"
                    ),
                    "scaling_min_khz": self._read_optional_text(
                        policy_dir / "scaling_min_freq"
                    ),
                    "scaling_max_khz": self._read_optional_text(
                        policy_dir / "scaling_max_freq"
                    ),
                    "scaling_current_khz": self._read_optional_text(
                        policy_dir / "scaling_cur_freq"
                    ),
                }
            )
        return {
            "policies": policies,
            "boost": self._read_optional_text(CPUFREQ_BASE / "boost"),
            "intel_pstate_no_turbo": self._read_optional_text(NO_TURBO_PATH),
            "temperature_millic": self._read_optional_text(THERMAL_ZONE0),
        }

    def verify_cpu_frequency(self, state: Dict[str, Any]) -> List[str]:
        frequency_mhz = self.config.cpu_frequency_mhz
        if frequency_mhz is None:
            return []
        target_khz = frequency_mhz * 1000
        policies = state.get("policies", [])
        if not policies:
            return [f"no CPU-frequency policies found below {CPUFREQ_BASE}"]
        errors: List[str] = []
        for policy in policies:
            name = policy.get("name", "unknown")
            for field in (
                "scaling_min_khz",
                "scaling_max_khz",
                "scaling_current_khz",
            ):
                if str(policy.get(field, "")) != str(target_khz):
                    errors.append(
                        f"{name} {field}={policy.get(field, '')}, "
                        f"expected {target_khz}"
                    )
        return errors

    def validate_environment(self) -> None:
        if self.config.rsusb_backend and not self.config.rsusb_usb_devices:
            raise RuntimeError(
                "RSUSB campaign constants require one USB device per connected camera"
            )
        helpers = []
        if self.config.cpu_frequency_mhz is not None:
            helpers.extend(
                (self.config.cpu_lock_script, self.config.cpu_restore_script)
            )
        if self.config.rsusb_backend:
            helpers.append(self.config.rsusb_helper)
        for helper in helpers:
            if not helper.is_file():
                raise RuntimeError(f"Required helper is missing: {helper}")
        self._cpu_isolation.validate_environment()

    def prepare_campaign(self, record_dir: Path) -> Dict[str, Any]:
        self._cpu_isolation.prepare(record_dir)
        cpu_state = self._lock_cpu_once(record_dir)
        if not self.config.rsusb_prepare_each_attempt:
            self._prepare_rsusb_once()
        if self.config.disable_realsense_autosuspend:
            self._enforce_realsense_autosuspend(
                record_dir / "realsense_autosuspend_campaign.json"
            )
        return cpu_state

    def prepare_attempt(self, attempt: int, attempt_dir: Path) -> None:
        self._cpu_isolation.verify(attempt_dir / "cpu_isolation_before.json")
        if self.config.rsusb_backend:
            if self.config.rsusb_prepare_each_attempt:
                self._run_rsusb_helper("unbind")
                self._rsusb_unbound = True
                if self.config.rsusb_unbind_settle_seconds > 0:
                    time.sleep(self.config.rsusb_unbind_settle_seconds)
            else:
                self._prepare_rsusb_once()
        if self.config.disable_realsense_autosuspend:
            output = (
                attempt_dir / "realsense_autosuspend.json"
                if attempt_dir.is_dir()
                else None
            )
            self._enforce_realsense_autosuspend(output, attempt=attempt)

    def _lock_cpu_once(self, record_dir: Path) -> Dict[str, Any]:
        frequency_mhz = self.config.cpu_frequency_mhz
        if frequency_mhz is None:
            return {"enabled": False, "policies": []}
        if self._cpu_locked:
            state = self.cpu_frequency_state()
            errors = self.verify_cpu_frequency(state)
            if errors:
                raise RuntimeError(
                    "CPU frequency changed during the campaign: "
                    + " | ".join(errors)
                )
            state.update(
                {
                    "enabled": True,
                    "requested_mhz": frequency_mhz,
                    "locked": True,
                }
            )
            return state

        self._cpu_original_state = self.cpu_frequency_state()
        if not self._cpu_original_state["policies"]:
            raise RuntimeError(
                f"No CPU-frequency policies found below {CPUFREQ_BASE}"
            )
        command = self._privileged(
            [str(self.config.cpu_lock_script), str(frequency_mhz * 1000)]
        )
        self._cpu_restore_needed = True
        completed = subprocess.run(
            command,
            cwd=self.config.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        (record_dir / "cpu_frequency_lock.txt").write_text(
            completed.stdout + completed.stderr,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Failed to lock CPU frequency; see cpu_frequency_lock.txt"
            )

        deadline = time.monotonic() + 1.0
        while True:
            state = self.cpu_frequency_state()
            errors = self.verify_cpu_frequency(state)
            if not errors or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if errors:
            raise RuntimeError(
                "CPU-frequency lock verification failed: " + " | ".join(errors)
            )

        self._cpu_locked = True
        state.update(
            {
                "enabled": True,
                "requested_mhz": frequency_mhz,
                "locked": True,
            }
        )
        print(
            f"[CPU-FREQ] locked {len(state['policies'])} policy/policies at "
            f"{frequency_mhz} MHz before the first measured run"
        )
        return state

    def _write_sysfs_value(self, path: Path, value: str) -> None:
        completed = subprocess.run(
            self._privileged(["tee", str(path)]),
            input=value + "\n",
            cwd=self.config.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"Failed to write {path}={value}: {detail}")

    def realsense_usb_power_state(self) -> List[Dict[str, str]]:
        devices: List[Dict[str, str]] = []
        for discovered in discover_realsense_devices(self.config.usb_sysfs_base):
            device_dir = Path(str(discovered["sysfs_path"]))
            control = device_dir / "power" / "control"
            devices.append(
                {
                    "usb_device": device_dir.name,
                    "sysfs_path": str(device_dir),
                    "vendor": str(discovered["vendor_id"]),
                    "product": str(discovered["product_id"]),
                    "model": str(discovered["model"]),
                    "serial": str(discovered["serial"]),
                    "power_control_path": str(control),
                    "power_control": self._read_optional_text(control),
                }
            )
        return devices

    def _enforce_realsense_autosuspend(
        self,
        output: Path | None,
        *,
        attempt: int | None = None,
    ) -> List[Dict[str, str]]:
        devices = self.realsense_usb_power_state()
        if not devices:
            raise RuntimeError(
                "No connected Intel RealSense video camera was found"
            )
        for device in devices:
            control = Path(device["power_control_path"])
            if not control.is_file():
                raise RuntimeError(
                    f"RealSense power control is unavailable: {control}"
                )
            if device["power_control"] != "on":
                self._write_sysfs_value(control, "on")

        verified = self.realsense_usb_power_state()
        errors = [
            f"{device['usb_device']}={device['power_control'] or 'unreadable'}"
            for device in verified
            if device["power_control"] != "on"
        ]
        if errors:
            raise RuntimeError(
                "RealSense USB autosuspend disable verification failed: "
                + ", ".join(errors)
            )
        if output is not None:
            output.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "attempt": attempt,
                        "devices": verified,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        print(
            "[USB-POWER] autosuspend disabled for "
            f"{len(verified)} connected RealSense camera(s)"
        )
        return verified

    def restore_cpu_frequency(self) -> None:
        if not self._cpu_restore_needed:
            return
        if self._cpu_original_state is None:
            raise RuntimeError("Original CPU-frequency state was not captured")

        print("[CPU-FREQ] restoring the pre-campaign CPU-frequency state")
        completed = subprocess.run(
            self._privileged([str(self.config.cpu_restore_script)]),
            cwd=self.config.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                "Default CPU-frequency restore failed: "
                + (detail or f"status {completed.returncode}")
            )

        for policy in self._cpu_original_state["policies"]:
            policy_dir = Path(policy["path"])
            scaling_max = str(policy.get("scaling_max_khz", ""))
            scaling_min = str(policy.get("scaling_min_khz", ""))
            governor = str(policy.get("governor", ""))
            if scaling_max:
                self._write_sysfs_value(
                    policy_dir / "scaling_max_freq", scaling_max
                )
            if scaling_min:
                self._write_sysfs_value(
                    policy_dir / "scaling_min_freq", scaling_min
                )
            if governor:
                self._write_sysfs_value(
                    policy_dir / "scaling_governor", governor
                )

        boost = str(self._cpu_original_state.get("boost", ""))
        if boost and (CPUFREQ_BASE / "boost").is_file():
            self._write_sysfs_value(CPUFREQ_BASE / "boost", boost)
        no_turbo = str(
            self._cpu_original_state.get("intel_pstate_no_turbo", "")
        )
        if no_turbo and NO_TURBO_PATH.is_file():
            self._write_sysfs_value(NO_TURBO_PATH, no_turbo)

        restored = self.cpu_frequency_state()
        restored_by_name = {
            item["name"]: item for item in restored.get("policies", [])
        }
        errors: List[str] = []
        for original in self._cpu_original_state["policies"]:
            actual = restored_by_name.get(original["name"], {})
            for field in ("scaling_min_khz", "scaling_max_khz", "governor"):
                if actual.get(field) != original.get(field):
                    errors.append(
                        f"{original['name']} {field}={actual.get(field, '')}, "
                        f"expected {original.get(field, '')}"
                    )
        if errors:
            raise RuntimeError(
                "Pre-campaign CPU-frequency state was not restored: "
                + " | ".join(errors)
            )
        self._cpu_restore_needed = False
        self._cpu_locked = False
        print("[CPU-FREQ] pre-campaign state restored")

    def _run_rsusb_helper(self, action: str) -> None:
        for device in self.config.rsusb_usb_devices:
            command = self._privileged(
                [str(self.config.rsusb_helper), action, device]
            )
            deadline = (
                time.monotonic()
                + self.config.rsusb_prepare_timeout_seconds
            )
            failures: List[str] = []
            while True:
                completed = subprocess.run(
                    command,
                    cwd=self.config.repo_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                output = (completed.stdout + completed.stderr).strip()
                if completed.returncode == 0:
                    if output:
                        print(f"[RSUSB] {output}")
                    break
                failures.append(
                    output or f"exit status {completed.returncode}"
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Failed to {action} UVC for {device}: "
                        f"{failures[-1]}"
                    )
                time.sleep(min(0.25, remaining))
            if failures:
                print(
                    f"[RSUSB] {action} succeeded after "
                    f"{len(failures)} transient failures"
                )

    def _prepare_rsusb_once(self) -> None:
        if not self.config.rsusb_backend or self._rsusb_unbound:
            return
        self._run_rsusb_helper("unbind")
        self._rsusb_unbound = True
        if self.config.rsusb_unbind_settle_seconds > 0:
            time.sleep(self.config.rsusb_unbind_settle_seconds)
        print("[RSUSB] kernel UVC interfaces unbound for the campaign")

    def restore_v4l2_binding(self) -> None:
        if not self._rsusb_unbound:
            return
        self._run_rsusb_helper("bind")
        self._rsusb_unbound = False
        print("[RSUSB] kernel UVC interfaces rebound")

    def snapshot_topology(self, output: Path) -> None:
        subprocess.run(
            [
                sys.executable,
                str(self.config.tool_dir / "snapshot_topology.py"),
                "--output",
                str(output),
            ],
            cwd=self.config.repo_root,
            check=False,
        )

    def kernel_log(self) -> tuple[str | None, str]:
        completed = subprocess.run(
            self._privileged(["dmesg", "--color=never"]),
            cwd=self.config.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return None, detail or f"dmesg exited with {completed.returncode}"
        return completed.stdout, ""

    @staticmethod
    def kernel_delta(before: str, after: str) -> tuple[str, str]:
        if after.startswith(before):
            return after[len(before):], ""
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        if not before_lines:
            return after, ""

        anchor = before_lines[-1]
        for index in range(len(after_lines) - 1, -1, -1):
            if after_lines[index] == anchor:
                delta = "\n".join(after_lines[index + 1 :])
                return (delta + "\n" if delta else ""), ""
        return "", "dmesg ring buffer changed and the pre-run anchor was lost"

    def capture_kernel_delta(
        self,
        record_dir: Path,
        before: str | None,
        before_error: str,
    ) -> None:
        after, after_error = self.kernel_log()
        error = before_error or after_error
        if before is not None and after is not None:
            delta, delta_error = self.kernel_delta(before, after)
            if not delta_error:
                (record_dir / "kernel_log.txt").write_text(
                    delta, encoding="utf-8"
                )
            error = error or delta_error
        if error:
            (record_dir / "kernel_log_capture_error.txt").write_text(
                error + "\n", encoding="utf-8"
            )

    def write_cpu_state(self, output: Path, state: Dict[str, Any]) -> None:
        output.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def cleanup(self) -> None:
        errors = []
        for description, action in (
            ("restore V4L2 binding", self.restore_v4l2_binding),
            ("restore CPU isolation", self._cpu_isolation.restore),
            ("restore CPU frequency", self.restore_cpu_frequency),
        ):
            try:
                action()
            except Exception as error:
                errors.append(f"{description}: {error}")
        if errors:
            raise RuntimeError(" | ".join(errors))
