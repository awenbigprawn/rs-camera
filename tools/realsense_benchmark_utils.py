"""Shared helpers for RealSense Benchkit campaigns."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict


DROP_CACHES_PATH = Path("/proc/sys/vm/drop_caches")
MEMINFO_PATH = Path("/proc/meminfo")
MEMINFO_FIELDS = (
    "MemTotal",
    "MemFree",
    "MemAvailable",
    "Buffers",
    "Cached",
    "SReclaimable",
    "Shmem",
    "SwapCached",
)
MEMORY_CLEANUP_FILENAME = "memory_cleanup_before_run.json"


def _read_meminfo_kib() -> Dict[str, int]:
    values: Dict[str, int] = {}
    for line in MEMINFO_PATH.read_text(encoding="utf-8").splitlines():
        key, separator, remainder = line.partition(":")
        if not separator or key not in MEMINFO_FIELDS:
            continue
        fields = remainder.split()
        if fields:
            values[key] = int(fields[0])
    return values


class DropCachesBeforeRun:
    """Benchkit pre-run hook that establishes a cold Linux filesystem cache."""

    def __init__(self, use_sudo: bool) -> None:
        self._use_sudo = use_sudo

    def validate(self) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("per-run cache dropping is supported only on Linux")
        if not DROP_CACHES_PATH.is_file():
            raise RuntimeError(f"Linux drop-caches control is missing: {DROP_CACHES_PATH}")
        for utility in ("sync", "tee"):
            if shutil.which(utility) is None:
                raise RuntimeError(f"{utility} is required for per-run cache dropping")
        if self._use_sudo:
            if shutil.which("sudo") is None:
                raise RuntimeError("sudo is required for per-run cache dropping")
        elif os.geteuid() != 0 and not os.access(DROP_CACHES_PATH, os.W_OK):
            raise RuntimeError(
                "dropping caches requires root; run with sudo enabled or pass "
                "--no-drop-caches"
            )

    def _drop_command(self) -> list[str]:
        command = ["tee", str(DROP_CACHES_PATH)]
        if self._use_sudo and os.geteuid() != 0:
            command = ["sudo", "--non-interactive", *command]
        return command

    @staticmethod
    def _run(
        command: list[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"{' '.join(command)} exited with {completed.returncode}"
                + (f": {detail}" if detail else "")
            )
        return completed

    def __call__(
        self,
        *,
        build_variables: Dict[str, Any],
        run_variables: Dict[str, Any],
        other_variables: Dict[str, Any],
        record_data_dir: Path,
    ) -> None:
        del build_variables, run_variables, other_variables
        record_dir = Path(record_data_dir)
        record_dir.mkdir(parents=True, exist_ok=True)
        output_path = record_dir / MEMORY_CLEANUP_FILENAME
        drop_command = self._drop_command()
        metadata: Dict[str, Any] = {
            "schema_version": 1,
            "enabled": True,
            "success": False,
            "operation": "sync_and_drop_linux_filesystem_caches",
            "drop_caches_value": 3,
            "scope": "page_cache_dentries_inodes",
            "anonymous_memory_cleared": False,
            "commands": [["sync"], drop_command],
            "meminfo_before_kib": _read_meminfo_kib(),
        }
        start_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        metadata["start_boottime_ns"] = start_ns
        try:
            self._run(["sync"])
            self._run(drop_command, input_text="3\n")
            metadata["success"] = True
        except Exception as error:
            metadata["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            end_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            metadata["end_boottime_ns"] = end_ns
            metadata["duration_ms"] = (end_ns - start_ns) / 1_000_000.0
            metadata["meminfo_after_kib"] = _read_meminfo_kib()
            output_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        print(
            "[MEMORY-CLEANUP] dropped page cache, dentries, and inodes "
            f"in {metadata['duration_ms']:.1f} ms"
        )


def memory_cleanup_result_fields(
    record_data_dir: Path,
    *,
    configured: bool,
) -> Dict[str, Any]:
    path = Path(record_data_dir) / MEMORY_CLEANUP_FILENAME
    metadata = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else {}
    )
    before = metadata.get("meminfo_before_kib", {})
    after = metadata.get("meminfo_after_kib", {})
    return {
        "memory_cleanup_enabled": configured,
        "memory_cleanup_recorded": bool(metadata),
        "memory_cleanup_success": metadata.get("success", ""),
        "memory_cleanup_duration_ms": metadata.get("duration_ms", ""),
        "memory_available_before_kib": before.get("MemAvailable", ""),
        "memory_available_after_kib": after.get("MemAvailable", ""),
        "memory_cached_before_kib": before.get("Cached", ""),
        "memory_cached_after_kib": after.get("Cached", ""),
        "memory_sreclaimable_before_kib": before.get("SReclaimable", ""),
        "memory_sreclaimable_after_kib": after.get("SReclaimable", ""),
    }
