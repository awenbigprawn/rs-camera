"""Command construction and pthread tracer build helpers."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable, List


def scheduler_prefix(policy: str, priority: int) -> List[str]:
    if policy == "other":
        return ["chrt", "--other", "0"]
    if policy == "rr":
        return ["chrt", "--rr", str(priority)]
    if policy == "fifo":
        return ["chrt", "--fifo", str(priority)]
    raise ValueError(f"Unsupported policy: {policy}")


def traced_command(
    *,
    scheduled_command: Iterable[str],
    tracer: Path,
    lifecycle_path: Path,
    lime: Path,
    lime_dir: Path,
    use_lime: bool,
    use_sudo: bool,
) -> List[str]:
    target = [
        "env",
        f"LD_PRELOAD={tracer}",
        f"RS_THREAD_TRACE_FILE={lifecycle_path}",
        *scheduled_command,
    ]
    command = target
    if use_lime:
        command = [
            str(lime),
            "trace",
            "--best-effort",
            "-o",
            str(lime_dir),
            "--",
            *target,
        ]
    if use_sudo:
        command = ["sudo", "--preserve-env=LD_LIBRARY_PATH", *command]
    return command


def validate_trace_environment(*, lime: Path, use_lime: bool) -> None:
    if use_lime and not lime.is_file():
        raise RuntimeError(
            f"LiME executable not found at {lime}. Build the unmodified dependency "
            "with: cargo build --release --manifest-path deps/lime-rtw/Cargo.toml"
        )
    if shutil.which("chrt") is None:
        raise RuntimeError("chrt is required (normally provided by util-linux)")


def build_pthread_tracer(
    *,
    output: Path,
    source: Path,
    compiler: str | None = None,
) -> None:
    subprocess.check_call(
        [
            compiler or os.environ.get("CC", "cc"),
            "-shared",
            "-fPIC",
            "-g",
            "-O2",
            "-fno-omit-frame-pointer",
            "-Wall",
            "-Wextra",
            "-o",
            str(output),
            str(source),
            "-ldl",
            "-pthread",
        ]
    )
