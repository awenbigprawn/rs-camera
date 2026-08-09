#!/usr/bin/env python3
"""Compare xHCI IRQ burst concentration in equal camera warm-up windows."""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
from typing import Any, Iterable


IRQ_ENTRY_RE = re.compile(
    r"\s(?P<timestamp>\d+\.\d+): irq_handler_entry:\s+irq=(?P<irq>\d+)\b"
)


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def stats(values: list[float]) -> dict[str, float | int]:
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


def phase_markers(attempt: Path) -> dict[str, int]:
    markers: dict[str, int] = {}
    with (attempt / "thread_lifecycle.jsonl").open(encoding="utf-8") as source:
        for line in source:
            event = json.loads(line)
            if event.get("event") == "phase_marker":
                markers[str(event["name"])] = int(event["timestamp_ns"])
    return markers


def candidate_window(attempt: Path, trim_start_seconds: float) -> tuple[int, int]:
    markers = phase_markers(attempt)
    start = markers["after_pipeline_start"] + round(trim_start_seconds * 1e9)
    end = markers.get("camera_warmup_complete", markers.get("before_pipeline_stop"))
    if end is None or end <= start:
        raise RuntimeError(f"No usable camera warm-up window in {attempt}")
    return start, end


def xhci_irqs(attempt: Path) -> list[int]:
    topology = json.loads((attempt / "topology_before.json").read_text(encoding="utf-8"))
    result = [
        int(line["irq"])
        for line in topology["parsed_interrupts"]["lines"]
        if "xhci-hcd" in line["description"]
    ]
    if len(result) != 2:
        raise RuntimeError(f"Expected two xHCI IRQs in {attempt}, found {result}")
    return sorted(result)


