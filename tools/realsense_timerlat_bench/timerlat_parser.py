"""Parse the stable histogram portion of ``rtla timerlat hist`` output."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping


_SUMMARY_KEYS = ("count", "min", "avg", "max")


def _integers(tokens: Iterable[str]) -> list[int]:
    values = []
    for token in tokens:
        values.append(int(token))
    return values


def _nearest_rank(
    buckets: Mapping[int, int],
    total: int,
    quantile: float,
) -> int | None:
    if total <= 0:
        return None
    target = max(1, int(total * quantile + 0.999999999))
    cumulative = 0
    for latency_us, count in sorted(buckets.items()):
        cumulative += count
        if cumulative >= target:
            return latency_us
    return None


def _maximum_known(values: Iterable[int | None]) -> int | None:
    collected = list(values)
    if not collected:
        return 0
    if any(value is None for value in collected):
        return None
    return max(int(value) for value in collected if value is not None)


def parse_timerlat_histogram(text: str) -> Dict[str, Any]:
    """Return per-context summary and quantiles while retaining overflow state."""

    columns: list[str] = []
    histograms: Dict[str, Dict[int, int]] = {}
    summary: Dict[str, Dict[str, int]] = {}
    overflow: Dict[str, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("Index "):
            columns = line.split()[1:]
            histograms = {column: {} for column in columns}
            summary = {column: {} for column in columns}
            overflow = {column: 0 for column in columns}
            continue
        if not columns:
            continue

        match = re.match(r"^(count|min|avg|max):\s+(.+)$", line)
        if match:
            values = _integers(match.group(2).split())
            if len(values) != len(columns):
                raise ValueError(
                    f"Timerlat {match.group(1)} column count does not match header"
                )
            for column, value in zip(columns, values):
                summary[column][match.group(1)] = value
            continue

        match = re.match(r"^over:\s+(.+)$", line)
        if match:
            values = _integers(match.group(1).split())
            if len(values) != len(columns):
                raise ValueError("Timerlat overflow column count does not match header")
            overflow = dict(zip(columns, values))
            continue

        match = re.match(r"^(\d+)\s+(.+)$", line)
        if not match:
            continue
        latency_us = int(match.group(1))
        values = _integers(match.group(2).split())
        if len(values) != len(columns):
            raise ValueError("Timerlat histogram column count does not match header")
        for column, value in zip(columns, values):
            if value:
                histograms[column][latency_us] = value

    if not columns:
        raise ValueError("Timerlat histogram header was not found")
    missing = [
        f"{column}:{key}"
        for column in columns
        for key in _SUMMARY_KEYS
        if key not in summary[column]
    ]
    if missing:
        raise ValueError("Timerlat summary is incomplete: " + ", ".join(missing))

    contexts: Dict[str, Dict[str, Any]] = {}
    for column in columns:
        in_range_count = sum(histograms[column].values())
        total = summary[column]["count"]
        if in_range_count + overflow[column] != total:
            raise ValueError(
                f"Timerlat sample count mismatch for {column}: "
                f"histogram={in_range_count}, over={overflow[column]}, count={total}"
            )
        contexts[column] = {
            **summary[column],
            "over": overflow[column],
            "histogram_count": in_range_count,
            "p50_us": _nearest_rank(histograms[column], total, 0.50),
            "p99_us": _nearest_rank(histograms[column], total, 0.99),
            "p999_us": _nearest_rank(histograms[column], total, 0.999),
            "p9999_us": _nearest_rank(histograms[column], total, 0.9999),
        }

    irq_contexts = [value for key, value in contexts.items() if key.startswith("IRQ-")]
    thread_contexts = [
        value for key, value in contexts.items() if key.startswith("Thr-")
    ]
    return {
        "schema_version": 1,
        "columns": columns,
        "contexts": contexts,
        "global": {
            "irq_max_us": max((value["max"] for value in irq_contexts), default=0),
            "thread_max_us": max(
                (value["max"] for value in thread_contexts), default=0
            ),
            "irq_p999_us_max_cpu": _maximum_known(
                value["p999_us"] for value in irq_contexts
            ),
            "thread_p999_us_max_cpu": _maximum_known(
                value["p999_us"] for value in thread_contexts
            ),
            "overflow_samples": sum(overflow.values()),
        },
    }
