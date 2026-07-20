#!/usr/bin/env python3

import argparse
import csv
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import sys


CLASS_COLORS = {
    "startup-transient": "#7b8cff",
    "enumeration-transient": "#34a0a4",
    "pipeline-start-transient": "#f4a261",
    "steady-state": "#2a9d8f",
    "cleanup-transient": "#e76f51",
    "process-lifetime": "#6d6875",
    "unknown": "#8a8a8a",
}


PHASE_LABELS = {
    "after_context": "context created",
    "after_query_devices": "enumeration complete",
    "before_pipeline_start": "pipeline.start begin",
    "after_pipeline_start": "pipeline.start return",
    "first_frame": "first frame",
    "steady_state_begin": "steady state",
    "before_pipeline_stop": "pipeline.stop begin",
    "after_pipeline_stop": "pipeline.stop return",
    "before_process_exit": "process exit",
}


def read_jsonl(path):
    events = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def read_summary(path):
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def to_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def short_function(function):
    if not function:
        return ""
    head = function.split("(", 1)[0]
    if "::" in head:
        parts = head.split("::")
        if len(parts) >= 2:
            return "::".join(parts[-2:])
    return head[-80:]


def short_path(path, repo_root=None):
    if not path or path == "??":
        return ""
    p = Path(path)
    if repo_root:
        try:
            return str(p.relative_to(repo_root))
        except ValueError:
            pass
    return p.name


def phase_map(events):
    origin = None
    phases = {}
    for event in events:
        if event.get("event") != "phase_marker":
            continue
        if event.get("name") == "process_start":
            origin = event.get("timestamp_ns")
            break
    if origin is None:
        origin = min((event.get("timestamp_ns") for event in events if isinstance(event.get("timestamp_ns"), int)), default=0)
    for event in events:
        if event.get("event") == "phase_marker":
            phases[event["name"]] = (event.get("timestamp_ns", origin) - origin) / 1_000_000
    return origin, phases


def classify_thread(row, phases, trace_end_ms):
    tid = row.get("tid", "")
    if row.get("parent_tid", "") == "" and to_float(row.get("started_ms"), 0) == 0:
        return "process-lifetime"

    created = to_float(row.get("created_ms"), to_float(row.get("started_ms"), 0))
    started = to_float(row.get("started_ms"), created)
    exited = to_float(row.get("exited_ms"), None)
    lifetime = to_float(row.get("observed_lifetime_ms"), 0)

    def p(name, default=None):
        return phases.get(name, default)

    if exited is None or exited >= p("before_process_exit", trace_end_ms + 1):
        return "process-lifetime"
    if created is not None and created >= p("before_pipeline_stop", trace_end_ms + 1):
        return "cleanup-transient"
    if exited >= p("before_pipeline_stop", trace_end_ms + 1) and started <= p("steady_state_begin", trace_end_ms):
        return "steady-state"
    if created <= p("after_context", -1) and exited <= p("after_context", -1) and lifetime < 1000:
        return "startup-transient"
    if p("before_query_devices", -1) <= created <= p("after_query_devices", -1):
        return "enumeration-transient"
    if p("before_pipeline_start", -1) <= created <= p("after_pipeline_start", -1):
        return "pipeline-start-transient"
    if p("before_pipeline_stop", trace_end_ms + 1) <= exited <= p("after_object_destruction", trace_end_ms + 1):
        return "cleanup-transient"
    return "unknown"


