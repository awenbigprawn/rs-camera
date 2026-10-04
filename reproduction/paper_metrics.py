"""Shared paper metrics; preserved from prepare_figure_data.py (see README)."""
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median

# Configured by E2's analysis adapter for each input/output directory.
OUTPUT = Path('.')
UVC_POOL_CELLS = {}
REPETITIONS = 3

def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source, delimiter=delimiter))


def read_benchkit(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("experiment_name;"))
    return list(csv.DictReader(lines[start:], delimiter=";"))


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot compute a percentile of an empty sample")
    position = quantile * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def frameset_latency_metrics(path: Path) -> dict[str, float]:
    """Measure the oldest component at three timestamp boundaries."""
    by_delivery: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(path):
        if row["timestamp_domain"] != "Global Time":
            raise ValueError(f"Non-global timestamp domain in {path}")
        by_delivery[(int(row["camera_index"]), int(row["delivery"]))].append(row)

    samples: dict[str, list[float]] = {
        "sensor_age_ms": [],
        "backend_to_return_ms": [],
        "arrival_to_return_ms": [],
    }
    for rows in by_delivery.values():
        samples["sensor_age_ms"].append(
            max(
                float(row["host_realtime_ns"]) / 1e6
                - float(row["sensor_timestamp_ms"])
                for row in rows
            )
        )
        samples["backend_to_return_ms"].append(
            max(float(row["backend_to_return_ms"]) for row in rows)
        )
        samples["arrival_to_return_ms"].append(
            max(float(row["arrival_to_return_ms"]) for row in rows)
        )

    result: dict[str, float] = {}
    for name, values in samples.items():
        if not values or any(value < 0 for value in values):
            raise ValueError(f"Invalid {name} samples in {path}")
        result[f"{name}_mean"] = fmean(values)
        result[f"{name}_p99"] = percentile(values, 0.99)
        result[f"{name}_max"] = max(values)
    return result


def write_csv(name: str, rows: list[dict[str, object]], fields: list[str]) -> Path:
    path = OUTPUT / name
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def selected_frame_events(benchmark_csv: Path, repetition: str) -> Path:
    artifact_root = benchmark_csv.with_suffix("")
    markers = list(artifact_root.glob(f"**/run-{repetition}/selected_attempt.txt"))
    if len(markers) != 1:
        raise ValueError(
            f"Expected one selected attempt for repetition {repetition} in "
            f"{artifact_root}, found {len(markers)}"
        )
    attempt = int(markers[0].read_text(encoding="utf-8").strip())
    path = markers[0].parent / f"attempt-{attempt}" / "frame_events.csv"
    if not path.is_file():
        raise ValueError(f"Missing selected frame events: {path}")
    return path


def frameset_problem_counts(path: Path) -> dict[str, int]:
    """Count affected deliveries rather than per-stream anomaly events."""
    highest: dict[tuple[int, str, int], int] = {}
    deliveries: set[tuple[int, int]] = set()
    stale: set[tuple[int, int]] = set()
    gaps: set[tuple[int, int]] = set()
    for row in read_csv(path):
        camera = int(row["camera_index"])
        delivery = (camera, int(row["delivery"]))
        stream = (camera, row["stream"], int(row["stream_index"]))
        frame_number = int(row["frame_number"])
        deliveries.add(delivery)
        if stream in highest:
            if frame_number <= highest[stream]:
                stale.add(delivery)
            elif frame_number > highest[stream] + 1:
                gaps.add(delivery)
        highest[stream] = max(highest.get(stream, frame_number), frame_number)
    return {
        "delivered_framesets": len(deliveries),
        "framesets_with_stale_streams": len(stale),
        "framesets_with_gaps": len(gaps),
        "problematic_framesets": len(stale | gaps),
    }


def prepare_uvc(sources: set[Path]) -> None:
    rows_out = []
    for urbs, cell in UVC_POOL_CELLS.items():
        for workload, fps in (("representative", "30 FPS"), ("stress", "60 FPS")):
            csvs = sorted((cell / workload).glob("benchmark_*.csv"))
            if len(csvs) != 1:
                raise ValueError(
                    f"Expected one UVC{urbs} benchmark CSV for {workload}, found {len(csvs)}"
                )
            path = csvs[0]
            sources.add(path)
            values: dict[str, int] = defaultdict(int)
            p99_values = []
            max_values = []
            for row in read_benchkit(path):
                if not row.get("case_id"):
                    continue
                values["runs"] += 1
                values["observed_frames"] += int(row["observed_frames"])
                values["duplicates"] += int(row["duplicate_frames"])
                values["sequence_gaps"] += int(row["sequence_gaps"])
                values["partially_stale"] += int(row["partially_stale_framesets"])
                frame_events = selected_frame_events(path, row["rep"])
                sources.add(frame_events)
                counts = frameset_problem_counts(frame_events)
                if counts["delivered_framesets"] != int(row["deliveries"]):
                    raise ValueError(
                        f"Delivery count disagrees with frame events in {frame_events}"
                    )
                for name, count in counts.items():
                    values[name] += count
                p99_values.append(float(row["delivery_interarrival_ms_p99"]))
                max_values.append(float(row["delivery_interarrival_ms_max"]))
            if values["runs"] != REPETITIONS:
                raise ValueError(f"Expected {REPETITIONS} UVC{urbs} runs for {fps}")
            rows_out.append(
                {
                    "fps": fps,
                    "urbs": urbs,
                    **values,
                    "p99_ms_median": f"{median(p99_values):.6f}",
                    "max_ms_worst": f"{max(max_values):.6f}",
                }
            )
    write_csv(
        "uvc_pool.csv",
        sorted(rows_out, key=lambda row: (row["fps"], row["urbs"])),
        ["fps", "urbs", "runs", "delivered_framesets", "observed_frames",
         "duplicates", "sequence_gaps", "partially_stale",
         "framesets_with_stale_streams", "framesets_with_gaps",
         "problematic_framesets", "p99_ms_median", "max_ms_worst"],
    )


def frameset_freshness_metrics(event_path: Path) -> dict[str, int]:
    """Count stale and gap-bearing deliveries from per-stream frame numbers."""
    deliveries: dict[tuple[int, int], dict[tuple[str, int], int]] = defaultdict(dict)
    for row in read_csv(event_path):
        delivery = (int(row["camera_index"]), int(row["delivery"]))
        stream = (row["stream"], int(row["stream_index"]))
        deliveries[delivery][stream] = int(row["frame_number"])

    previous: dict[int, dict[tuple[str, int], int]] = {}
    stale = gap_bearing = problem = duplicate_components = sequence_gaps = 0
    for (camera, _), streams in sorted(deliveries.items()):
        prior = previous.get(camera, {})
        has_duplicate = False
        has_gap = False
        for stream, current in streams.items():
            if stream not in prior:
                continue
            delta = current - prior[stream]
            if delta == 0:
                has_duplicate = True
                duplicate_components += 1
            elif delta > 1:
                has_gap = True
                sequence_gaps += delta - 1
        stale += int(has_duplicate)
        gap_bearing += int(has_gap)
        problem += int(has_duplicate or has_gap)
        previous[camera] = streams

    return {
        "deliveries": len(deliveries),
        "stale_framesets": stale,
        "gap_bearing_framesets": gap_bearing,
        "nonfresh_framesets": problem,
        "duplicate_components": duplicate_components,
        "sequence_gaps": sequence_gaps,
    }
