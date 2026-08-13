import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from build_startup_model import (
    detect_stable_period,
    percentile,
    selected_attempt_dirs,
)


def running_intervals(starts, duration=0.2):
    return [
        {
            "state": "running",
            "start_ms": str(start),
            "end_ms": str(start + duration),
        }
        for start in starts
    ]


class StartupModelTest(unittest.TestCase):
    def test_periodic_detection_allows_missing_releases(self):
        starts = [
            204.0,
            237.3,
            270.6,
            303.9,
            337.2,
            370.5,
            403.8,
            437.1,
            637.0,
            670.3,
            703.6,
            736.9,
            770.2,
            803.5,
        ]
        result = detect_stable_period(
            running_intervals(starts),
            80.0,
            850.0,
            min_periods=6,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.start_ms, 204.0)
        self.assertAlmostEqual(result.period_ms, 33.3, delta=0.2)
        self.assertGreaterEqual(result.match_ratio, 0.9)

    def test_periodic_detection_skips_mixed_rate_startup(self):
        starts = [
            88.6,
            181.8,
            282.7,
            294.2,
            305.0,
            315.6,
            326.4,
            336.9,
            347.5,
            448.0,
            458.9,
            469.6,
            480.0,
            580.5,
            681.3,
            781.8,
            882.5,
            983.1,
            1083.7,
            1184.3,
        ]
        result = detect_stable_period(
            running_intervals(starts),
            80.0,
            1230.0,
            min_periods=6,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.start_ms, 480.0, delta=1.0)
        self.assertAlmostEqual(result.period_ms, 100.6, delta=1.0)

    def test_nonperiodic_activity_is_rejected(self):
        result = detect_stable_period(
            running_intervals([100, 127, 183, 196, 271, 355, 401, 489]),
            80.0,
            550.0,
            min_periods=6,
        )
        self.assertIsNone(result)

    def test_percentile_is_linearly_interpolated(self):
        self.assertEqual(percentile([0.0, 10.0, 20.0], 0.5), 10.0)
        self.assertEqual(percentile([0.0, 10.0, 20.0], 0.25), 5.0)
        self.assertIsNone(percentile([], 0.5))

    def test_process_error_is_excluded_even_when_probe_reported_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary)
            attempt = campaign / "policy-other" / "run-1" / "attempt-1"
            attempt.mkdir(parents=True)
            (attempt / "thread_timing.csv").write_text(
                "header\n", encoding="utf-8"
            )
            (attempt / "summary.json").write_text(
                json.dumps({
                    "process_error": True,
                    "startup_result": {"success": True},
                })
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(selected_attempt_dirs(campaign, "other"), [])


if __name__ == "__main__":
    unittest.main()
