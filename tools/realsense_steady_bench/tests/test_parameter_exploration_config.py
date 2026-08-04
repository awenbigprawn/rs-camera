#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import unittest


CONFIG = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "parameter_exploration_5min.json"
)


class ParameterExplorationConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        cls.cases = {case["case_id"]: case for case in data["cases"]}

    def test_matrix_contains_the_two_expected_workloads(self) -> None:
        self.assertEqual(
            set(self.cases),
            {
                "representative_depth_color_30fps_5min",
                "stress_all_streams_60fps_5min",
            },
        )

    def test_each_case_has_ten_second_warmup_and_five_minute_measurement(self) -> None:
        for case in self.cases.values():
            probe = case["probe"]
            self.assertEqual(probe["warmup_frames"] / probe["fps"], 10)
            self.assertEqual(probe["measurement_duration_ms"], 300000)
            self.assertEqual(case["workload"]["measurement_seconds"], 300)

    def test_representative_profiles(self) -> None:
        probe = self.cases["representative_depth_color_30fps_5min"]["probe"]
        self.assertEqual(probe["stream_mode"], "depth_color")
        self.assertEqual(probe["fps"], 30)
        self.assertEqual((probe["depth_width"], probe["depth_height"]), (848, 480))
        self.assertEqual((probe["color_width"], probe["color_height"]), (640, 480))

    def test_stress_profiles(self) -> None:
        probe = self.cases["stress_all_streams_60fps_5min"]["probe"]
        self.assertEqual(probe["stream_mode"], "d435_all")
        self.assertEqual(probe["fps"], 60)
        self.assertEqual((probe["depth_width"], probe["depth_height"]), (848, 480))
        self.assertEqual((probe["color_width"], probe["color_height"]), (960, 540))


if __name__ == "__main__":
    unittest.main()