def load_create_metadata(symbolized_path, origin_ns):
    if not symbolized_path or not Path(symbolized_path).exists():
        return {}
    with Path(symbolized_path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    by_created_ms = {}
    for event in data.get("events", []):
        if event.get("event") != "pthread_create":
            continue
        created_ms = f"{(event.get('timestamp_ns', origin_ns) - origin_ns) / 1_000_000:.3f}"
        creator = event.get("creator", {})
        entry = event.get("inferred_child_entry") or {}
        if not entry:
            entry = {
                "function": event.get("entry_function", ""),
                "source_file": event.get("entry_source_file", ""),
                "source_line": event.get("entry_source_line", ""),
            }
        by_created_ms[created_ms] = {
            "creator": creator,
            "entry": entry,
            "stack": event.get("filtered_stack_symbolized", []),
        }
    return by_created_ms


def svg_text(x, y, text, size=11, anchor="start", klass="", fill="#222", extra=""):
    cls = f' class="{klass}"' if klass else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" '
        f'fill="{fill}"{cls} {extra}>{escape(text)}</text>'
    )


def render_svg(trace_jsonl, summary_csv, symbolized_json, output_svg, repo_root=None):
    events = read_jsonl(trace_jsonl)
    rows = read_summary(summary_csv)
    origin_ns, phases = phase_map(events)
    trace_end_ms = max(
        [(event.get("timestamp_ns", origin_ns) - origin_ns) / 1_000_000 for event in events if isinstance(event.get("timestamp_ns"), int)]
        or [0]
    )
    metadata = load_create_metadata(symbolized_json, origin_ns)

    repo_root_path = Path(repo_root).resolve() if repo_root else None
    left = 390
    right = 80
    top = 130
    row_h = 34
    bottom = 80
    timeline_w = 1750
    scale = timeline_w / max(trace_end_ms, 1)
    width = left + timeline_w + right
    height = top + row_h * max(len(rows), 1) + bottom

    def x(ms):
        return left + ms * scale

    row_y = {}
    for index, row in enumerate(rows):
        row_y[row.get("tid", "")] = top + index * row_h

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}">'
    )
    out.append(
        "<style>"
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
        ".axis{stroke:#222;stroke-width:1}.grid{stroke:#ddd;stroke-width:1}.phase{stroke:#b44;stroke-width:1;stroke-dasharray:4 4}"
        ".connector{stroke:#777;stroke-width:1.2;fill:none}.thread{stroke-width:4;stroke-linecap:round}"
        ".thread-bg{stroke:#fff;stroke-width:7;stroke-linecap:round}.tick{stroke:#222;stroke-width:1}"
        "</style>"
    )
    out.append('<rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>')
    out.append(svg_text(20, 28, "RealSense pthread lifecycle timeline", 18))
    out.append(svg_text(left, 52, "time since process_start (ms)", 12))

    # Time axis and grid.
    out.append(f'<line x1="{left}" y1="{top - 28}" x2="{left + timeline_w}" y2="{top - 28}" class="axis"/>')
    tick_step = 1000
    if trace_end_ms < 3000:
        tick_step = 250
    elif trace_end_ms < 8000:
        tick_step = 500
    tick = 0
    while tick <= trace_end_ms + 0.001:
        tx = x(tick)
        out.append(f'<line x1="{tx:.1f}" y1="{top - 33}" x2="{tx:.1f}" y2="{height - bottom + 12}" class="grid"/>')
        out.append(f'<line x1="{tx:.1f}" y1="{top - 33}" x2="{tx:.1f}" y2="{top - 23}" class="tick"/>')
        out.append(svg_text(tx, top - 40, f"{tick:.0f}", 10, "middle"))
        tick += tick_step

    # Phase markers.
    phase_items = [
        (name, label, x(phases[name]))
        for name, label in PHASE_LABELS.items()
        if name in phases
    ]
    phase_items.sort(key=lambda item: item[2])
    label_levels = []
    for name, label, px in phase_items:
        level = 0
        while level < len(label_levels) and px - label_levels[level] < 135:
            level += 1
        if level == len(label_levels):
            label_levels.append(px)
        else:
            label_levels[level] = px
        label_y = top - 100 + level * 13
        out.append(f'<line x1="{px:.1f}" y1="{top - 62}" x2="{px:.1f}" y2="{height - bottom + 16}" class="phase"/>')
        out.append(f'<line x1="{px:.1f}" y1="{top - 62}" x2="{px + 3:.1f}" y2="{label_y + 3:.1f}" stroke="#b44" stroke-width="0.8"/>')
        out.append(svg_text(px + 5, label_y + 3, label, 10, "start", fill="#8b2d2d"))

    # Row labels.
    out.append(svg_text(18, top - 10, "TID / name / parent / observed lifetime / class", 11, fill="#444"))
    for index, row in enumerate(rows):
        y = top + index * row_h
        cls = classify_thread(row, phases, trace_end_ms)
        row["lifecycle_class"] = cls
        if index % 2:
            out.append(f'<rect x="0" y="{y - 16:.1f}" width="{width:.0f}" height="{row_h}" fill="#fafafa"/>')
        label = (
            f'{row.get("tid") or "unknown"}  {row.get("name") or "-"}  '
            f'parent={row.get("parent_tid") or "-"}  '
            f'{row.get("observed_lifetime_ms") or "-"} ms  {cls}'
        )
        out.append(svg_text(18, y + 4, label[:78], 10.5, fill="#222"))

    # Parent-child connectors.
    for row in rows:
        parent = row.get("parent_tid", "")
        child = row.get("tid", "")
        if not parent or parent not in row_y or child not in row_y:
            continue
        created = to_float(row.get("created_ms"))
        started = to_float(row.get("started_ms"), created)
        if created is None:
            continue
        cx = x(created)
        py = row_y[parent]
        cy = row_y[child]
        out.append(f'<path d="M {cx:.1f} {py:.1f} V {cy:.1f}" class="connector"/>')
        if started is not None and abs(started - created) > 0.02:
            sx = x(started)
            out.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{sx:.1f}" y2="{cy:.1f}" stroke="#999" stroke-width="1" stroke-dasharray="2 2"/>')

    # Thread lifelines.
    for row in rows:
        y = row_y.get(row.get("tid", ""))
        if y is None:
            continue
        cls = row.get("lifecycle_class") or classify_thread(row, phases, trace_end_ms)
        color = CLASS_COLORS.get(cls, CLASS_COLORS["unknown"])
        start = to_float(row.get("started_ms"), to_float(row.get("created_ms"), 0))
        end = to_float(row.get("exited_ms"), trace_end_ms)
        sx = x(start)
        ex = x(end)
        if ex < sx:
            ex = sx
        created_key = row.get("created_ms", "")
        meta = metadata.get(created_key, {})
        creator = meta.get("creator", {})
        entry = meta.get("entry", {})
        creator_label = ""
        if creator:
            creator_label = (
                f'{short_function(creator.get("function", ""))}() '
                f'{short_path(creator.get("source_file", ""), repo_root_path)}:{creator.get("source_line", "")}'
            ).strip()
        entry_label = ""
        if entry:
            entry_label = (
                f'{short_function(entry.get("function", ""))}() '
                f'{short_path(entry.get("source_file", ""), repo_root_path)}:{entry.get("source_line", "")}'
            ).strip()

        tooltip = [
            f'TID: {row.get("tid", "")}',
            f'pthread: {row.get("pthread_value", "")}',
            f'name: {row.get("name", "")}',
            f'parent TID: {row.get("parent_tid", "")}',
            f'created_ms: {row.get("created_ms", "")}',
            f'started_ms: {row.get("started_ms", "")}',
            f'exited_ms: {row.get("exited_ms", "")}',
            f'observed_lifetime_ms: {row.get("observed_lifetime_ms", "")}',
            f'status: {row.get("status", "")}',
            f'joined_by: {row.get("joined_by", "")}',
            f'detached_by: {row.get("detached_by", "")}',
            f'lifecycle_class: {cls}',
            f'creator: {creator_label}',
            f'entry: {entry_label}',
        ]
        out.append("<g>")
        out.append(f"<title>{escape(chr(10).join(tooltip))}</title>")
        out.append(f'<line x1="{sx:.1f}" y1="{y:.1f}" x2="{ex:.1f}" y2="{y:.1f}" class="thread-bg"/>')
        out.append(f'<line x1="{sx:.1f}" y1="{y:.1f}" x2="{ex:.1f}" y2="{y:.1f}" class="thread" stroke="{color}"/>')
        out.append(f'<circle cx="{sx:.1f}" cy="{y:.1f}" r="3.2" fill="{color}" stroke="#222" stroke-width="0.5"/>')
        if row.get("status") == "exited":
            out.append(f'<circle cx="{ex:.1f}" cy="{y:.1f}" r="3.2" fill="#fff" stroke="{color}" stroke-width="2"/>')
        else:
            out.append(f'<path d="M {ex:.1f} {y - 4:.1f} L {ex + 8:.1f} {y:.1f} L {ex:.1f} {y + 4:.1f} Z" fill="{color}"/>')
        if ex - sx < 4:
            out.append(f'<rect x="{sx - 3:.1f}" y="{y - 7:.1f}" width="6" height="14" fill="none" stroke="{color}" stroke-width="1.5"/>')
        if creator_label:
            cx = x(to_float(row.get("created_ms"), start))
            out.append(svg_text(cx + 4, y - 7, creator_label[:70], 9, fill="#444"))
        if entry_label and entry_label != creator_label:
            out.append(svg_text(sx + 4, y + 14, entry_label[:70], 9, fill="#555"))
        out.append("</g>")

    # Legend.
    legend_x = left
    legend_y = height - 38
    out.append(svg_text(18, legend_y, "Lifecycle classes:", 10.5, fill="#444"))
    lx = 130
    for cls, color in CLASS_COLORS.items():
        out.append(f'<rect x="{lx}" y="{legend_y - 10}" width="12" height="12" fill="{color}"/>')
        out.append(svg_text(lx + 16, legend_y, cls, 10, fill="#333"))
        lx += 145

    out.append("</svg>")
    output_svg = Path(output_svg)
    output_svg.parent.mkdir(parents=True, exist_ok=True)
    output_svg.write_text("\n".join(out), encoding="utf-8")
    return output_svg


