#!/usr/bin/env python3

import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = TOOL_DIR.parent
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from run_multicamera_campaign import (  # noqa: E402
    _stereo_case,
    inventory_layout,
    plan_cases,
    validate_layout,
)


def _camera(label, model, serial, hub, controller):
    product_ids = {"D415": "0ad3", "D435": "0b07", "D455": "0b5c"}
    return {
        "label": label,
        "model": model,
        "serial": serial,
        "librealsense_serial": serial,
        "usb_descriptor_serial": f"usb-{serial}",
        "product_id": product_ids[model],
        "product": f"Intel RealSense {model}",
        "usb_device": f"{hub}.1",
        "upstream_hub": hub,
        "xhci_controller": controller,
        "speed_mbps": "5000",
        "power_control": "on",
    }


class MultiCameraPlanTest(unittest.TestCase):
    def setUp(self):
        self.layout = {
            "schema_version": 2,
            "max_cameras_per_powered_hub": 2,
            "cameras": [
                _camera("d455_1", "D455", "455-a", "3-1", "xhci-hcd.0"),
                _camera("d435_1", "D435", "435-a", "5-1", "xhci-hcd.1"),
                _camera("d455_2", "D455", "455-b", "3-1", "xhci-hcd.0"),
                _camera("d415_1", "D415", "415-a", "5-1", "xhci-hcd.1"),
            ],
        }

    def test_all_plan_is_bounded_and_d415_never_uses_stereo_all(self):
        cases = plan_cases(self.layout, "all", 60)
        self.assertTrue(cases)
        self.assertTrue(all(case["probe"]["camera_count"] <= 4 for case in cases))
        by_serial = {
            camera["serial"]: camera["model"] for camera in self.layout["cameras"]
        }
        for case in cases:
            if case["probe"]["stream_mode"] in {"stereo_all", "d435_all"}:
                self.assertNotIn(
                    "D415",
                    {by_serial[serial] for serial in case["probe"]["serials"]},
                )

    def test_all_plan_does_not_repeat_an_exact_workload_and_camera_set(self):
        cases = plan_cases(self.layout, "all", 60)
        signatures = [
            (
                tuple(sorted(case["probe"]["serials"])),
                case["probe"]["stream_mode"],
                case["probe"]["fps"],
            )
            for case in cases
        ]
        self.assertEqual(len(signatures), len(set(signatures)))

    def test_stereo_stress_uses_model_supported_color_geometry(self):
        d455_layout = dict(self.layout)
        d455_layout["cameras"] = [
            camera
            for camera in self.layout["cameras"]
            if camera["model"] == "D455"
        ]
        d455_cases = plan_cases(d455_layout, "stereo-stress", 60)
        self.assertTrue(d455_cases)
        singleton_labels = {
            case["physical"]["camera_labels"]
            for case in d455_cases
            if case["probe"]["camera_count"] == 1
        }
        self.assertEqual(singleton_labels, {"d455_1", "d455_2"})
        self.assertTrue(
            all(
                (case["probe"]["color_width"], case["probe"]["color_height"])
                == (848, 480)
                for case in d455_cases
            )
        )

        d435_layout = dict(self.layout)
        d435_layout["cameras"] = [
            camera
            for camera in self.layout["cameras"]
            if camera["model"] == "D435"
        ]
        d435_cases = plan_cases(d435_layout, "stereo-stress", 60)
        self.assertEqual(len(d435_cases), 1)
        self.assertEqual(
            (
                d435_cases[0]["probe"]["color_width"],
                d435_cases[0]["probe"]["color_height"],
            ),
            (960, 540),
        )

    def test_stereo_stress_rejects_mixed_model_profile(self):
        selected = [
            self.layout["cameras"][0],
            self.layout["cameras"][1],
        ]
        with self.assertRaisesRegex(ValueError, "model-specific"):
            _stereo_case("mixed", selected, 60)


class MultiCameraInventoryTest(unittest.TestCase):
    @staticmethod
    def _known_camera(model: str, optical: str, usb: str):
        product_ids = {"D415": "0ad3", "D435": "0b07", "D455": "0b5c"}
        return {
            "label": f"{model.lower()}_1",
            "model": model,
            "product_id": product_ids[model],
            "librealsense_serial": optical,
            "usb_descriptor_serial": usb,
        }

    def _add_device(
        self, root: Path, name: str, serial: str, product_id: str, product: str
    ) -> None:
        device = root / name
        (device / "power").mkdir(parents=True)
        values = {
            "idVendor": "8086",
            "idProduct": product_id,
            "serial": serial,
            "manufacturer": "Intel RealSense",
            "product": product,
            "speed": "5000",
        }
        for filename, value in values.items():
            (device / filename).write_text(value + "\n", encoding="utf-8")
        (device / "power" / "control").write_text("on\n", encoding="utf-8")

    def test_inventory_and_preflight_accept_mixed_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._add_device(root, "3-1.1", "455-usb", "0b5c", "D455")
            self._add_device(root, "5-1.1", "415-usb", "0ad3", "D415")
            known = [
                self._known_camera("D455", "455-optical", "455-usb"),
                self._known_camera("D415", "415-optical", "415-usb"),
            ]
            layout = inventory_layout(root, 2, known)
            self.assertEqual(
                {camera["model"] for camera in layout["cameras"]},
                {"D415", "D455"},
            )
            self.assertEqual(
                {camera["serial"] for camera in layout["cameras"]},
                {"415-optical", "455-optical"},
            )
            self.assertEqual(
                {camera["usb_descriptor_serial"] for camera in layout["cameras"]},
                {"415-usb", "455-usb"},
            )
            report = validate_layout(
                layout,
                usb_sysfs_base=root,
                allow_extra_cameras=False,
                allow_topology_change=False,
            )
            self.assertTrue(report["success"])

    def test_preflight_rejects_usb2_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._add_device(root, "3-1.1", "455-usb", "0b5c", "D455")
            layout = inventory_layout(
                root,
                2,
                [self._known_camera("D455", "455-optical", "455-usb")],
            )
            (root / "3-1.1" / "speed").write_text("480\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SuperSpeed"):
                validate_layout(
                    layout,
                    usb_sysfs_base=root,
                    allow_extra_cameras=False,
                    allow_topology_change=False,
                )

    def test_inventory_rejects_camera_missing_from_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._add_device(root, "3-1.1", "new-usb", "0b5c", "D455")
            with self.assertRaisesRegex(ValueError, "absent from the camera registry"):
                inventory_layout(root, 2, [])


if __name__ == "__main__":
    unittest.main()
