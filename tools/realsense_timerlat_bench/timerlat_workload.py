"""Managed RealSense load used while Timerlat samples the platform."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, Mapping, Sequence


def phase_marker_seen(path: Path, marker: str) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "phase_marker" and event.get("name") == marker:
            return True
    return False


class ManagedCameraLoad:
    """Start the steady probe and expose a stable-load readiness barrier."""

    def __init__(
        self,
        *,
        probe: Path,
        repo_root: Path,
        serials: Sequence[str],
        duration_seconds: int,
        timerlat_warmup_seconds: int,
        guard_seconds: int,
    ) -> None:
        self._probe = probe
        self._repo_root = repo_root
        self._serials = tuple(serials)
        self._duration_seconds = duration_seconds
        self._timerlat_warmup_seconds = timerlat_warmup_seconds
        self._guard_seconds = guard_seconds
        self._process: subprocess.Popen[str] | None = None
        self.command: list[str] = []
        self.summary_path: Path | None = None
        self.lifecycle_path: Path | None = None
        self.started_boottime_ns = 0
        self.ready_boottime_ns = 0

    @staticmethod
    def _boottime_ns() -> int:
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _build_command(
        self,
        case: Mapping[str, Any],
        record_dir: Path,
    ) -> list[str]:
        camera = case["camera"]
        fps = int(camera["fps"])
        measurement_seconds = (
            self._duration_seconds
            + self._timerlat_warmup_seconds
            + self._guard_seconds
        )
        frames = int(math.ceil(measurement_seconds * fps))
        self.summary_path = record_dir / "camera_summary.json"
        events_path = record_dir / "camera_frame_events.csv"
        self.lifecycle_path = record_dir / "camera_lifecycle.jsonl"

        command = ["chrt", "--other", "0", str(self._probe)]
        for serial in self._serials:
            command += ["--serial", serial]
        command += [
            "--camera-count",
            str(camera["count"]),
            "--stream-mode",
            str(camera["stream_mode"]),
            "--delivery",
            str(camera.get("delivery", "wait")),
            "--frames",
            str(frames),
            "--warmup-frames",
            str(camera["warmup_frames"]),
            "--frame-timeout-ms",
            str(camera["frame_timeout_ms"]),
            "--startup-timeout-ms",
            str(camera["startup_timeout_ms"]),
            "--fps",
            str(fps),
            "--depth-width",
            str(camera["depth_width"]),
            "--depth-height",
            str(camera["depth_height"]),
            "--color-width",
            str(camera["color_width"]),
            "--color-height",
            str(camera["color_height"]),
            "--summary-output",
            str(self.summary_path),
            "--events-output",
            str(events_path),
        ]
        return command

    def start(self, case: Mapping[str, Any], record_dir: Path) -> None:
        if self._process is not None:
            raise RuntimeError("Camera load is already active")
        self.command = self._build_command(case, record_dir)
        stdout_path = record_dir / "camera_stdout.txt"
        stderr_path = record_dir / "camera_stderr.txt"
        environment = os.environ.copy()
        assert self.lifecycle_path is not None
        environment["RS_THREAD_TRACE_FILE"] = str(self.lifecycle_path)
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_file:
            self.started_boottime_ns = self._boottime_ns()
            self._process = subprocess.Popen(
                self.command,
                cwd=self._repo_root,
                env=environment,
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
            )

    def wait_until_steady(self, timeout_seconds: float) -> Dict[str, Any]:
        if self._process is None or self.lifecycle_path is None:
            raise RuntimeError("Camera load was not started")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if phase_marker_seen(self.lifecycle_path, "steady_state_begin"):
                self.ready_boottime_ns = self._boottime_ns()
                return {
                    "ready": True,
                    "started_boottime_ns": self.started_boottime_ns,
                    "ready_boottime_ns": self.ready_boottime_ns,
                    "command": self.command,
                }
            returncode = self._process.poll()
            if returncode is not None:
                return {
                    "ready": False,
                    "returncode": returncode,
                    "error": "camera probe exited before steady_state_begin",
                    "command": self.command,
                }
            time.sleep(0.05)
        return {
            "ready": False,
            "returncode": self._process.poll(),
            "error": "timed out waiting for steady_state_begin",
            "command": self.command,
        }

    def read_summary(self) -> Dict[str, Any]:
        if self.summary_path is None or not self.summary_path.is_file():
            return {}
        try:
            return json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def finish(self, timeout_seconds: float) -> Dict[str, Any]:
        if self._process is None:
            return {"started": False, "returncode": "", "forced_kill": False}
        forced_kill = False
        terminated = False
        try:
            returncode = self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            terminated = True
            self._process.terminate()
            try:
                returncode = self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forced_kill = True
                self._process.kill()
                returncode = self._process.wait(timeout=5)
        result = {
            "started": True,
            "returncode": returncode,
            "terminated": terminated,
            "forced_kill": forced_kill,
            "started_boottime_ns": self.started_boottime_ns,
            "ready_boottime_ns": self.ready_boottime_ns,
            "ended_boottime_ns": self._boottime_ns(),
            "command": self.command,
        }
        self._process = None
        return result

    def stop(self) -> Dict[str, Any]:
        return self.finish(timeout_seconds=0.0)
