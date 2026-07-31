#!/usr/bin/env python3

from pathlib import Path
import sys
import unittest


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from timerlat_parser import parse_timerlat_histogram  # noqa: E402


SAMPLE = """
# RTLA timerlat histogram
Index   IRQ-000   Thr-000   IRQ-001   Thr-001
0            50         0        50         0
1            49        50        49        50
2             1        49         1        49
3             0         1         0         1
over:          0         0         0         0
count:       100       100       100       100
min:           0         1         0         1
avg:           0         1         0         1
max:           2         3         2         3
"""


class TimerlatParserTest(unittest.TestCase):
    def test_parses_contexts_quantiles_and_global_tail(self):
        result = parse_timerlat_histogram(SAMPLE)

        self.assertEqual(result["contexts"]["IRQ-000"]["p50_us"], 0)
        self.assertEqual(result["contexts"]["IRQ-000"]["p99_us"], 1)
        self.assertEqual(result["contexts"]["Thr-001"]["p99_us"], 2)
        self.assertEqual(result["global"]["irq_max_us"], 2)
        self.assertEqual(result["global"]["thread_max_us"], 3)
        self.assertEqual(result["global"]["overflow_samples"], 0)

    def test_overflow_makes_an_uncovered_tail_quantile_unknown(self):
        text = SAMPLE.replace(
            "over:          0         0         0         0",
            "over:          2         0         0         0",
        ).replace(
            "count:       100       100       100       100",
            "count:       102       100       100       100",
        )
        result = parse_timerlat_histogram(text)

        self.assertIsNone(result["contexts"]["IRQ-000"]["p99_us"])
        self.assertIsNone(result["global"]["irq_p999_us_max_cpu"])
        self.assertEqual(result["global"]["overflow_samples"], 2)

    def test_rejects_inconsistent_sample_count(self):
        text = SAMPLE.replace("count:       100", "count:       101", 1)
        with self.assertRaisesRegex(ValueError, "sample count mismatch"):
            parse_timerlat_histogram(text)

    def test_rejects_output_without_histogram(self):
        with self.assertRaisesRegex(ValueError, "header"):
            parse_timerlat_histogram("rtla failed\n")


if __name__ == "__main__":
    unittest.main()
