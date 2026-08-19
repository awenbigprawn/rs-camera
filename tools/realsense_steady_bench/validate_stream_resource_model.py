#!/usr/bin/env python3
"""Validate per-stream resource sums against frame and scheduler traces."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
from typing import Any, Iterable

try:
    from .analyze_predictive_resource_model import StreamDemand, stream_demands
except ImportError:
    from analyze_predictive_resource_model import StreamDemand, stream_demands


@dataclass(frozen=True)
class StreamObservation:
    name: str
    configured_fps: float
    observed_fps: float
    configured_payload_mib_s: float
    observed_payload_mib_s: float


@dataclass(frozen=True)
class RunObservation:
    case_id: str
    kernel: str
    configured_payload_mib_s: float
    observed_payload_mib_s: float
    configured_memory_touch_mib_s: float
    adjusted_memory_touch_mib_s: float
    userspace_running_cores: float
    streams: tuple[StreamObservation, ...]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_attempt(run_dir: Path, result: dict[str, Any]) -> Path | None:
    attempt = int(result.get("selected_attempt") or 0)
    path = run_dir / f"attempt-{attempt}"
    return path if attempt > 0 and path.is_dir() else None


def kernel_label(path: Path) -> str:
    parts = set(path.parts)
    if "standard" in parts:
        return "standard"
    if "rt" in parts:
        return "PREEMPT_RT"
    for parent in (path, *path.parents):
        uname = parent / "environment" / "uname.txt"
        if not uname.is_file():
            continue
        release = uname.read_text(encoding="utf-8")
        return "PREEMPT_RT" if "PREEMPT_RT" in release else "standard"
    return "unspecified"


def summary_stream(camera: dict[str, Any], demand: StreamDemand) -> dict[str, Any]:
    prefix = {
        "Depth": "Depth#",
        "Color": "Color#",
        "IR1": "Infrared#1",
        "IR2": "Infrared#2",
    }[demand.name]
    matches = [
        value
        for key, value in camera["streams"].items()
        if str(key).startswith(prefix)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {demand.name} summary for camera {camera.get('serial')}, "
            f"found {len(matches)}"
        )
    return matches[0]


def observed_fps(stream: dict[str, Any]) -> float:
    intervals = stream.get("sensor_interarrival_ms") or {}
    mean_ms = float(intervals.get("mean") or 0.0)
    if mean_ms <= 0.0:
        intervals = stream.get("host_interarrival_ms") or {}
        mean_ms = float(intervals.get("mean") or 0.0)
    if mean_ms <= 0.0:
        raise RuntimeError("stream summary has no positive inter-arrival mean")
    return 1000.0 / mean_ms


def adjusted_stream_demand(
    demand: StreamDemand, fps: float
) -> tuple[float, float]:
    scale = fps / demand.fps
    return demand.payload_mib_s * scale, demand.memory_touch_mib_s * scale


def userspace_running_cores(path: Path) -> float:
    summary = read_json(path)
    duration_ms = float(summary["duration_ms"])
    running_ms = sum(
        float(thread["running_ms"])
        for thread in summary["threads"]
        if thread.get("signature") != "process-main"
    )
    return running_ms / duration_ms


def load_runs(
    roots: Iterable[Path], selected_cases: set[str]
) -> list[RunObservation]:
    observations: list[RunObservation] = []
    seen: set[Path] = set()
    for root in roots:
        for results_path in root.resolve().glob("**/experiment_results.json"):
            run_dir = results_path.parent
            if run_dir in seen:
                continue
            seen.add(run_dir)
            values = read_json(results_path)
            if not values:
                continue
            result = values[0]
            case_id = str(result.get("case_id") or "")
            if selected_cases and case_id not in selected_cases:
                continue
            if not bool(result.get("success")):
                continue
            attempt = selected_attempt(run_dir, result)
            if attempt is None:
                continue
            steady_path = attempt / "steady_summary.json"
            thread_path = attempt / "thread_steady_summary.json"
            if not steady_path.is_file() or not thread_path.is_file():
                continue

            case = read_json(run_dir / "case.json")
            demands = stream_demands(case["probe"])
            steady = read_json(steady_path)
            cameras = steady["cameras"]
            if len(cameras) != int(case["probe"]["camera_count"]):
                raise RuntimeError(f"camera-count mismatch in {attempt}")

            configured_payload = 0.0
            configured_memory = 0.0
            measured_payload = 0.0
            adjusted_memory = 0.0
            stream_rows: list[StreamObservation] = []
            for camera in cameras:
                for demand in demands:
                    stream = summary_stream(camera, demand)
                    fps = observed_fps(stream)
                    payload, memory = adjusted_stream_demand(demand, fps)
                    configured_payload += demand.payload_mib_s
                    configured_memory += demand.memory_touch_mib_s
                    measured_payload += payload
                    adjusted_memory += memory
                    stream_rows.append(
                        StreamObservation(
                            name=demand.name,
                            configured_fps=demand.fps,
                            observed_fps=fps,
                            configured_payload_mib_s=demand.payload_mib_s,
                            observed_payload_mib_s=payload,
                        )
                    )
            observations.append(
                RunObservation(
                    case_id=case_id,
                    kernel=kernel_label(run_dir),
                    configured_payload_mib_s=configured_payload,
                    observed_payload_mib_s=measured_payload,
                    configured_memory_touch_mib_s=configured_memory,
                    adjusted_memory_touch_mib_s=adjusted_memory,
                    userspace_running_cores=userspace_running_cores(thread_path),
                    streams=tuple(stream_rows),
                )
            )
    return observations


def median(values: Iterable[float]) -> float:
    return statistics.median(values)


def relative_error(observed: float, predicted: float) -> float:
    return 100.0 * (observed - predicted) / predicted


def report(runs: list[RunObservation]) -> str:
    groups: dict[tuple[str, str], list[RunObservation]] = defaultdict(list)
    for run in runs:
        groups[(run.kernel, run.case_id)].append(run)

    lines = [
        "# Per-stream resource-model validation",
        "",
        "Observed USB payload is inferred from each stream's measured sensor inter-arrival mean and known wire-frame size; it is not a USB-controller byte counter. Adjusted memory touch applies the same measured frame rates to the analytical code-path model and is not measured DRAM traffic. Userspace CPU is measured by LiME and includes SDK and application wait workers but excludes the main thread and kernel receive work.",
        "",
        "## Workload totals",
        "",
        "| Kernel | Case | Runs | USB nominal | USB frame-derived | Error | Memory nominal | Memory adjusted | Userspace CPU |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for (kernel, case_id), selected in sorted(groups.items()):
        usb_nominal = median(run.configured_payload_mib_s for run in selected)
        usb_observed = median(run.observed_payload_mib_s for run in selected)
        mem_nominal = median(
            run.configured_memory_touch_mib_s for run in selected
        )
        mem_adjusted = median(
            run.adjusted_memory_touch_mib_s for run in selected
        )
        cpu = median(run.userspace_running_cores for run in selected)
        lines.append(
            f"| {kernel} | {case_id} | {len(selected)} | "
            f"{usb_nominal:.3f} MiB/s | {usb_observed:.3f} MiB/s | "
            f"{relative_error(usb_observed, usb_nominal):+.3f}% | "
            f"{mem_nominal:.3f} MiB/s | {mem_adjusted:.3f} MiB/s | "
            f"{cpu:.3f} cores |"
        )

    cpu_cases = {
        "depth": "model_ablation_d435_n1_depth60",
        "depth_color": "model_ablation_d435_n1_depth_color60",
        "depth_ir": "predict_d435_n1_common_stress60",
        "all": "model_scaling_d435_n1_stress60",
    }
    if all(("PREEMPT_RT", case_id) in groups for case_id in cpu_cases.values()):
        cpu = {
            label: median(
                run.userspace_running_cores
                for run in groups[("PREEMPT_RT", case_id)]
            )
            for label, case_id in cpu_cases.items()
        }
        color_increment = cpu["depth_color"] - cpu["depth"]
        infrared_increment = cpu["depth_ir"] - cpu["depth"]
        predicted_all = cpu["depth"] + color_increment + infrared_increment
        lines.extend(
            [
                "",
                "## CPU composition cross-validation",
                "",
                "The PREEMPT_RT prediction uses independently measured Depth, Depth+Color, and Depth+IR configurations. The held-out all-stream observation is not used to derive the Color or IR increment.",
                "",
                "| CPU term | Utilization |",
                "|---|---:|",
                f"| Depth/base | {cpu['depth']:.6f} cores |",
                f"| Increment from Color | {color_increment:.6f} cores |",
                f"| Increment from IR1+IR2 | {infrared_increment:.6f} cores |",
                f"| Predicted all streams | {predicted_all:.6f} cores |",
                f"| Observed all streams | {cpu['all']:.6f} cores |",
                f"| Relative prediction error | {relative_error(cpu['all'], predicted_all):+.3f}% |",
            ]
        )

    lines.extend(
        [
            "",
            "## Per-stream frame-rate check",
            "",
            "| Kernel | Case | Stream | Configured FPS | Observed FPS | Error |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for (kernel, case_id), selected in sorted(groups.items()):
        names = sorted({stream.name for run in selected for stream in run.streams})
        for name in names:
            matching = [
                stream
                for run in selected
                for stream in run.streams
                if stream.name == name
            ]
            configured = median(stream.configured_fps for stream in matching)
            observed = median(stream.observed_fps for stream in matching)
            lines.append(
                f"| {kernel} | {case_id} | {name} | {configured:.3f} | "
                f"{observed:.3f} | {relative_error(observed, configured):+.3f}% |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, action="append", required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = load_runs(args.results_root, set(args.case))
    if not runs:
        raise SystemExit("no matching successful traced runs were found")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report(runs), encoding="utf-8")
    print(f"wrote {output} from {len(runs)} logical runs")


if __name__ == "__main__":
    main()
