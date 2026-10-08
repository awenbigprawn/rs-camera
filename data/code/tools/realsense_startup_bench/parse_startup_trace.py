#!/usr/bin/env python3
"""Merge RealSense pthread lifecycle events with LiME/eBPF scheduler events."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

CYCLE_RE = re.compile(r"^cycle_(\d+)_(begin|end)$")
BLOCKING_EVENTS = {
    "enter_accept", "enter_clock_nano_sleep", "enter_epoll_pwait", "enter_futex",
    "enter_mq_timedreceive", "enter_msgrcv", "enter_nanosleep", "enter_pause",
    "enter_poll", "enter_pselect6", "enter_read_blk", "enter_read_chr",
    "enter_read_fifo", "enter_read_sock", "enter_recv", "enter_recvfrom",
    "enter_recvmsg", "enter_recvmmsg", "enter_rt_sigsuspend", "enter_select",
    "enter_semop", "enter_sig_timed_wait",
}


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
            if isinstance(value, dict):
                events.append(value)
    return sorted(events, key=lambda event: int(event.get("timestamp_ns", 0)))


def event_time(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        boottime = value.get("boottime_ns")
        return boottime if isinstance(boottime, int) else None
    return None


def policy_fields(policy: Any) -> Tuple[str, str]:
    if isinstance(policy, str):
        return policy, ""
    if not isinstance(policy, dict) or not policy:
        return "UNKNOWN", ""
    name, parameters = next(iter(policy.items()))
    if not isinstance(parameters, dict):
        return str(name), ""
    if "prio" in parameters:
        return str(name), str(parameters["prio"])
    return str(name), json.dumps(parameters, sort_keys=True, separators=(",", ":"))


def phase_data(events: Iterable[Dict[str, Any]]) -> Tuple[int, List[Dict[str, Any]], Dict[int, Tuple[int, int]]]:
    phases = [event for event in events if event.get("event") == "phase_marker"]
    origin = next(
        (int(event["timestamp_ns"]) for event in phases if event.get("name") == "process_start"),
        min((int(event["timestamp_ns"]) for event in phases), default=0),
    )
    starts: Dict[int, int] = {}
    ends: Dict[int, int] = {}
    for event in phases:
        match = CYCLE_RE.match(str(event.get("name", "")))
        if not match:
            continue
        cycle = int(match.group(1))
        if match.group(2) == "begin":
            starts[cycle] = int(event["timestamp_ns"])
        else:
            ends[cycle] = int(event["timestamp_ns"])
    ordered_starts = sorted(starts.items())
    bounds: Dict[int, Tuple[int, int]] = {}
    trace_end = max((int(event.get("timestamp_ns", 0)) for event in events), default=origin)
    for index, (cycle, start) in enumerate(ordered_starts):
        next_start = ordered_starts[index + 1][1] if index + 1 < len(ordered_starts) else trace_end + 1
        bounds[cycle] = (start, ends.get(cycle, next_start))
    return origin, phases, bounds


def classify_uvc_phase(phase_name: str) -> str:
    match = re.match(r"^cycle_\d+_(.+)$", phase_name)
    if not match:
        return "other"
    suffix = match.group(1)
    if suffix in {
        "begin", "before_context", "after_context",
        "before_pipeline_construction", "after_pipeline_construction",
        "before_pipeline_start",
    }:
        return "startup"
    if suffix in {"after_pipeline_start", "first_frame"}:
        return "streaming"
    if suffix in {
        "frames_complete", "before_pipeline_stop", "after_pipeline_stop",
        "before_object_destruction", "after_object_destruction",
        "threads_joined", "thread_join_timeout", "end",
    }:
        return "teardown"
    return "other"


def cycle_at(timestamp_ns: Optional[int], bounds: Dict[int, Tuple[int, int]]) -> int:
    if timestamp_ns is None:
        return 0
    for cycle, (begin, end) in sorted(bounds.items()):
        if begin <= timestamp_ns <= end:
            return cycle
    return 0



def thread_signature(event: Dict[str, Any]) -> str:
    """Reproduce the preload tracer's ASLR-independent creation-stack key."""
    explicit = str(event.get("signature") or "")
    if explicit:
        return explicit
    entry_module = Path(str(event.get("entry_module") or "")).name
    entry_offset = str(event.get("entry_module_offset") or event.get("entry_address") or "0x0")
    parts = [
        f"entry={entry_module}"
        if entry_module == "realsense_steady_probe"
        else f"entry={entry_module}@{entry_offset}"
    ]
    included = 0
    for frame in event.get("stack", []):
        if not isinstance(frame, dict):
            continue
        module = Path(str(frame.get("module") or "")).name
        if not module or "libtrace_pthreads.so" in module or module == entry_module:
            continue
        offset = str(frame.get("module_offset") or frame.get("address") or "0x0")
        parts.append(module if module == "realsense_steady_probe" else f"{module}@{offset}")
        included += 1
        if included == 6:
            break
    return "|".join(parts)

