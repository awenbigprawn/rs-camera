#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from parse_overrun_kernel_trace import (  # noqa: E402
    parse_trace as parse_kernel_trace,
    topology_xhci_irq_ids,
)
from parse_freshness_kernel_trace import (  # noqa: E402
    parse_trace as parse_freshness_trace,
)
from parse_v4l2_diagnostic_trace import (  # noqa: E402
    EVENT,
    HEADER,
    HEADER_BYTES,
    parse_trace as parse_v4l2_trace,
)


def write_lifecycle(path: Path) -> None:
    path.write_text(
        "".join(
            json.dumps(
                {
                    "event": "phase_marker",
                    "timestamp_ns": timestamp,
                    "name": name,
                }
            )
            + "\n"
            for name, timestamp in (
                ("steady_state_begin", 1_000_000_000),
                ("steady_state_end", 2_000_000_000),
            )
        ),
        encoding="utf-8",
    )


def write_activations(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tid",
                "activation",
                "release_ns",
                "completion_ns",
                "execution_ms",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "tid": 42,
                "activation": 7,
                "release_ns": 1_100_000_000,
                "completion_ns": 1_100_100_000,
                "execution_ms": 0.1,
            }
        )


class DiagnosticTraceTests(unittest.TestCase):
    def test_xhci_irqs_are_read_from_topology_snapshot(self) -> None:
        self.assertEqual(
            topology_xhci_irq_ids(
                {
                    "parsed_interrupts": {
                        "cpus": ["CPU0", "CPU1"],
                        "lines": [
                            {"irq": "132", "description": "xhci-hcd:usb2"},
                            {"irq": "200", "description": "timer"},
                        ],
                    }
                }
            ),
            {132},
        )

    def test_v4l2_stage_pair_is_attributed_to_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            activations = root / "thread_steady_activations.csv"
            trace = root / "v4l2.bin"
            write_lifecycle(lifecycle)
            write_activations(activations)
            events = b"".join(
                (
                    EVENT.pack(1_100_020_000, 42, 1, 4, 9, 0, 10, 0),
                    EVENT.pack(1_100_030_000, 42, 1, 5, 9, 0, 10, 0),
                    EVENT.pack(1_100_040_000, 42, 1, 26, -1, 0, 20, 0),
                    EVENT.pack(1_100_050_000, 42, 1, 27, -1, 0, 20, 0),
                )
            )
            header = HEADER.pack(
                b"RSV4L2D", 1, HEADER_BYTES, EVENT.size, 0, 4, 4, 0
            )
            trace.write_bytes(header + bytes(HEADER_BYTES - len(header)) + events)

            summary = parse_v4l2_trace(trace, lifecycle, root)

            self.assertEqual(summary["dropped_event_count"], 0)
            self.assertEqual(summary["stages_ms"]["video_dqbuf"]["n"], 1)
            self.assertEqual(summary["stages_ms"]["aggregator_enqueue"]["n"], 1)
            with (root / "v4l2_diagnostic_durations.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["activation"], "7")
            self.assertAlmostEqual(float(row["duration_ms"]), 0.01)

    def test_v4l2_raw_sequence_gap_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            trace = root / "v4l2.bin"
            write_lifecycle(lifecycle)
            events = b"".join(
                (
                    EVENT.pack(1_100_010_000, 42, 1, 5, 9, 0, 10, 0),
                    EVENT.pack(1_133_343_000, 42, 1, 5, 9, 0, 12, 0),
                )
            )
            header = HEADER.pack(
                b"RSV4L2D", 1, HEADER_BYTES, EVENT.size, 0, 2, 2, 0
            )
            trace.write_bytes(header + bytes(HEADER_BYTES - len(header)) + events)

            summary = parse_v4l2_trace(
                trace, lifecycle, root, compact_output=True
            )

            self.assertEqual(summary["raw_video_sequence_discontinuity_count"], 1)
            self.assertEqual(summary["raw_video_missing_frame_count"], 1)
            self.assertTrue(summary["compact_output"])
            self.assertFalse((root / "v4l2_diagnostic_events.csv").exists())
            self.assertFalse((root / "v4l2_diagnostic_durations.csv").exists())
            with (root / "v4l2_sequence_gaps.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["previous_sequence"], "10")
            self.assertEqual(row["sequence"], "12")
            self.assertEqual(row["missing_frames"], "1")

    @mock.patch("parse_freshness_kernel_trace.trace_report_lines")
    def test_freshness_gap_is_localized_to_uvc_buffer_starvation(
        self, report_lines: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            write_lifecycle(lifecycle)
            with (root / "v4l2_sequence_gaps.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "timestamp_ns",
                        "tid",
                        "fd",
                        "previous_sequence",
                        "sequence",
                        "missing_frames",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp_ns": 1_500_000_000,
                        "tid": 42,
                        "fd": 9,
                        "previous_sequence": 10,
                        "sequence": 12,
                        "missing_frames": 1,
                    }
                )
            report_lines.return_value = iter(
                "\n".join(
                    (
                        "kworker-7 [000] 1.400000: uvc_frame_ready: "
                        "queue=0x1000 type=1 sequence=10 state=2 error=0 bytesused=814080",
                        "worker-42 [001] 1.400100: v4l2_dqbuf: "
                        "minor = 17, type = VIDEO_CAPTURE, sequence = 10",
                        "kworker-7 [000] 1.490000: uvc_no_video_buffer: ret=0x0",
                        "kworker-7 [000] 1.499900: uvc_frame_ready: "
                        "queue=0x1000 type=1 sequence=12 state=2 error=0 bytesused=814080",
                        "worker-42 [001] 1.500000: v4l2_dqbuf: "
                        "minor = 17, type = VIDEO_CAPTURE, sequence = 12",
                    )
                ).splitlines(keepends=True)
            )

            summary = parse_freshness_trace(
                root / "freshness.dat", lifecycle, root
            )

            self.assertEqual(summary["steady_kernel_v4l2_missing_frame_count"], 1)
            self.assertEqual(summary["steady_uvc_missing_frame_count"], 1)
            self.assertEqual(summary["uvc_no_video_buffer_event_count"], 1)
            self.assertEqual(
                summary["classification_counts"], {"uvc_buffer_starvation": 1}
            )
            correlation = summary["correlations"][0]
            self.assertTrue(correlation["kernel_dqbuf_gap"])
            self.assertTrue(correlation["uvc_ready_gap"])
            self.assertTrue(correlation["uvc_no_buffer"])

    @mock.patch("parse_freshness_kernel_trace.trace_report_lines")
    def test_corrupted_uvc_buffer_takes_classification_precedence(
        self, report_lines: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            write_lifecycle(lifecycle)
            with (root / "v4l2_sequence_gaps.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["timestamp_ns", "previous_sequence", "sequence"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp_ns": 1_500_000_000,
                        "previous_sequence": 10,
                        "sequence": 12,
                    }
                )
            report_lines.return_value = iter(
                "\n".join(
                    (
                        "kworker-7 [000] 1.499000: uvc_buffer_error: "
                        "sequence=11 state=2 error=1 bytesused=814080",
                        "kworker-7 [000] 1.499500: xhci_urb_giveback: "
                        "status=-71 dir_in=1 type=1",
                    )
                ).splitlines(keepends=True)
            )

            summary = parse_freshness_trace(
                root / "freshness.dat", lifecycle, root
            )

            self.assertEqual(
                summary["classification_counts"], {"uvc_corrupted_buffer": 1}
            )
            self.assertEqual(summary["xhci_error_giveback_event_count"], 1)

    @mock.patch("parse_freshness_kernel_trace.trace_report_lines")
    def test_uvc_validation_identifies_device_payload_error_flag(
        self, report_lines: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            write_lifecycle(lifecycle)
            with (root / "v4l2_sequence_gaps.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["timestamp_ns", "previous_sequence", "sequence"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp_ns": 1_500_000_000,
                        "previous_sequence": 10,
                        "sequence": 12,
                    }
                )
            report_lines.return_value = iter(
                (
                    "kworker-7 [000] 1.466000: uvc_frame_validation: "
                    "stream=0x1000 buf=0x2000 sequence=11 prior_error=1 "
                    "bytesused=541448 expected=814080 frame_invalid_headers=0 "
                    "frame_header_errors=1"
                ).splitlines(keepends=True)
            )

            summary = parse_freshness_trace(
                root / "freshness.dat", lifecycle, root
            )

            self.assertEqual(
                summary["classification_counts"], {"uvc_payload_error_flag": 1}
            )
            self.assertEqual(
                summary["uvc_frame_validation_cause_counts"],
                {"uvc_payload_error_flag": 1},
            )
            correlation = summary["correlations"][0]
            self.assertTrue(correlation["uvc_validation"])
            self.assertEqual(correlation["uvc_validation_sequence"], 11)
            self.assertEqual(correlation["uvc_validation_bytesused"], 541448)
            self.assertEqual(correlation["uvc_validation_expected"], 814080)

    @mock.patch("parse_overrun_kernel_trace.trace_report_lines")
    def test_deadline_gate_excludes_post_steady_throttle(
        self, report_lines: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            activations = root / "thread_steady_activations.csv"
            write_lifecycle(lifecycle)
            write_activations(activations)
            report_lines.return_value = iter(
                "\n".join(
                    (
                        "worker-42 [001] 1.500000: dl_runtime_exhausted: runtime=-1 flags=0x8",
                        "worker-42 [001] 2.100000: dl_runtime_exhausted: runtime=-2 flags=0x8",
                    )
                ).splitlines(keepends=True)
            )

            summary = parse_kernel_trace(
                root / "kernel.dat", lifecycle, activations, root
            )

            self.assertEqual(summary["steady_deadline_runtime_exhaustion_count"], 1)
            self.assertEqual(
                summary["post_steady_deadline_runtime_exhaustion_count"], 1
            )
            self.assertFalse(summary["steady_deadline_overrun_gate_passed"])

    @mock.patch("parse_overrun_kernel_trace.trace_report_lines")
    def test_deadline_overrun_is_correlated_with_active_v4l2_stage(
        self, report_lines: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = root / "thread_lifecycle.jsonl"
            activations = root / "thread_steady_activations.csv"
            write_lifecycle(lifecycle)
            write_activations(activations)
            with (root / "v4l2_diagnostic_durations.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "tid",
                        "fd",
                        "stage",
                        "begin_ns",
                        "end_ns",
                        "duration_ms",
                        "sequence",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "tid": 42,
                        "fd": 9,
                        "stage": "sensor_invoke",
                        "begin_ns": 1_100_020_000,
                        "end_ns": 1_100_090_000,
                        "duration_ms": 0.07,
                        "sequence": 10,
                    }
                )
            report_lines.return_value = iter(
                (
                    "worker-42 [001] 1.100050: dl_runtime_exhausted: "
                    "runtime=-1 flags=0x8"
                ).splitlines(keepends=True)
            )

            summary = parse_kernel_trace(
                root / "kernel.dat", lifecycle, activations, root
            )

            correlation = summary["steady_deadline_overrun_correlations"][0]
            self.assertEqual(correlation["activation"], "7")
            self.assertEqual(correlation["active_v4l2_stages"], "sensor_invoke")
            self.assertEqual(
                json.loads(correlation["activation_v4l2_stage_durations_ms"]),
                {"sensor_invoke": 0.07},
            )
            self.assertEqual(
                json.loads(correlation["last_v4l2_stage_before_throttle"]),
                None,
            )
            self.assertAlmostEqual(
                float(correlation["irq_corrected_task_execution_ms"]), 0.1
            )


if __name__ == "__main__":
    unittest.main()
