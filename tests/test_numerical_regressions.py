"""Numerical contracts for the POLA-3 synthetic map; no CFX or display needed."""
from pathlib import Path
import unittest

import numpy as np

import plot_compressor_map as plot

ROOT = Path(__file__).resolve().parents[1]


class NumericalMapTests(unittest.TestCase):
    def test_example_surge_fit_and_boundary_coefficients(self):
        # Frozen from POLA-3's four surge points: (1,1.2), (1.3,1.4),
        # (1.6,1.65), (1.9,1.9). Changing fit degree, PR selection, flow
        # scaling or the boundary margin must change these outputs.
        # 1e-10 permits LAPACK rounding, far below the CSV's 0.001 PR scale.
        for pr_type, constant in (("static", 0.6780555555555556),
                                  ("total", 0.7980555555555556)):
            with self.subTest(pr_type=pr_type):
                rows = plot.load_rows(ROOT / "examples/compressor_map.csv", 10, pr_type)
                grouped = plot.prepare_speed_lines(rows, crop_gap_gs=0.20)
                self.assertEqual([len(grouped[s]) for s in sorted(grouped)], [5, 5, 5, 5])
                surge, fit, _ = plot.fit_surge_curve(grouped, "poly_deg_2")
                np.testing.assert_allclose([p["mass_flow_gs"] for p in surge],
                                           [1.0, 1.3, 1.6, 1.9], rtol=0, atol=1e-12)
                np.testing.assert_allclose(fit["coefficients"],
                    [0.1388888888888889, 0.3805555555555556, constant], rtol=0, atol=1e-10)
                self.assertAlmostEqual(fit["loo_rmse"], 0.0372677996249965, delta=1e-10)
                boundary = plot.enforce_surge_boundary(fit, rows)
                self.assertAlmostEqual(boundary["surge_boundary_offset"], 0.0235, delta=1e-10)
                np.testing.assert_allclose(boundary["coefficients"],
                    [0.1388888888888889, 0.3805555555555556, constant + 0.0235],
                    rtol=0, atol=1e-10)
                shift = 0.12 if pr_type == "total" else 0
                np.testing.assert_allclose(
                    [boundary["predictor"](x) for x in (1.0, 1.3, 1.6, 1.9)],
                    np.array([1.221, 1.431, 1.666, 1.926]) + shift, rtol=0, atol=1e-10)

    def test_disconnected_and_dense_surge_points_are_trimmed(self):
        # The POLA-3 example has only five points/line, so add a literal
        # dense synthetic line to exercise both trimming stages. IDs below
        # specify the retained set and order independently of the algorithm.
        flows = [1.000, 1.008, 1.016, 1.032, 1.064, 1.096, 1.128, 1.160, 1.192, 1.224]
        ratios = [2.000, 1.994, 1.988, 1.976, 1.952, 1.928, 1.904, 1.880, 1.856, 1.832]
        rows = []
        for speed in (20000, 40000):
            for index, (flow, pr) in enumerate(zip([0.4, 0.41] + flows, [2.3, 2.2] + ratios)):
                rows.append(dict(speed=speed, target_pressure=30-index,
                                 mass_flow_gs=flow, pressure_ratio=pr, result_file=index-2))
        grouped = plot.prepare_speed_lines(list(reversed(rows)), crop_gap_gs=0.20)
        for speed, ids in ((20000, [0, 3, 4, 5, 6, 7, 8, 9]),
                           (40000, [0, 4, 6, 7, 8, 9])):
            with self.subTest(speed=speed):
                self.assertEqual([p["result_file"] for p in grouped[speed]], ids)
                np.testing.assert_allclose([p["mass_flow_gs"] for p in grouped[speed]],
                                           [flows[i] for i in ids], rtol=0, atol=1e-12)

    def test_example_efficiency_interpolation_and_endpoint_clamping(self):
        # At 40000 rpm the source has (PR, efficiency %) =
        # (1.175,77.5), (1.2,76): their midpoint is 76.75%.
        # Assert direct lookup, interpolation and both clamped endpoints.
        for pr_type, shift in (("static", 0.0), ("total", 0.12)):
            with self.subTest(pr_type=pr_type):
                refs = plot.load_rows(ROOT / "examples/compressor_map.csv", 10, pr_type)
                primary = [dict(result_file=name, speed=40000, target_pressure=12,
                                pressure_ratio=pr+shift, efficiency_pct=None)
                           for name, pr in (("synthetic_40000_0.res", 3.0),
                                            ("midpoint", 1.1875), ("below", 1.0), ("above", 1.3))]
                primary.append(dict(result_file="already_known", speed=40000, target_pressure=12,
                                    pressure_ratio=1.1875+shift, efficiency_pct=81.25))
                actual = plot.interpolate_efficiency(primary, refs)
                # Absolute percentage-point tolerance; no stochastic computation here.
                np.testing.assert_allclose([p["efficiency_pct"] for p in actual],
                                           [76.0, 76.75, 82.0, 76.0, 81.25], rtol=0, atol=1e-10)


if __name__ == "__main__":
    unittest.main()
