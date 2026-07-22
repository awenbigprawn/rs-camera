import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from parse_startup_trace import parse_startup_trace
from run_startup_campaign import RealSenseStartupBench


class RetryStartupRunTest(unittest.TestCase):
    class _FailThenSucceedBench(RealSenseStartupBench):
        def __init__(self):
            self._cycles = 10
            self._frames = 10
            self._priority = 0
            self._serial = "test-serial"
            self._frame_timeout_ms = 1500
            self._join_timeout_ms = 10
            self._recovery_reset_timeout_ms = 5000
            self._recover_on_failure = "depth-prime"
            self._recovery_wait_seconds = 10.0
            self._recovery_settle_seconds = 0.0
            self._max_attempts_per_run = 3
            self._probe = Path("/test/d435_sensor_probe")
            self._lime = Path("/test/lime-rtw")
            self.recovery_calls = 0

        def _run_attempt(
            self,
            policy,
            cycle_delay_ms,
            attempt,
            attempt_dir,
            base_manifest,
            kwargs,
        ):
            del policy, cycle_delay_ms, base_manifest, kwargs
            attempt_dir.mkdir(parents=True, exist_ok=False)
            success = attempt == 2
            summary = {
                "device": {
                    "physical_port": (
                        "/sys/devices/usb2/2-1/2-1:1.0/video4linux/video4"
                    )
                },
                "startup_result": {
                    "success": success,
                    "completed_cycles": 10 if success else 0,
                },
                "cycles_started": 10 if success else 1,
                "startup_error": (
                    {}
                    if success
                    else {
                        "kind": "librealsense",
                        "message": "Frame did not arrive within 1500",
                    }
                ),
            }
            return f"attempt {attempt} output\n", summary

        def _recover_depth_prime(self, device, record_dir):
            del device
            self.recovery_calls += 1
            result = {
                "attempted": True,
                "method": "depth-prime",
                "success": True,
                "captured_buffers": 10,
            }
            (record_dir / "recovery.json").write_text(
                json.dumps(result) + "\n", encoding="utf-8"
            )
            return result

    def test_full_reset_resets_firmware_then_composite_usb_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bench = object.__new__(RealSenseStartupBench)
            bench._probe = Path("/test/d435_sensor_probe")
            bench._serial = "test-serial"
            bench._use_sudo = False
            bench._recovery_wait_seconds = 1.2
            bench._recovery_reset_timeout_ms = 5000
            completed = mock.Mock(
                returncode=0,
                stdout=(
                    'RS_HARDWARE_RESET {"state":"requested"}\n'
                    'RS_HARDWARE_RESET {"state":"complete"}\n'
                ),
                stderr="",
            )
            usb_result = {
                "attempted": True,
                "method": "usb",
                "success": True,
                "target": "002/003",
            }

            with mock.patch(
                "run_startup_campaign.subprocess.run", return_value=completed
            ) as run_mock, mock.patch.object(
                bench, "_recover_usb_device", return_value=usb_result
            ) as usb_mock:
                result = bench._recover_full_reset(
                    {
                        "physical_port": (
                            "/sys/devices/usb2/2-1/2-1:1.0/video4linux/video4"
                        )
                    },
                    root,
                )

            self.assertTrue(result["success"])
            self.assertTrue(result["hardware_reset"]["success"])
            self.assertTrue(result["usb_reset"]["success"])
            self.assertEqual(result["method"], "full-reset")
            command = run_mock.call_args.args[0]
            self.assertIn("--hardware-reset", command)
            self.assertIn("--serial", command)
            self.assertEqual(command[command.index("--reset-timeout-ms") + 1], "5000")
            self.assertEqual(run_mock.call_args.kwargs["timeout"], 10.0)
            usb_mock.assert_called_once()
            written = json.loads(
                (root / "recovery.json").read_text(encoding="utf-8")
            )
            self.assertTrue(written["success"])

    def test_failed_attempt_is_recovered_and_same_run_is_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bench = self._FailThenSucceedBench()

            output = bench.single_run(
                policy="other",
                cycle_delay_ms=500,
                record_data_dir=root,
            )

            summary = json.loads(
                (root / "summary.json").read_text(encoding="utf-8")
            )
            attempts = json.loads(
                (root / "attempts.json").read_text(encoding="utf-8")
            )
            self.assertEqual(output, "attempt 2 output\n")
            self.assertEqual(bench.recovery_calls, 1)
            self.assertTrue((root / "attempt-1" / "recovery.json").is_file())
            self.assertTrue((root / "attempt-2").is_dir())
            self.assertFalse((root / "attempt-3").exists())
            self.assertEqual(summary["attempt_count"], 2)
            self.assertEqual(summary["failed_attempt_count"], 1)
            self.assertFalse(summary["initial_attempt_success"])
            self.assertTrue(summary["eventual_success"])
            self.assertEqual(summary["recovery"]["count"], 1)
            self.assertFalse(attempts[0]["success"])
            self.assertTrue(attempts[1]["success"])
            self.assertEqual(
                (root / "selected_attempt.txt").read_text(encoding="utf-8"),
                "2\n",
            )


