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
SOURCE = REPO_ROOT / "src" / "realsense_cpu_noise.cpp"
TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from noise_workloads import CpuBusyLoopNoise, CpuNoiseConfig  # noqa: E402


class CpuNoiseWorkloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("c++")
        if compiler is None:
            raise unittest.SkipTest("c++ compiler is unavailable")
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls._temporary_directory.name) / "realsense_cpu_noise"
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

    def test_two_workers_report_register_only_load_and_exit_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready_path = root / "ready.json"
            summary_path = root / "summary.json"
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--workers",
                    "2",
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
                self.assertEqual(ready["working_set"], "register_only")
                self.assertTrue(ready["effective_cpu_affinity"])
                self.assertLess(
                    ready["process_start_boottime_ns"], ready["ready_boottime_ns"]
                )
                self.assertGreater(ready["warmup_cpu_equivalents"], 0.0)

                process.terminate()
                stdout, stderr = process.communicate(timeout=5.0)
                self.assertEqual(process.returncode, 0, msg=stdout + stderr)
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertTrue(summary["success"])
                self.assertEqual(summary["workers"], 2)
                self.assertEqual(summary["working_set"], "register_only")
                self.assertLess(summary["ready_boottime_ns"], summary["end_boottime_ns"])
                self.assertGreater(summary["measurement_cpu_equivalents"], 0.0)
                self.assertGreater(summary["total_iterations"], 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5.0)

    def test_runner_starts_waits_for_and_stops_cpu_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            noise = CpuBusyLoopNoise(
                CpuNoiseConfig(
                    executable=self.binary,
                    modes=("busy_loop",),
                    workers=2,
                    warmup_seconds=0.05,
                    ready_timeout_seconds=5.0,
                    cpu_affinity=None,
                ),
                REPO_ROOT,
            )
            try:
                ready = noise.start("busy_loop", root)
                self.assertTrue(ready["ready"])
                self.assertEqual(ready["workers"], 2)
                time.sleep(0.05)
                noise.stop(root)
            finally:
                noise.stop(root)

            process = json.loads(
                (root / "cpu_noise_process.json").read_text(encoding="utf-8")
            )
            summary = json.loads(
                (root / "cpu_noise_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(process["returncode"], 0)
            self.assertFalse(process["forced_kill"])
            self.assertTrue(summary["success"])
            self.assertEqual(summary["working_set"], "register_only")


if __name__ == "__main__":
    unittest.main()