def lifecycle_records(
    events: List[Dict[str, Any]],
    bounds: Dict[int, Tuple[int, int]],
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, int], Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    by_create: Dict[Tuple[str, int], Dict[str, Any]] = {}
    by_tid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    by_pthread: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    def new_record(**values: Any) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "tid": None, "tgid": None, "pthread_value": "", "name": "",
            "parent_tid": None, "created_ns": None, "started_ns": None,
            "exited_ns": None, "joined_ns": None, "joined_by": None,
            "detached_by": None, "cycle": 0, "creation_sequence": None,
            "signature": "", "entry_module": "", "entry_module_offset": "",
        }
        record.update(values)
        records.append(record)
        pthread_value = str(record.get("pthread_value") or "")
        if pthread_value:
            by_pthread[pthread_value].append(record)
        return record

    process_start = next(
        (event for event in events if event.get("event") == "phase_marker" and event.get("name") == "process_start"),
        None,
    )
    if process_start:
        tid = int(process_start["tid"])
        main = new_record(
            tid=tid,
            tgid=tid,
            name="main",
            created_ns=int(process_start["timestamp_ns"]),
            started_ns=int(process_start["timestamp_ns"]),
        )
        by_tid[tid].append(main)

    for event in events:
        kind = event.get("event")
        timestamp = event.get("timestamp_ns")
        timestamp = int(timestamp) if isinstance(timestamp, int) else None
        pthread_value = str(event.get("pthread_value") or "")

        if kind == "pthread_create" and event.get("success", event.get("result") == 0):
            record = new_record(
                pthread_value=pthread_value,
                parent_tid=event.get("caller_tid"),
                created_ns=timestamp,
                cycle=cycle_at(timestamp, bounds),
                creation_sequence=event.get("creation_sequence"),
                signature=thread_signature(event),
                entry_module=event.get("entry_module", ""),
                entry_module_offset=event.get("entry_module_offset", ""),
            )
            by_create[(pthread_value, int(timestamp or 0))] = record
        elif kind == "thread_start":
            create_timestamp = int(event.get("create_timestamp_ns", 0))
            record = by_create.get((pthread_value, create_timestamp))
            if record is None:
                record = new_record(
                    pthread_value=pthread_value,
                    created_ns=create_timestamp or timestamp,
                    cycle=cycle_at(create_timestamp or timestamp, bounds),
                )
                by_create[(pthread_value, create_timestamp)] = record
            record.update(
                tid=int(event["tid"]),
                tgid=next((r["tgid"] for r in records if r.get("tgid")), None),
                parent_tid=event.get("parent_tid", record.get("parent_tid")),
                started_ns=timestamp,
                name=event.get("name", "") or record.get("name", ""),
                creation_sequence=event.get(
                    "creation_sequence", record.get("creation_sequence")
                ),
                signature=(
                    event.get("signature", "")
                    or record.get("signature", "")
                    or thread_signature(event)
                ),
                entry_module=event.get(
                    "entry_module", record.get("entry_module", "")
                ),
                entry_module_offset=event.get(
                    "entry_module_offset", record.get("entry_module_offset", "")
                ),
            )
            by_tid[int(event["tid"])].append(record)
        elif kind == "thread_name":
            caller_tid = event.get("caller_tid")
            candidates = by_tid.get(int(caller_tid), []) if caller_tid is not None else []
            if not candidates:
                candidates = by_pthread.get(pthread_value, [])
            if candidates:
                candidates[-1]["name"] = event.get("name", "") or candidates[-1].get("name", "")
        elif kind == "thread_exit":
            tid = int(event["tid"])
            candidates = [record for record in by_tid.get(tid, []) if record.get("exited_ns") is None]
            if candidates:
                record = candidates[-1]
                record["exited_ns"] = timestamp
                record["name"] = event.get("name", "") or record.get("name", "")
        elif kind in ("pthread_join_end", "pthread_detach"):
            candidates = [
                record for record in by_pthread.get(pthread_value, [])
                if record.get("created_ns") is None or timestamp is None or record["created_ns"] <= timestamp
            ]
            if candidates:
                record = candidates[-1]
                if kind == "pthread_join_end" and event.get("result", 0) == 0:
                    record["joined_ns"] = timestamp
                    record["joined_by"] = event.get("caller_tid")
                elif kind == "pthread_detach" and event.get("result", 0) == 0:
                    record["detached_by"] = event.get("caller_tid")
        elif kind == "phase_marker" and event.get("name") in ("process_exit", "process_error"):
            for record in records:
                if record.get("tid") == record.get("tgid"):
                    record["exited_ns"] = timestamp

    app_tgid = next((record["tgid"] for record in records if record.get("tgid")), None)
    for record in records:
        record["tgid"] = app_tgid
        if not record.get("cycle"):
            record["cycle"] = cycle_at(record.get("created_ns"), bounds)
    return records, by_create


