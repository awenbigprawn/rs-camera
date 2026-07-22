import unittest

from pathlib import Path
import sys

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from calibrate_startup_timings import (
    STAGE_FRAME,
    STAGE_JOIN,
    parse_probe_output,
    rounded_timeout,
    summarize_candidates,
)


class CalibrateStartupTimingsTest(unittest.TestCase):
    def test_parse_probe_output_extracts_wait_metric(self):
        output = (
            'RS_STARTUP_CYCLE {"cycle":1,"success":true,"framesets":10,'
            '"start_call_ms":30.0,"first_frame_ms":600.0,'
            '"first_frame_wait_ms":520.0,"stop_call_ms":20.0,'
            '"join_wait_ms":0.5,"cycle_ms":1000.0,'
            '"threads_after_start":17,"extra_threads_after_join":0}\n'
            'RS_STARTUP_RESULT {"success":true,"completed_cycles":1,'
            '"requested_cycles":1}\n'
        )
        result = parse_probe_output(output, returncode=0, requested_cycles=1)
        self.assertTrue(result["success"])
        self.assertEqual(result["first_frame_wait_ms_mean"], 520.0)
        self.assertEqual(result["first_frame_wait_ms_max"], 520.0)
        self.assertEqual(result["join_wait_ms_max"], 0.5)

    def test_frame_timeout_requires_configured_headroom(self):
        rows = [
            {
                "stage": STAGE_FRAME,
                "candidate_ms": 750,
                "success": True,
                "cycles_completed": 10,
                "first_frame_wait_ms_max": 600.0,
                "join_wait_ms_max": 0.5,
                "elapsed_ms": 1000.0,
            },
            {
                "stage": STAGE_FRAME,
                "candidate_ms": 1000,
                "success": True,
                "cycles_completed": 10,
                "first_frame_wait_ms_max": 600.0,
                "join_wait_ms_max": 0.5,
                "elapsed_ms": 1000.0,
            },
        ]
        summaries, selected = summarize_candidates(
            stage=STAGE_FRAME,
            candidates=[750, 1000],
            rows=rows,
            trials=1,
            cycles_per_trial=10,
            safety_factor=1.5,
        )
        self.assertFalse(summaries[0]["qualified"])
        self.assertTrue(summaries[1]["qualified"])
        self.assertEqual(summaries[0]["headroom_required_ms"], 900)
        self.assertEqual(selected, 1000)

    def test_frame_timeout_uses_global_headroom_across_stages(self):
        rows = [
            {
                "stage": STAGE_FRAME,
                "candidate_ms": 1000,
                "success": True,
                "cycles_completed": 10,
                "first_frame_wait_ms_max": 588.473,
                "join_wait_ms_max": 0.5,
                "elapsed_ms": 1000.0,
            },
            {
                "stage": STAGE_FRAME,
                "candidate_ms": 1500,
                "success": True,
                "cycles_completed": 10,
                "first_frame_wait_ms_max": 913.080,
                "join_wait_ms_max": 0.5,
                "elapsed_ms": 1000.0,
            },
        ]
        summaries, selected = summarize_candidates(
            stage=STAGE_FRAME,
            candidates=[1000, 1500],
            rows=rows,
            trials=1,
            cycles_per_trial=10,
            safety_factor=1.5,
            headroom_rows=rows,
        )
        self.assertEqual(summaries[0]["headroom_required_ms"], 1370)
        self.assertFalse(summaries[0]["qualified"])
        self.assertTrue(summaries[1]["qualified"])
        self.assertEqual(selected, 1500)

    def test_join_timeout_includes_polling_margin(self):
        rows = [
            {
                "stage": STAGE_JOIN,
                "candidate_ms": 5,
                "success": True,
                "cycles_completed": 10,
                "first_frame_wait_ms_max": 500.0,
                "join_wait_ms_max": 0.8,
                "elapsed_ms": 1000.0,
            },
            {
                "stage": STAGE_JOIN,
                "candidate_ms": 10,
                "success": True,
                "cycles_completed": 10,
                "first_frame_wait_ms_max": 500.0,
                "join_wait_ms_max": 0.8,
                "elapsed_ms": 1000.0,
            },
        ]
        summaries, selected = summarize_candidates(
            stage=STAGE_JOIN,
            candidates=[5, 10],
            rows=rows,
            trials=1,
            cycles_per_trial=10,
            safety_factor=1.5,
        )
        self.assertEqual(summaries[0]["headroom_required_ms"], 7)
        self.assertFalse(summaries[0]["qualified"])
        self.assertEqual(selected, 10)

    def test_reset_timeout_recommendation_has_floor_and_margin(self):
        self.assertEqual(rounded_timeout(100.0, 1.5, 1000), 1200)
        self.assertEqual(rounded_timeout(2000.0, 1.5, 5000), 5000)


if __name__ == "__main__":
    unittest.main()
