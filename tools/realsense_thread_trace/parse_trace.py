#!/usr/bin/env python3

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import re


def _ms(value_ns, origin_ns):
    if value_ns is None or origin_ns is None:
        return ""
    return f"{(value_ns - origin_ns) / 1_000_000:.3f}"


def _duration_ms(start_ns, end_ns):
    if start_ns is None or end_ns is None:
        return ""
    return f"{(end_ns - start_ns) / 1_000_000:.3f}"


def parse_events(trace_path):
    events = []
    with Path(trace_path).open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                events.append(
                    {
                        "event": "parse_error",
                        "line_no": line_no,
                        "error": str(exc),
                        "raw": line,
                    }
                )
                continue
            event["_line_no"] = line_no
            events.append(event)
    return events


def parse_trace_file(trace_path, summary_csv):
    events = parse_events(trace_path)
    threads = []
    threads_by_pthread = defaultdict(list)
    threads_by_tid = {}
    phases = []
    origin_ns = None
    trace_end_ns = None

    def new_record(pthread_value="", tid=""):
        rec = {
            "pthread_value": pthread_value,
            "tid": tid,
            "name": "",
            "parent_tid": "",
            "created_ns": None,
            "started_ns": None,
            "exited_ns": None,
            "exit_kind": "",
            "joined_by": "",
            "detached_by": "",
            "entry_address": "",
            "entry_module": "",
            "entry_symbol": "",
            "create_result": "",
        }
        threads.append(rec)
        if pthread_value:
            threads_by_pthread[pthread_value].append(rec)
        if tid != "":
            threads_by_tid[tid] = rec
        return rec

    def record_for_tid(tid):
        rec = threads_by_tid.get(tid)
        if rec is None:
            rec = new_record(tid=tid)
        return rec

    def bind_tid(rec, tid):
        if tid == "":
            return
        rec["tid"] = tid
        threads_by_tid[tid] = rec

    def bind_pthread(rec, pthread_value):
        if not pthread_value:
            return
        if rec.get("pthread_value") == pthread_value:
            return
        rec["pthread_value"] = pthread_value
        threads_by_pthread[pthread_value].append(rec)

    def find_start_record(pthread_value, create_timestamp_ns):
        candidates = threads_by_pthread.get(pthread_value, [])
        if create_timestamp_ns is not None:
            for rec in reversed(candidates):
                if rec.get("created_ns") == create_timestamp_ns:
                    return rec
        for rec in reversed(candidates):
            if rec.get("started_ns") is None:
                return rec
        rec = new_record(pthread_value=pthread_value)
        if create_timestamp_ns is not None:
            rec["created_ns"] = create_timestamp_ns
        return rec

    def find_latest_pthread_record(pthread_value, timestamp_ns=None):
        candidates = threads_by_pthread.get(pthread_value, [])
        if not candidates:
            return new_record(pthread_value=pthread_value)
        if timestamp_ns is None:
            return candidates[-1]
        eligible = [
            rec
            for rec in candidates
            if rec.get("created_ns") is None or rec.get("created_ns") <= timestamp_ns
        ]
        if not eligible:
            return candidates[-1]
        return eligible[-1]

    def find_join_record(pthread_value, timestamp_ns):
        candidates = threads_by_pthread.get(pthread_value, [])
        eligible = [
            rec
            for rec in candidates
            if not rec.get("joined_by")
            and (rec.get("created_ns") is None or rec.get("created_ns") <= timestamp_ns)
        ]
        if eligible:
            return eligible[-1]
        return find_latest_pthread_record(pthread_value, timestamp_ns)

    for event in events:
        timestamp_ns = event.get("timestamp_ns")
        if isinstance(timestamp_ns, int):
            trace_end_ns = timestamp_ns if trace_end_ns is None else max(trace_end_ns, timestamp_ns)

        if event.get("event") == "phase_marker":
            if event.get("name") == "process_start" and origin_ns is None:
                origin_ns = timestamp_ns
                main = record_for_tid(event.get("tid", ""))
                main["name"] = main["name"] or "main"
                main["started_ns"] = timestamp_ns
            phases.append(event)
            continue

        if event.get("event") == "pthread_create":
            pthread_value = event.get("pthread_value", "")
            rec = None
            for candidate in reversed(threads_by_pthread.get(pthread_value, [])):
                if candidate.get("created_ns") == timestamp_ns:
                    rec = candidate
                    break
            if rec is None:
                rec = new_record(pthread_value=pthread_value)
            rec["created_ns"] = timestamp_ns
            rec["parent_tid"] = event.get("caller_tid", "")
            rec["entry_address"] = event.get("entry_address", "")
            rec["entry_module"] = event.get("entry_module", "")
            rec["entry_symbol"] = event.get("entry_symbol", "")
            rec["create_result"] = event.get("result", "")
            continue

        if event.get("event") == "thread_start":
            pthread_value = event.get("pthread_value", "")
            rec = find_start_record(pthread_value, event.get("create_timestamp_ns"))
            rec.update(
                {
                    "parent_tid": event.get("parent_tid", rec.get("parent_tid", "")),
                    "created_ns": rec.get("created_ns") or event.get("create_timestamp_ns"),
                    "started_ns": timestamp_ns,
                    "name": event.get("name", "") or rec.get("name", ""),
                    "entry_address": event.get("entry_address", rec.get("entry_address", "")),
                    "entry_module": event.get("entry_module", rec.get("entry_module", "")),
                    "entry_symbol": event.get("entry_symbol", rec.get("entry_symbol", "")),
                }
            )
            bind_tid(rec, event.get("tid", ""))
            continue

        if event.get("event") == "thread_name":
            pthread_value = event.get("pthread_value", "")
            caller_tid = event.get("caller_tid", "")
            rec = threads_by_tid.get(caller_tid) if caller_tid != "" else None
            if rec is None:
                rec = find_latest_pthread_record(pthread_value, timestamp_ns)
            rec["name"] = event.get("name", "") or rec.get("name", "")
            bind_pthread(rec, pthread_value)
            if caller_tid != "" and (not rec.get("tid") or str(rec.get("tid")) == ""):
                bind_tid(rec, caller_tid)
            continue

        if event.get("event") == "thread_exit":
            rec = None
            if event.get("tid") in threads_by_tid:
                rec = threads_by_tid[event.get("tid")]
            elif event.get("pthread_value") in threads_by_pthread:
                rec = find_latest_pthread_record(event.get("pthread_value"), timestamp_ns)
            else:
                rec = record_for_tid(event.get("tid", ""))
            rec["exited_ns"] = timestamp_ns
            rec["exit_kind"] = event.get("exit_kind", "")
            rec["name"] = event.get("name", "") or rec.get("name", "")
            continue

        if event.get("event") == "pthread_join_end":
            rec = find_join_record(event.get("pthread_value", ""), timestamp_ns)
            rec["joined_by"] = event.get("caller_tid", "")
            continue

        if event.get("event") == "pthread_detach":
            rec = find_latest_pthread_record(event.get("pthread_value", ""), timestamp_ns)
            rec["detached_by"] = event.get("caller_tid", "")
            continue

    if origin_ns is None:
        first_timestamp = next((e.get("timestamp_ns") for e in events if isinstance(e.get("timestamp_ns"), int)), None)
        origin_ns = first_timestamp

    threads.sort(
        key=lambda rec: (
            rec.get("started_ns") is None,
            rec.get("started_ns") or rec.get("created_ns") or 0,
            str(rec.get("tid", "")),
        )
    )

    summary_csv = Path(summary_csv)
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    with summary_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tid",
                "pthread_value",
                "name",
                "parent_tid",
                "created_ms",
                "started_ms",
                "exited_ms",
                "observed_lifetime_ms",
                "status",
                "exit_kind",
                "joined_by",
                "detached_by",
                "entry_address",
                "entry_module",
                "entry_symbol",
                "create_result",
            ],
        )
        writer.writeheader()
        for rec in threads:
            start_ns = rec.get("started_ns") or rec.get("created_ns")
            end_ns = rec.get("exited_ns") or trace_end_ns
            writer.writerow(
                {
                    "tid": rec.get("tid", ""),
                    "pthread_value": rec.get("pthread_value", ""),
                    "name": rec.get("name", ""),
                    "parent_tid": rec.get("parent_tid", ""),
                    "created_ms": _ms(rec.get("created_ns"), origin_ns),
                    "started_ms": _ms(rec.get("started_ns"), origin_ns),
                    "exited_ms": _ms(rec.get("exited_ns"), origin_ns),
                    "observed_lifetime_ms": _duration_ms(start_ns, end_ns),
                    "status": "exited" if rec.get("exited_ns") else "still_alive_at_trace_end",
                    "exit_kind": rec.get("exit_kind", ""),
                    "joined_by": rec.get("joined_by", ""),
                    "detached_by": rec.get("detached_by", ""),
                    "entry_address": rec.get("entry_address", ""),
                    "entry_module": rec.get("entry_module", ""),
                    "entry_symbol": rec.get("entry_symbol", ""),
                    "create_result": rec.get("create_result", ""),
                }
            )

    return {
        "events": len(events),
        "threads": len(threads),
        "phases": len(phases),
        "summary_csv": str(summary_csv),
    }


