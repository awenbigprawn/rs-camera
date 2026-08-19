#!/usr/bin/env python3
"""Compare steady worker-family and activation-period models across policies."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from generate_deadline_profile import _trace_threads  # noqa: E402


POLICY_ORDER = {"other": 0, "rr-rm": 1, "fifo-rm": 2, "deadline": 3}
STABLE_BASELINE_TOLERANCE_PCT = 5.0
POLICY_PERIOD_TOLERANCE_PCT = 5.0


def selected_runs(root: Path) -> dict[tuple[str, str], list[Path]]:
    runs: dict[tuple[str, str], list[Path]] = defaultdict(list)
    cases_root = root / "cases"
    for selection in cases_root.glob("*/*/results/**/selected_attempt.txt"):
        relative = selection.relative_to(cases_root)
        case_id, policy = relative.parts[:2]
        attempt_number = int(selection.read_text(encoding="utf-8").strip())
        attempt = selection.parent / f"attempt-{attempt_number}"
        if not (attempt / "thread_steady_summary.json").is_file():
            raise ValueError(f"selected attempt lacks LiME summary: {attempt}")
        if not (attempt / "steady_summary.json").is_file():
            raise ValueError(f"selected attempt lacks probe summary: {attempt}")
        runs[(case_id, policy)].append(attempt)
    for attempts in runs.values():
        attempts.sort()
    return runs


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def child_threads(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        thread
        for thread in summary.get("threads", [])
        if thread.get("signature") != "process-main"
    ]


def multiplicity(threads: Iterable[dict[str, Any]]) -> Counter[str]:
    return Counter(str(thread["signature"]) for thread in threads)


def median(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def p99_median(threads: Iterable[dict[str, Any]], field: str) -> float | None:
    values = []
    for thread in threads:
        distribution = thread.get(field) or {}
        if int(distribution.get("n") or 0) > 0:
            values.append(float(distribution.get("p99") or 0.0))
    return median(values)


def freshness(probe: dict[str, Any]) -> tuple[int, int, int, int, int]:
    aggregate = probe.get("aggregate") or {}
    return (
        int(aggregate.get("duplicate_frames") or 0),
        int(aggregate.get("sequence_gaps") or 0),
        int(aggregate.get("partially_stale_framesets") or 0),
        int(aggregate.get("stale_framesets") or 0),
        int(aggregate.get("timeouts") or 0),
    )


def analyze(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempts_by_cell = selected_runs(root)
    cases = sorted({case_id for case_id, _ in attempts_by_cell})
    rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []

    for case_id in cases:
        baseline_attempts = attempts_by_cell.get((case_id, "other"), [])
        if not baseline_attempts:
            raise ValueError(f"case has no SCHED_OTHER validation runs: {case_id}")

        baseline_summaries = [
            read_json(path / "thread_steady_summary.json")
            for path in baseline_attempts
        ]
        baseline_threads = [child_threads(summary) for summary in baseline_summaries]
        baseline_models = [
            _trace_threads(path, require_sched_other=False)[0]
            for path in baseline_attempts
        ]
        baseline_multiplicity = multiplicity(baseline_threads[0])
        if any(multiplicity(threads) != baseline_multiplicity for threads in baseline_threads):
            raise ValueError(
                f"SCHED_OTHER worker-family multiplicity changed across runs: {case_id}"
            )
        baseline_identities = set(baseline_models[0])
        if any(set(models) != baseline_identities for models in baseline_models):
            raise ValueError(
                f"SCHED_OTHER worker identities changed across runs: {case_id}"
            )

        baseline_periods: dict[tuple[str, int], list[float]] = defaultdict(list)
        for models in baseline_models:
            for identity, model in models.items():
                baseline_periods[identity].append(
                    float(model["principal_period_ns"]) / 1e6
                )
        baseline_period_median = {
            identity: statistics.median(values)
            for identity, values in baseline_periods.items()
            if values
        }
        stable_baseline_identities = {
            identity
            for identity, reference in baseline_period_median.items()
            if len(baseline_periods[identity]) == len(baseline_models)
            and max(
                abs(observed - reference) / reference * 100.0
                for observed in baseline_periods[identity]
            )
            <= STABLE_BASELINE_TOLERANCE_PCT
        }

        cells = sorted(
            (
                (policy, attempts)
                for (candidate_case, policy), attempts in attempts_by_cell.items()
                if candidate_case == case_id
            ),
            key=lambda item: POLICY_ORDER.get(item[0], 99),
        )
        for policy, attempts in cells:
            successful_runs = 0
            matching_runs = 0
            period_deltas: list[float] = []
            comparable_family_runs = 0
            ready_p99: list[float] = []
            execution_p99: list[float] = []
            duplicates = gaps = partial_stale = stale = timeouts = 0

            for attempt in attempts:
                lime = read_json(attempt / "thread_steady_summary.json")
                probe = read_json(attempt / "steady_summary.json")
                successful_runs += int(bool(probe.get("success")))
                threads = child_threads(lime)
                matching_runs += int(multiplicity(threads) == baseline_multiplicity)

                models, _ = _trace_threads(attempt, require_sched_other=False)
                if set(models) != baseline_identities:
                    matching_runs -= int(multiplicity(threads) == baseline_multiplicity)
                for identity in stable_baseline_identities:
                    reference = baseline_period_median[identity]
                    model = models.get(identity)
                    if model is None:
                        continue
                    comparable_family_runs += 1
                    observed = float(model["principal_period_ns"]) / 1e6
                    delta = abs(observed - reference) / reference * 100.0
                    period_deltas.append(delta)
                    identity_rows.append(
                        {
                            "case_id": case_id,
                            "policy": policy,
                            "logical_run": str(attempt.parent),
                            "signature": identity[0],
                            "instance": identity[1],
                            "name": model["name"],
                            "other_period_median_ms": reference,
                            "observed_period_ms": observed,
                            "absolute_delta_pct": delta,
                            "within_5pct": delta <= POLICY_PERIOD_TOLERANCE_PCT,
                        }
                    )

                value = p99_median(threads, "ready_per_activation_ms")
                if value is not None:
                    ready_p99.append(value)
                value = p99_median(threads, "execution_ms")
                if value is not None:
                    execution_p99.append(value)
                (
                    run_duplicates,
                    run_gaps,
                    run_partial_stale,
                    run_stale,
                    run_timeouts,
                ) = freshness(probe)
                duplicates += run_duplicates
                gaps += run_gaps
                partial_stale += run_partial_stale
                stale += run_stale
                timeouts += run_timeouts

            maximum_period_delta = max(period_deltas) if period_deltas else None
            row = {
                "case_id": case_id,
                "policy": policy,
                "runs": len(attempts),
                "successful_runs": successful_runs,
                "baseline_child_threads": sum(baseline_multiplicity.values()),
                "baseline_worker_families": len(baseline_multiplicity),
                "stable_period_identities": len(stable_baseline_identities),
                "nonstable_period_identities": (
                    len(baseline_identities) - len(stable_baseline_identities)
                ),
                "family_multiplicity_match_runs": matching_runs,
                "comparable_stable_family_runs": comparable_family_runs,
                "period_abs_delta_pct_median": median(period_deltas),
                "period_abs_delta_pct_max": maximum_period_delta,
                "ready_p99_ms_thread_median": median(ready_p99),
                "execution_p99_ms_thread_median": median(execution_p99),
                "duplicate_frames": duplicates,
                "sequence_gaps": gaps,
                "partially_stale_framesets": partial_stale,
                "stale_framesets": stale,
                "timeouts": timeouts,
                "structural_model_consistent": matching_runs == len(attempts),
                "stable_period_model_within_5pct": (
                    maximum_period_delta is not None
                    and maximum_period_delta <= POLICY_PERIOD_TOLERANCE_PCT
                ),
            }
            rows.append(row)
    return rows, identity_rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def format_value(value: Any, digits: int = 3) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_markdown(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Scheduling-policy time-model validation",
        "",
        (
            "A worker family is identified by its normalized creator-stack "
            "signature and instance number. The structural check requires the "
            "same identity set in every run. The temporal check applies the "
            "same burst-to-logical-job reconstruction used to generate the "
            "scheduler profiles. An identity is classified as stable only if "
            "all three OTHER estimates remain within 5% of their median. "
            "Policy runs are compared with that median using the same 5% "
            "descriptive tolerance. Event-driven and self-suspending "
            "identities remain in the structural check but not the periodic "
            "check. These thresholds are not schedulability bounds."
        ),
        "",
        (
            "The model does not require execution or ready delay to remain "
            "unchanged. Those quantities are scheduler-dependent and are "
            "reported separately."
        ),
        "",
        "| Case | Policy | Success | Family match | Stable IDs | Stable period median/max delta | Ready p99 | Exec. p99 | Dup./gap/partial/stale/timeout |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {case_id} | {policy} | {successful_runs}/{runs} | "
            "{family_multiplicity_match_runs}/{runs} | "
            "{stable_period_identities} | {period_median}% / "
            "{period_max}% | {ready} ms | {execution} ms | "
            "{duplicate_frames}/{sequence_gaps}/{partially_stale_framesets}/"
            "{stale_framesets}/{timeouts} |".format(
                **row,
                period_median=format_value(row["period_abs_delta_pct_median"]),
                period_max=format_value(row["period_abs_delta_pct_max"]),
                ready=format_value(row["ready_p99_ms_thread_median"]),
                execution=format_value(row["execution_p99_ms_thread_median"]),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()

    rows, identity_rows = analyze(args.results_root.resolve())
    if not rows:
        raise SystemExit("no selected policy-validation runs were found")
    prefix = args.output_prefix.resolve()
    write_csv(prefix.with_suffix(".csv"), rows)
    write_csv(prefix.with_name(prefix.name + "_identities").with_suffix(".csv"), identity_rows)
    write_markdown(prefix.with_suffix(".md"), rows)
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
