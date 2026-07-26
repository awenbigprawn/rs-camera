#!/usr/bin/env python3
"""Build a repeatable D435 startup-thread timing model from campaign CSV files."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
import html
import json
import math
from pathlib import Path
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


STATE_COLORS = {
    "running": "#16803c",
    "ready": "#e59f00",
    "sleeping": "#b8c7d9",
}


@dataclass
class PeriodicResult:
    start_ms: float
    period_ms: float
    match_ratio: float
    jitter_ratio: float
    releases: int


@dataclass
class ThreadSample:
    run_id: str
    attempt_dir: Path
    role: str
    rank: int
    tid: str
    name: str
    creation_phase: str
    created_ms: Optional[float]
    started_ms: Optional[float]
    first_run_ms: Optional[float]
    first_sleep_ms: Optional[float]
    exited_ms: Optional[float]
    joined_ms: Optional[float]
    status: str
    intervals: List[Dict[str, Any]] = field(default_factory=list)
    periodic: Optional[PeriodicResult] = None
    execution_to_cutoff_ms: Optional[float] = None
    sleep_to_cutoff_ms: Optional[float] = None
    ready_to_cutoff_ms: Optional[float] = None


@dataclass
class RunSample:
    run_id: str
    attempt_dir: Path
    summary: Dict[str, Any]
    phases: Dict[str, float]
    threads: List[ThreadSample]
    signature: Tuple[str, ...]
    cutoff_ms: Optional[float] = None


def optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def percentile(values: Iterable[Optional[float]], fraction: float) -> Optional[float]:
    ordered = sorted(value for value in values if value is not None and math.isfinite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def phase_times(events_path: Path) -> Dict[str, float]:
    phases: Dict[str, float] = {}
    with events_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["source"] == "pthread" and row["event"] == "phase_marker":
                phases[row["detail"]] = float(row["timestamp_ms"])
    return phases


def creation_phase(created_ms: Optional[float], phases: Dict[str, float], cycle: int) -> str:
    if created_ms is None:
        return "unknown"
    markers = [
        (f"cycle_{cycle:02d}_begin", "process_setup"),
        (f"cycle_{cycle:02d}_after_context", "context"),
        (f"cycle_{cycle:02d}_before_pipeline_construction", "device_discovery"),
        (f"cycle_{cycle:02d}_before_pipeline_start", "pipeline_construction"),
        (f"cycle_{cycle:02d}_after_pipeline_start", "pipeline_start"),
        (f"cycle_{cycle:02d}_frames_complete", "streaming"),
        (f"cycle_{cycle:02d}_end", "teardown"),
    ]
    previous = "process_setup"
    for marker, following in markers:
        timestamp = phases.get(marker)
        if timestamp is None:
            continue
        if created_ms < timestamp:
            return previous
        previous = following
    return previous


def merge_running_bursts(
    intervals: Sequence[Dict[str, Any]],
    window_start_ms: float,
    window_end_ms: float,
    merge_gap_ms: float,
) -> List[Tuple[float, float]]:
    running = sorted(
        (
            max(float(row["start_ms"]), window_start_ms),
            min(float(row["end_ms"]), window_end_ms),
        )
        for row in intervals
        if row["state"] == "running"
        and float(row["end_ms"]) >= window_start_ms
        and float(row["start_ms"]) <= window_end_ms
    )
    bursts: List[List[float]] = []
    for start, end in running:
        if end < start:
            continue
        if not bursts or start - bursts[-1][1] > merge_gap_ms:
            bursts.append([start, end])
        else:
            bursts[-1][1] = max(bursts[-1][1], end)
    return [(start, end) for start, end in bursts]


def _period_fit(
    gaps: Sequence[float],
    min_period_ms: float,
    max_period_ms: float,
    tolerance: float,
    max_multiple: int,
) -> Optional[Tuple[float, float, float, float]]:
    candidates: List[float] = []
    for gap in gaps:
        for multiple in range(1, max_multiple + 1):
            period = gap / multiple
            if min_period_ms <= period <= max_period_ms:
                candidates.append(period)
    best: Optional[Tuple[float, float, float, float]] = None
    for candidate in candidates:
        residuals: List[float] = []
        matches = 0
        for gap in gaps:
            multiple = max(1, round(gap / candidate))
            if multiple > max_multiple:
                residuals.append(float("inf"))
                continue
            residual = abs(gap - multiple * candidate) / candidate
            residuals.append(residual)
            if residual <= tolerance:
                matches += 1
        match_ratio = matches / len(gaps)
        finite = [value for value in residuals if math.isfinite(value)]
        jitter = statistics.median(finite) if finite else float("inf")
        direct_matches = sum(
            1 for gap in gaps if abs(gap - candidate) / candidate <= tolerance
        )
        score = (match_ratio, direct_matches / len(gaps), -jitter, candidate)
        if best is None or score > (best[1], best[2], -best[3], best[0]):
            best = (candidate, match_ratio, direct_matches / len(gaps), jitter)
    if best is None:
        return None
    return best[0], best[1], best[2], best[3]


def detect_stable_period(
    intervals: Sequence[Dict[str, Any]],
    window_start_ms: float,
    window_end_ms: float,
    *,
    merge_gap_ms: float = 2.0,
    min_period_ms: float = 5.0,
    max_period_ms: float = 250.0,
    min_periods: int = 6,
    tolerance: float = 0.18,
    required_match_ratio: float = 0.9,
    required_direct_ratio: float = 0.6,
    max_multiple: int = 8,
) -> Optional[PeriodicResult]:
    bursts = merge_running_bursts(
        intervals, window_start_ms, window_end_ms, merge_gap_ms
    )
    starts = [start for start, _ in bursts]
    if len(starts) < min_periods + 1:
        return None

    for index in range(0, len(starts) - min_periods):
        candidate_starts = starts[index:]
        gaps = [
            following - current
            for current, following in zip(candidate_starts, candidate_starts[1:])
        ]
        if len(gaps) < min_periods:
            continue
        fit = _period_fit(
            gaps, min_period_ms, max_period_ms, tolerance, max_multiple
        )
        if fit is None:
            continue
        period, match_ratio, direct_ratio, jitter = fit
        if match_ratio < required_match_ratio:
            continue
        if direct_ratio < required_direct_ratio:
            continue
        if candidate_starts[-1] - candidate_starts[0] < min_periods * period:
            continue
        if window_end_ms - candidate_starts[-1] > max_multiple * period * 1.2:
            continue
        return PeriodicResult(
            start_ms=candidate_starts[0],
            period_ms=period,
            match_ratio=match_ratio,
            jitter_ratio=jitter,
            releases=len(candidate_starts),
        )
    return None


def selected_attempt_dirs(campaign_dir: Path, policy: str) -> List[Path]:
    attempts: List[Path] = []
    policy_component = f"policy-{policy}"
    for timing_path in campaign_dir.rglob("thread_timing.csv"):
        attempt_dir = timing_path.parent
        if policy_component not in attempt_dir.parts:
            continue
        run_dir = attempt_dir.parent
        selected_path = run_dir / "selected_attempt.txt"
        if selected_path.exists():
            selected = selected_path.read_text(encoding="utf-8").strip()
            if attempt_dir.name != f"attempt-{selected}":
                continue
        summary_path = attempt_dir / "summary.json"
        if not summary_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not summary.get("startup_result", {}).get("success", False):
            continue
        attempts.append(attempt_dir)
    return sorted(attempts)


def read_run(
    attempt_dir: Path,
    cycle: int,
    periodic_options: Dict[str, Any],
) -> RunSample:
    summary = json.loads((attempt_dir / "summary.json").read_text(encoding="utf-8"))
    phases = phase_times(attempt_dir / "thread_events.csv")
    run_id = str(attempt_dir.relative_to(attempt_dir.parents[4]))

    intervals_by_tid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    with (attempt_dir / "thread_intervals.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            intervals_by_tid[row["tid"]].append(row)

    timing_rows: List[Dict[str, str]] = []
    with (attempt_dir / "thread_timing.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["cycle"] == str(cycle) or (
                row["cycle"] == "0" and row["tid"] == row["tgid"]
            ):
                timing_rows.append(row)
    timing_rows.sort(
        key=lambda row: (
            0 if row["tid"] == row["tgid"] else 1,
            optional_float(row["created_ms"]) or 0.0,
            int(row["tid"]),
        )
    )

    stream_start = phases[f"cycle_{cycle:02d}_after_pipeline_start"]
    stream_end = phases[f"cycle_{cycle:02d}_frames_complete"]
    threads: List[ThreadSample] = []
    for rank, row in enumerate(timing_rows):
        is_main = row["tid"] == row["tgid"]
        role = "T00-main" if is_main else f"T{rank:02d}"
        intervals = sorted(
            intervals_by_tid[row["tid"]], key=lambda value: float(value["start_ms"])
        )
        sample = ThreadSample(
            run_id=run_id,
            attempt_dir=attempt_dir,
            role=role,
            rank=rank,
            tid=row["tid"],
            name=row["name"] or "unnamed",
            creation_phase=(
                "process"
                if is_main
                else creation_phase(optional_float(row["created_ms"]), phases, cycle)
            ),
            created_ms=optional_float(row["created_ms"]),
            started_ms=optional_float(row["started_ms"]),
            first_run_ms=optional_float(row["first_run_ms"]),
            first_sleep_ms=optional_float(row["first_sleep_ms"]),
            exited_ms=optional_float(row["exited_ms"]),
            joined_ms=optional_float(row["joined_ms"]),
            status=row["status"],
            intervals=intervals,
        )
        sample.periodic = detect_stable_period(
            intervals, stream_start, stream_end, **periodic_options
        )
        threads.append(sample)

    signature = tuple(f"{thread.name}@{thread.creation_phase}" for thread in threads)
    return RunSample(
        run_id=run_id,
        attempt_dir=attempt_dir,
        summary=summary,
        phases=phases,
        threads=threads,
        signature=signature,
    )


def interval_total(
    intervals: Sequence[Dict[str, Any]], state: str, cutoff_ms: float
) -> float:
    total = 0.0
    for interval in intervals:
        if interval["state"] != state:
            continue
        start = max(0.0, float(interval["start_ms"]))
        end = min(cutoff_ms, float(interval["end_ms"]))
        if end > start:
            total += end - start
    return total


def metric_summary(
    samples: Sequence[ThreadSample], field_name: str
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    values = [getattr(sample, field_name) for sample in samples]
    return percentile(values, 0.05), percentile(values, 0.5), percentile(values, 0.95)


def format_interval(values: Tuple[Optional[float], Optional[float], Optional[float]]) -> str:
    low, median, high = values
    if median is None:
        return "-"
    return f"{median:.3f} [{low:.3f}, {high:.3f}]"


def format_value(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6f}"


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def svg_timeline(
    path: Path,
    run: RunSample,
    periodic_roles: Sequence[str],
    cutoff_ms: float,
    policy: str,
) -> None:
    left = 240
    right = 40
    top = 105
    row_height = 27
    plot_width = 1450
    width = left + plot_width + right
    height = top + row_height * len(run.threads) + 90
    scale = plot_width / max(cutoff_ms, 1.0)

    def x(timestamp: float) -> float:
        return left + max(0.0, min(timestamp, cutoff_ms)) * scale

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>"
        "text{font-family:DejaVu Sans,Arial,sans-serif;fill:#17202a}"
        ".small{font-size:11px}.label{font-size:12px}.title{font-size:20px;font-weight:600}"
        ".grid{stroke:#d7dde5;stroke-width:1}.phase{stroke:#6b7280;stroke-width:1;stroke-dasharray:4 4}"
        "</style>",
        f'<text x="{left}" y="28" class="title">D435 startup thread timeline — representative {html.escape(policy)}</text>',
        f'<text x="{left}" y="50" class="label">t=0 is process_start; chart ends at the last first stable-period release ({cutoff_ms:.3f} ms)</text>',
    ]

    tick_step = 50.0
    if cutoff_ms > 1000:
        tick_step = 100.0
    elif cutoff_ms < 300:
        tick_step = 25.0
    tick = 0.0
    while tick <= cutoff_ms + 1e-9:
        xpos = x(tick)
        elements.append(
            f'<line x1="{xpos:.2f}" y1="{top - 18}" x2="{xpos:.2f}" '
            f'y2="{height - 60}" class="grid"/>'
        )
        elements.append(
            f'<text x="{xpos:.2f}" y="{top - 25}" text-anchor="middle" class="small">{tick:g}</text>'
        )
        tick += tick_step

    phase_names = [
        ("cycle_01_after_context", "context ready"),
        ("cycle_01_before_pipeline_start", "pipeline.start"),
        ("cycle_01_after_pipeline_start", "streaming requested"),
        ("cycle_01_first_frame", "first frameset"),
    ]
    for phase, label in phase_names:
        timestamp = run.phases.get(phase)
        if timestamp is None or timestamp < 0 or timestamp > cutoff_ms:
            continue
        xpos = x(timestamp)
        elements.append(
            f'<line x1="{xpos:.2f}" y1="{top - 18}" x2="{xpos:.2f}" '
            f'y2="{height - 60}" class="phase"/>'
        )
        elements.append(
            f'<text x="{xpos + 3:.2f}" y="{top - 6}" class="small" '
            f'transform="rotate(-28 {xpos + 3:.2f},{top - 6})">{html.escape(label)}</text>'
        )

    periodic_set = set(periodic_roles)
    for index, thread in enumerate(run.threads):
        y = top + index * row_height
        label = f"{thread.role} {thread.name} ({thread.creation_phase})"
        elements.append(
            f'<text x="{left - 8}" y="{y + 16}" text-anchor="end" class="label">{html.escape(label)}</text>'
        )
        created = thread.created_ms or 0.0
        lifetime_end = min(thread.exited_ms or cutoff_ms, cutoff_ms)
        if lifetime_end > created:
            elements.append(
                f'<rect x="{x(created):.2f}" y="{y + 5}" '
                f'width="{max(0.8, (lifetime_end - created) * scale):.2f}" height="13" '
                f'fill="#eef1f5"/>'
            )
        for interval in thread.intervals:
            start = max(0.0, float(interval["start_ms"]))
            end = min(cutoff_ms, float(interval["end_ms"]))
            if end <= start:
                continue
            color = STATE_COLORS.get(interval["state"], "#dadde2")
            minimum = 1.1 if interval["state"] == "running" else 0.5
            elements.append(
                f'<rect x="{x(start):.2f}" y="{y + 5}" '
                f'width="{max(minimum, (end - start) * scale):.2f}" height="13" '
                f'fill="{color}"><title>{html.escape(thread.role)} {html.escape(interval["state"])} '
                f'{start:.3f}–{end:.3f} ms</title></rect>'
            )
        if thread.created_ms is not None and thread.created_ms <= cutoff_ms:
            xpos = x(thread.created_ms)
            elements.append(
                f'<line x1="{xpos:.2f}" y1="{y + 2}" x2="{xpos:.2f}" y2="{y + 21}" '
                f'stroke="#6f42c1" stroke-width="1.5"/>'
            )
        if thread.exited_ms is not None and thread.exited_ms <= cutoff_ms:
            xpos = x(thread.exited_ms)
            elements.append(
                f'<path d="M{xpos - 3:.2f},{y + 7} L{xpos + 3:.2f},{y + 17} '
                f'M{xpos + 3:.2f},{y + 7} L{xpos - 3:.2f},{y + 17}" '
                f'stroke="#111827" stroke-width="1.5"/>'
            )
        if thread.role in periodic_set and thread.periodic is not None:
            xpos = x(thread.periodic.start_ms)
            elements.append(
                f'<path d="M{xpos:.2f},{y + 1} L{xpos + 4:.2f},{y + 5} '
                f'L{xpos:.2f},{y + 9} L{xpos - 4:.2f},{y + 5} Z" fill="#d62728">'
                f'<title>stable period starts {thread.periodic.start_ms:.3f} ms; '
                f'period {thread.periodic.period_ms:.3f} ms</title></path>'
            )

    legend_y = height - 35
    legend = [
        ("running", STATE_COLORS["running"]),
        ("ready", STATE_COLORS["ready"]),
        ("sleeping", STATE_COLORS["sleeping"]),
    ]
    cursor = left
    for label, color in legend:
        elements.append(
            f'<rect x="{cursor}" y="{legend_y - 11}" width="16" height="11" fill="{color}"/>'
        )
        elements.append(
            f'<text x="{cursor + 21}" y="{legend_y}" class="label">{label}</text>'
        )
        cursor += 105
    elements.append(
        f'<line x1="{cursor}" y1="{legend_y - 13}" x2="{cursor}" y2="{legend_y + 3}" '
        f'stroke="#6f42c1" stroke-width="1.5"/>'
    )
    elements.append(
        f'<text x="{cursor + 8}" y="{legend_y}" class="label">pthread_create</text>'
    )
    cursor += 135
    elements.append(
        f'<path d="M{cursor},{legend_y - 13} L{cursor + 4},{legend_y - 9} '
        f'L{cursor},{legend_y - 5} L{cursor - 4},{legend_y - 9} Z" fill="#d62728"/>'
    )
    elements.append(
        f'<text x="{cursor + 9}" y="{legend_y}" class="label">first stable release</text>'
    )
    elements.append("</svg>")
    path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def build_model(
    campaign_dir: Path,
    output_dir: Path,
    *,
    policy: str,
    cycle: int,
    min_periods: int,
    periodic_presence: float,
) -> Dict[str, Any]:
    attempt_dirs = selected_attempt_dirs(campaign_dir, policy)
    if not attempt_dirs:
        raise ValueError(f"No successful selected attempts found below {campaign_dir}")

    policy_component = f"policy-{policy}"
    attempt_history: List[List[Dict[str, Any]]] = []
    for attempts_path in campaign_dir.rglob("attempts.json"):
        if policy_component not in attempts_path.parts:
            continue
        values = json.loads(attempts_path.read_text(encoding="utf-8"))
        if isinstance(values, list) and values:
            attempt_history.append(values)
    initial_failure_runs = sum(
        not bool(attempts[0].get("success", False))
        for attempts in attempt_history
    )
    eventual_failure_runs = sum(
        not any(bool(attempt.get("success", False)) for attempt in attempts)
        for attempts in attempt_history
    )
    failed_attempts = sum(
        not bool(attempt.get("success", False))
        for attempts in attempt_history
        for attempt in attempts
    )

    periodic_options = {"min_periods": min_periods}
    runs = [read_run(path, cycle, periodic_options) for path in attempt_dirs]
    signature_counts = Counter(run.signature for run in runs)
    modal_signature, modal_count = signature_counts.most_common(1)[0]
    accepted = [run for run in runs if run.signature == modal_signature]
    excluded = [run for run in runs if run.signature != modal_signature]

    samples_by_role: Dict[str, List[ThreadSample]] = defaultdict(list)
    for run in accepted:
        for thread in run.threads:
            samples_by_role[thread.role].append(thread)

    periodic_roles = [
        role
        for role, samples in samples_by_role.items()
        if sum(sample.periodic is not None for sample in samples) / len(samples)
        >= periodic_presence
    ]
    periodic_roles.sort(key=lambda role: samples_by_role[role][0].rank)
    if not periodic_roles:
        raise ValueError("No recurring periodic thread roles were detected")

    complete_runs: List[RunSample] = []
    for run in accepted:
        periodic_by_role = {thread.role: thread.periodic for thread in run.threads}
        if all(periodic_by_role.get(role) is not None for role in periodic_roles):
            run.cutoff_ms = max(
                periodic_by_role[role].start_ms  # type: ignore[union-attr]
                for role in periodic_roles
            )
            complete_runs.append(run)
    if not complete_runs:
        raise ValueError("No run detected every recurring periodic role")

    complete_ids = {run.run_id for run in complete_runs}
    for samples in samples_by_role.values():
        for sample in samples:
            run = next(run for run in complete_runs if run.run_id == sample.run_id) if sample.run_id in complete_ids else None
            if run is None or run.cutoff_ms is None:
                continue
            sample.execution_to_cutoff_ms = interval_total(
                sample.intervals, "running", run.cutoff_ms
            )
            sample.sleep_to_cutoff_ms = interval_total(
                sample.intervals, "sleeping", run.cutoff_ms
            )
            sample.ready_to_cutoff_ms = interval_total(
                sample.intervals, "ready", run.cutoff_ms
            )

    cutoff_values = [run.cutoff_ms for run in complete_runs]
    cutoff_median = percentile(cutoff_values, 0.5)
    assert cutoff_median is not None
    representative = min(
        complete_runs, key=lambda run: abs((run.cutoff_ms or 0.0) - cutoff_median)
    )
    assert representative.cutoff_ms is not None

    output_dir.mkdir(parents=True, exist_ok=True)
    sample_rows: List[Dict[str, Any]] = []
    model_rows: List[Dict[str, Any]] = []
    for role in sorted(samples_by_role, key=lambda value: samples_by_role[value][0].rank):
        samples = samples_by_role[role]
        complete_samples = [sample for sample in samples if sample.run_id in complete_ids]
        periodic_samples = [sample for sample in samples if sample.periodic is not None]
        for sample in samples:
            sample_rows.append({
                "run_id": sample.run_id,
                "role": sample.role,
                "tid": sample.tid,
                "name": sample.name,
                "creation_phase": sample.creation_phase,
                "created_ms": format_value(sample.created_ms),
                "started_ms": format_value(sample.started_ms),
                "first_run_ms": format_value(sample.first_run_ms),
                "first_sleep_ms": format_value(sample.first_sleep_ms),
                "stable_period_start_ms": format_value(
                    sample.periodic.start_ms if sample.periodic else None
                ),
                "period_ms": format_value(
                    sample.periodic.period_ms if sample.periodic else None
                ),
                "period_match_ratio": format_value(
                    sample.periodic.match_ratio if sample.periodic else None
                ),
                "period_jitter_ratio": format_value(
                    sample.periodic.jitter_ratio if sample.periodic else None
                ),
                "exited_ms": format_value(sample.exited_ms),
                "joined_ms": format_value(sample.joined_ms),
                "execution_to_cutoff_ms": format_value(sample.execution_to_cutoff_ms),
                "sleep_to_cutoff_ms": format_value(sample.sleep_to_cutoff_ms),
                "ready_to_cutoff_ms": format_value(sample.ready_to_cutoff_ms),
                "status": sample.status,
            })

        periodic_rate = len(periodic_samples) / len(samples)
        classification = (
            "periodic"
            if role in periodic_roles
            else (
                "transient"
                if sum(
                    sample.exited_ms is not None
                    and sample.exited_ms
                    <= next(
                        run.phases[f"cycle_{cycle:02d}_after_pipeline_start"]
                        for run in accepted
                        if run.run_id == sample.run_id
                    )
                    for sample in samples
                )
                >= len(samples) / 2
                else "event-driven/non-periodic"
            )
        )
        periodic_start_values = (
            [
                sample.periodic.start_ms if sample.periodic else None
                for sample in samples
            ]
            if role in periodic_roles
            else []
        )
        period_values = (
            [
                sample.periodic.period_ms if sample.periodic else None
                for sample in samples
            ]
            if role in periodic_roles
            else []
        )
        model_rows.append({
            "role": role,
            "name": samples[0].name,
            "creation_phase": samples[0].creation_phase,
            "observed_runs": len(samples),
            "accepted_runs": len(accepted),
            "classification": classification,
            "periodic_detection_rate": periodic_rate,
            "created_p05_ms": percentile(
                (sample.created_ms for sample in samples), 0.05
            ),
            "created_p50_ms": percentile(
                (sample.created_ms for sample in samples), 0.5
            ),
            "created_p95_ms": percentile(
                (sample.created_ms for sample in samples), 0.95
            ),
            "started_p05_ms": percentile(
                (sample.started_ms for sample in samples), 0.05
            ),
            "started_p50_ms": percentile(
                (sample.started_ms for sample in samples), 0.5
            ),
            "started_p95_ms": percentile(
                (sample.started_ms for sample in samples), 0.95
            ),
            "first_run_p05_ms": percentile(
                (sample.first_run_ms for sample in samples), 0.05
            ),
            "first_run_p50_ms": percentile(
                (sample.first_run_ms for sample in samples), 0.5
            ),
            "first_run_p95_ms": percentile(
                (sample.first_run_ms for sample in samples), 0.95
            ),
            "first_sleep_p05_ms": percentile(
                (sample.first_sleep_ms for sample in samples), 0.05
            ),
            "first_sleep_p50_ms": percentile(
                (sample.first_sleep_ms for sample in samples), 0.5
            ),
            "first_sleep_p95_ms": percentile(
                (sample.first_sleep_ms for sample in samples), 0.95
            ),
            "stable_period_start_p05_ms": percentile(periodic_start_values, 0.05),
            "stable_period_start_p50_ms": percentile(periodic_start_values, 0.5),
            "stable_period_start_p95_ms": percentile(periodic_start_values, 0.95),
            "period_p05_ms": percentile(period_values, 0.05),
            "period_p50_ms": percentile(period_values, 0.5),
            "period_p95_ms": percentile(period_values, 0.95),
            "exited_p05_ms": percentile(
                (sample.exited_ms for sample in samples), 0.05
            ),
            "exited_p50_ms": percentile(
                (sample.exited_ms for sample in samples), 0.5
            ),
            "exited_p95_ms": percentile(
                (sample.exited_ms for sample in samples), 0.95
            ),
            "execution_to_cutoff_p50_ms": percentile(
                (sample.execution_to_cutoff_ms for sample in complete_samples), 0.5
            ),
            "sleep_to_cutoff_p50_ms": percentile(
                (sample.sleep_to_cutoff_ms for sample in complete_samples), 0.5
            ),
            "ready_to_cutoff_p50_ms": percentile(
                (sample.ready_to_cutoff_ms for sample in complete_samples), 0.5
            ),
        })

    sample_fields = list(sample_rows[0])
    model_fields = list(model_rows[0])
    write_csv(output_dir / "startup_thread_samples.csv", sample_fields, sample_rows)
    write_csv(output_dir / "startup_thread_model.csv", model_fields, model_rows)

    svg_timeline(
        output_dir / "startup_timeline.svg",
        representative,
        periodic_roles,
        representative.cutoff_ms,
        policy,
    )

    cutoff_summary = (
        percentile(cutoff_values, 0.05),
        percentile(cutoff_values, 0.5),
        percentile(cutoff_values, 0.95),
    )
    cutoff_max = max(cutoff_values)
    report_lines = [
        "# D435 startup-thread timing model",
        "",
        f"- Campaign: `{campaign_dir}`",
        f"- Policy: `{policy}`",
        f"- Logical runs recorded: {len(attempt_history)}",
        f"- Initial-attempt failures: {initial_failure_runs} "
        f"({initial_failure_runs / max(len(attempt_history), 1):.1%})",
        f"- Eventual failures after recovery/retry: {eventual_failure_runs}",
        f"- Failed measured attempts preserved: {failed_attempts}",
        f"- Successful selected attempts discovered: {len(runs)}",
        f"- Runs with the modal {len(modal_signature)}-thread shape: {len(accepted)}",
        f"- Runs detecting every recurring periodic role: {len(complete_runs)}",
        f"- Excluded non-modal runs: {len(excluded)}",
        f"- Stable-period cutoff, ms: {format_interval(cutoff_summary)}",
        f"- Maximum observed stable-period cutoff, ms: {cutoff_max:.3f}",
        f"- Representative timeline: `{representative.run_id}` "
        f"(cutoff {representative.cutoff_ms:.3f} ms)",
        "",
        "Times are milliseconds from the application `process_start` marker. "
        "Each table cell is p50 [p05, p95] across accepted independent runs. "
        "The timeline is clipped at the representative run's last first stable "
        "periodic release, but exit times in the table use the complete run.",
        "",
        "A stable period requires at least "
        f"{min_periods} observed gaps, at least 90% agreement with one base period, "
        "and permits gaps that are integer multiples up to 8 periods. Scheduler "
        "slices separated by at most 2 ms are merged into one work burst.",
        "",
        "| Role | Kernel name | Creation phase | Class | Seen | Created ms | pthread start ms | "
        "First running ms | First sleeping ms | Stable-period start ms | Period ms | "
        "Exit ms | CPU to cutoff ms | Sleep to cutoff ms | Ready to cutoff ms |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in model_rows:
        report_lines.append(
            "| {role} | {name} | {creation_phase} | {classification} | "
            "{observed_runs}/{accepted_runs} | {created} | {started} | {first_run} | "
            "{first_sleep} | {periodic_start} | {period} | {exited} | {execution} | "
            "{sleep} | {ready} |".format(
                **row,
                created=format_interval((
                    row["created_p05_ms"], row["created_p50_ms"], row["created_p95_ms"]
                )),
                started=format_interval((
                    row["started_p05_ms"], row["started_p50_ms"], row["started_p95_ms"]
                )),
                first_run=format_interval((
                    row["first_run_p05_ms"], row["first_run_p50_ms"], row["first_run_p95_ms"]
                )),
                first_sleep=format_interval((
                    row["first_sleep_p05_ms"], row["first_sleep_p50_ms"], row["first_sleep_p95_ms"]
                )),
                periodic_start=format_interval((
                    row["stable_period_start_p05_ms"],
                    row["stable_period_start_p50_ms"],
                    row["stable_period_start_p95_ms"],
                )),
                period=format_interval((
                    row["period_p05_ms"], row["period_p50_ms"], row["period_p95_ms"]
                )),
                exited=format_interval((
                    row["exited_p05_ms"], row["exited_p50_ms"], row["exited_p95_ms"]
                )),
                execution=(
                    "-"
                    if row["execution_to_cutoff_p50_ms"] is None
                    else f'{row["execution_to_cutoff_p50_ms"]:.3f}'
                ),
                sleep=(
                    "-"
                    if row["sleep_to_cutoff_p50_ms"] is None
                    else f'{row["sleep_to_cutoff_p50_ms"]:.3f}'
                ),
                ready=(
                    "-"
                    if row["ready_to_cutoff_p50_ms"] is None
                    else f'{row["ready_to_cutoff_p50_ms"]:.3f}'
                ),
            )
        )
    report_lines.extend([
        "",
        "Thread roles are trace identities, not inferred librealsense function names. "
        "Except for explicit `libusb_event` names, librealsense workers inherit the "
        "`d435-probe` name. Roles T01, T02, ... are aligned by creation order after "
        "requiring the same modal name/creation-phase sequence in every accepted run.",
        "",
    ])
    (output_dir / "startup_thread_model.md").write_text(
        "\n".join(report_lines), encoding="utf-8"
    )

    metadata = {
        "schema_version": 1,
        "campaign_dir": str(campaign_dir),
        "policy": policy,
        "cycle": cycle,
        "logical_runs_recorded": len(attempt_history),
        "initial_failure_runs": initial_failure_runs,
        "initial_failure_rate": initial_failure_runs / max(len(attempt_history), 1),
        "eventual_failure_runs": eventual_failure_runs,
        "failed_attempts_preserved": failed_attempts,
        "successful_selected_attempts": len(runs),
        "modal_shape_runs": len(accepted),
        "periodic_complete_runs": len(complete_runs),
        "excluded_non_modal_runs": [run.run_id for run in excluded],
        "thread_roles": len(modal_signature),
        "periodic_roles": periodic_roles,
        "periodic_presence_threshold": periodic_presence,
        "stable_period_min_gaps": min_periods,
        "cutoff_p05_ms": cutoff_summary[0],
        "cutoff_p50_ms": cutoff_summary[1],
        "cutoff_p95_ms": cutoff_summary[2],
        "cutoff_max_ms": cutoff_max,
        "representative_run": representative.run_id,
        "representative_cutoff_ms": representative.cutoff_ms,
        "device": representative.summary.get("device", {}),
        "scheduler": representative.summary.get("scheduler", {}),
    }
    (output_dir / "model_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate repeated successful startup traces into a per-thread timing "
            "model and a horizontal scheduler-state timeline."
        )
    )
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--policy", choices=("other", "rr", "fifo"), default="other")
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument(
        "--min-periods",
        type=int,
        default=6,
        help="Minimum number of release gaps required for stable-period detection.",
    )
    parser.add_argument(
        "--periodic-presence",
        type=float,
        default=0.8,
        help="Fraction of modal runs in which a role must be periodic.",
    )
    args = parser.parse_args()
    if args.cycle < 1:
        parser.error("--cycle must be positive")
    if args.min_periods < 3:
        parser.error("--min-periods must be at least 3")
    if not 0.5 <= args.periodic_presence <= 1.0:
        parser.error("--periodic-presence must be in [0.5, 1.0]")
    metadata = build_model(
        args.campaign_dir.resolve(),
        args.output_dir.resolve(),
        policy=args.policy,
        cycle=args.cycle,
        min_periods=args.min_periods,
        periodic_presence=args.periodic_presence,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
