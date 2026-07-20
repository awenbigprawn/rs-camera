#!/usr/bin/env python3

import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil
import subprocess
import sys


def parse_jsonl(path):
    events = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            event["_line_no"] = line_no
            events.append(event)
    return events


def parse_file_line(value):
    if not value or value == "??:0" or value == "??":
        return {"file": "", "line": "", "column": ""}
    file_part = value
    line = ""
    column = ""
    if ":" in value:
        file_part, line_part = value.rsplit(":", 1)
        if ":" in file_part and line_part.isdigit():
            file_part, maybe_line = file_part.rsplit(":", 1)
            line = maybe_line
            column = line_part
        else:
            line = line_part
    return {"file": file_part, "line": line, "column": column}


class Symbolizer:
    def __init__(self):
        self.llvm_symbolizer = shutil.which("llvm-symbolizer")
        self.addr2line = shutil.which("addr2line")
        self.cache = {}
        self.tool = "llvm-symbolizer" if self.llvm_symbolizer else "addr2line"
        if not self.llvm_symbolizer and not self.addr2line:
            raise RuntimeError("neither llvm-symbolizer nor addr2line is available")

    def symbolize(self, module, offset):
        if not module or not offset:
            return []
        key = (module, offset)
        if key in self.cache:
            return self.cache[key]

        if self.llvm_symbolizer:
            frames = self._symbolize_with_llvm(module, offset)
        else:
            frames = self._symbolize_with_addr2line(module, offset)
        self.cache[key] = frames
        return frames

    def _symbolize_with_llvm(self, module, offset):
        cmd = [
            self.llvm_symbolizer,
            "--inlines",
            "--demangle",
            "--functions=linkage",
            f"--obj={module}",
            offset,
        ]
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            return [
                {
                    "function": "",
                    "source_file": "",
                    "source_line": "",
                    "source_column": "",
                    "error": completed.stderr.strip(),
                }
            ]
        return self._parse_symbolizer_pairs(completed.stdout)

    def _symbolize_with_addr2line(self, module, offset):
        cmd = [self.addr2line, "-C", "-f", "-i", "-e", module, offset]
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            return [
                {
                    "function": "",
                    "source_file": "",
                    "source_line": "",
                    "source_column": "",
                    "error": completed.stderr.strip(),
                }
            ]
        return self._parse_symbolizer_pairs(completed.stdout)

    @staticmethod
    def _parse_symbolizer_pairs(output):
        lines = [line for line in output.splitlines() if line.strip()]
        frames = []
        i = 0
        while i + 1 < len(lines):
            function = lines[i].strip()
            location = parse_file_line(lines[i + 1].strip())
            frames.append(
                {
                    "function": "" if function == "??" else function,
                    "source_file": location["file"],
                    "source_line": location["line"],
                    "source_column": location["column"],
                }
            )
            i += 2
        return frames


def is_tracer_frame(frame):
    module = frame.get("module", "")
    return "libtrace_pthreads.so" in module or module.endswith("/libtrace_pthreads.so")


def is_system_cpp_frame(symbolized_frame):
    source_file = symbolized_frame.get("source_file", "")
    function = symbolized_frame.get("function", "")
    if source_file.startswith("/usr/include/c++/"):
        return True
    if source_file.startswith("/usr/include/"):
        return True
    if function.startswith("std::"):
        return True
    return False


def best_source_frame(symbolized_frames):
    if not symbolized_frames:
        return {}
    for frame in reversed(symbolized_frames):
        if frame.get("source_file") and not is_system_cpp_frame(frame):
            return frame
    for frame in reversed(symbolized_frames):
        if frame.get("function"):
            return frame
    return symbolized_frames[-1]


def infer_creator(stack_frames):
    fallback = {}
    for raw in stack_frames:
        if is_tracer_frame(raw):
            continue
        best = best_source_frame(raw.get("symbolized", []))
        if not best:
            continue
        module = raw.get("module", "")
        if "libstdc++" in module or "libgcc" in module:
            continue
        candidate = {
            "module": module,
            "module_offset": raw.get("module_offset", ""),
            "function": best.get("function", ""),
            "source_file": best.get("source_file", ""),
            "source_line": best.get("source_line", ""),
            "source_column": best.get("source_column", ""),
        }
        if not fallback:
            fallback = candidate
        if best.get("source_file") and best.get("source_file") != "??":
            return candidate
    return fallback


def infer_child_entry(stack_frames):
    for raw in stack_frames:
        symbolized = raw.get("symbolized", [])
        for index, frame in enumerate(symbolized):
            function = frame.get("function", "")
            if "<lambda" in function and "thread<" in function:
                source = frame
                for outer in symbolized[index + 1 :]:
                    if outer.get("source_file") and outer.get("source_file") != "??" and not is_system_cpp_frame(outer):
                        source = outer
                        break
                return {
                    "function": function,
                    "source_file": source.get("source_file", ""),
                    "source_line": source.get("source_line", ""),
                    "source_column": source.get("source_column", ""),
                    "confidence": "medium",
                    "reason": "std::thread inline frame at pthread_create call site",
                }
    return {}


def symbolize_event(event, symbolizer):
    event = dict(event)

    if event.get("entry_module") and event.get("entry_module_offset"):
        entry_frames = symbolizer.symbolize(event["entry_module"], event["entry_module_offset"])
        event["entry_symbolized"] = entry_frames
        best = best_source_frame(entry_frames)
        if best:
            event["entry_function"] = best.get("function", "")
            event["entry_source_file"] = best.get("source_file", "")
            event["entry_source_line"] = best.get("source_line", "")

    if "stack" in event:
        stack_symbolized = []
        filtered_stack = []
        for frame in event["stack"]:
            frame = dict(frame)
            if frame.get("module") and frame.get("module_offset"):
                frame["symbolized"] = symbolizer.symbolize(frame["module"], frame["module_offset"])
            else:
                frame["symbolized"] = []
            stack_symbolized.append(frame)
            if not is_tracer_frame(frame):
                filtered_stack.append(frame)
        event["stack_symbolized"] = stack_symbolized
        event["filtered_stack_symbolized"] = filtered_stack

        if event.get("event") == "pthread_create":
            event["creator"] = infer_creator(stack_symbolized)
            event["inferred_child_entry"] = infer_child_entry(stack_symbolized)

    return event


def symbolize_trace(input_trace, output_json):
    events = parse_jsonl(input_trace)
    symbolizer = Symbolizer()
    symbolized_events = [symbolize_event(event, symbolizer) for event in events]

    modules = defaultdict(int)
    for event in events:
        if event.get("entry_module"):
            modules[event["entry_module"]] += 1
        for frame in event.get("stack", []):
            if frame.get("module"):
                modules[frame["module"]] += 1

    output = {
        "metadata": {
            "input_trace": str(input_trace),
            "symbolizer": symbolizer.tool,
            "unique_symbolized_addresses": len(symbolizer.cache),
            "modules": dict(sorted(modules.items())),
        },
        "events": symbolized_events,
    }

    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return output


def main():
    parser = argparse.ArgumentParser(description="Symbolize a RealSense pthread JSONL trace.")
    parser.add_argument("trace_jsonl")
    parser.add_argument("--output", default="symbolized_thread_trace.json")
    args = parser.parse_args()

    output = symbolize_trace(args.trace_jsonl, args.output)
    print(
        f"symbolized events={len(output['events'])} "
        f"addresses={output['metadata']['unique_symbolized_addresses']} "
        f"tool={output['metadata']['symbolizer']} output={args.output}"
    )


if __name__ == "__main__":
    sys.exit(main())
