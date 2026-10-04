"""Paper E4 acceptance checks and metrics; preserved from the manuscript helper."""
from __future__ import annotations
from collections import defaultdict
import csv
import json
from pathlib import Path
from typing import Any

PHASE_KERNEL = {
    "standard-hardirq": "standard",
    "standard-threaded": "standard",
    "rt-threaded": "rt",
}
POLICY_LABEL = {
    "other": "OTHER",
    "rr-rm": "RR-RM",
    "fifo-rm": "FIFO-RM",
    "deadline": "Deadline",
}
NOISE_LABEL = {
    "none": "None",
    "cpu4": "CPU",
    "memory2000": "Memory",
    "gpu": "GPU",
}



def reconstruct_freshness(path: Path) -> dict[str, int]:
    previous: dict[tuple[str, str, str], int] = {}
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            grouped[(row["camera_index"], row["delivery"])].append(row)

    problem = duplicate_components = sequence_gaps = 0
    for (camera, _delivery), rows in grouped.items():
        any_stale = False
        any_gap = False
        for row in rows:
            stream = (camera, row["stream"], row["stream_index"])
            current = int(row["frame_number"])
            prior = previous.get(stream)
            if prior is not None:
                if current <= prior:
                    any_stale = True
                    duplicate_components += 1
                elif current > prior + 1:
                    any_gap = True
                    sequence_gaps += current - prior - 1
            previous[stream] = current
        if any_stale or any_gap:
            problem += 1
    return {
        "problem_framesets": problem,
        "duplicate_components": duplicate_components,
        "sequence_gaps": sequence_gaps,
    }


def parse_run(done: Path, root: Path) -> tuple[dict[str, Any], list[Path]]:
    phase, formal, round_part, case_id, noise_part, policy_part, marker = (
        done.relative_to(root).parts
    )
    if formal != "formal" or marker != "DONE":
        raise ValueError(f"Unexpected cell path: {done}")

    summaries = sorted(
        (done.parent / "results").rglob("host_receive_latency_summary.json")
    )
    if len(summaries) != 1:
        raise ValueError(
            f"Expected one selected host-latency summary for {done.parent}, "
            f"found {len(summaries)}"
        )
    latency_path = summaries[0]
    run_dir = latency_path.parent
    steady_path = run_dir / "steady_summary.json"
    events_path = run_dir / "frame_events.csv"
    for path in (steady_path, events_path):
        if not path.is_file():
            raise ValueError(f"Missing selected-attempt artifact: {path}")

    steady = json.loads(steady_path.read_text(encoding="utf-8"))
    if not steady.get("success"):
        raise ValueError(f"Selected attempt is not successful: {steady_path}")
    summary = json.loads(latency_path.read_text(encoding="utf-8"))
    latency = summary["host_receive_to_frameset_latency"]
    if float(latency["coverage"]) < 0.95:
        raise ValueError(f"Latency coverage below 95%: {latency_path}")

    policy = policy_part.removeprefix("policy-")
    row = {
        "phase": phase,
        "kernel": PHASE_KERNEL[phase],
        "round": int(round_part.removeprefix("round-")),
        "workload": "60 FPS" if "stress60" in case_id else "30 FPS",
        "noise": NOISE_LABEL[noise_part.removeprefix("noise-")],
        "policy": POLICY_LABEL[policy],
        "p99_ms": float(latency["p99_us"]) / 1000.0,
        "max_ms": float(latency["max_us"]) / 1000.0,
        "coverage": float(latency["coverage"]),
        "matched_deliveries": int(latency["matched_deliveries"]),
        "total_deliveries": int(latency["total_deliveries"]),
        **reconstruct_freshness(events_path),
    }
    return row, [latency_path, steady_path, events_path]
