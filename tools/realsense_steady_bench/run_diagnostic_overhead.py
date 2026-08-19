#!/usr/bin/env python3
"""Measure the current librealsense diagnostic-marker overhead on a D435."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import time
from typing import Any


MODE_ORDERS = (
    ("off", "build-only", "full"),
    ("build-only", "full", "off"),
    ("full", "off", "build-only"),
)


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("[RUN]", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return values
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        token = value.strip().split()
        if token and token[0].lstrip("-").isdigit():
            values[key] = int(token[0])
    return values


def process_sample(pid: int) -> dict[str, int | str] | None:
    root = Path("/proc") / str(pid)
    try:
        stat_text = (root / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    right = stat_text.rfind(")")
    if right < 0:
        return None
    fields = stat_text[right + 2 :].split()
    if len(fields) < 20:
        return None
    status = read_key_values(root / "status")
    io = read_key_values(root / "io")
    return {
        "state": fields[0],
        "utime_ticks": int(fields[11]),
        "stime_ticks": int(fields[12]),
        "rss_kib": status.get("VmRSS", 0),
        "hwm_kib": status.get("VmHWM", 0),
        "voluntary_context_switches": status.get("voluntary_ctxt_switches", 0),
        "involuntary_context_switches": status.get("nonvoluntary_ctxt_switches", 0),
        "read_bytes": io.get("read_bytes", 0),
        "write_bytes": io.get("write_bytes", 0),
    }


def boottime_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def read_frequency_khz() -> int:
    values: list[int] = []
    for path in Path("/sys/devices/system/cpu/cpufreq").glob(
        "policy*/scaling_cur_freq"
    ):
        try:
            values.append(int(path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            pass
    return int(statistics.median(values)) if values else 0


def read_temperature_millicelsius() -> int:
    values: list[int] = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            values.append(int(path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            pass
    return max(values, default=0)


def wait_for_ready(path: Path, pid: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return
        sample = process_sample(pid)
        if sample is None or sample["state"] == "Z":
            raise RuntimeError("probe exited before the warm-up ready marker")
        time.sleep(0.05)
    raise TimeoutError("timed out waiting for the warm-up ready marker")


def wait4_process(process: subprocess.Popen[Any]) -> tuple[int, resource.struct_rusage]:
    waited_pid, status, usage = os.wait4(process.pid, 0)
    if waited_pid != process.pid:
        raise RuntimeError("wait4 returned an unexpected process")
    returncode = os.waitstatus_to_exitcode(status)
    process.returncode = returncode
    return returncode, usage


def nearest_sample(
    samples: list[dict[str, int | str]], timestamp_ns: int
) -> dict[str, int | str]:
    return min(samples, key=lambda row: abs(int(row["boottime_ns"]) - timestamp_ns))


def write_samples(path: Path, samples: list[dict[str, int | str]]) -> None:
    if not samples:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)


def configure_and_snapshot_variant(
    repo: Path,
    build_dir: Path,
    variant_root: Path,
    diagnostics: bool,
    jobs: int,
) -> dict[str, str]:
    mode = "on" if diagnostics else "off"
    destination = variant_root / mode
    if destination.exists():
        raise FileExistsError(f"variant output already exists: {destination}")
    run(
        [
            "cmake",
            "-S",
            str(repo),
            "-B",
            str(build_dir),
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            f"-DRS_CAMERA_V4L2_DIAGNOSTICS={'ON' if diagnostics else 'OFF'}",
        ],
        cwd=repo,
    )
    run(
        [
            "cmake",
            "--build",
            str(build_dir),
            "--target",
            "realsense_steady_probe",
            "d435_sensor_probe",
            f"-j{jobs}",
        ],
        cwd=repo,
    )
    library = build_dir / "RelWithDebInfo" / "librealsense2.so.2.58.3"
    files = {
        "realsense_steady_probe": build_dir / "realsense_steady_probe",
        "d435_sensor_probe": build_dir / "d435_sensor_probe",
        "librealsense2.so.2.58.3": library,
    }
    for source in files.values():
        if not source.is_file():
            raise FileNotFoundError(source)
    destination.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for name, source in files.items():
        target = destination / name
        shutil.copy2(source, target)
        hashes[name] = sha256(target)
    (destination / "librealsense2.so.2.58").symlink_to("librealsense2.so.2.58.3")
    (destination / "librealsense2.so.2").symlink_to("librealsense2.so.2.58.3")
    (destination / "build_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "diagnostics_compiled": diagnostics,
                "build_type": "RelWithDebInfo",
                "files_sha256": hashes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return hashes


def probe_command(
    variant: Path,
    serial: str,
    workload: str,
    output: Path,
    measurement_seconds: int,
) -> list[str]:
    fps = 30 if workload == "representative" else 60
    warmup_frames = fps * 10
    command = [
        str(variant / "realsense_steady_probe"),
        "--serial",
        serial,
        "--camera-count",
        "1",
        "--delivery",
        "wait",
        "--frames",
        "1000000",
        "--measurement-duration-ms",
        str(measurement_seconds * 1000),
        "--frame-timeout-ms",
        "1500",
        "--startup-timeout-ms",
        "15000",
        "--warmup-ready-file",
        str(output / "warmup.ready"),
        "--measurement-start-gate",
        str(output / "measurement.gate"),
        "--measurement-gate-timeout-ms",
        "30000",
        "--summary-output",
        str(output / "steady_summary.json"),
        "--events-output",
        str(output / "frame_events.csv"),
        "--fps",
        str(fps),
        "--depth-width",
        "848",
        "--depth-height",
        "480",
        "--warmup-frames",
        str(warmup_frames),
    ]
    if workload == "representative":
        command.extend(
            [
                "--stream-mode",
                "depth_color",
                "--color-width",
                "640",
                "--color-height",
                "480",
            ]
        )
    else:
        command.extend(
            [
                "--stream-mode",
                "stereo_all",
                "--color-width",
                "960",
                "--color-height",
                "540",
            ]
        )
    return command


def run_one(
    repo: Path,
    variants: Path,
    serial: str,
    workload: str,
    mode: str,
    rep: int,
    output: Path,
    measurement_seconds: int,
) -> None:
    variant = variants / ("off" if mode == "off" else "on")
    output.mkdir(parents=True)
    environment = os.environ.copy()
    environment["LD_LIBRARY_PATH"] = str(variant)
    trace = output / "v4l2_diagnostic_trace.bin"
    if mode == "full":
        environment["RS_V4L2_DIAGNOSTIC_TRACE_FILE"] = str(trace)
        environment["RS_V4L2_DIAGNOSTIC_TRACE_CAPACITY"] = "12000000"
    else:
        environment.pop("RS_V4L2_DIAGNOSTIC_TRACE_FILE", None)
        environment.pop("RS_V4L2_DIAGNOSTIC_TRACE_CAPACITY", None)

    reset = [
        str(variant / "d435_sensor_probe"),
        "--serial",
        serial,
        "--hardware-reset",
        "--reset-timeout-ms",
        "5000",
    ]
    run(reset, cwd=repo, env=environment)
    command = probe_command(
        variant, serial, workload, output, measurement_seconds
    )
    stdout_path = output / "probe_stdout.txt"
    stderr_path = output / "probe_stderr.txt"
    frequency_start = read_frequency_khz()
    temperature_start = read_temperature_millicelsius()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=repo,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            wait_for_ready(output / "warmup.ready", process.pid, 40.0)
            (output / "measurement.gate").write_text(
                f"{boottime_ns()}\n", encoding="utf-8"
            )
            samples: list[dict[str, int | str]] = []
            while True:
                sample = process_sample(process.pid)
                if sample is None:
                    break
                sample["boottime_ns"] = boottime_ns()
                sample["frequency_khz"] = read_frequency_khz()
                sample["temperature_millicelsius"] = read_temperature_millicelsius()
                samples.append(sample)
                if sample["state"] == "Z":
                    break
                time.sleep(0.10)
            returncode, usage = wait4_process(process)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise

    write_samples(output / "resource_samples.csv", samples)
    if returncode != 0:
        raise RuntimeError(
            f"probe failed for {workload}/{mode}/rep-{rep}: {returncode}"
        )
    summary = json.loads(
        (output / "steady_summary.json").read_text(encoding="utf-8")
    )
    measurement = summary["measurement"]
    start_ns = int(measurement["start_boottime_ns"])
    end_ns = int(measurement["end_boottime_ns"])
    measured = [
        row for row in samples if start_ns <= int(row["boottime_ns"]) <= end_ns
    ]
    if len(measured) < 2:
        measured = [nearest_sample(samples, start_ns), nearest_sample(samples, end_ns)]
    first = measured[0]
    last = measured[-1]
    ticks = os.sysconf("SC_CLK_TCK")
    sampled_seconds = (int(last["boottime_ns"]) - int(first["boottime_ns"])) / 1e9
    user_seconds = (int(last["utime_ticks"]) - int(first["utime_ticks"])) / ticks
    system_seconds = (int(last["stime_ticks"]) - int(first["stime_ticks"])) / ticks
    cpu_percent = 100.0 * (user_seconds + system_seconds) / sampled_seconds
    camera = summary["cameras"][0]
    trace_bytes = trace.stat().st_size if trace.is_file() else 0
    result = {
        "schema_version": 2,
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "librealsense_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo / "deps/librealsense", text=True
        ).strip(),
        "workload": workload,
        "mode": mode,
        "rep": rep,
        "serial": serial,
        "command": command,
        "probe_success": bool(summary.get("success")),
        "returncode": returncode,
        "measurement": measurement,
        "aggregate": {
            "deliveries": camera["deliveries"],
            "frames": camera["frames"],
            "duplicate_frames": camera["duplicate_frames"],
            "sequence_gaps": camera["sequence_gaps"],
            "out_of_order_frames": camera["out_of_order_frames"],
            "timeouts": camera["timeouts"],
            "delivery_interarrival_ms": camera["delivery_interarrival_ms"],
            "wait_ms": camera["wait_ms"],
        },
        "sample_count": len(measured),
        "sampled_seconds": sampled_seconds,
        "cpu_utilization_percent_of_one_core": cpu_percent,
        "user_cpu_seconds": user_seconds,
        "system_cpu_seconds": system_seconds,
        "rss_start_kib": int(first["rss_kib"]),
        "rss_mean_kib": statistics.fmean(int(row["rss_kib"]) for row in measured),
        "rss_max_kib": max(int(row["rss_kib"]) for row in measured),
        "hwm_max_kib": max(int(row["hwm_kib"]) for row in measured),
        "minor_faults": 0,
        "major_faults": 0,
        "voluntary_context_switches": int(last["voluntary_context_switches"])
        - int(first["voluntary_context_switches"]),
        "involuntary_context_switches": int(last["involuntary_context_switches"])
        - int(first["involuntary_context_switches"]),
        "read_bytes": int(last["read_bytes"]) - int(first["read_bytes"]),
        "write_bytes": int(last["write_bytes"]) - int(first["write_bytes"]),
        "frequency_start_khz": frequency_start,
        "frequency_end_khz": read_frequency_khz(),
        "temperature_start_millicelsius": temperature_start,
        "temperature_end_millicelsius": read_temperature_millicelsius(),
        "lifetime_user_cpu_seconds": usage.ru_utime,
        "lifetime_system_cpu_seconds": usage.ru_stime,
        "lifetime_max_rss_kib": usage.ru_maxrss,
        "lifetime_minor_faults": usage.ru_minflt,
        "lifetime_major_faults": usage.ru_majflt,
        "lifetime_block_input_operations": usage.ru_inblock,
        "lifetime_block_output_operations": usage.ru_oublock,
        "lifetime_voluntary_context_switches": usage.ru_nvcsw,
        "lifetime_involuntary_context_switches": usage.ru_nivcsw,
        "trace_final_allocated_bytes": trace_bytes,
        "trace_final_logical_bytes": trace_bytes,
    }
    (output / "resource_summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--build-dir", type=Path, default=Path("build-realsense-steady"))
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--measurement-seconds", type=int, default=30)
    parser.add_argument("--build-jobs", type=int, default=3)
    parser.add_argument(
        "--rerun-condition",
        metavar="WORKLOAD:REP:MODE",
        help=(
            "reuse an existing result directory and its saved variants to rerun "
            "one condition, for example representative:1:build-only"
        ),
    )
    return parser.parse_args()


def parse_rerun_condition(value: str) -> tuple[str, int, str]:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("rerun condition must be WORKLOAD:REP:MODE")
    workload, rep_text, mode = parts
    if workload not in {"representative", "stress"}:
        raise ValueError(f"unsupported rerun workload: {workload}")
    if mode not in {"off", "build-only", "full"}:
        raise ValueError(f"unsupported rerun mode: {mode}")
    try:
        rep = int(rep_text)
    except ValueError as error:
        raise ValueError(f"invalid rerun repetition: {rep_text}") from error
    if rep <= 0:
        raise ValueError("rerun repetition must be positive")
    return workload, rep, mode


def main() -> None:
    args = parse_args()
    repo = Path(__file__).resolve().parents[2]
    build_dir = (repo / args.build_dir).resolve()
    results = (repo / args.results_dir).resolve()
    if args.rerun_condition:
        if not results.is_dir():
            raise FileNotFoundError(f"results directory does not exist: {results}")
        variants = results / "variants"
        workload, rep, mode = parse_rerun_condition(args.rerun_condition)
        output = results / workload / f"rep-{rep}" / mode
        if output.exists():
            raise FileExistsError(
                f"rerun output already exists; archive it before rerunning: {output}"
            )
        lock = repo / "scripts/lock_cpu_freq.sh"
        restore = repo / "scripts/restore_cpu_freq_default.sh"
        try:
            run(["sudo", "-n", str(lock), "1500000"], cwd=repo)
            print(
                f"[OVERHEAD-RERUN] workload={workload} rep={rep} mode={mode}",
                flush=True,
            )
            run_one(
                repo,
                variants,
                args.serial,
                workload,
                mode,
                rep,
                output,
                args.measurement_seconds,
            )
        finally:
            subprocess.run(["sudo", "-n", str(restore)], cwd=repo, check=False)
        return
    if results.exists():
        raise FileExistsError(f"results directory already exists: {results}")
    if args.repetitions <= 0 or args.repetitions > len(MODE_ORDERS):
        raise ValueError(f"repetitions must be between 1 and {len(MODE_ORDERS)}")
    results.mkdir(parents=True)
    variants = results / "variants"
    campaign_manifest = {
        "schema_version": 1,
        "hostname": os.uname().nodename,
        "uname": " ".join(os.uname()),
        "serial": args.serial,
        "measurement_seconds": args.measurement_seconds,
        "repetitions": args.repetitions,
        "mode_orders": MODE_ORDERS[: args.repetitions],
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "librealsense_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo / "deps/librealsense", text=True
        ).strip(),
    }
    (results / "campaign_manifest.json").write_text(
        json.dumps(campaign_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lock = repo / "scripts/lock_cpu_freq.sh"
    restore = repo / "scripts/restore_cpu_freq_default.sh"
    try:
        run(["sudo", "-n", str(lock), "1500000"], cwd=repo)
        configure_and_snapshot_variant(
            repo, build_dir, variants, diagnostics=False, jobs=args.build_jobs
        )
        configure_and_snapshot_variant(
            repo, build_dir, variants, diagnostics=True, jobs=args.build_jobs
        )
        for workload in ("representative", "stress"):
            for rep in range(1, args.repetitions + 1):
                for mode in MODE_ORDERS[rep - 1]:
                    output = results / workload / f"rep-{rep}" / mode
                    print(
                        f"[OVERHEAD] workload={workload} rep={rep} mode={mode}",
                        flush=True,
                    )
                    run_one(
                        repo,
                        variants,
                        args.serial,
                        workload,
                        mode,
                        rep,
                        output,
                        args.measurement_seconds,
                    )
    finally:
        subprocess.run(["sudo", "-n", str(restore)], cwd=repo, check=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
