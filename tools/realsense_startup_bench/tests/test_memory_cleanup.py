#!/usr/bin/env python3

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from realsense_bench_common.memory import (  # noqa: E402
    DropCachesBeforeRun,
    MEMORY_CLEANUP_FILENAME,
    memory_cleanup_result_fields,
)


class MemoryCleanupTest(unittest.TestCase):
    def test_hook_runs_sync_then_drop_caches_and_records_metadata(self):
        hook = DropCachesBeforeRun(use_sudo=True)
        completed = subprocess.CompletedProcess([], 0, "", "")

        with tempfile.TemporaryDirectory() as temporary_directory:
            record_dir = Path(temporary_directory)
            with mock.patch.object(hook, "_run", return_value=completed) as run:
                hook(
                    build_variables={},
                    run_variables={"policy": "other"},
                    other_variables={},
                    record_data_dir=record_dir,
                )

            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0], mock.call(["sync"]))
            self.assertEqual(
                run.call_args_list[1],
                mock.call(
                    ["sudo", "--non-interactive", "tee", "/proc/sys/vm/drop_caches"],
                    input_text="3\n",
                ),
            )

            metadata = json.loads(
                (record_dir / MEMORY_CLEANUP_FILENAME).read_text(encoding="utf-8")
            )
            self.assertTrue(metadata["success"])
            self.assertEqual(metadata["drop_caches_value"], 3)
            self.assertEqual(metadata["scope"], "page_cache_dentries_inodes")
            self.assertFalse(metadata["anonymous_memory_cleared"])
            self.assertIn("MemAvailable", metadata["meminfo_before_kib"])
            self.assertIn("MemAvailable", metadata["meminfo_after_kib"])

            fields = memory_cleanup_result_fields(record_dir, configured=True)
            self.assertTrue(fields["memory_cleanup_enabled"])
            self.assertTrue(fields["memory_cleanup_recorded"])
            self.assertTrue(fields["memory_cleanup_success"])

    def test_hook_records_failure_before_raising(self):
        hook = DropCachesBeforeRun(use_sudo=False)

        with tempfile.TemporaryDirectory() as temporary_directory:
            record_dir = Path(temporary_directory)
            with mock.patch.object(
                hook,
                "_run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    RuntimeError("simulated drop failure"),
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated drop failure"):
                    hook(
                        build_variables={},
                        run_variables={},
                        other_variables={},
                        record_data_dir=record_dir,
                    )

            metadata = json.loads(
                (record_dir / MEMORY_CLEANUP_FILENAME).read_text(encoding="utf-8")
            )
            self.assertFalse(metadata["success"])
            self.assertIn("simulated drop failure", metadata["error"])


if __name__ == "__main__":
    unittest.main()
