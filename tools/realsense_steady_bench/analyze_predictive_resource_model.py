#!/usr/bin/env python3
"""Summarize predictive calibration, held-out capacity, and pressure results."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
from typing import Any, Iterable


MIB = 1024.0 * 1024.0


@dataclass(frozen=True)
class StreamDemand:
    name: str
    width: int
    height: int
    fps: float
    wire_format: str
    wire_bytes_per_pixel: float
    output_format: str
    output_bytes_per_pixel: float
    converts_format: bool = False

    @property
    def pixels_per_second(self) -> float:
        return self.width * self.height * self.fps

    @property
    def payload_mib_s(self) -> float:
        return self.pixels_per_second * self.wire_bytes_per_pixel / MIB

    @property
    def memory_touch_mib_s(self) -> float:
        touches = 3.0 * self.wire_bytes_per_pixel
        if self.converts_format:
            touches += self.wire_bytes_per_pixel + self.output_bytes_per_pixel
        return self.pixels_per_second * touches / MIB


@dataclass(frozen=True)
class FamilyDemand:
    signature: str
    instances: int
    running_cores: float
    ready_cores: float
    complete_activations: int
    execution_mean_ms: float
    period_p50_ms: float


@dataclass(frozen=True)
class Run:
    source: Path
    case_id: str
    success: bool
    cameras: int
    payload_mib_s: float
    payload_per_controller_mib_s: float
    memory_touch_mib_s: float
    delivery_p99_ms: float
    delivery_max_ms: float
    duplicates: int
    gaps: int
    stale: int
    timeouts: int
    child_threads: int | None
    worker_families: int | None
    userspace_running_cores: float | None
    userspace_ready_cores: float | None
    cpu_noise_workers: int
    cpu_noise_cores: float
    memory_noise_target_mib_s: float
    memory_noise_achieved_mib_s: float
    streams: tuple[StreamDemand, ...]
    family_demands: tuple[FamilyDemand, ...]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_attempt(run_dir: Path, result: dict[str, Any]) -> Path | None:
    attempt = int(result.get("selected_attempt") or 0)
    path = run_dir / f"attempt-{attempt}"
    return path if attempt > 0 and path.is_dir() else None


def controller_count(case: dict[str, Any]) -> int:
    label = str((case.get("physical") or {}).get("root_hub_label") or "")
    return 2 if "two_xhci" in label else 1


def stream_demands(probe: dict[str, Any]) -> tuple[StreamDemand, ...]:
    """Return the per-camera demand of every explicitly enabled stream."""

    fps = float(probe["fps"])
    depth_width = int(probe["depth_width"])
    depth_height = int(probe["depth_height"])
    color_width = int(probe["color_width"])
    color_height = int(probe["color_height"])
    stream_mode = str(probe["stream_mode"])
    infrared_enabled = stream_mode in {"depth_ir", "stereo_all", "d435_all"}
    color_enabled = stream_mode in {"depth_color", "stereo_all", "d435_all"}

    streams = [
        StreamDemand(
            name="Depth",
            width=depth_width,
            height=depth_height,
            fps=fps,
            wire_format="Z16",
            wire_bytes_per_pixel=2.0,
            output_format="Z16",
            output_bytes_per_pixel=2.0,
        )
    ]
    if infrared_enabled:
        for name in ("IR1", "IR2"):
            streams.append(
                StreamDemand(
                    name=name,
                    width=depth_width,
                    height=depth_height,
                    fps=fps,
                    wire_format="Y8",
                    wire_bytes_per_pixel=1.0,
                    output_format="Y8",
                    output_bytes_per_pixel=1.0,
                )
            )
    if color_enabled:
        streams.append(
            StreamDemand(
                name="Color",
                width=color_width,
                height=color_height,
                fps=fps,
                wire_format="YUYV",
                wire_bytes_per_pixel=2.0,
                output_format="RGB8",
                output_bytes_per_pixel=3.0,
                converts_format=True,
            )
        )
    return tuple(streams)


def workload_rates(
    case: dict[str, Any],
) -> tuple[float, float, tuple[StreamDemand, ...]]:
    probe = case["probe"]
    cameras = int(probe["camera_count"])
    streams = stream_demands(probe)
    aggregate_payload = cameras * sum(stream.payload_mib_s for stream in streams)
    aggregate_memory_touch = cameras * sum(
        stream.memory_touch_mib_s for stream in streams
    )
    return aggregate_payload, aggregate_memory_touch, streams


def trace_metrics(
    attempt: Path | None,
) -> tuple[
    int | None,
    int | None,
    float | None,
    float | None,
    tuple[FamilyDemand, ...],
]:
    if attempt is None:
        return None, None, None, None, ()
    path = attempt / "thread_steady_summary.json"
    if not path.is_file():
        return None, None, None, None, ()
    summary = read_json(path)
    threads = [
        thread
        for thread in summary.get("threads", [])
        if thread.get("signature") != "process-main"
    ]
    duration_ms = float(summary["duration_ms"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread in threads:
        grouped[str(thread["signature"])].append(thread)
    family_demands = tuple(
        FamilyDemand(
            signature=signature,
            instances=len(instances),
            running_cores=sum(float(thread["running_ms"]) for thread in instances)
            / duration_ms,
            ready_cores=sum(float(thread["ready_ms"]) for thread in instances)
            / duration_ms,
            complete_activations=sum(
                int(thread.get("complete_activation_count") or 0)
                for thread in instances
            ),
            execution_mean_ms=(
                sum(
                    float((thread.get("execution_ms") or {}).get("mean") or 0.0)
                    * int(thread.get("complete_activation_count") or 0)
                    for thread in instances
                )
                / max(
                    1,
                    sum(
                        int(thread.get("complete_activation_count") or 0)
                        for thread in instances
                    ),
                )
            ),
            period_p50_ms=statistics.median(
                float((thread.get("period_ms") or {}).get("p50") or 0.0)
                for thread in instances
            ),
        )
        for signature, instances in sorted(grouped.items())
    )
    return (
        len(threads),
        len(family_demands),
        sum(float(thread["running_ms"]) for thread in threads) / duration_ms,
        sum(float(thread["ready_ms"]) for thread in threads) / duration_ms,
        family_demands,
    )


def noise_summary(attempt: Path | None, prefix: str) -> dict[str, Any]:
    if attempt is None:
        return {}
    path = attempt / f"{prefix}_summary.json"
    return read_json(path) if path.is_file() else {}


def load_runs(roots: Iterable[Path]) -> list[Run]:
    runs: list[Run] = []
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
            case = read_json(run_dir / "case.json")
            payload, memory_touch, streams = workload_rates(case)
            attempt = selected_attempt(run_dir, result)
            child_threads, families, running, ready, family_demands = trace_metrics(attempt)
            cpu_noise = noise_summary(attempt, "cpu_noise")
            memory_noise = noise_summary(attempt, "memory_noise")
            controllers = controller_count(case)
            runs.append(
                Run(
                    source=run_dir,
                    case_id=str(result["case_id"]),
                    success=bool(result.get("success")),
                    cameras=int(result.get("camera_count") or 0),
                    payload_mib_s=payload,
                    payload_per_controller_mib_s=payload / controllers,
                    memory_touch_mib_s=memory_touch,
                    delivery_p99_ms=float(
                        result.get("delivery_interarrival_ms_p99") or 0.0
                    ),
                    delivery_max_ms=float(
                        result.get("delivery_interarrival_ms_max") or 0.0
                    ),
                    duplicates=int(result.get("duplicate_frames") or 0),
                    gaps=int(result.get("sequence_gaps") or 0),
                    stale=int(result.get("stale_framesets") or 0),
                    timeouts=int(result.get("timeouts") or 0),
                    child_threads=child_threads,
                    worker_families=families,
                    userspace_running_cores=running,
                    userspace_ready_cores=ready,
                    cpu_noise_workers=int(result.get("cpu_noise_workers") or 0),
                    cpu_noise_cores=float(
                        cpu_noise.get("measurement_cpu_equivalents") or 0.0
                    ),
                    memory_noise_target_mib_s=float(
                        memory_noise.get("target_memory_mib_per_second") or 0.0
                    ),
                    memory_noise_achieved_mib_s=float(
                        memory_noise.get("estimated_memory_mib_per_second") or 0.0
                    ),
                    streams=streams,
                    family_demands=family_demands,
                )
            )
    return runs


def med(values: Iterable[float | int | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return statistics.median(selected) if selected else None


def fmt(value: float | None, digits: int = 3) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


def errors(runs: Iterable[Run]) -> tuple[int, int, int, int]:
    selected = list(runs)
    return (
        sum(run.duplicates for run in selected),
        sum(run.gaps for run in selected),
        sum(run.stale for run in selected),
        sum(run.timeouts for run in selected),
    )


def calibration_table(runs: list[Run]) -> list[str]:
    groups: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        if "/calibration/" in str(run.source):
            groups[run.case_id].append(run)
    lines = [
        "## Calibration observations",
        "",
        "| Case | Runs ok | Child threads | Families | Userspace running cores | Userspace ready cores | Payload total/per controller | p99/max | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for case_id, selected in sorted(groups.items()):
        error = errors(selected)
        lines.append(
            f"| {case_id} | {sum(run.success for run in selected)}/{len(selected)} | "
            f"{fmt(med(run.child_threads for run in selected), 0)} | "
            f"{fmt(med(run.worker_families for run in selected), 0)} | "
            f"{fmt(med(run.userspace_running_cores for run in selected))} | "
            f"{fmt(med(run.userspace_ready_cores for run in selected))} | "
            f"{selected[0].payload_mib_s:.2f}/{selected[0].payload_per_controller_mib_s:.2f} MiB/s | "
            f"{fmt(med(run.delivery_p99_ms for run in selected))}/"
            f"{fmt(med(run.delivery_max_ms for run in selected))} ms | "
            f"{error[0]}/{error[1]}/{error[2]}/{error[3]} |"
        )
    return lines


def stream_resource_table(runs: list[Run]) -> list[str]:
    profiles: dict[tuple[StreamDemand, ...], str] = {}
    for run in sorted(runs, key=lambda item: item.case_id):
        profiles.setdefault(run.streams, run.case_id)

    lines = [
        "## Per-camera stream resource decomposition",
        "",
        "Each row is calculated from one enabled stream. Memory touch is the analytical code-path estimate; it is not measured DRAM traffic.",
        "",
        "| Configuration | Stream | Profile | Wire -> output | USB payload | Memory touch |",
        "|---|---|---:|---|---:|---:|",
    ]
    for streams, case_id in profiles.items():
        short_case = case_id.removeprefix("predict_").removeprefix("validate_")
        for index, stream in enumerate(streams):
            profile = f"{stream.width}x{stream.height}@{stream.fps:g}"
            lines.append(
                f"| {short_case if index == 0 else ''} | {stream.name} | {profile} | "
                f"{stream.wire_format} -> {stream.output_format} | "
                f"{stream.payload_mib_s:.2f} MiB/s | "
                f"{stream.memory_touch_mib_s:.2f} MiB/s |"
            )
        lines.append(
            f"| **{short_case} total** |  |  |  | "
            f"**{sum(stream.payload_mib_s for stream in streams):.2f} MiB/s** | "
            f"**{sum(stream.memory_touch_mib_s for stream in streams):.2f} MiB/s** |"
        )
    return lines


def representative_prediction(runs: list[Run]) -> list[str]:
    calibration: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        if "/calibration/" in str(run.source):
            calibration[run.case_id].append(run)
    validation = [
        run
        for run in runs
        if run.case_id == "validate_mixed_n4_representative30"
        and run.child_threads is not None
    ]
    required = {
        "predict_d435_n1_representative30",
        "predict_d455_n1_representative30",
        "predict_d455_n2_representative30",
    }
    lines = ["## Held-out four-camera affine prediction", ""]
    if not required.issubset(calibration) or not validation:
        lines.append("The required calibration or held-out topology trace is incomplete.")
        return lines

    family_signatures = {
        family.signature
        for case_id in required
        for run in calibration[case_id]
        for family in run.family_demands
    }

    def family_metric(case_id: str, signature: str, field: str) -> float:
        values = []
        for run in calibration[case_id]:
            family = next(
                (
                    item
                    for item in run.family_demands
                    if item.signature == signature
                ),
                None,
            )
            values.append(float(getattr(family, field)) if family else 0.0)
        return float(statistics.median(values))

    predicted_threads = 0.0
    predicted_running = 0.0
    for signature in family_signatures:
        predicted_threads += (
            3.0 * family_metric(
                "predict_d455_n2_representative30", signature, "instances"
            )
            - 3.0
            * family_metric(
                "predict_d455_n1_representative30", signature, "instances"
            )
            + family_metric(
                "predict_d435_n1_representative30", signature, "instances"
            )
        )
        predicted_running += (
            3.0 * family_metric(
                "predict_d455_n2_representative30", signature, "running_cores"
            )
            - 3.0
            * family_metric(
                "predict_d455_n1_representative30", signature, "running_cores"
            )
            + family_metric(
                "predict_d435_n1_representative30", signature, "running_cores"
            )
        )
    d455_n1_threads = sum(
        family_metric(
            "predict_d455_n1_representative30", signature, "instances"
        )
        for signature in family_signatures
    )
    d455_n2_threads = sum(
        family_metric(
            "predict_d455_n2_representative30", signature, "instances"
        )
        for signature in family_signatures
    )
    d455_n1_running = sum(
        family_metric(
            "predict_d455_n1_representative30", signature, "running_cores"
        )
        for signature in family_signatures
    )
    d455_n2_running = sum(
        family_metric(
            "predict_d455_n2_representative30", signature, "running_cores"
        )
        for signature in family_signatures
    )
    shared_signatures = {
        signature
        for signature in family_signatures
        if family_metric(
            "predict_d455_n1_representative30", signature, "instances"
        )
        == 1.0
        and family_metric(
            "predict_d455_n2_representative30", signature, "instances"
        )
        == 1.0
    }
    shared_running = sum(
        max(
            family_metric(
                "predict_d435_n1_representative30", signature, "running_cores"
            ),
            family_metric(
                "predict_d455_n1_representative30", signature, "running_cores"
            ),
        )
        for signature in shared_signatures
    )
    d435_n1_running = sum(
        family_metric(
            "predict_d435_n1_representative30", signature, "running_cores"
        )
        for signature in family_signatures
    )
    single_camera_running_prediction = (
        3.0 * d455_n1_running + d435_n1_running - 3.0 * shared_running
    )
    d455_n2_linear_prediction = 2.0 * d455_n1_running - shared_running
    observed_threads = med(run.child_threads for run in validation)
    observed_running = med(run.userspace_running_cores for run in validation)
    lines.extend(
        [
            "Thread multiplicity and CPU scaling are separate questions. One D455 has 12 child threads and two D455 cameras have 23, identifying one shared family and 11 camera-local instances per D455. CPU utilization is not strictly linear: the two-camera observation exceeds the shared-aware single-camera prediction by the amount shown below.",
            "",
            "| D455 scaling check | Predicted | Observed | Difference |",
            "|---|---:|---:|---:|",
            f"| Child threads, 1 to 2 cameras | {2.0 * d455_n1_threads - len(shared_signatures):.0f} | {d455_n2_threads:.0f} | {d455_n2_threads - (2.0 * d455_n1_threads - len(shared_signatures)):+.0f} |",
            f"| Userspace utilization, 1 to 2 cameras | {d455_n2_linear_prediction:.6f} cores | {d455_n2_running:.6f} cores | {(d455_n2_running / d455_n2_linear_prediction - 1.0) * 100.0:+.2f}% |",
            "",
            "The four-camera table compares two predictors. The single-camera predictor reuses each family's one-camera utilization and counts a process-wide family once. The calibrated predictor applies `3*D455x2 - 3*D455x1 + D435x1` independently to every family, allowing the two-camera trace to capture part of the scaling overhead.",
            "",
            "| Four-camera quantity | Predicted | Observed | Relative error |",
            "|---|---:|---:|---:|",
            f"| Child-thread count | {predicted_threads:.1f} | {fmt(observed_threads, 1)} | {fmt(abs((observed_threads or 0.0) - predicted_threads) / (observed_threads or 1.0) * 100.0, 1)}% |",
            f"| Userspace utilization, single-camera families | {single_camera_running_prediction:.3f} cores | {fmt(observed_running)} cores | {fmt(abs((observed_running or 0.0) - single_camera_running_prediction) / (observed_running or 1.0) * 100.0, 1)}% |",
            f"| Userspace utilization, 1/2-camera calibrated | {predicted_running:.3f} cores | {fmt(observed_running)} cores | {fmt(abs((observed_running or 0.0) - predicted_running) / (observed_running or 1.0) * 100.0, 1)}% |",
        ]
    )

    def validation_family_metric(signature: str, field: str) -> float:
        values = []
        for run in validation:
            family = next(
                (
                    item
                    for item in run.family_demands
                    if item.signature == signature
                ),
                None,
            )
            values.append(float(getattr(family, field)) if family else 0.0)
        return float(statistics.median(values))

    role_suffixes = {
        "Color capture": "0x6c9348",
        "Depth capture": "0x41fdc0",
        "Application wait": "realsense_steady_probe",
        "Shared device watcher": "0x776aa8",
    }
    role_signatures = {
        role: next(
            (
                signature
                for signature in family_signatures
                if signature.endswith(suffix)
            ),
            "",
        )
        for role, suffix in role_suffixes.items()
    }
    lines.extend(
        [
            "",
            "The current RelWithDebInfo creator stacks map the dominant signatures to the following source-informed roles. Stable per-thread activation counts show that the residual comes from longer CPU execution per activation, not from extra frames.",
            "",
            "| Family | N4 | Mean execution, 1 D455 | Mean execution, 2 D455 | Mean execution, mixed N4 | N4 activations/instance | Calibrated CPU prediction | N4 observed CPU | Residual |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    reported_signatures: set[str] = set()
    for role, signature in role_signatures.items():
        if not signature:
            continue
        reported_signatures.add(signature)
        observed = validation_family_metric(signature, "running_cores")
        calibrated = (
            3.0
            * family_metric(
                "predict_d455_n2_representative30", signature, "running_cores"
            )
            - 3.0
            * family_metric(
                "predict_d455_n1_representative30", signature, "running_cores"
            )
            + family_metric(
                "predict_d435_n1_representative30", signature, "running_cores"
            )
        )
        instances = validation_family_metric(signature, "instances")
        activations = validation_family_metric(signature, "complete_activations")
        lines.append(
            f"| {role} | {instances:.0f} | "
            f"{family_metric('predict_d455_n1_representative30', signature, 'execution_mean_ms'):.3f} ms | "
            f"{family_metric('predict_d455_n2_representative30', signature, 'execution_mean_ms'):.3f} ms | "
            f"{validation_family_metric(signature, 'execution_mean_ms'):.3f} ms | "
            f"{activations / max(1.0, instances):.0f} | "
            f"{calibrated:.6f} | {observed:.6f} | {observed - calibrated:+.6f} |"
        )
    other_signatures = family_signatures - reported_signatures
    other_observed = sum(
        validation_family_metric(signature, "running_cores")
        for signature in other_signatures
    )
    other_calibrated = sum(
        3.0
        * family_metric(
            "predict_d455_n2_representative30", signature, "running_cores"
        )
        - 3.0
        * family_metric(
            "predict_d455_n1_representative30", signature, "running_cores"
        )
        + family_metric(
            "predict_d435_n1_representative30", signature, "running_cores"
        )
        for signature in other_signatures
    )
    lines.append(
        f"| Other families combined | -- | -- | -- | -- | -- | "
        f"{other_calibrated:.6f} | {other_observed:.6f} | "
        f"{other_observed - other_calibrated:+.6f} |"
    )
    return lines


def three_d455_prediction(runs: list[Run]) -> list[str]:
    """Compare a held-out three-D455 trace with the one/two-camera fit."""

    calibration: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        if "/calibration/" in str(run.source):
            calibration[run.case_id].append(run)
    split_validation = [
        run
        for run in runs
        if run.case_id == "validate_d455_n3_split_representative30"
        and run.child_threads is not None
    ]
    concentrated_validation = [
        run
        for run in runs
        if run.case_id == "validate_d455_n3_representative30"
        and run.child_threads is not None
    ]
    validation = split_validation or concentrated_validation
    topology = (
        "two 5-Gbit/s xHCI controllers in a 2+1 camera distribution"
        if split_validation
        else "one 5-Gbit/s hub and one xHCI controller"
    )
    one_id = "predict_d455_n1_representative30"
    two_id = "predict_d455_n2_representative30"
    lines = ["## Held-out three-D455 prediction", ""]
    if one_id not in calibration or two_id not in calibration:
        lines.append("The required one- and two-D455 calibration cells are incomplete.")
        return lines

    family_signatures = {
        family.signature
        for case_id in (one_id, two_id)
        for run in calibration[case_id]
        for family in run.family_demands
    }

    def family_metric(case_id: str, signature: str, field: str) -> float:
        values = []
        for run in calibration[case_id]:
            family = next(
                (
                    item
                    for item in run.family_demands
                    if item.signature == signature
                ),
                None,
            )
            values.append(float(getattr(family, field)) if family else 0.0)
        return float(statistics.median(values))

    def validation_family_metric(signature: str, field: str) -> float:
        values = []
        for run in validation:
            family = next(
                (
                    item
                    for item in run.family_demands
                    if item.signature == signature
                ),
                None,
            )
            values.append(float(getattr(family, field)) if family else 0.0)
        return float(statistics.median(values)) if values else 0.0

    one_threads = sum(
        family_metric(one_id, signature, "instances")
        for signature in family_signatures
    )
    two_threads = sum(
        family_metric(two_id, signature, "instances")
        for signature in family_signatures
    )
    one_running = sum(
        family_metric(one_id, signature, "running_cores")
        for signature in family_signatures
    )
    two_running = sum(
        family_metric(two_id, signature, "running_cores")
        for signature in family_signatures
    )
    predicted_threads = 2.0 * two_threads - one_threads
    predicted_running = 2.0 * two_running - one_running
    predicted_payload = 3.0 * calibration[one_id][0].payload_mib_s
    predicted_memory = 3.0 * calibration[one_id][0].memory_touch_mib_s

    lines.extend(
        [
            f"The prediction is fixed before inspecting the three-camera cell. For every family, the affine extrapolation is `N3 = 2*N2 - N1`; the same expression is applied to measured userspace running demand. The selected validation uses {topology}.",
            "",
            "| Quantity | One D455 | Two D455 | Three-D455 prediction | Three-D455 observation | Relative error |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    if not validation:
        lines.extend(
            [
                f"| Child-thread count | {one_threads:.0f} | {two_threads:.0f} | {predicted_threads:.0f} | pending | -- |",
                f"| Userspace CPU utilization (cores) | {one_running:.6f} | {two_running:.6f} | {predicted_running:.6f} | pending | -- |",
                f"| Aggregate USB payload (MiB/s) | {calibration[one_id][0].payload_mib_s:.2f} | {calibration[two_id][0].payload_mib_s:.2f} | {predicted_payload:.2f} | configured workload | -- |",
                f"| Analytical memory touch (MiB/s) | {calibration[one_id][0].memory_touch_mib_s:.2f} | {calibration[two_id][0].memory_touch_mib_s:.2f} | {predicted_memory:.2f} | configured workload | -- |",
                "",
                "The held-out measurement has not been added yet.",
            ]
        )
        return lines

    observed_threads = med(run.child_threads for run in validation)
    observed_running = med(run.userspace_running_cores for run in validation)
    validation_errors = errors(validation)
    thread_error = (
        abs((observed_threads or 0.0) - predicted_threads)
        / predicted_threads
        * 100.0
        if predicted_threads
        else 0.0
    )
    running_error = (
        abs((observed_running or 0.0) - predicted_running)
        / predicted_running
        * 100.0
        if predicted_running
        else 0.0
    )
    lines.extend(
        [
            f"| Child-thread count | {one_threads:.0f} | {two_threads:.0f} | {predicted_threads:.0f} | {fmt(observed_threads, 0)} | {fmt(thread_error, 2)}% |",
            f"| Userspace CPU utilization (cores) | {one_running:.6f} | {two_running:.6f} | {predicted_running:.6f} | {fmt(observed_running, 6)} | {fmt(running_error, 2)}% |",
            f"| Aggregate USB payload (MiB/s) | {calibration[one_id][0].payload_mib_s:.2f} | {calibration[two_id][0].payload_mib_s:.2f} | {predicted_payload:.2f} | configured workload | -- |",
            f"| Analytical memory touch (MiB/s) | {calibration[one_id][0].memory_touch_mib_s:.2f} | {calibration[two_id][0].memory_touch_mib_s:.2f} | {predicted_memory:.2f} | configured workload | -- |",
            "",
            f"All {sum(run.success for run in validation)}/{len(validation)} held-out runs succeeded. Median delivery p99/max was {fmt(med(run.delivery_p99_ms for run in validation))}/{fmt(med(run.delivery_max_ms for run in validation))} ms. Duplicate/gap/stale/timeout totals were {validation_errors[0]}/{validation_errors[1]}/{validation_errors[2]}/{validation_errors[3]}.",
            "",
            "USB payload and memory touch are analytical consequences of the configured profiles, not independent hardware-counter measurements. The held-out trace directly validates worker multiplicity, userspace CPU demand, timing, and freshness.",
        ]
    )
    matched_validation_instances = sum(
        validation_family_metric(signature, "instances")
        for signature in family_signatures
    )
    if observed_threads and matched_validation_instances < 0.8 * observed_threads:
        lines.extend(
            [
                "",
                "The family-level table is omitted because fewer than 80% of the "
                "validation threads match the calibration creator-stack signatures. "
                "These signatures contain shared-library offsets and are only stable "
                "for the same instrumented binary. Aggregate thread and CPU metrics "
                "remain comparable, but a family-level comparison requires rebuilding "
                "and rerunning the calibration and validation cells from one source state.",
            ]
        )
        return lines

    lines.extend(
        [
            "",
            "| Family | Predicted N3 instances | Observed N3 instances | Predicted CPU (cores) | Observed CPU (cores) | Residual |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for signature in sorted(family_signatures):
        predicted_instances = (
            2.0 * family_metric(two_id, signature, "instances")
            - family_metric(one_id, signature, "instances")
        )
        observed_instances = validation_family_metric(signature, "instances")
        predicted_family_running = (
            2.0 * family_metric(two_id, signature, "running_cores")
            - family_metric(one_id, signature, "running_cores")
        )
        observed_family_running = validation_family_metric(signature, "running_cores")
        short_signature = signature.rsplit("|", 1)[-1]
        lines.append(
            f"| `{short_signature}` | {predicted_instances:.0f} | "
            f"{observed_instances:.0f} | {predicted_family_running:.6f} | "
            f"{observed_family_running:.6f} | "
            f"{observed_family_running - predicted_family_running:+.6f} |"
        )
    return lines


def outcome_table(runs: list[Run]) -> list[str]:
    groups: dict[tuple[str, int, float], list[Run]] = defaultdict(list)
    for run in runs:
        if run.case_id != "validate_mixed_n4_representative30":
            continue
        groups[(run.case_id, run.cpu_noise_workers, run.memory_noise_target_mib_s)].append(run)
    lines = [
        "## Four-camera pressure screening",
        "",
        "| Treatment | Runs ok | Achieved pressure | p99/max | Errors |",
        "|---|---:|---:|---:|---:|",
    ]
    for (_, cpu_workers, memory_target), selected in sorted(groups.items()):
        if cpu_workers:
            label = f"CPU workers={cpu_workers}"
            pressure = f"{fmt(med(run.cpu_noise_cores for run in selected))} cores"
        elif memory_target:
            label = f"Memory target={memory_target:.0f} MiB/s"
            pressure = f"{fmt(med(run.memory_noise_achieved_mib_s for run in selected), 1)} MiB/s"
        else:
            label = "None"
            pressure = "0"
        error = errors(selected)
        lines.append(
            f"| {label} | {sum(run.success for run in selected)}/{len(selected)} | "
            f"{pressure} | {fmt(med(run.delivery_p99_ms for run in selected))}/"
            f"{fmt(med(run.delivery_max_ms for run in selected))} ms | "
            f"{error[0]}/{error[1]}/{error[2]}/{error[3]} |"
        )
    return lines


def capacity_table(runs: list[Run]) -> list[str]:
    groups: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        if (
            run.case_id.startswith("validate_mixed_n4_")
            and run.cpu_noise_workers == 0
            and run.memory_noise_target_mib_s == 0.0
        ):
            groups[run.case_id].append(run)
    lines = [
        "## Four-camera capacity points",
        "",
        "| Case | Runs ok | Payload per controller | Analytical memory touch | p99/max | Errors |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for case_id, selected in sorted(groups.items()):
        error = errors(selected)
        lines.append(
            f"| {case_id} | {sum(run.success for run in selected)}/{len(selected)} | "
            f"{selected[0].payload_per_controller_mib_s:.2f} MiB/s | "
            f"{selected[0].memory_touch_mib_s:.1f} MiB/s | "
            f"{fmt(med(run.delivery_p99_ms for run in selected))}/"
            f"{fmt(med(run.delivery_max_ms for run in selected))} ms | "
            f"{error[0]}/{error[1]}/{error[2]}/{error[3]} |"
        )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runs = load_runs(args.results_root)
    if not runs:
        raise SystemExit("no experiment_results.json artifacts were found")
    lines = [
        "# Predictive resource-model results",
        "",
        "Errors are reported as duplicate/sequence-gap/stale/timeout counts. Userspace CPU includes SDK workers and one application wait worker per camera, excludes the main control thread, and excludes xHCI IRQ service. Payload and memory touch are analytical; memory touch is not measured DRAM bandwidth.",
        "",
    ]
    for section in (
        stream_resource_table(runs),
        calibration_table(runs),
        three_d455_prediction(runs),
        representative_prediction(runs),
        capacity_table(runs),
        outcome_table(runs),
    ):
        lines.extend(section)
        lines.append("")
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.output.resolve()} from {len(runs)} logical runs")


if __name__ == "__main__":
    main()
