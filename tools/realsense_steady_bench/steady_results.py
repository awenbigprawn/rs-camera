"""Result assembly for RealSense steady-state Benchkit records."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping

from noise_workloads import NoiseSuite
from realsense_benchmark_utils import memory_cleanup_result_fields


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
            "usb_type",
            "start_call_ms",
            "stop_call_ms",
            "deliveries",
            "frames",
            "drops",
            "timeouts",
        ):
            result[f"{prefix}_{key}"] = camera.get(key, "")
        flatten(
            f"{prefix}_interarrival_ms",
            camera.get("delivery_interarrival_ms", {}),
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
) -> Dict[str, Any]:
    path = record_dir / "steady_summary.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    aggregate = data.get("aggregate", {})
    result: Dict[str, Any] = {
        **memory_cleanup_result_fields(
            record_dir,
            configured=drop_caches_configured,
        ),
        "success": data.get("success", False),
        "error": data.get("error", "summary file missing"),
        "backend": backend_name,
        "policy_requested": policy_names[str(run_variables["policy"])],
        "cpu_noise_mode": run_variables["cpu_noise"],
        "cpu_noise_enabled": run_variables["cpu_noise"] != "none",
        "memory_noise_mode": run_variables["memory_noise"],
        "memory_noise_enabled": run_variables["memory_noise"] != "none",
        "gpu_noise_mode": run_variables["gpu_noise"],
        "gpu_noise_enabled": run_variables["gpu_noise"] != "none",
        "usb_storage_noise_mode": run_variables["usb_storage_noise"],
        "usb_storage_noise_enabled": run_variables["usb_storage_noise"] != "none",
        "camera_count": data.get("run", {}).get("camera_count", 0),
        "deliveries": aggregate.get("deliveries", 0),
        "frames": aggregate.get("frames", 0),
        "drops": aggregate.get("drops", 0),
        "timeouts": aggregate.get("timeouts", 0),
        "measurement_duration_ms": data.get("measurement", {}).get(
            "duration_ms", 0
        ),
        "record_data_dir": str(record_dir),
    }

    _add_noise_results(
        result,
        run_variables=run_variables,
        record_dir=record_dir,
        noise_suite=noise_suite,
    )
    flatten("workload", case.get("workload", {}), result)
    flatten("physical", case.get("physical", {}), result)
    flatten(
        "delivery_interarrival_ms",
        aggregate.get("delivery_interarrival_ms", {}),
        result,
    )
    flatten("wait_ms", aggregate.get("wait_ms", {}), result)
    _add_camera_results(result, data)
    _add_trace_results(result, record_dir)
    _add_kernel_results(result, record_dir)
    return result
