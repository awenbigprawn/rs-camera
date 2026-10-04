#!/usr/bin/env python3

from __future__ import annotations

import csv
from pathlib import Path
import sys
import tempfile
import unittest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from analyze_full_receive_path import (  # noqa: E402
    camera_irq_numbers,
    host_receive_latency,
)


class HostLatencyTraceTests(unittest.TestCase):
    def test_multi_camera_deliveries_are_separate_and_irq_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "frame_events.csv"
            output = root / "latency.csv"
            columns = [
                "camera_index",
                "serial",
                "delivery",
                "stream",
                "stream_index",
                "host_boottime_ns",
                "backend_to_return_ms",
                "arrival_to_return_ms",
            ]
            with events.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for camera_index, serial in ((0, "A"), (1, "B")):
                    for stream in ("Depth", "Color"):
                        writer.writerow(
                            {
                                "camera_index": camera_index,
                                "serial": serial,
                                "delivery": 1,
                                "stream": stream,
                                "stream_index": 0,
                                "host_boottime_ns": 10_000_000_000,
                                "backend_to_return_ms": 20.0,
                                "arrival_to_return_ms": 5.0,
                            }
                        )

            buffers = [
                {
                    "backend_s": 9.980,
                    "complete_s": 9.995,
                    "xhci_activation_s": 9.970,
                    "xhci_irq": 132,
                },
                {
                    "backend_s": 9.980,
                    "complete_s": 9.995,
                    "xhci_activation_s": 9.975,
                    "xhci_irq": 137,
                },
            ]
            result = host_receive_latency(
                events,
                buffers,
                "threaded_irq",
                output,
                {0: 132, 1: 137},
            )

            self.assertEqual(result["matched_deliveries"], 2)
            self.assertEqual(result["total_deliveries"], 2)
            self.assertEqual(result["coverage"], 1.0)
            self.assertEqual(len(result["per_camera"]), 2)
            self.assertAlmostEqual(result["per_camera"][0]["p50_us"], 30_000.0)
            self.assertAlmostEqual(result["per_camera"][1]["p50_us"], 25_000.0)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [(row["camera_index"], row["serial"]) for row in rows],
                [("0", "A"), ("1", "B")],
            )

    def test_camera_irq_mapping_uses_paired_usb2_root_bus(self) -> None:
        summary = {
            "cameras": [
                {"index": 0, "physical_port": "/devices/xhci-hcd.0/usb3/3-1"},
                {"index": 1, "physical_port": "/devices/xhci-hcd.1/usb5/5-1"},
            ]
        }
        topology = {
            "parsed_interrupts": {
                "lines": [
                    {"irq": "132", "description": "Edge xhci-hcd:usb2"},
                    {"irq": "137", "description": "Edge xhci-hcd:usb4"},
                ]
            }
        }
        self.assertEqual(camera_irq_numbers(summary, topology), {0: 132, 1: 137})


if __name__ == "__main__":
    unittest.main()
