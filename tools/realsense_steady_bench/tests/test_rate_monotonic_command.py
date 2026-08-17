#!/usr/bin/env python3

from pathlib import Path
import sys
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = TOOL_DIR.parent
BENCHKIT_DIR = TOOL_DIR.parents[1] / "deps" / "benchkit"
sys.path.insert(0, str(BENCHKIT_DIR))
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from steady_benchmark import RealSenseSteadyBench  # noqa: E402


class RateMonotonicCommandTest(unittest.TestCase):
    def _command(self, policy, allow_partial_deadline=False):
        bench = object.__new__(RealSenseSteadyBench)
        bench._priority = 80
        bench._probe = Path("/probe")
        return bench._scheduled_probe(
            case={
                "probe": {
                    "serials": ["camera-a", "camera-b"],
                    "camera_count": 2,
                    "measurement_duration_ms": 600000,
                    "warmup_frames": 300,
                    "warmup_health_window_frames": 30,
                    "deadline_profile": "/source-profile.csv",
                    "deadline_allow_partial_profile": allow_partial_deadline,
                }
            },
            policy=policy,
            summary_path=Path("/summary.json"),
            events_path=Path("/events.csv"),
            scheduler_profile_path=Path("/copied-profile.csv"),
        )

    def test_rr_rm_starts_other_then_requests_per_thread_rr(self):
        command = self._command("rr-rm")
        self.assertEqual(command[:3], ["chrt", "--other", "0"])
        self.assertIn("--rate-monotonic-profile", command)
        self.assertIn("--rate-monotonic-policy", command)
        policy_index = command.index("--rate-monotonic-policy")
        self.assertEqual(command[policy_index + 1], "rr")
        priority_index = command.index("--rate-monotonic-highest-priority")
        self.assertEqual(command[priority_index + 1], "80")

    def test_fifo_rm_starts_other_then_requests_per_thread_fifo(self):
        command = self._command("fifo-rm")
        self.assertEqual(command[:3], ["chrt", "--other", "0"])
        policy_index = command.index("--rate-monotonic-policy")
        self.assertEqual(command[policy_index + 1], "fifo")

    def test_fixed_measurement_duration_is_forwarded_to_probe(self):
        command = self._command("rr-rm")
        duration_index = command.index("--measurement-duration-ms")
        self.assertEqual(command[duration_index + 1], "600000")

    def test_warmup_health_window_is_forwarded_to_probe(self):
        command = self._command("rr-rm")
        window_index = command.index("--warmup-health-window-frames")
        self.assertEqual(command[window_index + 1], "30")

    def test_partial_deadline_profile_is_explicitly_forwarded(self):
        command = self._command("deadline", allow_partial_deadline=True)
        self.assertIn("--deadline-profile", command)
        self.assertIn("--deadline-allow-partial-profile", command)


if __name__ == "__main__":
    unittest.main()