class ParseStartupTraceTest(unittest.TestCase):
    def test_depth_video_node_comes_from_physical_port(self):
        self.assertEqual(
            RealSenseStartupBench._depth_video_node(
                "/sys/devices/usb2/2-1/2-1:1.0/video4linux/video4"
            ),
            Path("/dev/video4"),
        )
        self.assertIsNone(RealSenseStartupBench._depth_video_node("unknown"))

    def test_kernel_log_delta_uses_exact_run_window(self):
        delta, error = RealSenseStartupBench._kernel_log_delta(
            "old one\nold two\n",
            "old one\nold two\nnew one\nnew two\n",
        )
        self.assertEqual(delta, "new one\nnew two\n")
        self.assertEqual(error, "")

        delta, error = RealSenseStartupBench._kernel_log_delta(
            "lost anchor\n",
            "rotated buffer\n",
        )
        self.assertEqual(delta, "")
        self.assertIn("anchor was lost", error)

    def test_merges_pthread_lifecycle_and_scheduler_intervals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lime = root / "lime"
            output = root / "output"
            lime.mkdir()
            output.mkdir()
            (output / "kernel_log.txt").write_text(
                "<3>[   1.4] uvcvideo 2-1:1.2: Failed to resubmit video URB (-1).\n"
                "<3>[   1.6] uvcvideo 2-1:1.2: Failed to resubmit video URB (-1).\n"
                "<3>[   1.9] uvcvideo 2-1:1.4: Failed to resubmit video URB (-1).\n",
                encoding="utf-8",
            )

            lifecycle = [
                {"event": "phase_marker", "timestamp_ns": 1_000_000_000, "tid": 100, "name": "process_start"},
                {"event": "phase_marker", "timestamp_ns": 1_100_000_000, "tid": 100, "name": "cycle_01_begin"},
{"event": "phase_marker", "timestamp_ns": 1_300_000_000, "tid": 100, "name": "cycle_01_after_pipeline_start"},
{"event": "phase_marker", "timestamp_ns": 1_800_000_000, "tid": 100, "name": "cycle_01_frames_complete"},
{"event": "phase_marker", "timestamp_ns": 1_820_000_000, "tid": 100, "name": "cycle_01_before_object_destruction"},
                {"event": "pthread_create", "timestamp_ns": 1_200_000_000, "return_timestamp_ns": 1_201_000_000,
                 "caller_tid": 100, "pthread_value": "0xabc", "result": 0, "success": True},
                {"event": "thread_start", "timestamp_ns": 1_250_000_000, "tid": 101, "parent_tid": 100,
                 "pthread_value": "0xabc", "create_timestamp_ns": 1_200_000_000, "name": "worker"},
                {"event": "thread_exit", "timestamp_ns": 1_900_000_000, "tid": 101,
                 "pthread_value": "0xabc", "name": "worker", "exit_kind": "return"},
                {"event": "pthread_join_end", "timestamp_ns": 2_050_000_000, "caller_tid": 100,
                 "pthread_value": "0xabc", "duration_ns": 10, "result": 0},
                {"event": "phase_marker", "timestamp_ns": 2_080_000_000, "tid": 100, "name": "cycle_01_threads_joined"},
                {"event": "phase_marker", "timestamp_ns": 2_100_000_000, "tid": 100, "name": "cycle_01_end"},
                {"event": "phase_marker", "timestamp_ns": 2_200_000_000, "tid": 100, "name": "process_exit"},
            ]
            lifecycle_path = root / "thread_lifecycle.jsonl"
            lifecycle_path.write_text(
                "".join(json.dumps(event) + "\n" for event in lifecycle),
                encoding="utf-8",
            )

            (lime / "100-0.infos.json").write_text(json.dumps({
                "pid": 100, "tgid": 100, "comm": "d435-probe", "policy": "SCHED_OTHER",
                "first_event_time": {"boottime_ns": 1_000_000_000, "iso8601": ""},
                "last_event_time": {"boottime_ns": 2_200_000_000, "iso8601": ""},
            }), encoding="utf-8")
            (lime / "100-0.events.json").write_text("[]", encoding="utf-8")
            (lime / "101-0.infos.json").write_text(json.dumps({
                "pid": 101, "tgid": 100, "comm": "worker", "policy": "SCHED_OTHER",
                "first_event_time": {"boottime_ns": 1_240_000_000, "iso8601": ""},
                "last_event_time": {"boottime_ns": 1_900_000_000, "iso8601": ""},
            }), encoding="utf-8")
            worker_events = [
                {"ts": 1_240_000_000, "event": "sched_wake_up_new", "cpu": 2},
                {"ts": 1_260_000_000, "event": "sched_switched_in", "cpu": 2, "prio": 120, "preempt": False},
                {"ts": 1_390_000_000, "event": "enter_poll"},
                {"ts": 1_400_000_000, "event": "sched_switched_out", "cpu": 2, "prio": 120, "state": 1},
                {"ts": 1_500_000_000, "event": "sched_wake_up", "cpu": 2},
                {"ts": 1_550_000_000, "event": "sched_switched_in", "cpu": 2, "prio": 120, "preempt": False},
                {"ts": 1_700_000_000, "event": "sched_switched_out", "cpu": 2, "prio": 120, "state": 0},
                {"ts": 1_800_000_000, "event": "sched_switched_in", "cpu": 2, "prio": 120, "preempt": True},
                {"ts": 1_850_000_000, "event": "sched_switched_out", "cpu": 2, "prio": 120, "state": 1},
                {"ts": 1_900_000_000, "event": "sched_process_exit"},
            ]
            (lime / "101-0.events.json").write_text(json.dumps(worker_events), encoding="utf-8")

            stdout = root / "stdout.txt"
            stdout.write_text(
                'RS_DEVICE {"name":"Intel RealSense D435","serial":"123",'
                '"physical_port":"/sys/devices/usb2/2-1/2-1:1.0/video4linux/video4",'
                '"product_id":"0B07"}\n'
                'RS_SCHEDULER {"policy":"SCHED_OTHER","policy_id":0,"priority":0}\n'
                'RS_STARTUP_CYCLE {"cycle":1,"success":true,"framesets":10,'
                '"start_call_ms":12.0,"first_frame_ms":20.0,'
                '"first_frame_wait_ms":15.0,"stop_call_ms":2.0,'
                '"join_wait_ms":3.0,"cycle_ms":40.0,"threads_after_start":1,'
                '"extra_threads_after_join":0}\n'
                'RS_STARTUP_RESULT {"success":true,"completed_cycles":1,"requested_cycles":1}\n',
                encoding="utf-8",
            )

            summary = parse_startup_trace(lifecycle_path, lime, output, stdout)
            self.assertEqual(summary["device"]["serial"], "123")
            self.assertEqual(summary["device"]["product_id"], "0B07")
            self.assertEqual(summary["successful_cycles"], 1)
            self.assertEqual(summary["first_frame_wait_ms_mean"], 15.0)
            self.assertEqual(summary["first_frame_wait_ms_max"], 15.0)
            self.assertEqual(summary["thread_instances"], 1)
            self.assertTrue(summary["all_observed_threads_terminated"])
            self.assertEqual(summary["uvc_resubmit_errors"], 3)
            self.assertEqual(summary["uvc_resubmit_interfaces"], "2-1:1.2,2-1:1.4")
            self.assertEqual(summary["uvc_resubmit_error_codes"], "-1:3")
            self.assertEqual(summary["uvc_resubmit_errors_streaming"], 2)
            self.assertEqual(summary["uvc_resubmit_errors_teardown"], 1)
            self.assertEqual(summary["uvc_resubmit_errors_startup"], 0)

            with (output / "thread_timing.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            worker = next(row for row in rows if row["tid"] == "101")
            self.assertEqual(worker["cycle"], "1")
            self.assertEqual(worker["status"], "joined")
            self.assertAlmostEqual(float(worker["create_to_start_ms"]), 50.0)
            self.assertAlmostEqual(float(worker["execution_ms"]), 340.0)
            self.assertAlmostEqual(float(worker["sleep_ms"]), 150.0)
            self.assertAlmostEqual(float(worker["ready_ms"]), 170.0)
            self.assertAlmostEqual(float(worker["first_run_ms"]), 260.0)
            self.assertAlmostEqual(float(worker["first_sleep_ms"]), 400.0)

            with (output / "thread_intervals.csv").open(newline="", encoding="utf-8") as handle:
                intervals = list(csv.DictReader(handle))
            self.assertEqual(len([row for row in intervals if row["tid"] == "101"]), 8)


if __name__ == "__main__":
    unittest.main()