def load_lime(lime_dir: Path, app_tgid: Optional[int]) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[int, List[Tuple[str, str]]]]:
    events_by_tid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    policies_by_tid: Dict[int, List[Tuple[str, str]]] = defaultdict(list)

    for info_path in sorted(lime_dir.glob("*.infos.json")):
        task_id = info_path.name.removesuffix(".infos.json")
        event_path = lime_dir / f"{task_id}.events.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        tid = int(info.get("pid", task_id.split("-", 1)[0]))
        tgid = int(info.get("tgid", tid))
        if app_tgid is not None and tgid != app_tgid:
            continue

        policy = policy_fields(info.get("policy"))
        if policy not in policies_by_tid[tid]:
            policies_by_tid[tid].append(policy)
        if not event_path.exists():
            continue
        values = json.loads(event_path.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise ValueError(f"{event_path} does not contain a JSON event array")
        for event in values:
            if isinstance(event, dict) and isinstance(event.get("ts"), int):
                copy = dict(event)
                copy["_policy"] = policy[0]
                copy["_priority"] = policy[1]
                copy["_task_version"] = task_id
                events_by_tid[tid].append(copy)

    for events in events_by_tid.values():
        events.sort(key=lambda event: int(event["ts"]))
    return events_by_tid, policies_by_tid


def event_window(record: Dict[str, Any], trace_end: int) -> Tuple[int, int]:
    begin = int(record.get("created_ns") or record.get("started_ns") or 0)
    end = int(record.get("exited_ns") or record.get("joined_ns") or trace_end)
    return begin, end


def scheduler_intervals(
    events: List[Dict[str, Any]],
    begin: int,
    end: int,
    cycle: int,
    tid: int,
    name: str,
    origin: int,
) -> Tuple[List[Dict[str, Any]], Optional[int], Optional[int]]:
    relevant = [event for event in events if begin <= int(event["ts"]) <= end]
    intervals: List[Dict[str, Any]] = []
    current_state: Optional[str] = None
    current_start: Optional[int] = None
    current_cpu: Any = ""
    current_reason = ""
    last_blocking = ""
    first_run: Optional[int] = None
    first_sleep: Optional[int] = None

    def close(timestamp: int) -> None:
        nonlocal current_state, current_start
        if current_state is None or current_start is None or timestamp < current_start:
            return
        intervals.append({
            "cycle": cycle,
            "tid": tid,
            "name": name,
            "state": current_state,
            "start_ns": current_start,
            "end_ns": timestamp,
            "start_ms": (current_start - origin) / 1_000_000,
            "end_ms": (timestamp - origin) / 1_000_000,
            "duration_ms": (timestamp - current_start) / 1_000_000,
            "cpu": current_cpu,
            "reason": current_reason,
        })
        current_state = None
        current_start = None

    for event in relevant:
        timestamp = int(event["ts"])
        kind = str(event.get("event", ""))
        if kind in BLOCKING_EVENTS:
            last_blocking = kind
        elif kind == "sched_switched_in":
            close(timestamp)
            current_state, current_start = "running", timestamp
            current_cpu, current_reason = event.get("cpu", ""), ""
            if first_run is None:
                first_run = timestamp
        elif kind == "sched_switched_out":
            close(timestamp)
            state = int(event.get("state", 0))
            current_state = "ready" if state == 0 else "sleeping"
            current_start = timestamp
            current_cpu = event.get("cpu", current_cpu)
            current_reason = "preempted" if state == 0 else (last_blocking or f"task_state_{state}")
            if state != 0 and first_sleep is None:
                first_sleep = timestamp
        elif kind in ("sched_wake_up", "sched_wake_up_new"):
            if current_state == "sleeping":
                close(timestamp)
            if current_state is None:
                current_state, current_start = "ready", timestamp
                current_cpu, current_reason = event.get("cpu", ""), kind
        elif kind == "sched_process_exit":
            close(timestamp)

    close(end)
    return intervals, first_run, first_sleep


def parse_startup_trace(
    lifecycle_path: Path,
    lime_dir: Path,
    output_dir: Path,
    stdout_path: Optional[Path] = None,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    lifecycle = read_jsonl(lifecycle_path)
    origin, phases, bounds = phase_data(lifecycle)
    records, by_create = lifecycle_records(lifecycle, bounds)
    app_tgid = next((record.get("tgid") for record in records if record.get("tgid")), None)
    lime_events, policies = load_lime(lime_dir, app_tgid)
    trace_end = max(
        [int(event.get("timestamp_ns", 0)) for event in lifecycle]
        + [int(event["ts"]) for events in lime_events.values() for event in events]
        + [origin],
    )

    interval_rows: List[Dict[str, Any]] = []
    timing_rows: List[Dict[str, Any]] = []
    record_for_create = {
        (record.get("pthread_value", ""), int(record.get("created_ns") or 0)): record
        for record in records
    }

    for record in records:
        tid = record.get("tid")
        if tid is None:
            continue
        begin, end = event_window(record, trace_end)
        intervals, first_run, first_sleep = scheduler_intervals(
            lime_events.get(int(tid), []),
            begin,
            end,
            int(record.get("cycle") or 0),
            int(tid),
            str(record.get("name") or ""),
            origin,
        )
        interval_rows.extend(intervals)
        totals = defaultdict(float)
        counts = defaultdict(int)
        for interval in intervals:
            totals[interval["state"]] += float(interval["duration_ms"])
            counts[interval["state"]] += 1

        policy_values = policies.get(int(tid), [])
        policy = "|".join(value[0] for value in policy_values) or "UNKNOWN"
        priority = "|".join(value[1] for value in policy_values if value[1])
        created = record.get("created_ns")
        started = record.get("started_ns")
        exited = record.get("exited_ns")
        joined = record.get("joined_ns")
        if joined is not None:
            status = "joined"
        elif record.get("detached_by") is not None and exited is not None:
            status = "detached_and_exited"
        elif record.get("detached_by") is not None:
            status = "detached_unobserved_exit"
        elif exited is not None:
            status = "exited"
        else:
            status = "unobserved_exit"

        def relative(timestamp: Optional[int]) -> Any:
            return "" if timestamp is None else (timestamp - origin) / 1_000_000

        timing_rows.append({
            "cycle": record.get("cycle", 0),
            "tid": tid,
            "tgid": record.get("tgid", ""),
            "pthread_value": record.get("pthread_value", ""),
            "name": record.get("name", ""),
            "parent_tid": record.get("parent_tid", ""),
            "policy": policy,
            "priority": priority,
            "created_ns": created if created is not None else "",
            "created_ms": relative(created),
            "started_ns": started if started is not None else "",
            "started_ms": relative(started),
            "first_run_ns": first_run if first_run is not None else "",
            "first_run_ms": relative(first_run),
            "first_sleep_ns": first_sleep if first_sleep is not None else "",
            "first_sleep_ms": relative(first_sleep),
            "exited_ns": exited if exited is not None else "",
            "exited_ms": relative(exited),
            "joined_ns": joined if joined is not None else "",
            "joined_ms": relative(joined),
            "create_to_start_ms": "" if created is None or started is None else (started - created) / 1_000_000,
            "execution_ms": totals["running"],
            "sleep_ms": totals["sleeping"],
            "ready_ms": totals["ready"],
            "lifetime_ms": "" if started is None or exited is None else (exited - started) / 1_000_000,
            "running_intervals": counts["running"],
            "sleep_intervals": counts["sleeping"],
            "ready_intervals": counts["ready"],
            "joined_by": record.get("joined_by", ""),
            "detached_by": record.get("detached_by", ""),
            "status": status,
        })

    timing_fields = [
        "cycle", "tid", "tgid", "pthread_value", "name", "parent_tid", "policy", "priority",
        "created_ns", "created_ms", "started_ns", "started_ms", "first_run_ns", "first_run_ms",
        "first_sleep_ns", "first_sleep_ms", "exited_ns", "exited_ms", "joined_ns", "joined_ms",
        "create_to_start_ms", "execution_ms", "sleep_ms", "ready_ms", "lifetime_ms",
        "running_intervals", "sleep_intervals", "ready_intervals", "joined_by", "detached_by", "status",
    ]
    interval_fields = [
        "cycle", "tid", "name", "state", "start_ns", "end_ns", "start_ms", "end_ms",
        "duration_ms", "cpu", "reason",
    ]
    write_csv(output_dir / "thread_timing.csv", timing_fields, timing_rows)
    write_csv(output_dir / "thread_intervals.csv", interval_fields, interval_rows)

    timeline_rows: List[Dict[str, Any]] = []
    for event in lifecycle:
        timestamp = event.get("timestamp_ns")
        if not isinstance(timestamp, int):
            continue
        tid: Any = event.get("tid", event.get("caller_tid", ""))
        if event.get("event") == "pthread_create":
            record = record_for_create.get((str(event.get("pthread_value") or ""), timestamp))
            if record:
                tid = record.get("tid", "")
        timeline_rows.append({
            "cycle": cycle_at(timestamp, bounds),
            "tid": tid,
            "source": "pthread",
            "event": event.get("event", ""),
            "timestamp_ns": timestamp,
            "timestamp_ms": (timestamp - origin) / 1_000_000,
            "cpu": "",
            "state": event.get("state", ""),
            "detail": event.get("name", ""),
        })
    for tid, events in lime_events.items():
        for event in events:
            timestamp = int(event["ts"])
            timeline_rows.append({
                "cycle": cycle_at(timestamp, bounds),
                "tid": tid,
                "source": "lime_ebpf",
                "event": event.get("event", ""),
                "timestamp_ns": timestamp,
                "timestamp_ms": (timestamp - origin) / 1_000_000,
                "cpu": event.get("cpu", ""),
                "state": event.get("state", ""),
                "detail": event.get("_task_version", ""),
            })
    timeline_rows.sort(key=lambda row: (int(row["timestamp_ns"]), str(row["source"])))
    write_csv(
        output_dir / "thread_events.csv",
        ["cycle", "tid", "source", "event", "timestamp_ns", "timestamp_ms", "cpu", "state", "detail"],
        timeline_rows,
    )

    cycle_results: List[Dict[str, Any]] = []
    device: Dict[str, Any] = {}
    scheduler: Dict[str, Any] = {}
    final_result: Dict[str, Any] = {}
    startup_error: Dict[str, Any] = {}
    if stdout_path and stdout_path.exists():
        for line in stdout_path.read_text(encoding="utf-8").splitlines():
            for prefix, target in (
                ("RS_DEVICE ", device),
                ("RS_SCHEDULER ", scheduler),
                ("RS_STARTUP_RESULT ", final_result),
                ("RS_STARTUP_ERROR ", startup_error),
            ):
                if line.startswith(prefix):
                    target.update(json.loads(line[len(prefix):]))
            if line.startswith("RS_STARTUP_CYCLE "):
                cycle_results.append(json.loads(line[len("RS_STARTUP_CYCLE "):]))

    def mean(field: str) -> float:
        values = [float(cycle[field]) for cycle in cycle_results if field in cycle]
        return statistics.fmean(values) if values else 0.0

    def maximum(field: str) -> float:
        values = [float(cycle[field]) for cycle in cycle_results if field in cycle]
        return max(values) if values else 0.0

    manifest_path = output_dir / "run_manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    kernel_log_path = output_dir / "kernel_log.txt"
    kernel_log_captured = kernel_log_path.exists()
    uvc_events: List[Dict[str, Any]] = []
    ordered_phases = sorted(phases, key=lambda phase: int(phase.get("timestamp_ns", 0)))
    if kernel_log_captured:
        uvc_pattern = re.compile(
            r"\[\s*(\d+(?:\.\d+)?)\].*?uvcvideo\s+(\S+):\s+"
            r"Failed to resubmit video URB\s+\((-?\d+)\)"
        )
        for line in kernel_log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = uvc_pattern.search(line)
            if not match:
                continue
            timestamp_ns = int(float(match.group(1)) * 1_000_000_000)
            preceding = [
                phase for phase in ordered_phases
                if int(phase.get("timestamp_ns", 0)) <= timestamp_ns
            ]
            phase_name = str(preceding[-1].get("name", "")) if preceding else ""
            phase_class = classify_uvc_phase(phase_name)
            uvc_events.append({
                "timestamp_ns": timestamp_ns,
                "timestamp_ms": (timestamp_ns - origin) / 1_000_000,
                "cycle": cycle_at(timestamp_ns, bounds),
                "interface": match.group(2),
                "error_code": match.group(3),
                "phase": phase_name,
                "phase_class": phase_class,
                "line": line,
            })
    write_csv(
        output_dir / "kernel_events.csv",
        ["timestamp_ns", "timestamp_ms", "cycle", "interface", "error_code",
         "phase", "phase_class", "line"],
        uvc_events,
    )
    uvc_interfaces = sorted({event["interface"] for event in uvc_events})
    uvc_error_codes = Counter(event["error_code"] for event in uvc_events)
    uvc_phase_counts = Counter(event["phase_class"] for event in uvc_events)

    phase_names = {str(phase.get("name", "")) for phase in phases}
    process_error = "process_error" in phase_names
    cycles_started = len(bounds)
    cycles_completed_by_phase = sum(
        1 for name in phase_names if CYCLE_RE.match(name) and name.endswith("_end")
    )
    if not final_result:
        final_result = {
            "success": False,
            "completed_cycles": cycles_completed_by_phase,
            "requested_cycles": int(manifest.get("cycles", cycles_started)),
        }

    worker_rows = [row for row in timing_rows if int(row.get("cycle") or 0) > 0]
    summary = {
        "schema_version": 1,
        "clock": "CLOCK_BOOTTIME",
        "device": device,
        "scheduler": scheduler,
        "startup_result": final_result,
        "startup_error": startup_error,
        "process_error": process_error,
        "cycles_started": cycles_started,
        "cycles_completed_by_phase": cycles_completed_by_phase,
        "cycles": cycle_results,
        "cycle_count": len(cycle_results),
        "successful_cycles": sum(1 for cycle in cycle_results if cycle.get("success")),
        "thread_instances": len(worker_rows),
        "all_observed_threads_terminated": all(row["exited_ns"] != "" for row in worker_rows),
        "start_call_ms_mean": mean("start_call_ms"),
        "start_call_ms_max": maximum("start_call_ms"),
        "first_frame_ms_mean": mean("first_frame_ms"),
        "first_frame_ms_max": maximum("first_frame_ms"),
        "first_frame_wait_ms_mean": mean("first_frame_wait_ms"),
        "first_frame_wait_ms_max": maximum("first_frame_wait_ms"),
        "join_wait_ms_mean": mean("join_wait_ms"),
        "join_wait_ms_max": maximum("join_wait_ms"),
        "execution_ms_total": sum(float(row["execution_ms"]) for row in worker_rows),
        "sleep_ms_total": sum(float(row["sleep_ms"]) for row in worker_rows),
        "ready_ms_total": sum(float(row["ready_ms"]) for row in worker_rows),
        "cycle_delay_ms": int(manifest.get("cycle_delay_ms", 0)),
        "kernel_log_captured": kernel_log_captured,
        "uvc_resubmit_errors": len(uvc_events),
        "uvc_resubmit_errors_startup": uvc_phase_counts["startup"],
        "uvc_resubmit_errors_streaming": uvc_phase_counts["streaming"],
        "uvc_resubmit_errors_teardown": uvc_phase_counts["teardown"],
        "uvc_resubmit_errors_other": uvc_phase_counts["other"],
        "uvc_resubmit_interfaces": ",".join(uvc_interfaces),
        "uvc_resubmit_error_codes": ",".join(
            f"{code}:{count}" for code, count in sorted(uvc_error_codes.items())
        ),
        "phase_count": len(phases),
        "lime_task_count": len(lime_events),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def write_csv(path: Path, fields: List[str], rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    argument_parser = argparse.ArgumentParser(
        description="Merge LiME/eBPF scheduling events with pthread lifecycle/phase events."
    )
    argument_parser.add_argument("--lifecycle", type=Path, required=True)
    argument_parser.add_argument("--lime-dir", type=Path, required=True)
    argument_parser.add_argument("--stdout", type=Path)
    argument_parser.add_argument("--output-dir", type=Path, required=True)
    args = argument_parser.parse_args()
    summary = parse_startup_trace(args.lifecycle, args.lime_dir, args.output_dir, args.stdout)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
