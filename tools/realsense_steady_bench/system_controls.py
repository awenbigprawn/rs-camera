"""Machine-level controls used by the RealSense steady-state campaign."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import time
from typing import List


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


class SystemControls:
    """Own CPU-frequency, backend-binding, topology, and kernel-log state."""

    def __init__(self, config: SystemControlConfig) -> None:
        self.config = config
        self._cpu_locked = False
        self._cpu_restore_needed = False
        self._rsusb_unbound = False

    @property
    def backend_name(self) -> str:
        return "RSUSB" if self.config.rsusb_backend else "V4L2"

    def _privileged(self, command: List[str]) -> List[str]:
        return (
            ["sudo", "--non-interactive", *command]
            if self.config.use_sudo
            else command
        )

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

    def prepare_campaign(self, record_dir: Path) -> None:
        self._lock_cpu_once(record_dir)
        self._prepare_rsusb_once()

    def _lock_cpu_once(self, record_dir: Path) -> None:
        frequency_mhz = self.config.cpu_frequency_mhz
        if frequency_mhz is None or self._cpu_locked:
            return
        command = self._privileged(
            [str(self.config.cpu_lock_script), str(frequency_mhz * 1000)]
        )
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
        self._cpu_restore_needed = True
        if completed.returncode != 0:
            raise RuntimeError("Failed to lock CPU frequency; see cpu_frequency_lock.txt")
        self._cpu_locked = True
        print(f"[CPU-FREQ] locked at {frequency_mhz} MHz")

    def restore_cpu_frequency(self) -> None:
        if not self._cpu_restore_needed:
            return
        completed = subprocess.run(
            self._privileged([str(self.config.cpu_restore_script)]),
            cwd=self.config.repo_root,
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
        for device in self.config.rsusb_usb_devices:
            completed = subprocess.run(
                self._privileged([str(self.config.rsusb_helper), action, device]),
                cwd=self.config.repo_root,
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
        if not self.config.rsusb_backend or self._rsusb_unbound:
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
    def kernel_delta(before: str, after: str) -> str:
        if after.startswith(before):
            return after[len(before):]
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        common = 0
        for old, new in zip(before_lines, after_lines):
            if old != new:
                break
            common += 1
        return "\n".join(after_lines[common:]) + (
            "\n" if common < len(after_lines) else ""
        )

    def capture_kernel_delta(
        self,
        record_dir: Path,
        before: str | None,
        before_error: str,
    ) -> None:
        after, after_error = self.kernel_log()
        error = before_error or after_error
        if before is not None and after is not None:
            (record_dir / "kernel_log.txt").write_text(
                self.kernel_delta(before, after),
                encoding="utf-8",
            )
        elif error:
            (record_dir / "kernel_log_capture_error.txt").write_text(
                error + "\n",
                encoding="utf-8",
            )

    def cleanup(self) -> None:
        errors = []
        for description, action in (
            ("restore V4L2 binding", self.restore_v4l2_binding),
            ("restore CPU frequency", self.restore_cpu_frequency),
        ):
            try:
                action()
            except Exception as error:
                errors.append(f"{description}: {error}")
        if errors:
            raise RuntimeError(" | ".join(errors))
