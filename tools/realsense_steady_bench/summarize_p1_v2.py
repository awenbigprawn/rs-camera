#!/usr/bin/env python3
"""Summarize the P1 v2 matrix and reconstruct u/g freshness outcomes."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys
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
RUN_FIELDS = [
    "phase",
    "kernel",
    "round",
    "workload",
    "noise",
    "policy",
    "p99_ms",
    "max_ms",
    "deliveries",
    "problem_framesets",
    "stale_framesets_ug",
    "gap_bearing_framesets",
    "duplicate_components",
    "sequence_gaps",
    "partially_stale_framesets",
    "fully_stale_framesets",
    "timeouts",
    "attempt_count",
]


def read_result_csv(path: Path) -> dict[str, str]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    rows = list(csv.DictReader(lines, delimiter=";"))
    if len(rows) != 1:
        raise ValueError(f"expected one row in {path}, found {len(rows)}")
    return rows[0]


def selected_frame_events(row: dict[str, str]) -> Path:
    selected = Path(row["selected_attempt_data_dir"])
    local = selected / "frame_events.csv"
    if local.is_file():
        return local
    raise ValueError(f"frame events are missing: {local}")


def reconstruct_freshness(path: Path) -> dict[str, int]:
    previous: dict[tuple[str, str, str], int] = {}
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            grouped[(row["camera_index"], row["delivery"])].append(row)

    problem = stale = gap_bearing = duplicate_components = sequence_gaps = 0
    for (camera, _delivery), rows in grouped.items():
        any_stale = False
        any_gap = False
        for row in rows:
            stream_key = (camera, row["stream"], row["stream_index"])
            current = int(row["frame_number"])
            prior = previous.get(stream_key)
            if prior is not None:
                if current <= prior:
                    any_stale = True
                    duplicate_components += 1
                elif current > prior + 1:
                    any_gap = True
                    sequence_gaps += current - prior - 1
            previous[stream_key] = current
        if any_stale:
            stale += 1
        if any_gap:
            gap_bearing += 1
        if any_stale or any_gap:
            problem += 1
    return {
        "problem_framesets": problem,
        "stale_framesets_ug": stale,
        "gap_bearing_framesets": gap_bearing,
        "duplicate_components": duplicate_components,
        "sequence_gaps": sequence_gaps,
    }


def parse_cell(validation_path: Path, root: Path) -> dict[str, Any]:
    relative = validation_path.parent.relative_to(root)
    parts = relative.parts
    if len(parts) < 6 or parts[1] != "formal":
        raise ValueError(f"unexpected P1 cell path: {relative}")
    phase = parts[0]
    round_number = int(parts[2].removeprefix("round-"))
    case_id = parts[3]
    noise = parts[4].removeprefix("noise-")
    policy = parts[5].removeprefix("policy-")
    csvs = sorted((validation_path.parent / "results").glob("benchmark_*.csv"))
    if len(csvs) != 1:
        raise ValueError(f"expected one result CSV beside {validation_path}")
    row = read_result_csv(csvs[0])
    events = Path(row["selected_attempt_data_dir"]) / "frame_events.csv"
    if not events.is_file():
        # Backed-up trees preserve the relative tail but not the original prefix.
        selected_attempt = int(row.get("selected_attempt") or 0)
        candidates = sorted(
            (validation_path.parent / "results").rglob(
                f"attempt-{selected_attempt}/frame_events.csv"
            )
        )
        if len(candidates) != 1:
            raise ValueError(
                "cannot locate the selected attempt's frame_events.csv for "
                f"{validation_path} (attempt {selected_attempt})"
            )
        events = candidates[0]
    freshness = reconstruct_freshness(events)
    workload = "60 FPS" if "stress60" in case_id else "30 FPS"
    return {
        "phase": phase,
        "kernel": PHASE_KERNEL[phase],
        "round": round_number,
        "workload": workload,
        "noise": NOISE_LABEL[noise],
        "policy": POLICY_LABEL[policy],
        "p99_ms": float(row["delivery_interarrival_ms_p99"]),
        "max_ms": float(row["delivery_interarrival_ms_max"]),
        "deliveries": int(row.get("deliveries") or 0),
        **freshness,
        "partially_stale_framesets": int(row.get("partially_stale_framesets") or 0),
        "fully_stale_framesets": int(row.get("stale_framesets") or 0),
        "timeouts": int(row.get("timeouts") or 0),
        "attempt_count": int(row.get("attempt_count") or 0),
    }


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--paper-data-dir", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    runs: list[dict[str, Any]] = []
    for path in sorted(root.rglob("cell_validation.json")):
        try:
            runs.append(parse_cell(path, root))
        except ValueError as error:
            if not args.allow_incomplete:
                raise
            print(f"warning: skipping incomplete cell: {error}", file=sys.stderr)
    if not args.allow_incomplete and len(runs) != 192:
        raise SystemExit(f"expected 192 valid P1 cells, found {len(runs)}")

    # The standard baseline uses hard IRQs only for OTHER.  Its modeled
    # treatments use threadirqs; every RT cell uses the native threaded path.
    for row in runs:
        if row["kernel"] == "standard":
            expected_phase = (
                "standard-hardirq" if row["policy"] == "OTHER" else "standard-threaded"
            )
            if row["phase"] != expected_phase:
                raise SystemExit(f"invalid standard-kernel phase mapping: {row}")

    runs.sort(key=lambda row: (
        row["kernel"], row["workload"], row["noise"], row["policy"], row["round"]
    ))
    write_csv(root / "formal_runs.csv", RUN_FIELDS, runs)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in runs:
        grouped[(row["kernel"], row["workload"], row["noise"], row["policy"])].append(row)
    summary_fields = [
        "kernel", "workload", "noise", "policy", "runs", "p99_ms", "max_ms",
        "deliveries", "problem_framesets", "stale_framesets_ug",
        "gap_bearing_framesets", "duplicate_components", "sequence_gaps",
        "partially_stale_framesets", "fully_stale_framesets", "timeouts",
    ]
    summary: list[dict[str, Any]] = []
    for key, values in sorted(grouped.items()):
        summary.append({
            "kernel": key[0],
            "workload": key[1],
            "noise": key[2],
            "policy": key[3],
            "runs": len(values),
            "p99_ms": statistics.mean(row["p99_ms"] for row in values),
            "max_ms": max(row["max_ms"] for row in values),
            **{
                field: sum(int(row[field]) for row in values)
                for field in summary_fields[7:]
            },
        })
    write_csv(root / "formal_group_summary.csv", summary_fields, summary)

    audit = {
        "run_count": len(runs),
        "group_count": len(summary),
        "complete": len(runs) == 192 and len(summary) == 64,
        "problem_framesets": sum(row["problem_framesets"] for row in runs),
        "timeouts": sum(row["timeouts"] for row in runs),
    }
    (root / "matrix_summary.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if args.paper_data_dir:
        paper_runs = [
            {
                "kernel": row["kernel"],
                "round": row["round"],
                "workload": row["workload"],
                "noise": row["noise"],
                "policy": row["policy"],
                "p99_ms": row["p99_ms"],
                "max_ms": row["max_ms"],
                "problem_framesets": row["problem_framesets"],
                "duplicate_components": row["duplicate_components"],
                "sequence_gaps": row["sequence_gaps"],
            }
            for row in runs
        ]
        paper_summary = [dict(row) for row in summary]
        paper_runs_path = args.paper_data_dir / "main_matrix_runs.csv"
        paper_summary_path = args.paper_data_dir / "main_matrix_summary.csv"
        write_csv(
            paper_runs_path,
            list(paper_runs[0]) if paper_runs else [],
            paper_runs,
        )
        write_csv(
            paper_summary_path,
            summary_fields,
            paper_summary,
        )
        try:
            source_root = str(root.relative_to(Path.cwd().resolve()))
        except ValueError:
            source_root = str(root)
        source_commit = (root / "git_commit.txt").read_text(encoding="utf-8").strip()
        paper_manifest = {
            "schema_version": 1,
            "generated_by": "tools/realsense_steady_bench/summarize_p1_v2.py",
            "source_result_root": source_root,
            "source_git_commit": source_commit,
            "validations": audit,
            "outputs": {
                paper_runs_path.name: {
                    "rows": len(paper_runs),
                    "sha256": sha256(paper_runs_path),
                },
                paper_summary_path.name: {
                    "rows": len(paper_summary),
                    "sha256": sha256(paper_summary_path),
                },
            },
        }
        (args.paper_data_dir / "main_matrix_p1_v2_manifest.json").write_text(
            json.dumps(paper_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(audit, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
