#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = TOOL_DIR.parent
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from generate_deadline_profile import (  # noqa: E402
    _maximum_job_execution_ns,
    _minimum_stable_period_ns,
    _split_logical_jobs,
    _thread_identities,
    generate_profile,
)
from realsense_bench_common.commands import scheduler_prefix  # noqa: E402


def activation(release_ms: float, execution_ms: float):
    return {
        "release_ns": int(release_ms * 1_000_000),
        "execution_ms": execution_ms,
    }


class DeadlineProfileModelTest(unittest.TestCase):
    def test_profile_identities_exclude_process_main(self):
        events = [
            {
                "event": "phase_marker",
                "timestamp_ns": 1,
                "tid": 100,
                "name": "process_start",
            },
            {
                "event": "pthread_create",
                "timestamp_ns": 2,
                "caller_tid": 100,
                "pthread_value": "abc",
                "success": True,
                "creation_sequence": 1,
                "signature": "entry=worker@0x1",
            },
            {
                "event": "thread_start",
                "timestamp_ns": 3,
                "tid": 101,
                "parent_tid": 100,
                "pthread_value": "abc",
                "create_timestamp_ns": 2,
                "creation_sequence": 1,
                "signature": "entry=worker@0x1",
                "name": "worker",
            },
            {
                "event": "phase_marker",
                "timestamp_ns": 10,
                "tid": 100,
                "name": "steady_state_begin",
            },
            {
                "event": "phase_marker",
                "timestamp_ns": 100,
                "tid": 100,
                "name": "steady_state_end",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = Path(directory) / "thread_lifecycle.jsonl"
            lifecycle.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            identities, begin, end = _thread_identities(lifecycle)

        self.assertEqual(begin, 10)
        self.assertEqual(end, 100)
        self.assertNotIn(100, identities)
        self.assertEqual(identities[101], ("entry=worker@0x1", 1, "worker"))

    def test_burst_activations_are_aggregated_into_one_logical_job(self):
        rows = []
        for base in (0.0, 1000.0, 2000.0, 3000.0):
            rows.extend(
                [
                    activation(base, 0.10),
                    activation(base + 0.5, 0.20),
                    activation(base + 1.0, 0.30),
                ]
            )

        groups, threshold = _split_logical_jobs(rows)

        self.assertIsNotNone(threshold)
        self.assertEqual([len(group) for group in groups], [3, 3, 3, 3])
        self.assertEqual(_minimum_stable_period_ns(groups), 1_000_000_000)
        self.assertEqual(_maximum_job_execution_ns(groups), 600_000)

    def test_minimum_period_is_taken_from_stable_mode_not_one_outlier(self):
        rows = [
            activation(0.0, 0.1),
            activation(33.0, 0.1),
            activation(66.4, 0.1),
            activation(99.7, 0.1),
            activation(133.2, 0.1),
        ]
        groups, threshold = _split_logical_jobs(rows)

        self.assertIsNone(threshold)
        self.assertEqual(_minimum_stable_period_ns(groups), 33_000_000)

    def test_profile_uses_cross_run_max_execution_and_min_period(self):
        key = ("entry=worker@0x1|lib.so@0x2", 1)
        runs = [
            (
                {
                    key: {
                        "name": "worker",
                        "execution_ns": 1_000_000,
                        "period_ns": 12_000_000,
                    }
                },
                {"input": "one"},
            ),
            (
                {
                    key: {
                        "name": "worker",
                        "execution_ns": 2_000_000,
                        "period_ns": 10_000_000,
                    }
                },
                {"input": "two"},
            ),
        ]
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "generate_deadline_profile._trace_threads", side_effect=runs
        ):
            output = Path(directory) / "profile.csv"
            metadata = generate_profile(
                trace_runs=[Path("one"), Path("two")],
                output=output,
                runtime_margin=1.20,
                period_scale=0.91,
                minimum_runtime_us=100,
                maximum_period_us=4_194_304,
            )
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            saved_metadata = json.loads(
                output.with_suffix(".csv.json").read_text(encoding="utf-8")
            )
            profile_bytes = output.read_bytes()

        self.assertNotIn(b"\r\n", profile_bytes)
        self.assertEqual(int(row["runtime_ns"]), 2_400_000)
        self.assertEqual(int(row["deadline_ns"]), 9_100_000)
        self.assertEqual(row["deadline_ns"], row["period_ns"])
        self.assertAlmostEqual(
            metadata["total_reserved_cpu_utilization"], 2.4 / 9.1
        )
        self.assertEqual(saved_metadata["runtime_margin"], 1.20)
        self.assertEqual(saved_metadata["period_scale"], 0.91)

    def test_profile_can_exclude_workers_above_modeled_period_limit(self):
        fast = ("entry=fast@0x1", 1)
        slow = ("entry=slow@0x2", 1)
        threads = {
            fast: {
                "name": "fast",
                "execution_ns": 1_000_000,
                "period_ns": 20_000_000,
            },
            slow: {
                "name": "slow",
                "execution_ns": 100_000,
                "period_ns": 5_000_000_000,
            },
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "generate_deadline_profile._trace_threads",
            return_value=(threads, {"input": "one"}),
        ):
            output = Path(directory) / "profile.csv"
            metadata = generate_profile(
                trace_runs=[Path("one")],
                output=output,
                runtime_margin=1.20,
                period_scale=0.91,
                minimum_runtime_us=100,
                maximum_period_us=4_194_304,
                maximum_modeled_period_us=1_000_000,
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual([row["signature"] for row in rows], [fast[0]])
        self.assertEqual(metadata["source_thread_count"], 2)
        self.assertEqual(metadata["thread_count"], 1)
        self.assertEqual(metadata["excluded_thread_count"], 1)
        self.assertEqual(metadata["excluded_threads"][0]["signature"], slow[0])

    def test_profile_shares_worst_case_parameters_across_role_instances(self):
        signature = "entry=capture@0x1|lib.so@0x2"
        first = (signature, 1)
        second = (signature, 2)
        threads = {
            first: {
                "name": "capture",
                "execution_ns": 500_000,
                "period_ns": 32_000_000,
            },
            second: {
                "name": "capture",
                "execution_ns": 600_000,
                "period_ns": 30_000_000,
            },
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "generate_deadline_profile._trace_threads",
            return_value=(threads, {"input": "one"}),
        ):
            output = Path(directory) / "profile.csv"
            metadata = generate_profile(
                trace_runs=[Path("one")],
                output=output,
                runtime_margin=1.20,
                period_scale=0.91,
                minimum_runtime_us=100,
                maximum_period_us=4_194_304,
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual([int(row["instance"]) for row in rows], [1, 2])
        self.assertEqual({int(row["runtime_ns"]) for row in rows}, {720_000})
        self.assertEqual({int(row["period_ns"]) for row in rows}, {27_300_000})
        self.assertTrue(metadata["shared_parameters_per_thread_signature"])
        self.assertTrue(
            all(thread["role_instance_count"] == 2 for thread in metadata["threads"])
        )

    def test_deadline_policy_starts_process_as_sched_other(self):
        self.assertEqual(scheduler_prefix("deadline", 80), ["chrt", "--other", "0"])

    def test_rate_monotonic_policies_start_process_as_sched_other(self):
        self.assertEqual(scheduler_prefix("rr-rm", 80), ["chrt", "--other", "0"])
        self.assertEqual(
            scheduler_prefix("fifo-rm", 80), ["chrt", "--other", "0"]
        )


if __name__ == "__main__":
    unittest.main()
