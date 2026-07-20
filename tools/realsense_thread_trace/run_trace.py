#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import parse_trace
import render_timeline
import symbolize


SCRIPT_DIR = Path(__file__).resolve().parent


def build_tracer(output_dir):
    compiler = os.environ.get("CC", "cc")
    tracer = output_dir / "libtrace_pthreads.so"
    source = SCRIPT_DIR / "trace_pthreads.c"
    cmd = [
        compiler,
        "-shared",
        "-fPIC",
        "-g",
        "-O2",
        "-fno-omit-frame-pointer",
        "-Wall",
        "-Wextra",
        "-o",
        str(tracer),
        str(source),
        "-ldl",
        "-pthread",
    ]
    subprocess.check_call(cmd)
    return tracer


def resolve_executable(command):
    exe = command[0]
    if "/" in exe:
        return str(Path(exe).resolve())
    found = shutil.which(exe)
    return found or exe


def run_ldd(executable, output_dir):
    ldd_path = output_dir / "ldd.txt"
    with ldd_path.open("w", encoding="utf-8") as handle:
        subprocess.run(["ldd", executable], stdout=handle, stderr=subprocess.STDOUT, check=False)
    return ldd_path


def run_once(args, command, run_index=1):
    output_dir = Path(args.output)
    if args.repeat > 1:
        output_dir = output_dir / f"run_{run_index:02d}"
    output_dir.mkdir(parents=True, exist_ok=True)

    tracer = Path(args.tracer).resolve() if args.tracer else build_tracer(output_dir)
    trace_path = output_dir / "thread_trace.jsonl"
    if trace_path.exists():
        trace_path.unlink()

    executable = resolve_executable(command)
    ldd_path = run_ldd(executable, output_dir)

    env = os.environ.copy()
    old_preload = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = str(tracer) if not old_preload else f"{tracer}:{old_preload}"
    env["RS_THREAD_TRACE_FILE"] = str(trace_path)

    stdout_path = output_dir / "app_stdout.txt"
    stderr_path = output_dir / "app_stderr.txt"
    perf_data = output_dir / "perf.data"
    perf_script = output_dir / "perf.script"
    perf_stdout_path = output_dir / "perf_stdout.txt"
    perf_stderr_path = output_dir / "perf_stderr.txt"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        app = subprocess.Popen(command, env=env, stdout=stdout, stderr=stderr)
        perf_proc = None
        if args.perf:
            if args.perf_attach_delay > 0:
                time.sleep(args.perf_attach_delay)
            perf_cmd = [
                "perf",
                "record",
                "-F",
                str(args.perf_frequency),
                "-g",
                "--call-graph",
                "dwarf",
                "-o",
                str(perf_data),
                "-p",
                str(app.pid),
            ]
            with perf_stdout_path.open("w", encoding="utf-8") as perf_stdout, perf_stderr_path.open("w", encoding="utf-8") as perf_stderr:
                try:
                    perf_proc = subprocess.Popen(perf_cmd, stdout=perf_stdout, stderr=perf_stderr)
                    time.sleep(0.1)
                except FileNotFoundError:
                    perf_proc = None

        returncode = app.wait()
        if perf_proc is not None:
            if perf_proc.poll() is None:
                perf_proc.send_signal(signal.SIGINT)
            try:
                perf_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                perf_proc.terminate()
                try:
                    perf_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    perf_proc.kill()
                    perf_proc.wait()

    summary_csv = output_dir / "thread_summary.csv"
    parse_result = parse_trace.parse_trace_file(trace_path, summary_csv)
    perf_result = None
    if args.perf and perf_data.exists():
        script_cmd = [
            "perf",
            "script",
            "-F",
            "comm,tid,time,ip,sym,dso,dsoff,srcline",
            "-i",
            str(perf_data),
        ]
        with perf_script.open("w", encoding="utf-8") as script_out:
            subprocess.run(script_cmd, stdout=script_out, stderr=subprocess.STDOUT, check=False)
        thread_function_summary = output_dir / "thread_function_summary.csv"
        perf_result = parse_trace.parse_perf_script(perf_script, summary_csv, thread_function_summary)
    symbolized_json = output_dir / "symbolized_thread_trace.json"
    symbolized_result = None
    if not args.no_symbolize:
        symbolized_result = symbolize.symbolize_trace(trace_path, symbolized_json)
    render_result = None
    if not args.no_render:
        render_result = render_timeline.render_outputs(
            trace_path,
            summary_csv,
            symbolized_json if symbolized_json.exists() else "",
            output_dir,
            repo_root=Path.cwd(),
        )

    print(f"run {run_index}: command exit code {returncode}")
    print(f"run {run_index}: tracer {tracer}")
    print(f"run {run_index}: ldd {ldd_path}")
    print(f"run {run_index}: trace {trace_path}")
    print(f"run {run_index}: parsed events={parse_result['events']} threads={parse_result['threads']} phases={parse_result['phases']}")
    print(f"run {run_index}: summary {summary_csv}")
    if perf_result is not None:
        print(f"run {run_index}: perf functions={perf_result['functions']} samples={perf_result['samples']} summary={perf_result['output_csv']}")
    if symbolized_result is not None:
        print(
            f"run {run_index}: symbolized {symbolized_json} "
            f"addresses={symbolized_result['metadata']['unique_symbolized_addresses']}"
        )
    if render_result is not None:
        print(f"run {run_index}: timeline svg={render_result['svg']} html={render_result['html']} png={render_result['png']}")

    return returncode


def main():
    parser = argparse.ArgumentParser(
        description="Run a command under the RealSense pthread lifecycle tracer."
    )
    parser.add_argument("--output", default=str(SCRIPT_DIR / "output"))
    parser.add_argument("--tracer", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--perf", action="store_true")
    parser.add_argument("--perf-frequency", type=int, default=499)
    parser.add_argument("--perf-attach-delay", type=float, default=0.0)
    parser.add_argument("--no-symbolize", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")

    exit_code = 0
    for run_index in range(1, args.repeat + 1):
        run_exit = run_once(args, command, run_index)
        if run_exit != 0 and exit_code == 0:
            exit_code = run_exit

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
