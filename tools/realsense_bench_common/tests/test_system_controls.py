#!/usr/bin/env python3

from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.system_controls import (  # noqa: E402
    SystemControlConfig,
    SystemControls,
)


class SystemControlsUsbPowerTest(unittest.TestCase):
    def _controls(self, root: Path) -> SystemControls:
        return SystemControls(
            SystemControlConfig(
                repo_root=root,
                tool_dir=root,
                use_sudo=False,
                cpu_frequency_mhz=None,
                cpu_lock_script=root / "unused-lock",
                cpu_restore_script=root / "unused-restore",
                rsusb_backend=False,
                rsusb_usb_devices=(),
                rsusb_helper=root / "unused-rsusb",
                disable_realsense_autosuspend=True,
                usb_sysfs_base=root / "usb-devices",
            )
        )

    def test_enforces_power_control_on_for_every_connected_d435(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, serial in (("3-1", "camera-a"), ("5-1", "camera-b")):
                device = root / "usb-devices" / name
                (device / "power").mkdir(parents=True)
                (device / "idVendor").write_text("8086\n", encoding="utf-8")
                (device / "idProduct").write_text("0b07\n", encoding="utf-8")
                (device / "serial").write_text(serial + "\n", encoding="utf-8")
                (device / "power" / "control").write_text(
                    "auto\n", encoding="utf-8"
                )

            output = root / "autosuspend.json"
            states = self._controls(root)._enforce_realsense_autosuspend(output)

            self.assertEqual(len(states), 2)
            self.assertTrue(output.is_file())
            self.assertEqual(
                (root / "usb-devices" / "3-1" / "power" / "control")
                .read_text(encoding="utf-8")
                .strip(),
                "on",
            )

    def test_rejects_missing_camera(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usb-devices").mkdir()
            with self.assertRaisesRegex(RuntimeError, "No connected"):
                self._controls(root)._enforce_realsense_autosuspend(None)


if __name__ == "__main__":
    unittest.main()
