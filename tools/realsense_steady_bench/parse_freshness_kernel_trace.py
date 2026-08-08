#!/usr/bin/env python3
"""Localize raw V4L2 sequence gaps across UVC, VB2, and xHCI."""

from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List

from parse_v4l2_diagnostic_trace import phase_window


LINE = re.compile(
    r"^\s*(?P<task>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\].*?"
    r"\s(?P<seconds>\d+\.\d+):\s+(?P<event>[\w-]+(?::[\w-]+)?):\s*(?P<fields>.*)$"
)


def trace_report_lines(trace_path: Path) -> Iterator[str]:
    import subprocess

    process = subprocess.Popen(
        ["trace-cmd", "report", "-i", str(trace_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    yield from process.stdout
    stderr = process.stderr.read() if process.stderr is not None else ""
    returncode = process.wait()
    if returncode:
        raise subprocess.CalledProcessError(returncode, process.args, stderr=stderr)


def field_int(text: str, name: str, default: int = 0) -> int:
    match = re.search(
        rf"(?:^|[\s,]){re.escape(name)}\s*=\s*(-?(?:0x)?[0-9a-fA-F]+)",
        text,
    )
    return int(match.group(1), 0) if match else default


def is_video_capture(fields: str) -> bool:
    return bool(re.search(r"(?:^|[\s,])type\s*=\s*VIDEO_CAPTURE(?:[\s,]|$)", fields))


def load_csv(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: List[Dict[str, Any]], default_fields: Iterable[str]) -> None:
    fields = list(rows[0]) if rows else list(default_fields)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def discontinuities(
    rows: List[Dict[str, Any]], key_name: str
) -> List[Dict[str, Any]]:
    previous: Dict[int, int] = {}
    gaps: List[Dict[str, Any]] = []
    for row in rows:
        key = int(row[key_name])
        sequence = int(row["sequence"])
        last = previous.get(key)
        if last is not None and sequence != last + 1:
            gaps.append(
                {
                    **row,
                    "previous_sequence": last,
                    "sequence_delta": sequence - last,
                    "missing_frames": max(0, sequence - last - 1),
                    "out_of_order": sequence <= last,
                }
            )
        previous[key] = sequence
    return gaps


def nearest(
    rows: List[Dict[str, Any]],
    timestamps: List[int],
    timestamp_ns: int,
    radius_ns: int,
    sequence: int | None = None,
) -> Dict[str, Any] | None:
    begin = bisect.bisect_left(timestamps, timestamp_ns - radius_ns)
    end = bisect.bisect_right(timestamps, timestamp_ns + radius_ns)
    candidates = rows[begin:end]
    if sequence is not None:
        exact = [row for row in candidates if int(row.get("sequence", -1)) == sequence]
        if exact:
            candidates = exact
    return min(
        candidates,
        key=lambda row: abs(int(row["timestamp_ns"]) - timestamp_ns),
        default=None,
    )


def sparse_event(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp_ns": int(row["timestamp_ns"]),
        "delta_ms": float(row.get("delta_ms", 0.0)),
        "event": row.get("event", ""),
        "cpu": int(row.get("cpu", 0)),
        "context_tid": int(row.get("context_tid", 0)),
        "sequence": int(row.get("sequence", 0)),
        "minor": int(row.get("minor", -1)),
        "queue": int(row.get("queue", 0)),
        "error": int(row.get("error", 0)),
        "prior_error": int(row.get("prior_error", 0)),
        "bytesused": int(row.get("bytesused", 0)),
        "expected": int(row.get("expected", 0)),
        "frame_invalid_headers": int(row.get("frame_invalid_headers", 0)),
        "frame_header_errors": int(row.get("frame_header_errors", 0)),
        "validation_cause": row.get("validation_cause", ""),
        "queued": int(row.get("queued", -1)),
        "owned_by_drv": int(row.get("owned_by_drv", -1)),
        "fields": row.get("fields", ""),
    }


def parse_trace(
    trace_path: Path,
    lifecycle_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    steady_begin, steady_end = phase_window(lifecycle_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    kernel_dqbuf: List[Dict[str, Any]] = []
    uvc_ready: List[Dict[str, Any]] = []
    uvc_validations: List[Dict[str, Any]] = []
    uvc_errors: List[Dict[str, Any]] = []
    no_buffer: List[Dict[str, Any]] = []
    xhci_errors: List[Dict[str, Any]] = []
    vb2_events: List[Dict[str, Any]] = []
    event_counts: Counter[str] = Counter()

    for line in trace_report_lines(trace_path):
        match = LINE.match(line)
        if not match:
            continue
        timestamp_ns = round(float(match.group("seconds")) * 1_000_000_000)
        event = match.group("event").rsplit(":", 1)[-1]
        fields = match.group("fields")
        event_counts[event] += 1
        base = {
            "timestamp_ns": timestamp_ns,
            "time_from_steady_begin_ms": (timestamp_ns - steady_begin) / 1_000_000,
            "in_steady_state": steady_begin <= timestamp_ns <= steady_end,
            "event": event,
            "task": match.group("task").strip(),
            "context_tid": int(match.group("pid")),
            "cpu": int(match.group("cpu")),
            "fields": fields,
        }
        if event == "v4l2_dqbuf" and is_video_capture(fields):
            kernel_dqbuf.append(
                {
                    **base,
                    "minor": field_int(fields, "minor", -1),
                    "sequence": field_int(fields, "sequence"),
                }
            )
        elif event == "uvc_frame_ready":
            uvc_ready.append(
                {
                    **base,
                    "queue": field_int(fields, "queue"),
                    "sequence": field_int(fields, "sequence"),
                    "state": field_int(fields, "state"),
                    "error": field_int(fields, "error"),
                    "bytesused": field_int(fields, "bytesused"),
                }
            )
        elif event == "uvc_buffer_error":
            uvc_errors.append(
                {
                    **base,
                    "sequence": field_int(fields, "sequence"),
                    "state": field_int(fields, "state"),
                    "error": field_int(fields, "error"),
                    "bytesused": field_int(fields, "bytesused"),
                }
            )
        elif event == "uvc_frame_validation":
            prior_error = field_int(fields, "prior_error")
            bytesused = field_int(fields, "bytesused")
            expected = field_int(fields, "expected")
            header_errors = field_int(fields, "frame_header_errors")
            invalid_headers = field_int(fields, "frame_invalid_headers")
            if not prior_error and bytesused == expected:
                continue
            if bytesused > expected:
                cause = "uvc_frame_overflow"
            elif prior_error and header_errors:
                cause = "uvc_payload_error_flag"
            elif prior_error and bytesused < expected:
                cause = "uvc_iso_packet_error"
            elif invalid_headers:
                cause = "uvc_invalid_payload_header"
            elif bytesused < expected:
                cause = "uvc_short_frame"
            else:
                cause = "uvc_preexisting_buffer_error"
            uvc_validations.append(
                {
                    **base,
                    "buf": field_int(fields, "buf"),
                    "sequence": field_int(fields, "sequence"),
                    "prior_error": prior_error,
                    "bytesused": bytesused,
                    "expected": expected,
                    "frame_invalid_headers": invalid_headers,
                    "frame_header_errors": header_errors,
                    "validation_cause": cause,
                }
            )
        elif event == "uvc_no_video_buffer":
            no_buffer.append({**base, "ret": field_int(fields, "ret")})
        elif event == "xhci_urb_giveback":
            xhci_errors.append(
                {
                    **base,
                    "status": field_int(fields, "status"),
                    "actual": field_int(fields, "actual"),
                    "length": field_int(fields, "length"),
                    "epnum": field_int(fields, "epnum"),
                    "slot_id": field_int(fields, "slot"),
                }
            )
        elif event in {"vb2_buf_done", "vb2_dqbuf", "vb2_qbuf"}:
            vb2_events.append(
                {
                    **base,
                    "queued": field_int(fields, "queued", -1),
                    "owned_by_drv": field_int(fields, "owned_by_drv", -1),
                    "index": field_int(fields, "index", -1),
                    "bytesused": field_int(fields, "bytesused"),
                }
            )

    for rows in (
        kernel_dqbuf,
        uvc_ready,
        uvc_validations,
        uvc_errors,
        no_buffer,
        xhci_errors,
        vb2_events,
    ):
        rows.sort(key=lambda row: int(row["timestamp_ns"]))

    steady_kernel_dqbuf = [row for row in kernel_dqbuf if row["in_steady_state"]]
    steady_uvc_ready = [row for row in uvc_ready if row["in_steady_state"]]
    kernel_gaps = discontinuities(steady_kernel_dqbuf, "minor")
    uvc_gaps = discontinuities(steady_uvc_ready, "queue")

    raw_gaps = load_csv(output_dir / "v4l2_sequence_gaps.csv")
    raw_gaps.sort(key=lambda row: int(row["timestamp_ns"]))
    lookup_sets = {
        "kernel": (kernel_gaps, [int(row["timestamp_ns"]) for row in kernel_gaps]),
        "uvc": (uvc_gaps, [int(row["timestamp_ns"]) for row in uvc_gaps]),
        "validation": (
            uvc_validations,
            [int(row["timestamp_ns"]) for row in uvc_validations],
        ),
        "uvc_error": (uvc_errors, [int(row["timestamp_ns"]) for row in uvc_errors]),
        "no_buffer": (no_buffer, [int(row["timestamp_ns"]) for row in no_buffer]),
        "xhci": (xhci_errors, [int(row["timestamp_ns"]) for row in xhci_errors]),
        "vb2": (vb2_events, [int(row["timestamp_ns"]) for row in vb2_events]),
    }

    correlations: List[Dict[str, Any]] = []
    classifications: Counter[str] = Counter()
    for raw in raw_gaps:
        timestamp_ns = int(raw["timestamp_ns"])
        sequence = int(raw["sequence"])
        missing_sequence = int(raw["previous_sequence"]) + 1
        kernel = nearest(*lookup_sets["kernel"], timestamp_ns, 20_000_000, sequence)
        uvc = nearest(*lookup_sets["uvc"], timestamp_ns, 20_000_000, sequence)
        validation = nearest(
            *lookup_sets["validation"],
            timestamp_ns,
            100_000_000,
            missing_sequence,
        )
        uvc_error = nearest(*lookup_sets["uvc_error"], timestamp_ns, 50_000_000)
        empty = nearest(*lookup_sets["no_buffer"], timestamp_ns, 50_000_000)
        xhci = nearest(*lookup_sets["xhci"], timestamp_ns, 50_000_000)
        vb2 = nearest(*lookup_sets["vb2"], timestamp_ns, 20_000_000)

        if validation is not None:
            classification = str(validation["validation_cause"])
        elif uvc_error is not None:
            classification = "uvc_corrupted_buffer"
        elif empty is not None:
            classification = "uvc_buffer_starvation"
        elif xhci is not None:
            classification = "xhci_urb_error"
        elif uvc is not None:
            classification = "uvc_or_usb_before_frame_ready"
        elif kernel is not None:
            classification = "between_uvc_ready_and_v4l2_dqbuf"
        else:
            classification = "between_kernel_dqbuf_and_librealsense"
        classifications[classification] += 1

        def delta(event: Dict[str, Any] | None) -> float | str:
            return (
                (int(event["timestamp_ns"]) - timestamp_ns) / 1_000_000
                if event is not None
                else ""
            )

        correlations.append(
            {
                **raw,
                "classification": classification,
                "kernel_dqbuf_gap": kernel is not None,
                "kernel_dqbuf_delta_ms": delta(kernel),
                "kernel_minor": kernel.get("minor", "") if kernel else "",
                "uvc_ready_gap": uvc is not None,
                "uvc_ready_delta_ms": delta(uvc),
                "uvc_queue": hex(int(uvc["queue"])) if uvc else "",
                "uvc_validation": validation is not None,
                "uvc_validation_delta_ms": delta(validation),
                "uvc_validation_sequence": (
                    validation.get("sequence", "") if validation else ""
                ),
                "uvc_validation_prior_error": (
                    validation.get("prior_error", "") if validation else ""
                ),
                "uvc_validation_bytesused": (
                    validation.get("bytesused", "") if validation else ""
                ),
                "uvc_validation_expected": (
                    validation.get("expected", "") if validation else ""
                ),
                "uvc_validation_header_errors": (
                    validation.get("frame_header_errors", "")
                    if validation
                    else ""
                ),
                "uvc_validation_invalid_headers": (
                    validation.get("frame_invalid_headers", "")
                    if validation
                    else ""
                ),
                "uvc_error": uvc_error is not None,
                "uvc_error_delta_ms": delta(uvc_error),
                "uvc_no_buffer": empty is not None,
                "uvc_no_buffer_delta_ms": delta(empty),
                "xhci_error": xhci is not None,
                "xhci_error_delta_ms": delta(xhci),
                "nearest_vb2_event": vb2.get("event", "") if vb2 else "",
                "nearest_vb2_delta_ms": delta(vb2),
                "nearest_vb2_queued": vb2.get("queued", "") if vb2 else "",
                "nearest_vb2_owned_by_drv": (
                    vb2.get("owned_by_drv", "") if vb2 else ""
                ),
            }
        )

    write_csv(
        output_dir / "kernel_v4l2_dqbuf.csv",
        [sparse_event(row) for row in kernel_dqbuf],
        ("timestamp_ns", "event", "minor", "sequence"),
    )
    write_csv(
        output_dir / "kernel_uvc_frame_ready.csv",
        [sparse_event(row) for row in uvc_ready],
        ("timestamp_ns", "event", "queue", "sequence"),
    )
    write_csv(
        output_dir / "kernel_freshness_exception_events.csv",
        [sparse_event(row) for row in sorted(
            [*uvc_validations, *uvc_errors, *no_buffer, *xhci_errors],
            key=lambda row: int(row["timestamp_ns"]),
        )],
        ("timestamp_ns", "event"),
    )
    write_csv(
        output_dir / "freshness_path_correlations.csv",
        correlations,
        ("timestamp_ns", "classification"),
    )

    summary = {
        "schema_version": 1,
        "event_counts": dict(sorted(event_counts.items())),
        "steady_kernel_v4l2_dqbuf_gap_count": len(kernel_gaps),
        "steady_kernel_v4l2_missing_frame_count": sum(
            int(row["missing_frames"]) for row in kernel_gaps
        ),
        "steady_uvc_frame_ready_gap_count": len(uvc_gaps),
        "steady_uvc_missing_frame_count": sum(
            int(row["missing_frames"]) for row in uvc_gaps
        ),
        "uvc_corrupted_buffer_event_count": len(uvc_errors),
        "uvc_frame_validation_event_count": len(uvc_validations),
        "uvc_frame_validation_cause_counts": dict(
            sorted(Counter(row["validation_cause"] for row in uvc_validations).items())
        ),
        "uvc_no_video_buffer_event_count": len(no_buffer),
        "xhci_error_giveback_event_count": len(xhci_errors),
        "raw_librealsense_gap_count": len(raw_gaps),
        "correlated_raw_gap_count": len(correlations),
        "classification_counts": dict(sorted(classifications.items())),
        "correlations": correlations,
    }
    (output_dir / "freshness_kernel_trace_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--lifecycle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(parse_trace(args.trace, args.lifecycle, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