def read_irq_entries(
    attempt: Path, irqs: list[int], start_ns: int, end_ns: int
) -> dict[int, list[int]]:
    entries = {irq: [] for irq in irqs}
    process = subprocess.Popen(
        ["trace-cmd", "report", "-i", str(attempt / "overrun_kernel_trace.dat")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        match = IRQ_ENTRY_RE.search(line)
        if not match:
            continue
        irq = int(match.group("irq"))
        if irq not in entries:
            continue
        timestamp_ns = round(float(match.group("timestamp")) * 1e9)
        if start_ns <= timestamp_ns < end_ns:
            entries[irq].append(timestamp_ns)
    stderr = process.communicate()[1]
    if process.returncode:
        raise RuntimeError(
            f"trace-cmd failed for {attempt} with {process.returncode}: {stderr}"
        )
    return entries


def nearest_deltas_us(left: list[int], right: list[int]) -> list[float]:
    if not left or not right:
        return []
    result: list[float] = []
    index = 0
    for timestamp in left:
        while index + 1 < len(right) and right[index + 1] <= timestamp:
            index += 1
        candidates = [abs(timestamp - right[index])]
        if index + 1 < len(right):
            candidates.append(abs(timestamp - right[index + 1]))
        result.append(min(candidates) / 1000.0)
    return result


def sliding_window_max(timestamps: list[int], width_ns: int) -> int:
    active: deque[int] = deque()
    maximum = 0
    for timestamp in timestamps:
        active.append(timestamp)
        while active and timestamp - active[0] >= width_ns:
            active.popleft()
        maximum = max(maximum, len(active))
    return maximum


def binned_concentration(
    timestamps: list[int], start_ns: int, end_ns: int, width_ns: int
) -> dict[str, float | int]:
    bin_count = max(1, math.ceil((end_ns - start_ns) / width_ns))
    counts = [0] * bin_count
    for timestamp in timestamps:
        index = min((timestamp - start_ns) // width_ns, bin_count - 1)
        counts[index] += 1
    float_counts = [float(value) for value in counts]
    mean = statistics.fmean(counts)
    variance = statistics.pvariance(counts)
    return {
        "width_us": width_ns / 1000.0,
        "bins": bin_count,
        "mean_count": mean,
        "p99_count": percentile(float_counts, 0.99),
        "max_count": max(counts),
        "zero_fraction": sum(value == 0 for value in counts) / bin_count,
        "fano_factor": variance / mean if mean else 0.0,
        "sliding_max_count": sliding_window_max(timestamps, width_ns),
    }


def analyze_attempt(attempt: Path, start_ns: int, end_ns: int) -> dict[str, Any]:
    summary = json.loads((attempt / "steady_summary.json").read_text(encoding="utf-8"))
    irqs = xhci_irqs(attempt)
    entries = read_irq_entries(attempt, irqs, start_ns, end_ns)
    combined = sorted(entries[irqs[0]] + entries[irqs[1]])
    duration_seconds = (end_ns - start_ns) / 1e9
    nearest = nearest_deltas_us(entries[irqs[0]], entries[irqs[1]])
    return {
        "attempt": str(attempt.resolve()),
        "window_start_boottime_ns": start_ns,
        "window_end_boottime_ns": end_ns,
        "window_seconds": duration_seconds,
        "probe_success": bool(summary.get("success")),
        "probe_error": str(summary.get("error", "")),
        "warmup_freshness": [
            {
                "serial": camera.get("serial", ""),
                "deliveries": camera.get("warmup_deliveries", 0),
                "duplicate_frames": camera.get("warmup_duplicate_frames", 0),
                "sequence_gaps": camera.get("warmup_sequence_gaps", 0),
                "out_of_order_frames": camera.get("warmup_out_of_order_frames", 0),
            }
            for camera in summary.get("cameras", [])
        ],
        "xhci_irqs": irqs,
        "irq_counts": {str(irq): len(entries[irq]) for irq in irqs},
        "irq_rates_per_second": {
            str(irq): len(entries[irq]) / duration_seconds for irq in irqs
        },
        "combined_irq_count": len(combined),
        "combined_irq_rate_per_second": len(combined) / duration_seconds,
        "cross_controller_nearest_delta_us": stats(nearest),
        "cross_controller_nearest_fraction": {
            f"le_{threshold}_us": (
                sum(value <= threshold for value in nearest) / len(nearest)
                if nearest else 0.0
            )
            for threshold in (50, 100, 250, 500, 1000)
        },
        "combined_concentration": {
            f"{width_us}_us": binned_concentration(
                combined, start_ns, end_ns, width_us * 1000
            )
            for width_us in (100, 250, 500, 1000)
        },
    }


def group_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    def median_at(*keys: str) -> float:
        values: list[float] = []
        for result in results:
            value: Any = result
            for key in keys:
                value = value[key]
            values.append(float(value))
        return statistics.median(values)

    return {
        "attempts": len(results),
        "successful_attempts": sum(result["probe_success"] for result in results),
        "attempts_with_warmup_sequence_gaps": sum(
            any(camera["sequence_gaps"] for camera in result["warmup_freshness"])
            for result in results
        ),
        "median_combined_irq_rate_per_second": median_at(
            "combined_irq_rate_per_second"
        ),
        "median_cross_controller_nearest_delta_p50_us": median_at(
            "cross_controller_nearest_delta_us", "p50"
        ),
        "median_cross_controller_fraction_le_100_us": median_at(
            "cross_controller_nearest_fraction", "le_100_us"
        ),
        "concentration_medians": {
            f"{width_us}_us": {
                "fano_factor": median_at(
                    "combined_concentration", f"{width_us}_us", "fano_factor"
                ),
                "p99_count": median_at(
                    "combined_concentration", f"{width_us}_us", "p99_count"
                ),
                "sliding_max_count": median_at(
                    "combined_concentration",
                    f"{width_us}_us",
                    "sliding_max_count",
                ),
            }
            for width_us in (100, 250, 500, 1000)
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--async-attempt", type=Path, action="append", required=True)
    parser.add_argument("--sync-attempt", type=Path, action="append", required=True)
    parser.add_argument("--trim-start-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.trim_start_seconds < 0:
        parser.error("trim-start-seconds must be non-negative")
    return args


def main() -> int:
    args = parse_args()
    attempts = [*args.async_attempt, *args.sync_attempt]
    candidates = [candidate_window(path, args.trim_start_seconds) for path in attempts]
    common_duration_ns = min(end - start for start, end in candidates)
    windows = [(end - common_duration_ns, end) for _, end in candidates]
    async_count = len(args.async_attempt)
    async_results = [
        analyze_attempt(path, *window)
        for path, window in zip(attempts[:async_count], windows[:async_count])
    ]
    sync_results = [
        analyze_attempt(path, *window)
        for path, window in zip(attempts[async_count:], windows[async_count:])
    ]
    result = {
        "schema_version": 1,
        "interpretation": (
            "Equal-duration warm-up windows. Higher short-window peak/Fano values "
            "or smaller cross-controller nearest-IRQ deltas indicate more "
            "temporally concentrated host USB interrupt pressure; they do not "
            "measure camera-side exposure skew."
        ),
        "common_window_seconds": common_duration_ns / 1e9,
        "async_attempts": async_results,
        "hardware_sync_attempts": sync_results,
        "async_group": group_summary(async_results),
        "hardware_sync_group": group_summary(sync_results),
    }
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
