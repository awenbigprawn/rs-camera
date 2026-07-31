"""Attempt selection and retry semantics for steady-state camera runs."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Mapping


AttemptFunction = Callable[[int, Path], tuple[str, Dict[str, Any]]]


def measurement_started(summary: Mapping[str, Any]) -> bool:
    value = summary.get("measurement", {}).get("start_boottime_ns", 0)
    return bool(int(value or 0))


def camera_descriptors(
    case: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    cameras = summary.get("cameras", [])
    if isinstance(cameras, list) and cameras:
        return [dict(camera) for camera in cameras if isinstance(camera, dict)]
    probe = case.get("probe", {})
    serials = probe.get("serials", probe.get("serial", []))
    if isinstance(serials, str):
        serials = [serials] if serials else []
    return [{"serial": str(serial)} for serial in serials]


def _promote_attempt(attempt_dir: Path, record_dir: Path) -> None:
    for child in attempt_dir.iterdir():
        destination = record_dir / child.name
        if destination.exists():
            raise RuntimeError(
                f"Cannot promote {child}; destination already exists: {destination}"
            )
        child.replace(destination)
    attempt_dir.rmdir()


def run_steady_attempts(
    *,
    case: Mapping[str, Any],
    record_dir: Path,
    base_manifest: Mapping[str, Any],
    recover_on_failure: str,
    recovery_settle_seconds: float,
    max_attempts_per_run: int,
    recovery: Any,
    run_attempt: AttemptFunction,
) -> str:
    attempts: List[Dict[str, Any]] = []
    recoveries: List[Dict[str, Any]] = []
    final_output = ""
    final_summary: Dict[str, Any] | None = None
    selected_attempt = 0
    selected_attempt_dir: Path | None = None

    for attempt in range(1, max_attempts_per_run + 1):
        attempt_dir = record_dir / f"attempt-{attempt}"
        output, summary = run_attempt(attempt, attempt_dir)
        final_output = output
        final_summary = summary
        selected_attempt = attempt
        selected_attempt_dir = attempt_dir
        success = bool(summary.get("success", False))
        has_measurement_started = measurement_started(summary)
        startup_failure = not success and not has_measurement_started
        if success:
            failure_phase = "none"
        elif startup_failure:
            failure_phase = "startup"
        else:
            failure_phase = "measurement"
        attempt_record: Dict[str, Any] = {
            "attempt": attempt,
            "success": success,
            "failure_phase": failure_phase,
            "error": str(summary.get("error", "")),
            "measurement_started": has_measurement_started,
            "record_data_dir": str(attempt_dir),
            "cameras": [
                {
                    "serial": camera.get("serial", ""),
                    "warmup_deliveries": camera.get("warmup_deliveries", 0),
                    "timeouts": camera.get("timeouts", 0),
                }
                for camera in summary.get("cameras", [])
            ],
        }

        if not success and recover_on_failure == "full-reset":
            print(
                f"[RECOVERY] attempt {attempt}/{max_attempts_per_run} "
                f"failed during {failure_phase}: {attempt_record['error']}"
            )
            recovery_result = recovery.recover(
                camera_descriptors(case, summary), attempt_dir
            )
            recoveries.append(recovery_result)
            attempt_record["recovery"] = recovery_result

        attempts.append(attempt_record)
        if success:
            break
        if not startup_failure:
            # A measurement-phase failure is an experimental outcome. Reset the
            # cameras for the next point, but do not select a later success.
            break
        if attempt >= max_attempts_per_run:
            break
        print(
            f"[RETRY] repeating the same case/policy as attempt "
            f"{attempt + 1}/{max_attempts_per_run}"
        )
        if recovery_settle_seconds > 0:
            time.sleep(recovery_settle_seconds)

    if final_summary is None or selected_attempt_dir is None:
        raise RuntimeError("No steady-state workload attempt was executed")

    recovery_errors = [
        str(item.get("error", "")) for item in recoveries if item.get("error")
    ]
    aggregate_recovery: Dict[str, Any] = {
        "attempted": bool(recoveries),
        "method": recover_on_failure,
        "count": len(recoveries),
    }
    if recoveries:
        aggregate_recovery["success"] = all(
            bool(item.get("success", False)) for item in recoveries
        )
    if recovery_errors:
        aggregate_recovery["error"] = " | ".join(recovery_errors)

    final_success = bool(final_summary.get("success", False))
    final_summary.update(
        {
            "attempt_count": len(attempts),
            "failed_attempt_count": sum(
                1 for item in attempts if not item["success"]
            ),
            "initial_attempt_success": bool(attempts[0]["success"]),
            "eventual_success": final_success,
            "selected_attempt": selected_attempt,
            "attempts": attempts,
            "recovery": aggregate_recovery,
        }
    )

    _promote_attempt(selected_attempt_dir, record_dir)
    attempts[-1]["record_data_dir"] = str(record_dir)
    (record_dir / "steady_summary.json").write_text(
        json.dumps(final_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (record_dir / "attempts.json").write_text(
        json.dumps(attempts, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (record_dir / "selected_attempt.txt").write_text(
        f"{selected_attempt}\n", encoding="utf-8"
    )
    (record_dir / "probe_stdout.txt").write_text(final_output, encoding="utf-8")

    attempt_manifest_path = record_dir / "attempt_manifest.json"
    selected_manifest = (
        json.loads(attempt_manifest_path.read_text(encoding="utf-8"))
        if attempt_manifest_path.is_file()
        else dict(base_manifest)
    )
    selected_manifest.update(
        {
            "selected_attempt": selected_attempt,
            "attempt_count": len(attempts),
            "eventual_success": final_success,
        }
    )
    (record_dir / "run_manifest.json").write_text(
        json.dumps(selected_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return final_output
