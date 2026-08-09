#!/usr/bin/env python3
"""Run a short two-camera D435 hardware-sync smoke benchmark."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import subprocess
import sys
from typing import Any, Iterable


TOOL_DIR = Path(__file__).resolve().parent
TOOLS_DIR = TOOL_DIR.parent
REPO_ROOT = TOOLS_DIR.parent
STEADY_RUNNER = TOOLS_DIR / "realsense_steady_bench" / "run_steady_campaign.py"


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def statistics_json(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0, "min": 0.0, "mean": 0.0, "p50": 0.0,
                "p90": 0.0, "p99": 0.0, "max": 0.0, "stddev": 0.0}
    return {
        "n": len(values),
        "min": min(values),
        "mean": statistics.fmean(values),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "stddev": statistics.pstdev(values),
    }


def build_case(
    *,
    master: str,
    slave: str,
    duration_seconds: float,
    warmup_seconds: float,
    fps: int,
    depth_width: int,
    depth_height: int,
    workload: str = "depth",
    hardware_sync_enabled: bool = True,
) -> dict[str, Any]:
    stress = workload == "stress"
    mode = "hardware_sync" if hardware_sync_enabled else "async"
    probe: dict[str, Any] = {
        # Keep a fixed camera order in both halves of an A/B comparison.
        "serials": [slave, master],
        "camera_count": 2,
        "stream_mode": "d435_all" if stress else "depth",
        "delivery": "wait",
        "frames": max(1, round(duration_seconds * fps)),
        "measurement_duration_ms": round(duration_seconds * 1000),
        "warmup_frames": max(1, round(warmup_seconds * fps)),
        "frame_timeout_ms": 1500,
        "startup_timeout_ms": 15000,
        "fps": fps,
        "depth_width": depth_width,
        "depth_height": depth_height,
        "color_width": 960 if stress else 640,
        "color_height": 540 if stress else 480,
    }
    if hardware_sync_enabled:
        probe.update(
            {
                "hardware_sync_master": master,
                "hardware_sync_slaves": [slave],
            }
        )
    return {
        "case_id": f"d435_{workload}_{mode}_smoke",
        "workload": {
            "class": f"{workload}_{mode}_smoke",
            "measurement_seconds": duration_seconds,
            "depth": f"{depth_width}x{depth_height}_Z16_{fps}fps",
            "color": f"960x540_RGB8_{fps}fps" if stress else "disabled",
            "infrared": (
                f"IR1+IR2_{depth_width}x{depth_height}_Y8_{fps}fps"
                if stress
                else "disabled"
            ),
        },
        "physical": {
            "camera_count": 2,
            "sync_topology": "one_master_one_slave",
            "sync_master_serial": master,
            "sync_slave_serial": slave,
            "hardware_sync_enabled": hardware_sync_enabled,
            "usb_speed_label": "usb3_superspeed",
        },
        "probe": probe,
    }


def read_depth_events(path: Path, serial: str) -> list[dict[str, float | int]]:
    events: list[dict[str, float | int]] = []
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            if row["serial"] != serial or row["stream"] != "Depth":
                continue
            if int(row["stream_index"]) != 0:
                continue
            events.append(
                {
                    "frame_number": int(row["frame_number"]),
                    "sensor_timestamp_ms": float(row["sensor_timestamp_ms"]),
                    "host_boottime_ns": int(row["host_boottime_ns"]),
                }
            )
    events.sort(key=lambda event: int(event["host_boottime_ns"]))
    return events


def pair_depth_events(
    master_events: list[dict[str, float | int]],
    slave_events: list[dict[str, float | int]],
) -> tuple[list[tuple[dict[str, float | int], dict[str, float | int]]], int]:
    sample_count = min(len(master_events), len(slave_events), 100)
    if sample_count == 0:
        return [], 0
    offsets = [
        int(slave_events[index]["frame_number"])
        - int(master_events[index]["frame_number"])
        for index in range(sample_count)
    ]
    frame_number_offset = round(statistics.median(offsets))
    slave_by_number = {
        int(event["frame_number"]): event for event in slave_events
    }
    pairs = [
        (event, slave_by_number[int(event["frame_number"]) + frame_number_offset])
        for event in master_events
        if int(event["frame_number"]) + frame_number_offset in slave_by_number
    ]
    return pairs, frame_number_offset


def analyze_attempt(
    attempt_dir: Path,
    *,
    master: str,
    slave: str,
    max_p99_residual_ms: float,
    expect_hardware_sync: bool = True,
) -> dict[str, Any]:
    summary = json.loads((attempt_dir / "steady_summary.json").read_text(encoding="utf-8"))
    master_events = read_depth_events(attempt_dir / "frame_events.csv", master)
    slave_events = read_depth_events(attempt_dir / "frame_events.csv", slave)
    pairs, frame_number_offset = pair_depth_events(master_events, slave_events)
    sensor_deltas = [
        float(slave_event["sensor_timestamp_ms"])
        - float(master_event["sensor_timestamp_ms"])
        for master_event, slave_event in pairs
    ]
    median_sensor_delta = statistics.median(sensor_deltas) if sensor_deltas else 0.0
    centered_sensor_deltas = [
        abs(delta - median_sensor_delta) for delta in sensor_deltas
    ]
    host_arrival_deltas = [
        abs(
            int(slave_event["host_boottime_ns"])
            - int(master_event["host_boottime_ns"])
        )
        / 1_000_000.0
        for master_event, slave_event in pairs
    ]
    hardware_sync = summary.get("hardware_sync", {})
    operational_success = bool(summary.get("success"))
    configuration_success = bool(
        operational_success
        and (
            hardware_sync.get("enabled")
            and hardware_sync.get("all_applied")
            and hardware_sync.get("all_restored")
            if expect_hardware_sync
            else not hardware_sync.get("enabled")
        )
    )
    minimum_pairs = max(10, int(0.8 * min(len(master_events), len(slave_events))))
    timing_smoke_success = bool(
        configuration_success
        and len(pairs) >= minimum_pairs
        and statistics_json(centered_sensor_deltas)["p99"]
        <= max_p99_residual_ms
    )
    return {
        "schema_version": 1,
        "expected_hardware_sync": expect_hardware_sync,
        "operational_success": operational_success,
        "configuration_success": configuration_success,
        "timing_smoke_success": timing_smoke_success,
        "interpretation": (
            "Operational smoke check. Mode read-back plus slave frame delivery "
            "confirms that the configured master/slave pipeline runs; centered "
            "sensor-timestamp residual is a consistency check, not a calibrated "
            "measurement of exposure skew."
        ),
        "master_serial": master,
        "slave_serial": slave,
        "master_depth_events": len(master_events),
        "slave_depth_events": len(slave_events),
        "paired_depth_events": len(pairs),
        "frame_number_offset_slave_minus_master": frame_number_offset,
        "median_sensor_timestamp_delta_ms": median_sensor_delta,
        "absolute_centered_sensor_timestamp_delta_ms": statistics_json(
            centered_sensor_deltas
        ),
        "absolute_host_arrival_delta_ms": statistics_json(host_arrival_deltas),
        "max_p99_centered_sensor_delta_ms": max_p99_residual_ms,
        "hardware_sync": hardware_sync,
    }


def selected_attempt_dir(campaign_dir: Path) -> Path:
    selections = list(campaign_dir.rglob("selected_attempt.txt"))
    if len(selections) != 1:
        raise RuntimeError(
            f"Expected one logical run below {campaign_dir}, found {len(selections)}"
        )
    run_dir = selections[0].parent
    attempt = int(selections[0].read_text(encoding="utf-8").strip())
    return run_dir / f"attempt-{attempt}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, help="librealsense serial")
    parser.add_argument("--slave", required=True, help="librealsense serial")
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--warmup-seconds", type=float, default=5.0)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--depth-width", type=int, default=848)
    parser.add_argument("--depth-height", type=int, default=480)
    parser.add_argument("--workload", choices=("depth", "stress"), default="depth")
    parser.add_argument(
        "--hardware-sync",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="enable master/slave modes; --no-hardware-sync provides an A/B control",
    )
    parser.add_argument(
        "--kernel-irq-trace",
        action="store_true",
        help="record xHCI IRQ entry/exit events with the existing kernel tracer",
    )
    parser.add_argument("--build-jobs", type=int, default=3)
    parser.add_argument(
        "--build-dir", type=Path, default=REPO_ROOT / "build-realsense-steady"
    )
    parser.add_argument(
        "--results-dir", type=Path, default=TOOL_DIR / "results"
    )
    parser.add_argument("--max-p99-centered-sensor-delta-ms", type=float, default=2.0)
    parser.add_argument("--no-sudo", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.master == args.slave:
        parser.error("master and slave must be different cameras")
    if args.duration_seconds <= 0 or args.warmup_seconds <= 0 or args.fps <= 0:
        parser.error("duration, warm-up, and fps must be positive")
    return args


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_dir = args.results_dir.resolve() / f"hardware_sync_smoke_{timestamp}"
    campaign_dir = session_dir / "campaign"
    session_dir.mkdir(parents=True, exist_ok=False)
    case = build_case(
        master=args.master,
        slave=args.slave,
        duration_seconds=args.duration_seconds,
        warmup_seconds=args.warmup_seconds,
        fps=args.fps,
        depth_width=args.depth_width,
        depth_height=args.depth_height,
        workload=args.workload,
        hardware_sync_enabled=args.hardware_sync,
    )
    config_path = session_dir / "hardware_sync_case.json"
    config_path.write_text(
        json.dumps({"description": __doc__, "cases": [case]}, indent=2) + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(STEADY_RUNNER),
        "--config",
        str(config_path),
        "--policies",
        "other",
        "--nb-runs",
        "1",
        "--no-lime",
        "--recover-on-failure",
        "full-reset",
        "--reset-before-run",
        "--max-attempts-per-run",
        "3",
        "--recovery-settle-seconds",
        "0",
        "--build-jobs",
        str(args.build_jobs),
        "--build-dir",
        str(args.build_dir.resolve()),
        "--results-dir",
        str(campaign_dir),
    ]
    if args.no_sudo:
        command.append("--no-sudo")
    if args.kernel_irq_trace:
        command.append("--overrun-kernel-trace")
    (session_dir / "command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    print("[HARDWARE-SYNC] " + " ".join(command), flush=True)
    if args.dry_run:
        print(f"[HARDWARE-SYNC] generated {config_path}")
        return 0
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        print(
            f"[HARDWARE-SYNC] steady campaign failed with exit {completed.returncode}",
            file=sys.stderr,
        )
        return completed.returncode
    attempt_dir = selected_attempt_dir(campaign_dir)
    analysis = analyze_attempt(
        attempt_dir,
        master=args.master,
        slave=args.slave,
        max_p99_residual_ms=args.max_p99_centered_sensor_delta_ms,
        expect_hardware_sync=args.hardware_sync,
    )
    analysis_path = session_dir / "hardware_sync_analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(analysis, indent=2))
    print(f"[HARDWARE-SYNC] analysis={analysis_path}")
    if args.hardware_sync:
        return 0 if analysis["timing_smoke_success"] else 2
    return 0 if analysis["operational_success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
