"""Result-artifact discovery across current and historical layouts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class SelectedAttempt:
    run_dir: Path
    attempt: int
    data_dir: Path
    layout: str


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def selected_attempt_number(run_dir: Path) -> int:
    selected_path = run_dir / "selected_attempt.txt"
    if selected_path.is_file():
        try:
            attempt = int(selected_path.read_text(encoding="utf-8").strip())
        except ValueError as error:
            raise ValueError(
                f"Invalid selected attempt in {selected_path}"
            ) from error
        if attempt < 1:
            raise ValueError(f"Selected attempt must be positive: {selected_path}")
        return attempt

    attempts_path = run_dir / "attempts.json"
    if attempts_path.is_file():
        attempts = json.loads(attempts_path.read_text(encoding="utf-8"))
        if isinstance(attempts, list) and attempts:
            return int(attempts[-1].get("attempt", len(attempts)))
    return 1


def resolve_selected_attempt(run_dir: Path) -> SelectedAttempt:
    """Resolve both canonical attempt-N and legacy promoted-root results."""

    run_dir = Path(run_dir).resolve()
    attempt = selected_attempt_number(run_dir)
    attempt_dir = run_dir / f"attempt-{attempt}"
    if attempt_dir.is_dir():
        return SelectedAttempt(
            run_dir=run_dir,
            attempt=attempt,
            data_dir=attempt_dir,
            layout="attempt-directory",
        )
    return SelectedAttempt(
        run_dir=run_dir,
        attempt=attempt,
        data_dir=run_dir,
        layout="legacy-promoted-root",
    )
