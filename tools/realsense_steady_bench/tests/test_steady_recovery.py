#!/usr/bin/env python3

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = TOOL_DIR.parent
BENCHKIT_DIR = TOOLS_DIR.parent / "deps" / "benchkit"
sys.path.insert(0, str(BENCHKIT_DIR))
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.recovery import (  # noqa: E402
    CameraRecoveryConfig,
    MultiCameraFullReset,
)
from steady_benchmark import RealSenseSteadyBench  # noqa: E402
from steady_attempts import camera_descriptors  # noqa: E402


class _SystemControls:
    backend_name = "V4L2"
    config = SimpleNamespace(
        cpu_frequency_mhz=1500,
        disable_realsense_autosuspend=True,
    )

    def prepare_campaign(self, record_dir):
        del record_dir

    @staticmethod
    def cpu_isolation_state():
        return {"enabled": False, "active": False, "xhci_irqs": []}

    def prepare_attempt(self, attempt, attempt_dir):
        del attempt, attempt_dir

    def prepare_rsusb_access(self):
        pass

    def verify_cpu_isolation(self, output):
        del output


class _NoiseSuite:
    def manifest(self, noise_modes):
        return dict(noise_modes)


class _Recovery:
    def __init__(self):
        self.config = SimpleNamespace(
            reset_timeout_ms=5000,
            enumeration_timeout_seconds=1.2,
        )
        self.calls = []
        self.pre_run_calls = []

    def recover(self, cameras, record_dir):
        descriptors = [dict(camera) for camera in cameras]
        self.calls.append(descriptors)
        result = {
            "attempted": True,
            "method": "full-reset",
            "success": True,
            "camera_count": len(descriptors),
            "cameras": [
                {"serial": camera["serial"], "success": True}
                for camera in descriptors
            ],
        }
        (record_dir / "recovery.json").write_text(
            json.dumps(result) + "\n", encoding="utf-8"
        )
        return result

    def reset_before_run(self, cameras, record_dir):
        descriptors = [dict(camera) for camera in cameras]
        self.pre_run_calls.append(descriptors)
        result = {
            "attempted": True,
            "method": "full-reset",
            "operation": "pre-run-reset",
            "success": True,
            "camera_count": len(descriptors),
            "duration_ms": 1.0,
        }
        (record_dir / "pre_run_reset.json").write_text(
            json.dumps(result) + "\n", encoding="utf-8"
        )
        return result


class _FakeSteadyBench(RealSenseSteadyBench):
    serials = ("camera-a", "camera-b")

    def __init__(self, summaries):
        self._cases = {
            "two-camera": {
                "case_id": "two-camera",
                "probe": {"serials": list(self.serials)},
            }
        }
        self._priority = 80
        self._use_lime = True
        self._drop_caches_before_run = False
        self._recover_on_failure = "full-reset"
        self._recovery_settle_seconds = 0.0
        self._max_attempts_per_run = 3
        self._recovery = _Recovery()
        self._system_controls = _SystemControls()
        self._noise_suite = _NoiseSuite()
        self._summaries = summaries

    def _run_attempt(
        self,
        *,
        case_id,
        case,
        policy,
        attempt,
        attempt_dir,
        base_manifest,
        noise_modes,
        kwargs,
    ):
        del case_id, case, policy, base_manifest, noise_modes, kwargs
        attempt_dir.mkdir(parents=True, exist_ok=False)
        summary = dict(self._summaries[attempt - 1])
        (attempt_dir / "steady_summary.json").write_text(
            json.dumps(summary) + "\n", encoding="utf-8"
        )
        (attempt_dir / "probe_stdout.txt").write_text(
            f"attempt {attempt}\n", encoding="utf-8"
        )
        return f"attempt {attempt}\n", summary


def _summary(*, success, measurement_start, error=""):
    return {
        "success": success,
        "error": error,
        "run": {"camera_count": 2},
        "measurement": {
            "start_boottime_ns": measurement_start,
            "end_boottime_ns": measurement_start,
            "duration_ms": 0.0,
        },
        "aggregate": {},
        "cameras": [
            {
                "serial": "camera-a",
                "physical_port": "/sys/devices/usb3/3-1",
                "warmup_deliveries": 30,
                "timeouts": 0,
            },
            {
                "serial": "camera-b",
                "physical_port": "/sys/devices/usb4/4-1",
                "warmup_deliveries": 0 if not measurement_start else 30,
                "timeouts": 1,
            },
        ],
    }


