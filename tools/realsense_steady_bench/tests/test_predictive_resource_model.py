#!/usr/bin/env python3

import unittest

from tools.realsense_steady_bench.analyze_predictive_resource_model import (
    stream_demands,
    workload_rates,
)


class PredictiveResourceModelTest(unittest.TestCase):
    @staticmethod
    def probe(stream_mode: str, fps: int = 30) -> dict[str, object]:
        return {
            "camera_count": 1,
            "stream_mode": stream_mode,
            "fps": fps,
            "depth_width": 848,
            "depth_height": 480,
            "color_width": 640,
            "color_height": 480,
        }

    def test_depth_color_is_decomposed_per_stream(self) -> None:
        streams = stream_demands(self.probe("depth_color"))
        self.assertEqual([stream.name for stream in streams], ["Depth", "Color"])
        self.assertAlmostEqual(sum(stream.payload_mib_s for stream in streams), 40.869141, places=6)
        self.assertAlmostEqual(
            sum(stream.memory_touch_mib_s for stream in streams),
            166.552734,
            places=6,
        )

    def test_depth_ir_has_two_independent_ir_payloads(self) -> None:
        streams = stream_demands(self.probe("depth_ir", fps=60))
        self.assertEqual(
            [stream.name for stream in streams], ["Depth", "IR1", "IR2"]
        )
        self.assertAlmostEqual(sum(stream.payload_mib_s for stream in streams), 93.164062, places=6)
        self.assertAlmostEqual(
            sum(stream.memory_touch_mib_s for stream in streams),
            279.492188,
            places=6,
        )

    def test_camera_count_only_aggregates_per_camera_demand(self) -> None:
        case = {
            "probe": {
                **self.probe("depth_color"),
                "camera_count": 4,
            }
        }
        payload, memory_touch, streams = workload_rates(case)
        self.assertEqual(len(streams), 2)
        self.assertAlmostEqual(payload, 4.0 * 40.869141, places=5)
        self.assertAlmostEqual(memory_touch, 4.0 * 166.552734, places=5)


if __name__ == "__main__":
    unittest.main()