def _thread_names_from_summary(summary_csv):
    names = {}
    path = Path(summary_csv)
    if not path.exists():
        return names
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("tid"):
                names[str(row["tid"])] = row.get("name", "")
    return names


def parse_perf_script(perf_script_path, summary_csv, output_csv):
    thread_names = _thread_names_from_summary(summary_csv)
    counts = defaultdict(int)
    totals = defaultdict(int)
    first_seen = {}
    last_seen = {}
    field_line_re = re.compile(
        r"^\s*(?P<comm>\S+)\s+(?P<tid>\d+)\s+(?P<time>\d+(?:\.\d+)?)\s+"
        r"(?P<ip>[0-9a-fA-F]+)\s+(?P<rest>.+?)\s*$"
    )
    header_re = re.compile(r"^\s*(?P<comm>\S+)\s+(?P<tid>\d+)\s+(?P<time>\d+(?:\.\d+)?):")
    frame_re = re.compile(r"^\s*(?P<ip>[0-9a-fA-F]+)\s+(?P<rest>.+?)\s*$")
    module_re = re.compile(r"^(?P<function>.+)\s+\((?P<module>[^)]+?)(?:\+0x[0-9a-fA-F]+)?\)$")

    def add_sample(tid, time_value, function, module, srcline):
        if not function or function in ("[unknown]", "0"):
            return
        source_file = ""
        source_line = ""
        srcline = (srcline or "").replace(" (inlined)", "")
        if srcline and srcline != "??:0" and ":" in srcline:
            source_file, source_line = srcline.rsplit(":", 1)
        key = (tid, function, module, source_file, source_line)
        counts[key] += 1
        totals[tid] += 1
        first_seen[key] = min(first_seen.get(key, time_value), time_value)
        last_seen[key] = max(last_seen.get(key, time_value), time_value)

    lines = Path(perf_script_path).read_text(encoding="utf-8", errors="replace").splitlines()
    current_tid = None
    current_time = None
    i = 0
    while i < len(lines):
        line = lines[i]

        field_match = field_line_re.match(line)
        if field_match and field_match.group("rest").split():
            tid = field_match.group("tid")
            time_value = field_match.group("time")
            rest = field_match.group("rest").split()
            if len(rest) >= 4:
                srcline = rest[-1]
                module = rest[-3]
                function = " ".join(rest[:-3])
                add_sample(tid, time_value, function, module, srcline)
            i += 1
            continue

        header_match = header_re.match(line)
        if header_match:
            current_tid = header_match.group("tid")
            current_time = header_match.group("time")
            i += 1
            continue

        frame_match = frame_re.match(line)
        if frame_match and current_tid is not None:
            rest = frame_match.group("rest")
            module = ""
            function = rest.strip()
            module_match = module_re.match(rest)
            if module_match:
                function = module_match.group("function").strip()
                module = module_match.group("module").strip()
            srcline = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if next_line and not header_re.match(lines[i + 1]) and not frame_re.match(lines[i + 1]):
                    srcline = next_line
                    i += 1
            add_sample(current_tid, current_time, function, module, srcline)
        i += 1

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tid",
                "thread_name",
                "function",
                "module",
                "source_file",
                "source_line",
                "sample_count",
                "sample_percentage",
                "first_seen_time",
                "last_seen_time",
            ],
        )
        writer.writeheader()
        for (tid, function, module, source_file, source_line), count in sorted(
            counts.items(), key=lambda item: (item[0][0], -item[1], item[0][1])
        ):
            total = totals.get(tid, 1)
            writer.writerow(
                {
                    "tid": tid,
                    "thread_name": thread_names.get(tid, ""),
                    "function": function,
                    "module": module,
                    "source_file": source_file,
                    "source_line": source_line,
                    "sample_count": count,
                    "sample_percentage": f"{100.0 * count / total:.3f}",
                    "first_seen_time": first_seen[(tid, function, module, source_file, source_line)],
                    "last_seen_time": last_seen[(tid, function, module, source_file, source_line)],
                }
            )

    return {"functions": len(counts), "samples": sum(counts.values()), "output_csv": str(output_csv)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_jsonl")
    parser.add_argument("--summary-csv", default="thread_summary.csv")
    parser.add_argument("--perf-script", default="")
    parser.add_argument("--function-summary-csv", default="thread_function_summary.csv")
    args = parser.parse_args()

    result = parse_trace_file(args.trace_jsonl, args.summary_csv)
    print(
        f"parsed events={result['events']} threads={result['threads']} "
        f"phases={result['phases']} summary={result['summary_csv']}"
    )
    if args.perf_script:
        perf_result = parse_perf_script(args.perf_script, args.summary_csv, args.function_summary_csv)
        print(
            f"parsed perf functions={perf_result['functions']} samples={perf_result['samples']} "
            f"summary={perf_result['output_csv']}"
        )


if __name__ == "__main__":
    main()