class SteadyRunRetryTest(unittest.TestCase):
    def test_rsusb_camera_descriptors_use_explicit_sysfs_ports(self):
        case = {
            "probe": {
                "serials": ["camera-a", "camera-b"],
                "rsusb_usb_devices": ["3-1", "5-1"],
            }
        }
        summary = {
            "cameras": [
                {"serial": "camera-a", "physical_port": "3-1-42"},
                {"serial": "camera-b", "physical_port": "5-1-43"},
            ]
        }

        self.assertEqual(
            camera_descriptors(case, summary),
            [
                {
                    "serial": "camera-a",
                    "physical_port": "/sys/bus/usb/devices/3-1",
                },
                {
                    "serial": "camera-b",
                    "physical_port": "/sys/bus/usb/devices/5-1",
                },
            ],
        )

    def test_pre_run_reset_covers_every_camera_before_attempt_one(self):
        bench = _FakeSteadyBench(
            [_summary(success=True, measurement_start=123456)]
        )
        bench._reset_before_run = True

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            bench.single_run(
                case_id="two-camera",
                policy="other",
                cpu_noise="none",
                memory_noise="none",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )

            self.assertEqual(len(bench._recovery.pre_run_calls), 1)
            self.assertEqual(
                [
                    camera["serial"]
                    for camera in bench._recovery.pre_run_calls[0]
                ],
                ["camera-a", "camera-b"],
            )
            self.assertEqual(bench._recovery.calls, [])
            self.assertTrue((record_dir / "pre_run_reset.json").is_file())

    def test_pre_run_reset_failure_recovers_and_retries_same_run(self):
        bench = _FakeSteadyBench(
            [
                _summary(success=True, measurement_start=111111),
                _summary(success=True, measurement_start=222222),
            ]
        )
        bench._reset_before_run = True
        bench._recovery.reset_before_run = mock.Mock(
            side_effect=RuntimeError("camera did not reappear")
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            output = bench.single_run(
                case_id="two-camera",
                policy="other",
                cpu_noise="none",
                memory_noise="none",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )

            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )
            first_attempt = json.loads(
                (record_dir / "attempt-1" / "steady_summary.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(output, "attempt 2\n")
            self.assertEqual(len(bench._recovery.calls), 1)
            self.assertEqual(
                [camera["serial"] for camera in bench._recovery.calls[0]],
                ["camera-a", "camera-b"],
            )
            self.assertTrue(
                first_attempt["error"].startswith("Pre-run reset failed:")
            )
            self.assertEqual(summary["attempt_count"], 2)
            self.assertEqual(summary["failed_attempt_count"], 1)
            self.assertFalse(summary["initial_attempt_success"])
            self.assertTrue(summary["eventual_success"])
            self.assertEqual(summary["attempts"][0]["failure_phase"], "startup")
            self.assertTrue(summary["attempts"][1]["success"])

    def test_startup_failure_resets_both_cameras_and_retries_same_run(self):
        bench = _FakeSteadyBench(
            [
                _summary(
                    success=False,
                    measurement_start=0,
                    error="camera-b: Frame didn't arrive within 1500",
                ),
                _summary(success=True, measurement_start=123456),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            output = bench.single_run(
                case_id="two-camera",
                policy="other",
                cpu_noise="none",
                memory_noise="none",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )

            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )
            attempts = json.loads(
                (record_dir / "attempts.json").read_text(encoding="utf-8")
            )

            self.assertEqual(output, "attempt 2\n")
            self.assertEqual(len(bench._recovery.calls), 1)
            self.assertEqual(
                [camera["serial"] for camera in bench._recovery.calls[0]],
                ["camera-a", "camera-b"],
            )
            self.assertTrue((record_dir / "attempt-1" / "recovery.json").is_file())
            self.assertTrue((record_dir / "attempt-2").is_dir())
            self.assertEqual(summary["attempt_count"], 2)
            self.assertEqual(summary["failed_attempt_count"], 1)
            self.assertFalse(summary["initial_attempt_success"])
            self.assertTrue(summary["eventual_success"])
            self.assertEqual(summary["selected_attempt"], 2)
            self.assertEqual(attempts[0]["failure_phase"], "startup")
            self.assertTrue(attempts[1]["success"])

    def test_scheduler_setup_failure_is_not_reset_or_retried(self):
        bench = _FakeSteadyBench(
            [
                _summary(
                    success=False,
                    measurement_start=0,
                    error="SCHED_DEADLINE setup failed: profile mismatch",
                ),
                _summary(success=True, measurement_start=223456),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            bench.single_run(
                case_id="two-camera",
                policy="deadline",
                cpu_noise="none",
                memory_noise="none",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )
            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )

            self.assertEqual(bench._recovery.calls, [])
            self.assertEqual(summary["attempt_count"], 1)
            self.assertFalse(summary["eventual_success"])
            self.assertEqual(
                summary["attempts"][0]["failure_phase"], "scheduler_setup"
            )

    def test_rate_monotonic_setup_failure_is_not_reset_or_retried(self):
        bench = _FakeSteadyBench(
            [
                _summary(
                    success=False,
                    measurement_start=0,
                    error="Rate-monotonic setup failed: profile mismatch",
                ),
                _summary(success=True, measurement_start=223456),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            bench.single_run(
                case_id="two-camera",
                policy="rr-rm",
                cpu_noise="none",
                memory_noise="none",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )
            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )

            self.assertEqual(bench._recovery.calls, [])
            self.assertEqual(summary["attempt_count"], 1)
            self.assertFalse(summary["eventual_success"])
            self.assertEqual(
                summary["attempts"][0]["failure_phase"], "scheduler_setup"
            )

    def test_measurement_failure_is_reset_and_retried(self):
        bench = _FakeSteadyBench(
            [
                _summary(
                    success=False,
                    measurement_start=123456,
                    error="Timed out during steady-state measurement",
                ),
                _summary(success=True, measurement_start=223456),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            bench.single_run(
                case_id="two-camera",
                policy="other",
                cpu_noise="none",
                memory_noise="none",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )
            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )

            self.assertEqual(len(bench._recovery.calls), 1)
            self.assertEqual(summary["attempt_count"], 2)
            self.assertTrue(summary["eventual_success"])
            self.assertEqual(summary["attempts"][0]["failure_phase"], "measurement")

    def test_noise_setup_failure_is_not_reset_or_retried(self):
        bench = _FakeSteadyBench(
            [
                _summary(
                    success=False,
                    measurement_start=0,
                    error="Noise setup failed: memory noise exited before ready",
                ),
                _summary(success=True, measurement_start=223456),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            bench.single_run(
                case_id="two-camera",
                policy="other",
                cpu_noise="none",
                memory_noise="fixed_copy",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )
            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )

            self.assertEqual(bench._recovery.calls, [])
            self.assertEqual(summary["attempt_count"], 1)
            self.assertFalse(summary["eventual_success"])
            self.assertEqual(summary["attempts"][0]["failure_phase"], "noise_setup")

    def test_frame_failure_during_noise_transition_is_reset_and_retried(self):
        bench = _FakeSteadyBench(
            [
                _summary(
                    success=False,
                    measurement_start=0,
                    error=(
                        "Noise transition frame failure: camera-b: "
                        "Frame didn't arrive within 1500"
                    ),
                ),
                _summary(success=True, measurement_start=223456),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            bench.single_run(
                case_id="two-camera",
                policy="other",
                cpu_noise="none",
                memory_noise="fixed_copy",
                gpu_noise="none",
                usb_storage_noise="none",
                record_data_dir=record_dir,
            )
            summary = json.loads(
                (record_dir / "steady_summary.json").read_text(encoding="utf-8")
            )

            self.assertEqual(len(bench._recovery.calls), 1)
            self.assertEqual(summary["attempt_count"], 2)
            self.assertTrue(summary["eventual_success"])
            self.assertEqual(
                summary["attempts"][0]["failure_phase"], "noise_transition"
            )


class MultiCameraFullResetTest(unittest.TestCase):
    def test_every_unique_selected_camera_is_reset(self):
        config = CameraRecoveryConfig(
            repo_root=Path("/repo"),
            reset_probe=Path("/repo/probe"),
            use_sudo=False,
            reset_timeout_ms=5000,
            enumeration_timeout_seconds=1.2,
        )
        recovery = MultiCameraFullReset(config)

        def recover_camera(camera):
            return {"serial": camera["serial"], "success": True}

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            recovery, "_recover_camera", side_effect=recover_camera
        ) as reset:
            result = recovery.recover(
                [
                    {"serial": "camera-a"},
                    {"serial": "camera-b"},
                    {"serial": "camera-a"},
                ],
                Path(temporary),
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["camera_count"], 2)
            self.assertEqual(reset.call_count, 2)
            self.assertEqual(
                [call.args[0]["serial"] for call in reset.call_args_list],
                ["camera-a", "camera-b"],
            )


if __name__ == "__main__":
    unittest.main()
