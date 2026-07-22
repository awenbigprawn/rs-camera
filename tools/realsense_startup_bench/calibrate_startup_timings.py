#!/usr/bin/env python3
"""Calibrate D435 startup timeouts without LiME tracing overhead."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time
from typing import Any, Dict, Iterable, List, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "realsense_startup_bench"
DEFAULT_BUILD_DIR = REPO_ROOT / "build-realsense-thread-trace"
DEFAULT_RESULTS_DIR = TOOL_DIR / "results" / "timing_calibration"

STAGE_SETTLE = "recovery_settle_ms"
STAGE_FRAME = "frame_timeout_ms"
STAGE_JOIN = "join_timeout_ms"
STAGE_CYCLE = "cycle_delay_ms"
STAGE_VALIDATION = "combined_validation"

RESULT_FIELDS = [
    "stage",
    "candidate_ms",
    "trial",
    "success",
    "returncode",
    "elapsed_ms",
    "cycles_requested",
    "cycles_completed",
    "cycles_observed",
    "start_call_ms_mean",
    "start_call_ms_max",
    "first_frame_ms_mean",
    "first_frame_ms_max",
    "first_frame_wait_ms_mean",
    "first_frame_wait_ms_max",
    "stop_call_ms_mean",
    "stop_call_ms_max",
    "join_wait_ms_mean",
    "join_wait_ms_max",
    "cycle_ms_mean",
    "cycle_ms_max",
    "error_kind",
    "error_message",
    "reset_success",
    "hardware_reset_ms",
    "usb_reset_ms",
    "enumeration_ms",
    "settle_ms",
    "frame_timeout_ms",
    "join_timeout_ms",
    "cycle_delay_ms",
    "stdout_file",
    "reset_file",
]

CYCLE_FIELDS = [
    "stage",
    "candidate_ms",
    "trial",
    "cycle",
    "success",
    "framesets",
    "start_call_ms",
    "first_frame_ms",
    "first_frame_wait_ms",
    "stop_call_ms",
    "join_wait_ms",
    "cycle_ms",
    "threads_after_start",
    "extra_threads_after_join",
]

SUMMARY_FIELDS = [
    "stage",
    "candidate_ms",
    "trials",
    "successful_trials",
    "cycles_requested",
    "cycles_completed",
    "observed_success",
    "headroom_required_ms",
    "qualified",
    "first_frame_wait_ms_max",
    "join_wait_ms_max",
    "elapsed_ms_total",
]


def prefixed_json(output: str, prefix: str) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            values.append(json.loads(line[len(prefix) :]))
        except json.JSONDecodeError:
            continue
    return values


def metric(cycles: Sequence[Dict[str, Any]], field: str, operation: str) -> float:
    values = [float(cycle[field]) for cycle in cycles if field in cycle]
    if not values:
        return 0.0
    if operation == "mean":
        return statistics.fmean(values)
    if operation == "max":
        return max(values)
    raise ValueError(operation)


def parse_probe_output(
    output: str,
    returncode: int,
    requested_cycles: int,
) -> Dict[str, Any]:
    cycles = prefixed_json(output, "RS_STARTUP_CYCLE ")
    results = prefixed_json(output, "RS_STARTUP_RESULT ")
    errors = prefixed_json(output, "RS_STARTUP_ERROR ")
    result = results[-1] if results else {}
    error = errors[-1] if errors else {}
    completed = int(result.get("completed_cycles", len(cycles)))
    success = bool(
        returncode == 0
        and result.get("success", False)
        and completed == requested_cycles
        and len(cycles) == requested_cycles
        and all(bool(cycle.get("success", False)) for cycle in cycles)
    )
    parsed: Dict[str, Any] = {
        "success": success,
        "returncode": returncode,
        "cycles_requested": requested_cycles,
        "cycles_completed": completed,
        "cycles_observed": len(cycles),
        "error_kind": error.get("kind", ""),
        "error_message": error.get("message", ""),
        "cycles": cycles,
    }
    for field in (
        "start_call_ms",
        "first_frame_ms",
        "first_frame_wait_ms",
        "stop_call_ms",
        "join_wait_ms",
        "cycle_ms",
    ):
        parsed[f"{field}_mean"] = metric(cycles, field, "mean")
        parsed[f"{field}_max"] = metric(cycles, field, "max")
    return parsed


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def unique_candidates(values: Sequence[int], allow_zero: bool) -> List[int]:
    result = sorted(set(values))
    if not result:
        raise ValueError("candidate list cannot be empty")
    minimum = 0 if allow_zero else 1
    if any(value < minimum for value in result):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"candidate values must be {qualifier}")
    return result


def usb_device_from_physical_port(physical_port: str) -> Path | None:
    if not physical_port.startswith("/sys/"):
        return None
    path = Path(physical_port).resolve()
    for candidate in (path, *path.parents):
        if (candidate / "busnum").is_file() and (candidate / "devnum").is_file():
            return candidate
    return None


def sudo_command(command: List[str], use_sudo: bool, preserve_library_path: bool = False) -> List[str]:
    if not use_sudo:
        return command
    result = ["sudo", "--non-interactive"]
    if preserve_library_path:
        result.append("--preserve-env=LD_LIBRARY_PATH")
    return [*result, *command]


def query_device(probe: Path, serial: str, timeout_seconds: float = 5.0) -> tuple[Dict[str, Any], str]:
    command = [str(probe), "--list-only"]
    if serial:
        command += ["--serial", serial]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {}, str(error)
    output = completed.stdout + completed.stderr
    devices = prefixed_json(output, "RS_DEVICE ")
    if completed.returncode == 0 and devices:
        return devices[-1], output.strip()
    return {}, output.strip() or f"device query exited with {completed.returncode}"


def full_reset(
    probe: Path,
    serial: str,
    device: Dict[str, Any],
    use_sudo: bool,
    reset_timeout_ms: int,
    enumeration_timeout_ms: int,
    settle_ms: int,
) -> Dict[str, Any]:
    started = time.monotonic()
    result: Dict[str, Any] = {
        "method": "firmware-and-composite-usb",
        "success": False,
        "settle_ms": settle_ms,
        "hardware_reset": {"success": False},
        "usb_reset": {"success": False},
        "enumeration": {"success": False},
    }

    hardware_command = [
        str(probe),
        "--hardware-reset",
        "--reset-timeout-ms",
        str(reset_timeout_ms),
    ]
    if serial:
        hardware_command += ["--serial", serial]
    hardware_command = sudo_command(
        hardware_command,
        use_sudo=use_sudo,
        preserve_library_path=True,
    )
    hardware_started = time.monotonic()
    try:
        completed = subprocess.run(
            hardware_command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=reset_timeout_ms / 1000.0 + 15.0,
            check=False,
        )
        hardware_output = (completed.stdout + completed.stderr).strip()
        hardware_success = (
            completed.returncode == 0
            and '"state":"complete"' in hardware_output
        )
        result["hardware_reset"] = {
            "success": hardware_success,
            "returncode": completed.returncode,
            "elapsed_ms": (time.monotonic() - hardware_started) * 1000.0,
            "command": hardware_command,
            "output": hardware_output,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        result["hardware_reset"] = {
            "success": False,
            "elapsed_ms": (time.monotonic() - hardware_started) * 1000.0,
            "command": hardware_command,
            "error": str(error),
        }

    physical_port = str(device.get("physical_port", ""))
    usb_device = usb_device_from_physical_port(physical_port)
    if usb_device is None:
        result["usb_reset"]["error"] = "could not resolve composite USB parent"
    else:
        try:
            bus = int((usb_device / "busnum").read_text(encoding="utf-8").strip())
            device_number = int(
                (usb_device / "devnum").read_text(encoding="utf-8").strip()
            )
            target = f"{bus:03d}/{device_number:03d}"
            usb_command = sudo_command(["usbreset", target], use_sudo)
            usb_started = time.monotonic()
            completed = subprocess.run(
                usb_command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            result["usb_reset"] = {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "elapsed_ms": (time.monotonic() - usb_started) * 1000.0,
                "command": usb_command,
                "target": target,
                "usb_device": str(usb_device),
                "output": (completed.stdout + completed.stderr).strip(),
            }
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result["usb_reset"] = {"success": False, "error": str(error)}

    enumeration_started = time.monotonic()
    enumeration_deadline = enumeration_started + enumeration_timeout_ms / 1000.0
    last_output = ""
    enumerated_device: Dict[str, Any] = {}
    while time.monotonic() < enumeration_deadline:
        enumerated_device, last_output = query_device(probe, serial, timeout_seconds=2.0)
        if enumerated_device:
            break
        time.sleep(0.1)
    result["enumeration"] = {
        "success": bool(enumerated_device),
        "elapsed_ms": (time.monotonic() - enumeration_started) * 1000.0,
        "device": enumerated_device,
        "last_output": last_output,
    }

    result["success"] = bool(
        result["hardware_reset"].get("success", False)
        and result["usb_reset"].get("success", False)
        and result["enumeration"].get("success", False)
    )
    if result["success"] and settle_ms:
        time.sleep(settle_ms / 1000.0)
    result["elapsed_ms"] = (time.monotonic() - started) * 1000.0
    return result


def run_probe(
    probe: Path,
    serial: str,
    frames: int,
    cycles: int,
    frame_timeout_ms: int,
    join_timeout_ms: int,
    cycle_delay_ms: int,
) -> tuple[str, Dict[str, Any]]:
    command = [
        "chrt",
        "--other",
        "0",
        str(probe),
        "--cycles",
        str(cycles),
        "--frames",
        str(frames),
        "--frame-timeout-ms",
        str(frame_timeout_ms),
        "--join-timeout-ms",
        str(join_timeout_ms),
        "--cycle-delay-ms",
        str(cycle_delay_ms),
        "--strict-streams",
    ]
    if serial:
        command += ["--serial", serial]

    frame_seconds = math.ceil(frame_timeout_ms / 1000.0)
    join_seconds = math.ceil(join_timeout_ms / 1000.0)
    delay_seconds = math.ceil(max(0, cycles - 1) * cycle_delay_ms / 1000.0)
    timeout_seconds = max(
        30,
        cycles * (frame_seconds + join_seconds + 5) + delay_seconds,
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        output = completed.stdout + completed.stderr
        parsed = parse_probe_output(output, completed.returncode, cycles)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
        output = stdout + stderr
        parsed = parse_probe_output(output, 124, cycles)
        parsed["error_kind"] = "outer-timeout"
        parsed["error_message"] = f"probe exceeded {timeout_seconds}s"
    parsed["elapsed_ms"] = (time.monotonic() - started) * 1000.0
    parsed["command"] = command
    return output, parsed


def summarize_candidates(
    stage: str,
    candidates: Sequence[int],
    rows: Sequence[Dict[str, Any]],
    trials: int,
    cycles_per_trial: int,
    safety_factor: float,
    join_poll_ms: int = 5,
    headroom_rows: Sequence[Dict[str, Any]] | None = None,
) -> tuple[List[Dict[str, Any]], int | None]:
    summaries: List[Dict[str, Any]] = []
    for candidate in candidates:
        selected = [
            row
            for row in rows
            if row["stage"] == stage and int(row["candidate_ms"]) == candidate
        ]
        successful_trials = sum(bool(row["success"]) for row in selected)
        cycles_completed = sum(int(row["cycles_completed"]) for row in selected)
        observed_success = bool(
            len(selected) == trials
            and successful_trials == trials
            and cycles_completed == trials * cycles_per_trial
        )
        first_wait_max = max(
            (float(row["first_frame_wait_ms_max"]) for row in selected),
            default=0.0,
        )
        join_wait_max = max(
            (float(row["join_wait_ms_max"]) for row in selected),
            default=0.0,
        )
        headroom_source = selected if headroom_rows is None else headroom_rows
        headroom_first_wait_max = max(
            (float(row["first_frame_wait_ms_max"]) for row in headroom_source),
            default=0.0,
        )
        headroom_join_wait_max = max(
            (float(row["join_wait_ms_max"]) for row in headroom_source),
            default=0.0,
        )
        headroom_required = 0
        if stage == STAGE_FRAME:
            headroom_required = math.ceil(
                headroom_first_wait_max * safety_factor
            )
        elif stage == STAGE_JOIN:
            headroom_required = math.ceil(
                headroom_join_wait_max * safety_factor + join_poll_ms
            )
        qualified = observed_success and candidate >= headroom_required
        summaries.append(
            {
                "stage": stage,
                "candidate_ms": candidate,
                "trials": len(selected),
                "successful_trials": successful_trials,
                "cycles_requested": trials * cycles_per_trial,
                "cycles_completed": cycles_completed,
                "observed_success": observed_success,
                "headroom_required_ms": headroom_required,
                "qualified": qualified,
                "first_frame_wait_ms_max": first_wait_max,
                "join_wait_ms_max": join_wait_max,
                "elapsed_ms_total": sum(float(row["elapsed_ms"]) for row in selected),
            }
        )
    qualified_values = [
        int(summary["candidate_ms"])
        for summary in summaries
        if summary["qualified"]
    ]
    return summaries, min(qualified_values) if qualified_values else None


def rounded_timeout(observed_ms: float, safety_factor: float, floor_ms: int) -> int:
    value = max(floor_ms, math.ceil(observed_ms * safety_factor + 1000.0))
    return int(math.ceil(value / 100.0) * 100)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Repeatedly start all four D435 streams to find conservative lower "
            "bounds for startup benchmark timeouts and waits."
        )
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--cycles-per-trial", type=int, default=10)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--validation-trials", type=int, default=3)
    parser.add_argument("--safety-factor", type=float, default=1.5)
    parser.add_argument(
        "--settle-candidates-ms",
        nargs="+",
        type=int,
        default=[0, 1000, 2500, 5000, 10000],
    )
    parser.add_argument(
        "--frame-timeout-candidates-ms",
        nargs="+",
        type=int,
        default=[750, 1000, 1500, 3000],
    )
    parser.add_argument(
        "--join-timeout-candidates-ms",
        nargs="+",
        type=int,
        default=[0, 5, 10, 25, 100],
    )
    parser.add_argument(
        "--cycle-delay-candidates-ms",
        nargs="+",
        type=int,
        default=[0, 10, 50, 100, 500],
    )
    parser.add_argument("--reset-timeout-ms", type=int, default=5000)
    parser.add_argument("--enumeration-timeout-ms", type=int, default=1200)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-sudo", action="store_true")
    args = parser.parse_args()

    if args.frames < 1 or args.cycles_per_trial < 1:
        parser.error("--frames and --cycles-per-trial must be positive")
    if args.trials < 1 or args.validation_trials < 1:
        parser.error("--trials and --validation-trials must be positive")
    if args.safety_factor < 1.0:
        parser.error("--safety-factor must be at least 1")
    if args.reset_timeout_ms < 1 or args.enumeration_timeout_ms < 1:
        parser.error("reset and enumeration timeouts must be positive")

    try:
        settle_candidates = unique_candidates(args.settle_candidates_ms, True)
        frame_candidates = unique_candidates(
            args.frame_timeout_candidates_ms, False
        )
        join_candidates = unique_candidates(args.join_timeout_candidates_ms, True)
        cycle_candidates = unique_candidates(args.cycle_delay_candidates_ms, True)
    except ValueError as error:
        parser.error(str(error))

    use_sudo = os.geteuid() != 0 and not args.no_sudo
    if shutil.which("chrt") is None:
        raise SystemExit("chrt is required")
    if shutil.which("usbreset") is None:
        raise SystemExit("usbreset is required")
    if use_sudo:
        sudo_check = subprocess.run(
            ["sudo", "--non-interactive", "-v"],
            capture_output=True,
            text=True,
            check=False,
        )
        if sudo_check.returncode != 0:
            raise SystemExit("Run 'sudo -v' immediately before calibration.")

    build_dir = args.build_dir.resolve()
    probe = build_dir / "d435_sensor_probe"
    if not args.skip_build:
        subprocess.check_call(
            [
                "cmake",
                "-S",
                str(REPO_ROOT),
                "-B",
                str(build_dir),
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            ]
        )
        subprocess.check_call(
            [
                "cmake",
                "--build",
                str(build_dir),
                "--target",
                "d435_sensor_probe",
                "-j4",
            ]
        )
    if not probe.is_file():
        raise SystemExit(f"probe not found: {probe}")

    device, query_output = query_device(probe, args.serial)
    if not device:
        raise SystemExit(f"camera preflight failed: {query_output}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (args.results_dir / f"calibration_{stamp}").resolve()
    root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 2,
        "serial": args.serial,
        "device": device,
        "frames": args.frames,
        "cycles_per_trial": args.cycles_per_trial,
        "trials": args.trials,
        "validation_trials": args.validation_trials,
        "safety_factor": args.safety_factor,
        "settle_candidates_ms": settle_candidates,
        "frame_timeout_candidates_ms": frame_candidates,
        "join_timeout_candidates_ms": join_candidates,
        "cycle_delay_candidates_ms": cycle_candidates,
        "reset_timeout_ms": args.reset_timeout_ms,
        "enumeration_timeout_ms": args.enumeration_timeout_ms,
        "probe": str(probe),
        "use_sudo": use_sudo,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result_rows: List[Dict[str, Any]] = []
    cycle_rows: List[Dict[str, Any]] = []
    reset_records: List[Dict[str, Any]] = []

    safe_settings = {
        "frame_timeout_ms": max(3000, max(frame_candidates)),
        "join_timeout_ms": max(100, max(join_candidates)),
        "cycle_delay_ms": max(cycle_candidates),
    }

    def execute_stage(
        stage: str,
        candidates: Sequence[int],
        settings: Dict[str, int],
        default_settle_ms: int,
        trials: int,
    ) -> tuple[List[Dict[str, Any]], int | None]:
        nonlocal device
        print(
            f"[CALIBRATE] stage={stage} candidates={list(candidates)} "
            f"trials={trials} cycles/trial={args.cycles_per_trial}"
        )
        for candidate in candidates:
            for trial in range(1, trials + 1):
                settle_ms = (
                    candidate if stage == STAGE_SETTLE else default_settle_ms
                )
                trial_dir = (
                    root / stage / f"candidate-{candidate}" / f"trial-{trial}"
                )
                trial_dir.mkdir(parents=True, exist_ok=False)
                print(
                    f"[CALIBRATE] {stage}={candidate}ms "
                    f"trial={trial}/{trials}: full reset"
                )
                reset = full_reset(
                    probe=probe,
                    serial=args.serial,
                    device=device,
                    use_sudo=use_sudo,
                    reset_timeout_ms=args.reset_timeout_ms,
                    enumeration_timeout_ms=args.enumeration_timeout_ms,
                    settle_ms=settle_ms,
                )
                reset_path = trial_dir / "reset.json"
                reset_path.write_text(
                    json.dumps(reset, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                reset_records.append(reset)
                enumerated = reset.get("enumeration", {}).get("device", {})
                if enumerated:
                    device = enumerated

                current = dict(settings)
                if stage == STAGE_FRAME:
                    current["frame_timeout_ms"] = candidate
                elif stage == STAGE_JOIN:
                    current["join_timeout_ms"] = candidate
                elif stage == STAGE_CYCLE:
                    current["cycle_delay_ms"] = candidate

                output = ""
                parsed: Dict[str, Any]
                if reset.get("success", False):
                    output, parsed = run_probe(
                        probe=probe,
                        serial=args.serial,
                        frames=args.frames,
                        cycles=args.cycles_per_trial,
                        frame_timeout_ms=current["frame_timeout_ms"],
                        join_timeout_ms=current["join_timeout_ms"],
                        cycle_delay_ms=current["cycle_delay_ms"],
                    )
                else:
                    parsed = {
                        "success": False,
                        "returncode": -1,
                        "elapsed_ms": 0.0,
                        "cycles_requested": args.cycles_per_trial,
                        "cycles_completed": 0,
                        "cycles_observed": 0,
                        "error_kind": "recovery",
                        "error_message": "full reset failed before candidate",
                        "cycles": [],
                    }
                    for metric_name in (
                        "start_call_ms",
                        "first_frame_ms",
                        "first_frame_wait_ms",
                        "stop_call_ms",
                        "join_wait_ms",
                        "cycle_ms",
                    ):
                        parsed[f"{metric_name}_mean"] = 0.0
                        parsed[f"{metric_name}_max"] = 0.0

                stdout_path = trial_dir / "probe_stdout.txt"
                stdout_path.write_text(output, encoding="utf-8")
                row: Dict[str, Any] = {
                    "stage": stage,
                    "candidate_ms": candidate,
                    "trial": trial,
                    **parsed,
                    "reset_success": reset.get("success", False),
                    "hardware_reset_ms": reset.get("hardware_reset", {}).get(
                        "elapsed_ms", 0.0
                    ),
                    "usb_reset_ms": reset.get("usb_reset", {}).get(
                        "elapsed_ms", 0.0
                    ),
                    "enumeration_ms": reset.get("enumeration", {}).get(
                        "elapsed_ms", 0.0
                    ),
                    "settle_ms": settle_ms,
                    **current,
                    "stdout_file": str(stdout_path),
                    "reset_file": str(reset_path),
                }
                result_rows.append(row)
                for cycle in parsed.get("cycles", []):
                    cycle_rows.append(
                        {
                            "stage": stage,
                            "candidate_ms": candidate,
                            "trial": trial,
                            **cycle,
                        }
                    )
                print(
                    f"[CALIBRATE] result success={row['success']} "
                    f"cycles={row['cycles_completed']}/{args.cycles_per_trial} "
                    f"first-wait-max={row['first_frame_wait_ms_max']:.3f}ms "
                    f"join-max={row['join_wait_ms_max']:.3f}ms"
                )
                write_csv(root / "candidate_results.csv", RESULT_FIELDS, result_rows)
                write_csv(root / "cycle_results.csv", CYCLE_FIELDS, cycle_rows)

        summaries, selected = summarize_candidates(
            stage=stage,
            candidates=candidates,
            rows=result_rows,
            trials=trials,
            cycles_per_trial=args.cycles_per_trial,
            safety_factor=args.safety_factor,
        )
        return summaries, selected

    all_summaries: List[Dict[str, Any]] = []

    summaries, selected_settle = execute_stage(
        STAGE_SETTLE,
        settle_candidates,
        safe_settings,
        default_settle_ms=max(settle_candidates),
        trials=args.trials,
    )
    all_summaries.extend(summaries)
    effective_settle = (
        selected_settle if selected_settle is not None else max(settle_candidates)
    )

    summaries, selected_frame = execute_stage(
        STAGE_FRAME,
        frame_candidates,
        safe_settings,
        default_settle_ms=effective_settle,
        trials=args.trials,
    )
    all_summaries.extend(summaries)
    effective_frame = (
        selected_frame
        if selected_frame is not None
        else max(3000, max(frame_candidates))
    )

    join_settings = dict(safe_settings)
    join_settings["frame_timeout_ms"] = effective_frame
    summaries, selected_join = execute_stage(
        STAGE_JOIN,
        join_candidates,
        join_settings,
        default_settle_ms=effective_settle,
        trials=args.trials,
    )
    all_summaries.extend(summaries)
    effective_join = (
        selected_join
        if selected_join is not None
        else max(100, max(join_candidates))
    )

    cycle_settings = dict(join_settings)
    cycle_settings["join_timeout_ms"] = effective_join
    summaries, selected_cycle = execute_stage(
        STAGE_CYCLE,
        cycle_candidates,
        cycle_settings,
        default_settle_ms=effective_settle,
        trials=args.trials,
    )
    all_summaries.extend(summaries)
    effective_cycle = (
        selected_cycle if selected_cycle is not None else max(cycle_candidates)
    )

    # A timeout must cover the worst successful startup observed anywhere in the
    # calibration, not only the trials executed with that timeout candidate.
    # Otherwise an outlier in another stage can make the recommendation unsafe.
    frame_summaries, selected_frame = summarize_candidates(
        stage=STAGE_FRAME,
        candidates=frame_candidates,
        rows=result_rows,
        trials=args.trials,
        cycles_per_trial=args.cycles_per_trial,
        safety_factor=args.safety_factor,
        headroom_rows=result_rows,
    )
    join_summaries, selected_join = summarize_candidates(
        stage=STAGE_JOIN,
        candidates=join_candidates,
        rows=result_rows,
        trials=args.trials,
        cycles_per_trial=args.cycles_per_trial,
        safety_factor=args.safety_factor,
        headroom_rows=result_rows,
    )
    replacements = {
        (summary["stage"], int(summary["candidate_ms"])): summary
        for summary in [*frame_summaries, *join_summaries]
    }
    all_summaries = [
        replacements.get(
            (summary["stage"], int(summary["candidate_ms"])), summary
        )
        for summary in all_summaries
    ]
    effective_frame = (
        selected_frame
        if selected_frame is not None
        else max(3000, max(frame_candidates))
    )
    effective_join = (
        selected_join
        if selected_join is not None
        else max(100, max(join_candidates))
    )

    validation_settings = {
        "frame_timeout_ms": effective_frame,
        "join_timeout_ms": effective_join,
        "cycle_delay_ms": effective_cycle,
    }
    validation_summaries, _ = execute_stage(
        STAGE_VALIDATION,
        [0],
        validation_settings,
        default_settle_ms=effective_settle,
        trials=args.validation_trials,
    )
    all_summaries.extend(validation_summaries)

    validation_rows = [
        row for row in result_rows if row["stage"] == STAGE_VALIDATION
    ]
    validation_success = bool(
        len(validation_rows) == args.validation_trials
        and all(bool(row["success"]) for row in validation_rows)
    )

    # Include validation outliers in the final recommendation and summary.
    frame_summaries, selected_frame = summarize_candidates(
        stage=STAGE_FRAME,
        candidates=frame_candidates,
        rows=result_rows,
        trials=args.trials,
        cycles_per_trial=args.cycles_per_trial,
        safety_factor=args.safety_factor,
        headroom_rows=result_rows,
    )
    join_summaries, selected_join = summarize_candidates(
        stage=STAGE_JOIN,
        candidates=join_candidates,
        rows=result_rows,
        trials=args.trials,
        cycles_per_trial=args.cycles_per_trial,
        safety_factor=args.safety_factor,
        headroom_rows=result_rows,
    )
    replacements = {
        (summary["stage"], int(summary["candidate_ms"])): summary
        for summary in [*frame_summaries, *join_summaries]
    }
    all_summaries = [
        replacements.get(
            (summary["stage"], int(summary["candidate_ms"])), summary
        )
        for summary in all_summaries
    ]
    validation_success = bool(
        validation_success
        and selected_frame is not None
        and selected_join is not None
        and selected_cycle is not None
        and selected_settle is not None
    )
    global_first_wait_max = max(
        (float(row["first_frame_wait_ms_max"]) for row in result_rows),
        default=0.0,
    )
    global_join_wait_max = max(
        (float(row["join_wait_ms_max"]) for row in result_rows),
        default=0.0,
    )

    hardware_max = max(
        (
            float(reset.get("hardware_reset", {}).get("elapsed_ms", 0.0))
            for reset in reset_records
        ),
        default=0.0,
    )
    enumeration_max = max(
        (
            float(reset.get("enumeration", {}).get("elapsed_ms", 0.0))
            for reset in reset_records
        ),
        default=0.0,
    )
    recommendation = {
        "schema_version": 2,
        "provisional": True,
        "validated": validation_success,
        "successful_validation_trials": sum(
            bool(row["success"]) for row in validation_rows
        ),
        "requested_validation_trials": args.validation_trials,
        "selection": {
            "frame_timeout_ms": selected_frame,
            "join_timeout_ms": selected_join,
            "cycle_delay_ms": selected_cycle,
            "recovery_settle_ms": selected_settle,
            "reset_timeout_ms": rounded_timeout(
                hardware_max, args.safety_factor, 5000
            ),
            "enumeration_timeout_ms": rounded_timeout(
                enumeration_max, args.safety_factor, 1000
            ),
        },
        "effective_validation_settings": {
            **validation_settings,
            "recovery_settle_ms": effective_settle,
        },
        "observed_maxima": {
            "first_frame_wait_ms": global_first_wait_max,
            "join_wait_ms": global_join_wait_max,
            "hardware_reset_ms": hardware_max,
            "enumeration_ms": enumeration_max,
        },
        "safety_factor": args.safety_factor,
        "headroom_scope": "all_stages_including_validation",
        "qualification": (
            "All configured trials and cycles must succeed. Frame timeout uses "
            "safety_factor headroom over the maximum first-frame wait observed in "
            "all stages. Join timeout uses the same global rule plus the 5 ms "
            "polling interval."
        ),
        "warning": (
            "These values are platform-, firmware-, topology-, and load-specific. "
            "Repeat with more trials and under intended stress before changing "
            "the production benchmark defaults."
        ),
    }

    write_csv(root / "candidate_results.csv", RESULT_FIELDS, result_rows)
    write_csv(root / "cycle_results.csv", CYCLE_FIELDS, cycle_rows)
    write_csv(root / "candidate_summary.csv", SUMMARY_FIELDS, all_summaries)
    (root / "recommendation.json").write_text(
        json.dumps(recommendation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"[CALIBRATE] results={root}")
    print(json.dumps(recommendation, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
