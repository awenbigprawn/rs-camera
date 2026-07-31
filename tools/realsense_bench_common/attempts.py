"""Reusable attempt, recovery, and retry orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Mapping


AttemptFunction = Callable[[int, Path], tuple[str, Dict[str, Any]]]
AttemptClassifier = Callable[[Mapping[str, Any]], "AttemptDecision"]
BeforeAttempt = Callable[[int, Path], None]
RecoveryFunction = Callable[
    [Mapping[str, Any], Path, "AttemptDecision"], Dict[str, Any]
]
AttemptRecordBuilder = Callable[
    [Mapping[str, Any], "AttemptDecision"], Mapping[str, Any]
]


@dataclass(frozen=True)
class AttemptDecision:
    """Benchmark-specific interpretation of one attempt summary."""

    success: bool
    failure_phase: str
    retry: bool
    recover: bool = True
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AttemptLoopResult:
    """Selected attempt plus common retry and recovery metadata."""

    output: str
    summary: Dict[str, Any]
    selected_attempt: int
    selected_attempt_dir: Path
    attempts: List[Dict[str, Any]]
    recoveries: List[Dict[str, Any]]
    recovery: Dict[str, Any]


def aggregate_recoveries(
    recovery_method: str,
    recoveries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    errors = [
        str(item.get("error", ""))
        for item in recoveries
        if item.get("error")
    ]
    aggregate: Dict[str, Any] = {
        "attempted": bool(recoveries),
        "method": recovery_method,
        "count": len(recoveries),
    }
    if recoveries:
        aggregate["success"] = all(
            bool(item.get("success", False)) for item in recoveries
        )
    if errors:
        aggregate["error"] = " | ".join(errors)
    return aggregate


def run_attempt_loop(
    *,
    record_dir: Path,
    max_attempts: int,
    recovery_method: str,
    recovery_settle_seconds: float,
    run_attempt: AttemptFunction,
    classify_attempt: AttemptClassifier,
    recover_attempt: RecoveryFunction | None = None,
    before_attempt: BeforeAttempt | None = None,
    build_attempt_record: AttemptRecordBuilder | None = None,
) -> AttemptLoopResult:
    """Run one logical benchmark point, retrying only when its adapter asks."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    attempts: List[Dict[str, Any]] = []
    recoveries: List[Dict[str, Any]] = []
    final_output = ""
    final_summary: Dict[str, Any] | None = None
    final_decision: AttemptDecision | None = None
    selected_attempt = 0
    selected_attempt_dir: Path | None = None

    for attempt in range(1, max_attempts + 1):
        attempt_dir = record_dir / f"attempt-{attempt}"
        if before_attempt is not None:
            before_attempt(attempt, attempt_dir)
        output, summary = run_attempt(attempt, attempt_dir)
        decision = classify_attempt(summary)
        final_output = output
        final_summary = summary
        final_decision = decision
        selected_attempt = attempt
        selected_attempt_dir = attempt_dir

        attempt_record: Dict[str, Any] = {
            "attempt": attempt,
            "success": decision.success,
            "failure_phase": decision.failure_phase,
            "error": decision.error,
            "record_data_dir": str(attempt_dir),
            **dict(decision.metadata),
        }
        if build_attempt_record is not None:
            attempt_record.update(build_attempt_record(summary, decision))

        if (
            not decision.success
            and decision.recover
            and recovery_method != "none"
            and recover_attempt is not None
        ):
            print(
                f"[RECOVERY] attempt {attempt}/{max_attempts} failed during "
                f"{decision.failure_phase}: {decision.error or 'workload failed'}"
            )
            recovery = recover_attempt(summary, attempt_dir, decision)
            recoveries.append(recovery)
            attempt_record["recovery"] = recovery

        attempts.append(attempt_record)
        retry = not decision.success and decision.retry and attempt < max_attempts
        if not retry:
            break
        print(
            f"[RETRY] repeating the same benchmark point as attempt "
            f"{attempt + 1}/{max_attempts}"
        )
        if recovery_settle_seconds > 0:
            time.sleep(recovery_settle_seconds)

    if (
        final_summary is None
        or final_decision is None
        or selected_attempt_dir is None
    ):
        raise RuntimeError("No benchmark attempt was executed")

    recovery = aggregate_recoveries(recovery_method, recoveries)
    final_summary.update(
        {
            "attempt_count": len(attempts),
            "failed_attempt_count": sum(
                1 for item in attempts if not item["success"]
            ),
            "initial_attempt_success": bool(attempts[0]["success"]),
            "eventual_success": final_decision.success,
            "selected_attempt": selected_attempt,
            "attempts": attempts,
            "recovery": recovery,
        }
    )
    return AttemptLoopResult(
        output=final_output,
        summary=final_summary,
        selected_attempt=selected_attempt,
        selected_attempt_dir=selected_attempt_dir,
        attempts=attempts,
        recoveries=recoveries,
        recovery=recovery,
    )
