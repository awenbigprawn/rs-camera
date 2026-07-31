#!/usr/bin/env python3

from pathlib import Path
import sys
import tempfile
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = TOOL_DIR.parent
BENCHKIT_DIR = TOOLS_DIR.parent / "deps" / "benchkit"
STEADY_TOOL_DIR = TOOLS_DIR / "realsense_steady_bench"
sys.path.insert(0, str(BENCHKIT_DIR))
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(STEADY_TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR))

from run_timerlat_campaign import load_matrix  # noqa: E402
from timerlat_benchmark import RealSenseTimerlatBench  # noqa: E402
from timerlat_workload import ManagedCameraLoad, phase_marker_seen  # noqa: E402


CONFIG = TOOL_DIR / "configs" / "minimal_matrix.json"


class TimerlatMatrixTest(unittest.TestCase):
    def test_minimal_matrix_separates_cpu_and_camera_pressure(self):
        timerlat, cases = load_matrix(CONFIG)
        by_id = {case["case_id"]: case for case in cases}

        self.assertEqual(timerlat["duration_seconds"], 300)
        self.assertEqual(
            list(by_id),
            [
                "idle",
                "cpu_busy_only",
                "one_camera_representative",
                "two_camera_stress",
            ],
        )
        self.assertEqual(by_id["cpu_busy_only"]["noise"]["cpu"], "busy_loop")
        self.assertFalse(by_id["cpu_busy_only"]["camera"]["enabled"])
        self.assertEqual(by_id["two_camera_stress"]["noise"]["cpu"], "none")
        self.assertEqual(by_id["two_camera_stress"]["camera"]["count"], 2)

    def test_timerlat_command_uses_the_fixed_active_probe_parameters(self):
        timerlat, _ = load_matrix(CONFIG)
        benchmark = RealSenseTimerlatBench.__new__(RealSenseTimerlatBench)
        benchmark._rtla = "/usr/bin/rtla"
        benchmark._timerlat = timerlat
        benchmark._use_sudo = True

        command = benchmark._timerlat_command()

        self.assertEqual(
            command[:5],
            ["sudo", "--non-interactive", "/usr/bin/rtla", "timerlat", "hist"],
        )
        self.assertIn("-k", command)
        self.assertEqual(command[command.index("-P") + 1], "f:95")
        self.assertEqual(command[command.index("-p") + 1], "1000")
        self.assertEqual(command[command.index("--warm-up") + 1], "10")
        self.assertIn("--no-aa", command)

    def test_camera_command_covers_timerlat_warmup_duration_and_guard(self):
        _, cases = load_matrix(CONFIG)
        case = next(
            case for case in cases if case["case_id"] == "one_camera_representative"
        )
        load = ManagedCameraLoad(
            probe=Path("/build/realsense_steady_probe"),
            repo_root=Path("/repo"),
            serials=["camera-a"],
            duration_seconds=20,
            timerlat_warmup_seconds=3,
            guard_seconds=2,
        )
        with tempfile.TemporaryDirectory() as temporary:
            command = load._build_command(case, Path(temporary))

        frame_index = command.index("--frames") + 1
        self.assertEqual(command[frame_index], "750")
        self.assertIn("camera-a", command)

    def test_phase_marker_reader_ignores_a_partial_final_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lifecycle.jsonl"
            path.write_text(
                '{"event":"phase_marker","name":"process_start"}\n'
                '{"event":"phase_marker","name":"steady_state_begin"}\n'
                '{"event":',
                encoding="utf-8",
            )

            self.assertTrue(phase_marker_seen(path, "steady_state_begin"))
            self.assertFalse(phase_marker_seen(path, "steady_state_end"))


if __name__ == "__main__":
    unittest.main()
