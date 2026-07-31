"""Full-device recovery for failed multi-camera RealSense runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, Mapping


@dataclass(frozen=True)
class CameraRecoveryConfig:
    repo_root: Path
    reset_probe: Path
    use_sudo: bool
    reset_timeout_ms: int
    enumeration_timeout_seconds: float


class MultiCameraFullReset:
    """Reset firmware and the composite USB device for every selected camera."""

    def __init__(self, config: CameraRecoveryConfig) -> None:
        self.config = config

    def validate_environment(self) -> None:
        if shutil.which("usbreset") is None:
            raise RuntimeError("usbreset is required for full-reset recovery")
        if self.config.use_sudo and shutil.which("sudo") is None:
            raise RuntimeError("sudo is required for full-reset recovery")

    def _privileged(
        self,
        command: list[str],
        *,
        preserve_library_path: bool = False,
    ) -> list[str]:
        if not self.config.use_sudo:
            return command
        prefix = ["sudo", "--non-interactive"]
        if preserve_library_path:
            prefix.append("--preserve-env=LD_LIBRARY_PATH")
        return [*prefix, *command]

    @staticmethod
    def _usb_device_from_physical_port(physical_port: str) -> Path | None:
        if not physical_port.startswith("/sys/"):
            return None
        path = Path(physical_port).resolve()
        for candidate in (path, *path.parents):
            if (candidate / "busnum").is_file() and (candidate / "devnum").is_file():
                return candidate
        return None

    @staticmethod
    def _physical_port_from_reset_output(output: str) -> str:
        for line in output.splitlines():
            prefix = "RS_HARDWARE_RESET "
            if not line.startswith(prefix):
                continue
            try:
                payload = json.loads(line[len(prefix):])
            except json.JSONDecodeError:
                continue
            if payload.get("state") == "requested":
                return str(payload.get("physical_port", ""))
        return ""

    def _probe_can_find_serial(self, serial: str, timeout_seconds: float) -> tuple[bool, str]:
        command = [str(self.config.reset_probe), "--list-only", "--serial", serial]
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.repo_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "device enumeration probe timed out"
        output = completed.stdout + completed.stderr
        return completed.returncode == 0, output.strip()

    def _firmware_reset(self, serial: str) -> Dict[str, Any]:
        command = self._privileged(
            [
                str(self.config.reset_probe),
                "--hardware-reset",
                "--reset-timeout-ms",
                str(self.config.reset_timeout_ms),
                "--serial",
                serial,
            ],
            preserve_library_path=True,
        )
        result: Dict[str, Any] = {
            "command": command,
            "success": False,
        }
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.repo_root,
                capture_output=True,
                text=True,
                timeout=self.config.reset_timeout_ms / 1000.0 + 5.0,
                check=False,
            )
            output = completed.stdout + completed.stderr
            result.update(
                {
                    "returncode": completed.returncode,
                    "output": output.strip(),
                    "success": completed.returncode == 0
                    and '"state":"complete"' in output,
                    "reported_physical_port": self._physical_port_from_reset_output(
                        output
                    ),
                }
            )
            if not result["success"]:
                result["error"] = "D435 firmware hardware reset did not complete"
        except (OSError, subprocess.TimeoutExpired) as error:
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    def _usb_reset(self, serial: str, physical_port: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "physical_port": physical_port,
            "success": False,
        }
        usb_device = self._usb_device_from_physical_port(physical_port)
        if usb_device is None:
            result["error"] = "could not resolve the camera to a composite USB device"
            return result

        try:
            bus = int((usb_device / "busnum").read_text(encoding="utf-8").strip())
            device_number = int(
                (usb_device / "devnum").read_text(encoding="utf-8").strip()
            )
            target = f"{bus:03d}/{device_number:03d}"
            command = self._privileged(["usbreset", target])
            result.update(
                {
                    "usb_device": str(usb_device),
                    "target": target,
                    "command": command,
                }
            )
            completed = subprocess.run(
                command,
                cwd=self.config.repo_root,
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            result["returncode"] = completed.returncode
            result["output"] = (completed.stdout + completed.stderr).strip()
            if completed.returncode != 0:
                result["error"] = "usbreset failed"
                return result

            deadline = time.monotonic() + self.config.enumeration_timeout_seconds
            last_output = ""
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                visible, last_output = self._probe_can_find_serial(
                    serial,
                    timeout_seconds=max(0.05, min(2.0, remaining)),
                )
                if visible:
                    result["success"] = True
                    break
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            result["enumeration_output"] = last_output
            if not result["success"]:
                result["error"] = "camera did not reappear before recovery timeout"
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    def _recover_camera(self, camera: Mapping[str, Any]) -> Dict[str, Any]:
        serial = str(camera.get("serial", ""))
        physical_port = str(camera.get("physical_port", ""))
        result: Dict[str, Any] = {
            "serial": serial,
            "physical_port": physical_port,
            "success": False,
        }
        if not serial:
            result["error"] = "camera serial is missing"
            return result

        print(f"[RECOVERY] resetting D435 firmware serial={serial}")
        firmware = self._firmware_reset(serial)
        result["hardware_reset"] = firmware
        if not physical_port:
            physical_port = str(firmware.get("reported_physical_port", ""))
            result["physical_port"] = physical_port

        print(f"[RECOVERY] resetting composite USB device serial={serial}")
        usb = self._usb_reset(serial, physical_port)
        result["usb_reset"] = usb
        result["success"] = bool(
            firmware.get("success", False) and usb.get("success", False)
        )
        errors = [
            str(item.get("error", ""))
            for item in (firmware, usb)
            if item.get("error")
        ]
        if errors:
            result["error"] = " | ".join(errors)
        return result

    def recover(
        self,
        cameras: Iterable[Mapping[str, Any]],
        record_dir: Path,
    ) -> Dict[str, Any]:
        """Reset every camera, including all UVC interfaces on each device."""
        descriptors = []
        seen_serials = set()
        for camera in cameras:
            serial = str(camera.get("serial", ""))
            if serial and serial in seen_serials:
                continue
            if serial:
                seen_serials.add(serial)
            descriptors.append(camera)

        result: Dict[str, Any] = {
            "attempted": True,
            "method": "full-reset",
            "success": False,
            "camera_count": len(descriptors),
            "cameras": [],
        }
        if not descriptors:
            result["error"] = "failed run did not report any camera descriptors"
        else:
            result["cameras"] = [
                self._recover_camera(camera) for camera in descriptors
            ]
            result["success"] = all(
                bool(camera.get("success", False))
                for camera in result["cameras"]
            )
            errors = [
                f"{camera.get('serial', 'unknown')}: {camera.get('error', '')}"
                for camera in result["cameras"]
                if camera.get("error")
            ]
            if errors:
                result["error"] = " | ".join(errors)

        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "recovery.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result
