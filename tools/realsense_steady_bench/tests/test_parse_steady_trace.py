#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from parse_steady_trace import parse_steady_trace  # noqa: E402


class ParseSteadyTraceTests(unittest.TestCase):
    def test_groups_preemption_fragments_into_one_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            lime = root / "lime"
            output = root / "output"
            lime.mkdir()

            lifecycle_events = [
                {
                    "event": "phase_marker",
                    "timestamp_ns": 900_000_000,
                    "tid": 100,
                    "name": "process_start",
                },
                {
                    "event": "pthread_create",
                    "timestamp_ns": 910_000_000,
                    "caller_tid": 100,
                    "pthread_value": "abc",
                    "success": True,
                },
                {
                    "event": "thread_start",
                    "timestamp_ns": 920_000_000,
                    "tid": 101,
                    "parent_tid": 100,
                    "pthread_value": "abc",
                    "create_timestamp_ns": 910_000_000,
                    "name": "worker",
                },
                {
                    "event": "phase_marker",
                    "timestamp_ns": 1_000_000_000,
                    "tid": 100,
                    "name": "steady_state_begin",
                },
                {
                    "event": "phase_marker",
                    "timestamp_ns": 5_000_000_000,
                    "tid": 100,
                    "name": "steady_state_end",
                },
                {
                    "event": "thread_exit",
                    "timestamp_ns": 5_100_000_000,
                    "tid": 101,
                    "pthread_value": "abc",
                    "name": "worker",
                },
            ]
            lifecycle.write_text(
                "".join(json.dumps(event) + "\n" for event in lifecycle_events),
                encoding="utf-8",
            )
            (lime / "101-0.infos.json").write_text(
                json.dumps(
                    {
                        "pid": 101,
                        "tgid": 100,
                        "policy": {"SCHED_OTHER": {}},
                    }
                ),
                encoding="utf-8",
            )
            (lime / "101-0.events.json").write_text(
                json.dumps(
                    [
                        {"event": "sched_wake_up", "ts": 1_100_000_000, "cpu": 1},
                        {"event": "sched_switched_in", "ts": 1_200_000_000, "cpu": 1},
                        {
                            "event": "sched_switched_out",
                            "ts": 1_300_000_000,
                            "cpu": 1,
                            "state": 0,
                        },
                        {"event": "sched_switched_in", "ts": 1_350_000_000, "cpu": 2},
                        {
                            "event": "sched_switched_out",
                            "ts": 1_400_000_000,
                            "cpu": 2,
                            "state": 1,
                        },
                        {"event": "sched_wake_up", "ts": 2_000_000_000, "cpu": 2},
                        {"event": "sched_switched_in", "ts": 2_100_000_000, "cpu": 2},
                        {
                            "event": "sched_switched_out",
                            "ts": 2_300_000_000,
                            "cpu": 2,
                            "state": 1,
                        },
                        {"event": "sched_wake_up", "ts": 3_000_000_000, "cpu": 3},
                        {"event": "sched_switched_in", "ts": 3_050_000_000, "cpu": 3},
                        {
                            "event": "sched_switched_out",
                            "ts": 3_200_000_000,
                            "cpu": 3,
                            "state": 1,
                        },
                    ]
                ),
                encoding="utf-8",
            )

            summary = parse_steady_trace(lifecycle, lime, output)
            worker = next(row for row in summary["threads"] if row["tid"] == 101)
            self.assertEqual(worker["activation_count"], 3)
            self.assertEqual(worker["complete_activation_count"], 2)
            self.assertAlmostEqual(worker["execution_ms"]["max"], 200.0)
            self.assertEqual(worker["cpus"], [1, 2, 3])
            self.assertAlmostEqual(worker["trace_coverage_ms"], 3900.0)
            self.assertAlmostEqual(worker["unobserved_ms"], 100.0)
            activation_csv = (output / "thread_steady_activations.csv").read_text()
            self.assertIn("worker", activation_csv)
            self.assertIn("150.0,150.0,300.0", activation_csv)

    def test_rejects_missing_measurement_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            lifecycle.write_text(
                json.dumps(
                    {
                        "event": "phase_marker",
                        "timestamp_ns": 1,
                        "tid": 1,
                        "name": "process_start",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "steady_state_begin"):
                parse_steady_trace(lifecycle, root / "lime", root / "output")


if __name__ == "__main__":
    unittest.main()
