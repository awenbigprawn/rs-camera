"""Result assembly for RealSense steady-state Benchkit records."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping

from noise_workloads import NoiseSuite
from realsense_bench_common.memory import memory_cleanup_result_fields
from realsense_bench_common.artifacts import resolve_selected_attempt
from realsense_bench_common.results import common_attempt_result_fields


def flatten(prefix: str, value: Any, output: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            flatten(f"{prefix}_{key}" if prefix else str(key), child, output)
    elif isinstance(value, list):
        output[prefix] = json.dumps(value, sort_keys=True)
    else:
        output[prefix] = value


def interrupt_totals(path: Path) -> Dict[str, int]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return {
        f"{line.get('irq', '')}:{line.get('description', '')}": sum(
            int(count) for count in line.get("counts", [])
        )
        for line in value.get("parsed_interrupts", {}).get("lines", [])
    }


def _add_noise_results(
    result: Dict[str, Any],
    *,
    run_variables: Mapping[str, Any],
    record_dir: Path,
    noise_suite: NoiseSuite,
) -> None:
    noise_modes = {
        key: str(run_variables[key]) for key in NoiseSuite.mode_keys
    }
    for artifacts in noise_suite.artifacts(noise_modes, record_dir):
        prefix = artifacts.prefix
        result[f"{prefix}_ready"] = bool(artifacts.ready.get("ready", False))
        result[f"{prefix}_process_returncode"] = artifacts.process.get(
            "returncode", ""
        )
        result[f"{prefix}_forced_kill"] = artifacts.process.get(
            "forced_kill", False
        )
        if artifacts.summary:
            flatten(prefix, artifacts.summary, result)
        result.update(artifacts.extra_result_fields)
        result[f"{prefix}_valid"] = artifacts.valid
        if not artifacts.valid:
            result["success"] = False
            result["error"] = (
                str(result.get("error", "")) + f" | {artifacts.error}"
            ).strip(" |")


def _add_camera_results(
    result: Dict[str, Any],
    data: Mapping[str, Any],
) -> None:
    for camera in data.get("cameras", []):
        index = camera.get("index", 0)
        prefix = f"camera_{index}"
        for key in (
            "serial",
            "physical_port",
            "usb_type",
            "start_call_ms",
            "stop_call_ms",
            "warmup_deliveries",
            "warmup_health_deliveries",
            "warmup_observed_frames",
            "warmup_duplicate_frames",
            "warmup_sequence_gaps",
            "warmup_out_of_order_frames",
            "deliveries",
            "frames",
            "drops",
            "timeouts",
            "pre_measurement_timeouts",
            "measurement_timeouts",
            "observed_frames",
            "unique_frames",
            "duplicate_frames",
            "sequence_gaps",
            "nonadvancing_frames",
            "out_of_order_frames",
            "fully_fresh_framesets",
            "partially_stale_framesets",
            "stale_framesets",
        ):
            result[f"{prefix}_{key}"] = camera.get(key, "")
        flatten(
            f"{prefix}_interarrival_ms",
            camera.get("delivery_interarrival_ms", {}),
            result,
        )
        flatten(
            f"{prefix}_storage",
            camera.get("storage", {}),
            result,
        )


def _add_trace_results(result: Dict[str, Any], record_dir: Path) -> None:
    thread_path = record_dir / "thread_steady_summary.json"
    if thread_path.is_file():
        thread_data = json.loads(thread_path.read_text(encoding="utf-8"))
        result["traced_thread_count"] = thread_data.get("thread_count", 0)
        result["traced_activation_count"] = thread_data.get("activation_count", 0)
    else:
        result["traced_thread_count"] = 0
        result["traced_activation_count"] = 0


def _add_kernel_results(result: Dict[str, Any], record_dir: Path) -> None:
    kernel_path = record_dir / "kernel_log.txt"
    kernel_text = (
        kernel_path.read_text(encoding="utf-8", errors="replace")
        if kernel_path.is_file()
        else ""
    )
    uvc_matches = re.findall(
        r"uvcvideo\s+(\S+):\s+Failed to resubmit video URB\s+\((-?\d+)\)",
        kernel_text,
    )
    result["kernel_log_captured"] = kernel_path.is_file()
    result["uvc_resubmit_errors"] = len(uvc_matches)
    result["uvc_resubmit_interfaces"] = ",".join(
        sorted({match[0] for match in uvc_matches})
    )
    result["uvc_resubmit_error_codes"] = ",".join(
        sorted({match[1] for match in uvc_matches})
    )

    before_totals = interrupt_totals(record_dir / "topology_before.json")
    after_totals = interrupt_totals(record_dir / "topology_after.json")
    result["irq_delta_json"] = json.dumps(
        {
            key: after_totals.get(key, 0) - before_totals.get(key, 0)
            for key in sorted(set(before_totals) | set(after_totals))
        },
        sort_keys=True,
    )


def parse_steady_results(
    *,
    record_dir: Path,
    case: Mapping[str, Any],
    run_variables: Mapping[str, Any],
    backend_name: str,
    policy_names: Mapping[str, str],
    drop_caches_configured: bool,
    noise_suite: NoiseSuite,
    cpu_isolation_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    path = record_dir / "steady_summary.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    selected = resolve_selected_attempt(record_dir)
    selected_dir = selected.data_dir
    aggregate = data.get("aggregate", {})
    result: Dict[str, Any] = {
        **memory_cleanup_result_fields(
            selected_dir,
            configured=drop_caches_configured,
        ),
        **common_attempt_result_fields(data),
        "success": data.get("success", False),
        "error": data.get("error", "summary file missing"),
        "backend": backend_name,
        "policy_requested": policy_names[str(run_variables["policy"])],
        "process_launch_policy_effective": data.get("scheduler", {}).get(
            "policy", ""
        ),
        "main_thread_policy_effective": data.get("scheduler", {}).get(
            "main_thread_policy", data.get("scheduler", {}).get("policy", "")
        ),
        "steady_worker_policy_effective": data.get("scheduler", {}).get(
            "steady_worker_policy", ""
        ),
        "deadline_profile_applied": data.get("deadline") is not None,
        "rate_monotonic_profile_applied": data.get("rate_monotonic") is not None,
        "cpu_noise_mode": run_variables["cpu_noise"],
        "cpu_noise_enabled": run_variables["cpu_noise"] != "none",
        "memory_noise_mode": run_variables["memory_noise"],
        "memory_noise_enabled": run_variables["memory_noise"] != "none",
        "gpu_noise_mode": run_variables["gpu_noise"],
        "gpu_noise_enabled": run_variables["gpu_noise"] != "none",
        "usb_storage_noise_mode": run_variables["usb_storage_noise"],
        "usb_storage_noise_enabled": run_variables["usb_storage_noise"] != "none",
        "camera_count": data.get("run", {}).get("camera_count", 0),
        "fixed_event_storage": data.get("run", {}).get(
            "fixed_event_storage", False
        ),
        "deliveries": aggregate.get("deliveries", 0),
        "frames": aggregate.get("frames", 0),
        "drops": aggregate.get("drops", 0),
        "timeouts": aggregate.get("timeouts", 0),
        "pre_measurement_timeouts": aggregate.get("pre_measurement_timeouts", 0),
        "measurement_timeouts": aggregate.get("measurement_timeouts", 0),
        "observed_frames": aggregate.get(
            "observed_frames", aggregate.get("frames", 0)
        ),
        "unique_frames": aggregate.get("unique_frames", aggregate.get("frames", 0)),
        "duplicate_frames": aggregate.get("duplicate_frames", 0),
        "sequence_gaps": aggregate.get(
            "sequence_gaps", aggregate.get("drops", 0)
        ),
        "nonadvancing_frames": aggregate.get("nonadvancing_frames", 0),
        "out_of_order_frames": aggregate.get("out_of_order_frames", 0),
        "fully_fresh_framesets": aggregate.get("fully_fresh_framesets", 0),
        "partially_stale_framesets": aggregate.get("partially_stale_framesets", 0),
        "stale_framesets": aggregate.get("stale_framesets", 0),
        "freshness_analysis_ms": data.get("postprocess", {}).get(
            "freshness_analysis_ms", 0.0
        ),
        "measurement_mode": data.get("measurement", {}).get(
            "mode", data.get("run", {}).get("measurement_mode", "deliveries")
        ),
        "measurement_requested_duration_ms": data.get("measurement", {}).get(
            "requested_duration_ms", 0
        ),
        "measurement_duration_ms": data.get("measurement", {}).get(
            "duration_ms", 0
        ),
        "transition_noise_gate_enabled": False,
        "transition_warmup_ready_boottime_ns": 0,
        "transition_measurement_gate_open_boottime_ns": 0,
        "transition_warmup_to_gate_ms": 0.0,
        "deadline_assignments": "[]",
        "deadline_live_threads": 0,
        "deadline_overrun_signals": 0,
        "deadline_partial_profile": False,
        "deadline_profile_entries": 0,
        "deadline_profile_path": "",
        "deadline_unassigned_live_threads": 0,
        "rate_monotonic_assignments": "[]",
        "rate_monotonic_highest_priority": 0,
        "rate_monotonic_live_threads": 0,
        "rate_monotonic_lowest_priority": 0,
        "rate_monotonic_policy": "",
        "rate_monotonic_priority_levels": 0,
        "rate_monotonic_profile_entries": 0,
        "rate_monotonic_profile_path": "",
        "record_data_dir": str(record_dir),
        "selected_attempt_data_dir": str(selected_dir),
        "artifact_layout": selected.layout,
    }

    _add_noise_results(
        result,
        run_variables=run_variables,
        record_dir=selected_dir,
        noise_suite=noise_suite,
    )
    flatten("cpu_isolation", cpu_isolation_state or {}, result)
    flatten("workload", case.get("workload", {}), result)
    flatten("physical", case.get("physical", {}), result)
    flatten(
        "delivery_interarrival_ms",
        aggregate.get("delivery_interarrival_ms", {}),
        result,
    )
    flatten("wait_ms", aggregate.get("wait_ms", {}), result)
    flatten("transition", data.get("transition", {}), result)
    if data.get("deadline") is not None:
        flatten("deadline", data["deadline"], result)
    if data.get("rate_monotonic") is not None:
        flatten("rate_monotonic", data["rate_monotonic"], result)
    _add_camera_results(result, data)
    _add_trace_results(result, selected_dir)
    _add_kernel_results(result, selected_dir)
    return result
