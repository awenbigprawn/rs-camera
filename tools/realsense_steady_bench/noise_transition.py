"""Coordinate camera warm-up, noise warm-up, and measurement start."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Mapping

from noise_workloads import NoiseSuite


def _boottime_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


class NoiseTransition:
    """Start noise only after the probe reports that every camera is warm."""

    def __init__(
        self,
        *,
        noise_suite: NoiseSuite,
        modes: Mapping[str, str],
        record_dir: Path,
    ) -> None:
        self._noise_suite = noise_suite
        self._modes = dict(modes)
        self._record_dir = record_dir
        self.warmup_ready_path = record_dir / "camera_warmup_ready"
        self.measurement_gate_path = record_dir / "measurement_start_gate"
        self.error_path = Path(str(self.measurement_gate_path) + ".error")
        self._probe_done = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: Dict[str, Any] = {
            "enabled": noise_suite.any_enabled(self._modes),
            "camera_warmup_ready_boottime_ns": 0,
            "noise_start_boottime_ns": 0,
            "noise_ready_boottime_ns": 0,
            "error": "",
        }

    @property
    def enabled(self) -> bool:
        return bool(self._state["enabled"])

    @property
    def gate_timeout_ms(self) -> int:
        seconds = self._noise_suite.startup_timeout_seconds(self._modes) + 5.0
        return max(5000, int(seconds * 1000.0 + 0.5))

    def probe_arguments(self) -> list[str]:
        if not self.enabled:
            return []
        return [
            "--warmup-ready-file",
            str(self.warmup_ready_path),
            "--measurement-start-gate",
            str(self.measurement_gate_path),
            "--measurement-gate-timeout-ms",
            str(self.gate_timeout_ms),
        ]

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread is not None:
            raise RuntimeError("Noise transition coordinator was already started")
        self._thread = threading.Thread(
            target=self._run,
            name="rs-noise-transition",
            daemon=True,
        )
        self._thread.start()

    def finish(self) -> None:
        self._probe_done.set()
        if self._thread is not None:
            self._thread.join()
        self._write_state()

    def _write_state(self) -> None:
        (self._record_dir / "noise_transition.json").write_text(
            json.dumps(self._state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _publish_error(self, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}"
        self._state["error"] = detail
        _atomic_write(self.error_path, detail + "\n")
        self._write_state()

    def _wait_for_camera_warmup(self) -> bool:
        while not self._probe_done.is_set():
            if self.warmup_ready_path.is_file():
                try:
                    timestamp = int(
                        self.warmup_ready_path.read_text(encoding="utf-8").strip()
                    )
                except (OSError, ValueError):
                    time.sleep(0.01)
                    continue
                if timestamp > 0:
                    self._state["camera_warmup_ready_boottime_ns"] = timestamp
                    return True
            time.sleep(0.01)
        return False

    def _run(self) -> None:
        try:
            if not self._wait_for_camera_warmup():
                return
            self._state["noise_start_boottime_ns"] = _boottime_ns()
            self._noise_suite.start_all(self._modes, self._record_dir)
            if self._probe_done.is_set():
                return
            ready_ns = _boottime_ns()
            self._state["noise_ready_boottime_ns"] = ready_ns
            _atomic_write(self.measurement_gate_path, f"{ready_ns}\n")
            self._write_state()
        except BaseException as error:
            self._publish_error(error)
