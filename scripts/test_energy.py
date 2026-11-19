"""Focused tests for energy.py's sensor integration and input validation."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import energy


class EnergyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_path = self.root / "run.json"
        self.series_path = self.root / "series.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_inputs(self, start: int, end: int, samples: list[tuple[int, float]]) -> None:
        self.run_path.write_text(json.dumps({
            "start_unix_us": start,
            "end_unix_us": end,
            "batches": 2,
        }), encoding="utf-8")
        self.series_path.write_text(json.dumps({
            "schema_version": 1,
            "scope": "cpu_package",
            "source": "synthetic test fixture",
            "sensor_name": "test power",
            "samples": [
                {"timestamp_unix_us": t, "power_w": p} for t, p in samples
            ],
        }), encoding="utf-8")

    def test_constant_power_and_baseline_subtraction(self) -> None:
        self.write_inputs(4_000_000, 6_000_000, [
            (0, 10), (1_000_000, 10), (2_000_000, 10),
            (3_000_000, 10), (4_000_000, 20), (5_000_000, 20),
            (6_000_000, 20),
        ])
        result = energy.analyze(str(self.run_path), str(self.series_path),
                                0, 2_000_000, 1.0)
        self.assertEqual(result["sensor_energy_j"], 40.0)
        self.assertEqual(result["baseline_mean_power_w"], 10.0)
        self.assertEqual(result["incremental_energy_j_estimate"], 20.0)
        self.assertEqual(result["measurement_status"], "measured_sensor_series_estimate")
        self.assertEqual(result["workload"]["batches"], 2)

    def test_linear_trapezoid_with_interpolated_endpoints(self) -> None:
        # P(t)=t W for t in seconds. Integral from 2s to 8s is 30 J.
        samples = [(0, 0), (10_000_000, 10)]
        self.assertAlmostEqual(energy.integrate(samples, 2_000_000, 8_000_000,
                                                 10_000_000, "test"), 30.0)

    def test_missing_interval_coverage_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not cover"):
            energy.integrate([(1_000_000, 10), (3_000_000, 10)],
                             0, 2_000_000, 1_000_000, "test")

    def test_out_of_order_samples_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            energy.parse_series({
                "schema_version": 1,
                "scope": "system",
                "source": "fixture",
                "sensor_name": "fixture",
                "samples": [
                    {"timestamp_unix_us": 2, "power_w": 1},
                    {"timestamp_unix_us": 1, "power_w": 1},
                ],
            })

    def test_short_window_inside_large_raw_gap_rejected(self) -> None:
        # The clipped interval is only 100 ms, but the raw pair spans 10 s.
        with self.assertRaisesRegex(ValueError, "sensor gap of 10.000s"):
            energy.integrate([(0, 10), (10_000_000, 10)], 4_000_000,
                             4_100_000, 1_000_000, "test")

    def test_nonfinite_gap_threshold_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.write_inputs(1_000_000, 2_000_000, [
                    (0, 10), (1_000_000, 10), (2_000_000, 20),
                ])
                with self.assertRaisesRegex(ValueError, "finite number"):
                    energy.analyze(str(self.run_path), str(self.series_path),
                                   0, 500_000, value)

    def test_nonfinite_power_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    energy.parse_series({
                        "schema_version": 1,
                        "scope": "cpu_package",
                        "source": "fixture",
                        "sensor_name": "fixture",
                        "samples": [
                            {"timestamp_unix_us": 0, "power_w": 1},
                            {"timestamp_unix_us": 1, "power_w": value},
                        ],
                    })


if __name__ == "__main__":
    unittest.main()
