"""Steady-state adapter for shared attempt and retry orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

from realsense_bench_common.attempts import (
    AttemptDecision,
    run_attempt_loop,
)


AttemptFunction = Callable[[int, Path], tuple[str, Dict[str, Any]]]


def record_pre_run_reset_failure(
    *,
    case: Mapping[str, Any],
    attempt: int,
    attempt_dir: Path,
    base_manifest: Mapping[str, Any],
    error: Exception,
) -> tuple[str, Dict[str, Any]]:
    """Record a pre-run reset failure as a retryable startup attempt."""

    attempt_dir.mkdir(parents=True, exist_ok=False)
    message = f"Pre-run reset failed: {type(error).__name__}: {error}"
    cameras = camera_descriptors(case, {})
    summary: Dict[str, Any] = {
        "schema_version": 1,
        "success": False,
        "error": message,
        "run": {"camera_count": len(cameras)},
        "measurement": {
            "start_boottime_ns": 0,
            "end_boottime_ns": 0,
            "duration_ms": 0.0,
        },
        "aggregate": {},
        "cameras": cameras,
    }
    manifest = {
        **base_manifest,
        "attempt": attempt,
        "record_data_dir": str(attempt_dir),
        "pre_run_reset_failed": True,
        "pre_run_reset_error": message,
    }
    (attempt_dir / "steady_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (attempt_dir / "attempt_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = message + "\n"
    (attempt_dir / "probe_stdout.txt").write_text(output, encoding="utf-8")
    return output, summary


def measurement_started(summary: Mapping[str, Any]) -> bool:
    value = summary.get("measurement", {}).get("start_boottime_ns", 0)
    return bool(int(value or 0))


def camera_descriptors(
    case: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    probe = case.get("probe", {})
    serials = probe.get("serials", probe.get("serial", []))
    if isinstance(serials, str):
        serials = [serials] if serials else []
    usb_devices = probe.get("rsusb_usb_devices", [])
    rsusb_ports = {
        str(serial): f"/sys/bus/usb/devices/{usb_device}"
        for serial, usb_device in zip(serials, usb_devices)
    }
    cameras = summary.get("cameras", [])
    if isinstance(cameras, list) and cameras:
        descriptors = [
            dict(camera) for camera in cameras if isinstance(camera, dict)
        ]
        for descriptor in descriptors:
            port = rsusb_ports.get(str(descriptor.get("serial", "")))
            if port:
                descriptor["physical_port"] = port
        return descriptors
    return [
        {
            "serial": str(serial),
            **(
                {"physical_port": rsusb_ports[str(serial)]}
                if str(serial) in rsusb_ports
                else {}
            ),
        }
        for serial in serials
    ]


def _classify_attempt(summary: Mapping[str, Any]) -> AttemptDecision:
    success = bool(summary.get("success", False))
    has_measurement_started = measurement_started(summary)
    error = str(summary.get("error", ""))
    if success:
        phase = "none"
    elif error.startswith((
        "SCHED_DEADLINE setup failed:",
        "Rate-monotonic setup failed:",
    )):
        phase = "scheduler_setup"
    elif error.startswith("Noise setup failed:"):
        phase = "noise_setup"
    elif error.startswith("Noise transition frame failure:"):
        phase = "noise_transition"
    elif has_measurement_started:
        phase = "measurement"
    else:
        phase = "startup"
    retryable_phases = {"startup", "noise_transition", "measurement"}
    return AttemptDecision(
        success=success,
        failure_phase=phase,
        retry=not success and phase in retryable_phases,
        recover=not success and phase in retryable_phases,
        error=error,
        metadata={"measurement_started": has_measurement_started},
    )


def _steady_attempt_record(
    summary: Mapping[str, Any],
    _decision: AttemptDecision,
) -> Mapping[str, Any]:
    return {
        "cameras": [
            {
                "serial": camera.get("serial", ""),
                "warmup_deliveries": camera.get("warmup_deliveries", 0),
                "timeouts": camera.get("timeouts", 0),
            }
            for camera in summary.get("cameras", [])
        ],
    }


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
    def recover_attempt(
        summary: Mapping[str, Any],
        attempt_dir: Path,
        _decision: AttemptDecision,
    ) -> Dict[str, Any]:
        return recovery.recover(camera_descriptors(case, summary), attempt_dir)

    result = run_attempt_loop(
        record_dir=record_dir,
        max_attempts=max_attempts_per_run,
        recovery_method=recover_on_failure,
        recovery_settle_seconds=recovery_settle_seconds,
        run_attempt=run_attempt,
        classify_attempt=_classify_attempt,
        recover_attempt=(
            recover_attempt if recover_on_failure == "full-reset" else None
        ),
        build_attempt_record=_steady_attempt_record,
    )

    (record_dir / "steady_summary.json").write_text(
        json.dumps(result.summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (record_dir / "attempts.json").write_text(
        json.dumps(result.attempts, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (record_dir / "selected_attempt.txt").write_text(
        f"{result.selected_attempt}\n", encoding="utf-8"
    )
    (record_dir / "probe_stdout.txt").write_text(result.output, encoding="utf-8")

    attempt_manifest_path = (
        result.selected_attempt_dir / "attempt_manifest.json"
    )
    selected_manifest = (
        json.loads(attempt_manifest_path.read_text(encoding="utf-8"))
        if attempt_manifest_path.is_file()
        else dict(base_manifest)
    )
    selected_manifest.update(
        {
            "selected_attempt": result.selected_attempt,
            "attempt_count": len(result.attempts),
            "eventual_success": result.summary["eventual_success"],
        }
    )
    (record_dir / "run_manifest.json").write_text(
        json.dumps(selected_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result.output
