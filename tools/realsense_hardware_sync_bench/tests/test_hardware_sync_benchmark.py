from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "run_hardware_sync_benchmark.py"
SPEC = importlib.util.spec_from_file_location("hardware_sync_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HardwareSyncBenchmarkTests(unittest.TestCase):
    def test_case_starts_slave_before_master_and_enables_depth_only(self) -> None:
        case = MODULE.build_case(
            master="master",
            slave="slave",
            duration_seconds=30,
            warmup_seconds=5,
            fps=30,
            depth_width=848,
            depth_height=480,
        )
        probe = case["probe"]
        self.assertEqual(probe["serials"], ["slave", "master"])
        self.assertEqual(probe["hardware_sync_master"], "master")
        self.assertEqual(probe["hardware_sync_slaves"], ["slave"])
        self.assertEqual(probe["stream_mode"], "depth")
        self.assertEqual(probe["measurement_duration_ms"], 30_000)

    def test_stress_async_control_omits_sync_options(self) -> None:
        case = MODULE.build_case(
            master="master",
            slave="slave",
            duration_seconds=30,
            warmup_seconds=10,
            fps=60,
            depth_width=848,
            depth_height=480,
            workload="stress",
            hardware_sync_enabled=False,
        )
        probe = case["probe"]
        self.assertEqual(probe["stream_mode"], "d435_all")
        self.assertEqual((probe["color_width"], probe["color_height"]), (960, 540))
        self.assertNotIn("hardware_sync_master", probe)
        self.assertNotIn("hardware_sync_slaves", probe)

    def test_depth_pairing_preserves_constant_offset(self) -> None:
        master = [
            {"frame_number": index, "sensor_timestamp_ms": index * 33.3,
             "host_boottime_ns": index * 33_300_000}
            for index in range(10, 20)
        ]
        slave = [
            {"frame_number": index + 4,
             "sensor_timestamp_ms": index * 33.3 + 7.0,
             "host_boottime_ns": index * 33_300_000 + 100_000}
            for index in range(10, 20)
        ]
        pairs, offset = MODULE.pair_depth_events(master, slave)
        self.assertEqual(offset, 4)
        self.assertEqual(len(pairs), 10)
        deltas = [
            float(slave_event["sensor_timestamp_ms"])
            - float(master_event["sensor_timestamp_ms"])
            for master_event, slave_event in pairs
        ]
        self.assertAlmostEqual(MODULE.statistics_json(deltas)["p99"], 7.0)

    def test_reads_only_depth_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.csv"
            with path.open("w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow([
                    "camera_index", "serial", "delivery", "stream",
                    "stream_index", "frame_number", "sensor_timestamp_ms",
                    "timestamp_domain", "host_boottime_ns", "relative_ms",
                ])
                writer.writerow([0, "camera", 1, "Depth", 0, 10, 20.0,
                                 "Global Time", 30, 0.0])
                writer.writerow([0, "camera", 1, "Color", 0, 10, 20.0,
                                 "Global Time", 31, 0.0])
            events = MODULE.read_depth_events(path, "camera")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["frame_number"], 10)


if __name__ == "__main__":
    unittest.main()
