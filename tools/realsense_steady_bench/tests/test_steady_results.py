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

from steady_results import parse_steady_results  # noqa: E402


class _NoiseSuite:
    @staticmethod
    def artifacts(modes, record_dir):
        del modes, record_dir
        return []


def _summary(deadline, rate_monotonic=None):
    return {
        "success": True,
        "error": "",
        "scheduler": {
            "policy": "SCHED_OTHER",
            "main_thread_policy": "SCHED_OTHER",
            "steady_worker_policy": (
                "SCHED_DEADLINE"
                if deadline is not None
                else (
                    "SCHED_FIFO"
                    if rate_monotonic is not None
                    else "SCHED_OTHER"
                )
            ),
        },
        "deadline": deadline,
        "rate_monotonic": rate_monotonic,
        "run": {"camera_count": 0},
        "transition": {
            "noise_gate_enabled": False,
            "warmup_ready_boottime_ns": 100,
            "measurement_gate_open_boottime_ns": 100,
            "warmup_to_gate_ms": 0.0,
        },
        "measurement": {
            "mode": "duration",
            "requested_duration_ms": 10,
            "duration_ms": 10.0,
        },
        "aggregate": {
            "frames": 100,
            "observed_frames": 100,
            "unique_frames": 91,
            "duplicate_frames": 9,
            "sequence_gaps": 7,
            "nonadvancing_frames": 10,
            "out_of_order_frames": 1,
            "fully_fresh_framesets": 40,
            "partially_stale_framesets": 5,
            "stale_framesets": 2,
            "timeouts": 3,
            "pre_measurement_timeouts": 1,
            "measurement_timeouts": 2,
        },
        "postprocess": {"freshness_analysis_ms": 1.25},
        "cameras": [],
    }


class SteadyResultSchemaTest(unittest.TestCase):
    def test_modeled_and_non_modeled_rows_have_identical_columns(self):
        run_variables = {
            "policy": "other",
            "cpu_noise": "none",
            "memory_noise": "none",
            "gpu_noise": "none",
            "usb_storage_noise": "none",
        }
        deadline = {
            "assignments": [{"tid": 1, "applied": True}],
            "live_threads": 1,
            "partial_profile": False,
            "profile_entries": 1,
            "profile_path": "/profile.csv",
            "unassigned_live_threads": 0,
        }
        rate_monotonic = {
            "assignments": [{"tid": 2, "applied": True, "priority": 80}],
            "highest_priority": 80,
            "lowest_priority": 79,
            "live_threads": 1,
            "policy": "SCHED_FIFO",
            "priority_levels": 2,
            "profile_entries": 1,
            "profile_path": "/rm-profile.csv",
        }
        rows = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (deadline_value, rm_value) in enumerate(
                ((None, None), (deadline, None), (None, rate_monotonic))
            ):
                record_dir = root / str(index)
                record_dir.mkdir()
                (record_dir / "steady_summary.json").write_text(
                    json.dumps(_summary(deadline_value, rm_value)) + "\n",
                    encoding="utf-8",
                )
                rows.append(
                    parse_steady_results(
                        record_dir=record_dir,
                        case={},
                        run_variables=run_variables,
                        backend_name="V4L2",
                        policy_names={"other": "SCHED_OTHER"},
                        drop_caches_configured=False,
                        noise_suite=_NoiseSuite(),
                        cpu_isolation_state={
                            "enabled": True,
                            "active": True,
                            "housekeeping_cpus": "0",
                            "benchmark_cpus": "1-3",
                            "xhci_irqs": [{"irq": 132}],
                        },
                    )
                )

        self.assertEqual(list(rows[0]), list(rows[1]))
        self.assertEqual(list(rows[0]), list(rows[2]))
        self.assertEqual(rows[0]["deadline_assignments"], "[]")
        self.assertEqual(rows[0]["deadline_overrun_signals"], 0)
        self.assertEqual(rows[1]["deadline_profile_entries"], 1)
        self.assertFalse(rows[1]["deadline_partial_profile"])
        self.assertEqual(rows[1]["deadline_unassigned_live_threads"], 0)
        self.assertEqual(rows[0]["rate_monotonic_assignments"], "[]")
        self.assertEqual(rows[2]["rate_monotonic_priority_levels"], 2)
        self.assertEqual(rows[2]["rate_monotonic_highest_priority"], 80)
        self.assertEqual(rows[0]["measurement_mode"], "duration")
        self.assertEqual(rows[0]["measurement_requested_duration_ms"], 10)
        self.assertEqual(rows[0]["pre_measurement_timeouts"], 1)
        self.assertEqual(rows[0]["measurement_timeouts"], 2)
        self.assertEqual(rows[0]["unique_frames"], 91)
        self.assertEqual(rows[0]["duplicate_frames"], 9)
        self.assertEqual(rows[0]["sequence_gaps"], 7)
        self.assertEqual(rows[0]["stale_framesets"], 2)
        self.assertEqual(rows[0]["freshness_analysis_ms"], 1.25)
        self.assertTrue(rows[0]["cpu_isolation_enabled"])
        self.assertEqual(rows[0]["cpu_isolation_benchmark_cpus"], "1-3")
        self.assertIn("132", rows[0]["cpu_isolation_xhci_irqs"])


if __name__ == "__main__":
    unittest.main()
