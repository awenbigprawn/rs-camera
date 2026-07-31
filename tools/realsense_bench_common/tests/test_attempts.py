#!/usr/bin/env python3

import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS_DIR))

from realsense_bench_common.artifacts import resolve_selected_attempt
from realsense_bench_common.attempts import (
    AttemptDecision,
    run_attempt_loop,
)


class AttemptLoopTest(unittest.TestCase):
    def test_retries_only_when_classifier_requests_it(self):
        summaries = [
            {"success": False, "error": "startup failed"},
            {"success": True},
        ]
        recoveries = []

        def run_attempt(attempt, attempt_dir):
            attempt_dir.mkdir()
            return f"attempt {attempt}\n", dict(summaries[attempt - 1])

        def classify(summary):
            success = summary["success"]
            return AttemptDecision(
                success=success,
                failure_phase="none" if success else "startup",
                retry=not success,
                error=summary.get("error", ""),
            )

        def recover(summary, attempt_dir, decision):
            del summary, decision
            result = {"success": True, "record_data_dir": str(attempt_dir)}
            recoveries.append(result)
            return result

        with tempfile.TemporaryDirectory() as temporary:
            result = run_attempt_loop(
                record_dir=Path(temporary),
                max_attempts=3,
                recovery_method="full-reset",
                recovery_settle_seconds=0,
                run_attempt=run_attempt,
                classify_attempt=classify,
                recover_attempt=recover,
            )

        self.assertEqual(result.selected_attempt, 2)
        self.assertEqual(result.output, "attempt 2\n")
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(len(recoveries), 1)
        self.assertFalse(result.summary["initial_attempt_success"])
        self.assertTrue(result.summary["eventual_success"])

    def test_measured_failure_is_recovered_but_not_retried(self):
        calls = []

        def run_attempt(attempt, attempt_dir):
            calls.append(attempt)
            attempt_dir.mkdir()
            return "", {"success": False, "error": "measurement timeout"}

        def recover(_summary, _attempt_dir, _decision):
            return {"success": True}

        with tempfile.TemporaryDirectory() as temporary:
            result = run_attempt_loop(
                record_dir=Path(temporary),
                max_attempts=3,
                recovery_method="full-reset",
                recovery_settle_seconds=0,
                run_attempt=run_attempt,
                classify_attempt=lambda summary: AttemptDecision(
                    success=False,
                    failure_phase="measurement",
                    retry=False,
                    error=summary["error"],
                ),
                recover_attempt=recover,
            )

        self.assertEqual(calls, [1])
        self.assertEqual(result.recovery["count"], 1)
        self.assertFalse(result.summary["eventual_success"])


class SelectedAttemptTest(unittest.TestCase):
    def test_resolves_canonical_attempt_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "selected_attempt.txt").write_text(
                "2\n", encoding="utf-8"
            )
            (run_dir / "attempt-2").mkdir()
            selected = resolve_selected_attempt(run_dir)

        self.assertEqual(selected.attempt, 2)
        self.assertEqual(selected.layout, "attempt-directory")
        self.assertEqual(selected.data_dir.name, "attempt-2")

    def test_resolves_legacy_promoted_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "selected_attempt.txt").write_text(
                "2\n", encoding="utf-8"
            )
            (run_dir / "attempts.json").write_text(
                json.dumps([{"attempt": 1}, {"attempt": 2}]),
                encoding="utf-8",
            )
            selected = resolve_selected_attempt(run_dir)

        self.assertEqual(selected.attempt, 2)
        self.assertEqual(selected.layout, "legacy-promoted-root")
        self.assertEqual(selected.data_dir, selected.run_dir)


if __name__ == "__main__":
    unittest.main()
