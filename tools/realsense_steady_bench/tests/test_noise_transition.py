#!/usr/bin/env python3

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from noise_transition import NoiseTransition  # noqa: E402


class _NoiseSuite:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.start_saw_camera_ready = False

    @staticmethod
    def any_enabled(modes):
        return any(value != "none" for value in modes.values())

    @staticmethod
    def startup_timeout_seconds(modes):
        del modes
        return 0.1

    def start_all(self, modes, record_dir):
        del modes
        self.start_saw_camera_ready = (
            record_dir / "camera_warmup_ready"
        ).is_file()
        self.started.set()
        time.sleep(0.02)
        return {}


class NoiseTransitionTest(unittest.TestCase):
    def test_noise_starts_after_camera_warmup_and_before_measurement_gate(self):
        suite = _NoiseSuite()
        modes = {
            "cpu_noise": "none",
            "memory_noise": "fixed_copy",
            "usb_storage_noise": "none",
            "gpu_noise": "none",
        }
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            transition = NoiseTransition(
                noise_suite=suite,
                modes=modes,
                record_dir=record_dir,
            )
            transition.start()
            self.assertFalse(suite.started.wait(timeout=0.05))
            self.assertFalse(transition.measurement_gate_path.exists())

            transition.warmup_ready_path.write_text("123456\n", encoding="utf-8")
            self.assertTrue(suite.started.wait(timeout=0.5))
            deadline = time.monotonic() + 0.5
            while (
                not transition.measurement_gate_path.is_file()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            transition.finish()

            self.assertTrue(suite.start_saw_camera_ready)
            self.assertTrue(transition.measurement_gate_path.is_file())
            self.assertGreater(
                int(transition.measurement_gate_path.read_text().strip()), 0
            )
            self.assertTrue((record_dir / "noise_transition.json").is_file())

    def test_no_noise_requires_no_gate(self):
        suite = _NoiseSuite()
        modes = {
            "cpu_noise": "none",
            "memory_noise": "none",
            "usb_storage_noise": "none",
            "gpu_noise": "none",
        }
        with tempfile.TemporaryDirectory() as temporary:
            transition = NoiseTransition(
                noise_suite=suite,
                modes=modes,
                record_dir=Path(temporary),
            )
            self.assertFalse(transition.enabled)
            self.assertEqual(transition.probe_arguments(), [])


if __name__ == "__main__":
    unittest.main()
