"""Common Benchkit result columns for retry and recovery metadata."""

from __future__ import annotations

from typing import Any, Dict, Mapping


def common_attempt_result_fields(
    summary: Mapping[str, Any],
) -> Dict[str, Any]:
    success = bool(summary.get("success", False))
    recovery = summary.get("recovery", {})
    return {
        "attempt_count": summary.get("attempt_count", 1),
        "failed_attempt_count": summary.get("failed_attempt_count", 0),
        "initial_attempt_success": summary.get(
            "initial_attempt_success", success
        ),
        "eventual_success": summary.get("eventual_success", success),
        "selected_attempt": summary.get("selected_attempt", 1),
        "recovery_attempted": recovery.get("attempted", False),
        "recovery_count": recovery.get("count", 0),
        "recovery_method": recovery.get("method", "none"),
        "recovery_success": recovery.get("success", ""),
        "recovery_error": recovery.get("error", ""),
    }
