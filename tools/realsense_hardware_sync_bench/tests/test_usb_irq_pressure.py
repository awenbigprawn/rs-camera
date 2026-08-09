from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "compare_usb_irq_pressure.py"
SPEC = importlib.util.spec_from_file_location("usb_irq_pressure", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class UsbIrqPressureTests(unittest.TestCase):
    def test_sliding_window_counts_boundary_exclusively(self) -> None:
        timestamps = [0, 50_000, 99_999, 100_000, 150_000]
        self.assertEqual(MODULE.sliding_window_max(timestamps, 100_000), 3)

    def test_group_summary_uses_attempt_medians_and_health(self) -> None:
        def result(rate: float, p50: float, close: float, success: bool) -> dict:
            concentration = {
                f"{width}_us": {
                    "fano_factor": rate / 1000,
                    "p99_count": width / 100,
                    "sliding_max_count": width / 50,
                }
                for width in (100, 250, 500, 1000)
            }
            return {
                "probe_success": success,
                "warmup_freshness": [{"sequence_gaps": 0 if success else 1}],
                "combined_irq_rate_per_second": rate,
                "cross_controller_nearest_delta_us": {"p50": p50},
                "cross_controller_nearest_fraction": {"le_100_us": close},
                "combined_concentration": concentration,
            }

        summary = MODULE.group_summary(
            [
                result(9000, 100, 0.4, True),
                result(11000, 80, 0.6, False),
                result(10000, 90, 0.5, True),
            ]
        )
        self.assertEqual(summary["successful_attempts"], 2)
        self.assertEqual(summary["attempts_with_warmup_sequence_gaps"], 1)
        self.assertEqual(summary["median_combined_irq_rate_per_second"], 10000)
        self.assertEqual(
            summary["median_cross_controller_nearest_delta_p50_us"], 90
        )


if __name__ == "__main__":
    unittest.main()
