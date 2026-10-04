#!/usr/bin/env python3
"""Summarize a latency-only xHCI-to-frameset trace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analyze_full_receive_path import (
    analyze_kernel,
    camera_irq_numbers,
    host_receive_latency,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir

    summary = json.loads(
        (run_dir / "steady_summary.json").read_text(encoding="utf-8")
    )
    measurement = summary["measurement"]
    begin = int(measurement["start_boottime_ns"]) / 1_000_000_000.0
    end = int(measurement["end_boottime_ns"]) / 1_000_000_000.0
    topology_path = run_dir / "topology_before.json"
    topology = (
        json.loads(topology_path.read_text(encoding="utf-8"))
        if topology_path.is_file()
        else None
    )
    irq_numbers = camera_irq_numbers(summary, topology)
    kernel_stages, video_buffers, irq_mode = analyze_kernel(
        run_dir / "host_latency_kernel_trace.dat", begin, end
    )
    latency = host_receive_latency(
        run_dir / "frame_events.csv",
        video_buffers,
        irq_mode,
        run_dir / "host_receive_latency_frames.csv",
        irq_numbers,
    )
    per_camera = latency["per_camera"]
    expected_cameras = len(summary.get("cameras", []))
    if len(per_camera) != expected_cameras:
        raise RuntimeError(
            "latency correlation returned "
            f"{len(per_camera)} camera(s), expected {expected_cameras}"
        )
    low_coverage = [
        item
        for item in per_camera
        if float(item.get("coverage", 0.0)) < 0.95
    ]
    if low_coverage:
        details = ", ".join(
            f"camera {item['camera_index']} ({item['serial']}): "
            f"{float(item['coverage']):.3%}"
            for item in low_coverage
        )
        raise RuntimeError(f"host-latency correlation coverage below 95%: {details}")
    result = {
        "schema_version": 1,
        "trace_mode": "host-latency-only",
        "measurement": measurement,
        "camera_irq_numbers": irq_numbers,
        "kernel_stages": {
            key: value
            for key, value in kernel_stages.items()
            if key
            in {
                "xhci_irq_handler",
                "xhci_irq_wakeup_to_run",
                "xhci_irq_run_to_first_hcd",
                "hcd_to_uvc_callback",
                "uvc_complete",
            }
        },
        "host_receive_to_frameset_latency": latency,
    }
    destination = run_dir / "host_receive_latency_summary.json"
    destination.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(destination)


if __name__ == "__main__":
    main()