def write_html(svg_path, html_path):
    svg_text_content = Path(svg_path).read_text(encoding="utf-8")
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>RealSense Thread Timeline</title>"
        "<style>body{margin:0;padding:16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f7f8}"
        ".wrap{background:white;border:1px solid #ddd;overflow:auto;padding:12px}</style></head>"
        "<body><div class=\"wrap\">"
        f"{svg_text_content}"
        "</div></body></html>\n"
    )
    Path(html_path).write_text(html, encoding="utf-8")


def write_png(svg_path, png_path):
    convert = shutil.which("convert")
    if not convert:
        return False
    completed = subprocess.run([convert, str(svg_path), str(png_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return completed.returncode == 0


def render_outputs(trace_jsonl, summary_csv, symbolized_json, output_dir, repo_root=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / "thread_timeline.svg"
    html_path = output_dir / "thread_timeline.html"
    png_path = output_dir / "thread_timeline.png"
    render_svg(trace_jsonl, summary_csv, symbolized_json, svg_path, repo_root=repo_root)
    write_html(svg_path, html_path)
    png_written = write_png(svg_path, png_path)
    return {"svg": str(svg_path), "html": str(html_path), "png": str(png_path) if png_written else ""}


def main():
    parser = argparse.ArgumentParser(description="Render a horizontal pthread tree timeline.")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--symbolized", default="")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--repo-root", default=str(Path.cwd()))
    args = parser.parse_args()
    result = render_outputs(args.trace, args.summary, args.symbolized, args.output_dir, repo_root=args.repo_root)
    print(f"svg={result['svg']}")
    print(f"html={result['html']}")
    if result["png"]:
        print(f"png={result['png']}")
    else:
        print("png=")


if __name__ == "__main__":
    sys.exit(main())
