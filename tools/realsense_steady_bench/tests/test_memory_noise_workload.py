#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = REPO_ROOT / "src" / "realsense_memory_noise.cpp"
TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from noise_workloads import FixedCopyMemoryNoise, MemoryNoiseConfig  # noqa: E402


class MemoryNoiseWorkloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("c++")
        if compiler is None:
            raise unittest.SkipTest("c++ compiler is unavailable")
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls._temporary_directory.name) / "realsense_memory_noise"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-pthread",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                str(SOURCE),
                "-o",
                str(cls.binary),
            ],
            check=True,
            text=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def test_two_workers_copy_fixed_private_buffers_and_exit_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready_path = root / "ready.json"
            summary_path = root / "summary.json"
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--workers",
                    "2",
                    "--buffer-size-mib",
                    "4",
                    "--copy-chunk-kib",
                    "256",
                    "--warmup-seconds",
                    "0.05",
                    "--ready-file",
                    str(ready_path),
                    "--summary-output",
                    str(summary_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not ready_path.is_file():
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(ready_path.is_file())
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
                self.assertTrue(ready["ready"])
                self.assertEqual(ready["workers"], 2)
                self.assertEqual(ready["buffer_size_mib"], 4)
                self.assertEqual(ready["copy_chunk_kib"], 256)
                self.assertEqual(ready["chunks_per_buffer_pass"], 16)
                self.assertEqual(ready["buffers_per_worker"], 2)
                self.assertEqual(ready["total_allocated_bytes"], 16 * 1024 * 1024)
                self.assertEqual(
                    ready["memory_access"], "thread_private_memcpy_read_write"
                )
                self.assertGreater(
                    ready["warmup_estimated_memory_mib_per_second"], 0.0
                )
                self.assertFalse(ready["rate_limited"])
                self.assertEqual(ready["target_memory_mib_per_second"], 0.0)

                time.sleep(0.05)
                process.terminate()
                stdout, stderr = process.communicate(timeout=5.0)
                self.assertEqual(process.returncode, 0, msg=stdout + stderr)
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertTrue(summary["success"])
                self.assertEqual(summary["workers"], 2)
                self.assertEqual(summary["buffer_size_mib"], 4)
                self.assertGreater(summary["payload_bytes_copied"], 0)
                self.assertGreater(summary["total_chunks"], 0)
                self.assertEqual(
                    summary["estimated_memory_traffic_bytes"],
                    2 * summary["payload_bytes_copied"],
                )
                self.assertGreater(summary["estimated_memory_mib_per_second"], 0.0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5.0)

    def test_runner_starts_waits_for_and_stops_memory_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            noise = FixedCopyMemoryNoise(
                MemoryNoiseConfig(
                    executable=self.binary,
                    modes=("fixed_copy",),
                    workers=2,
                    buffer_size_mib=4,
                    copy_chunk_kib=256,
                    target_memory_mib_per_second=256.0,
                    warmup_seconds=0.05,
                    ready_timeout_seconds=5.0,
                    cpu_affinity=None,
                ),
                REPO_ROOT,
            )
            try:
                ready = noise.start("fixed_copy", root)
                self.assertTrue(ready["ready"])
                self.assertEqual(ready["workers"], 2)
                self.assertEqual(ready["copy_chunk_kib"], 256)
                self.assertTrue(ready["rate_limited"])
                self.assertEqual(ready["target_memory_mib_per_second"], 256.0)
                time.sleep(0.05)
                noise.stop(root)
            finally:
                noise.stop(root)

            process = json.loads(
                (root / "memory_noise_process.json").read_text(encoding="utf-8")
            )
            summary = json.loads(
                (root / "memory_noise_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(process["returncode"], 0)
            self.assertFalse(process["forced_kill"])
            self.assertTrue(summary["success"])
            self.assertEqual(
                summary["memory_access"], "thread_private_memcpy_read_write"
            )
            self.assertEqual(summary["target_memory_mib_per_second"], 256.0)
            self.assertEqual(summary["copy_chunk_kib"], 256)
            self.assertLess(summary["estimated_memory_mib_per_second"], 400.0)


if __name__ == "__main__":
    unittest.main()
