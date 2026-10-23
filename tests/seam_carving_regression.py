#!/usr/bin/env python3
"""Regression tests for the seam-carving displacement helper.

These tests exercise ``seam_carving.build_seam_field`` and the private seam
finders in ``seam_carving.py``.  numpy and cv2 are optional at import time:
the helper module must import with only the standard library (lazy imports),
and numeric tests skip when the dependency they need is missing.

Run directly:

    python3 tests/seam_carving_regression.py
    python3 -m unittest tests.seam_carving_regression -v
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import seam_carving
    HAVE_MODULE = True
except ImportError:
    seam_carving = None
    HAVE_MODULE = False

HAVE_NUMPY = np is not None
HAVE_CV2 = cv2 is not None


class MissingHelperTest(unittest.TestCase):
    """RED gate: the helper module and its entry points must exist."""

    def test_module_imports(self):
        self.assertTrue(HAVE_MODULE, "seam_carving helper is missing")

    def test_build_seam_field_is_callable(self):
        if not HAVE_MODULE:
            self.fail("seam_carving helper is missing")
        self.assertTrue(callable(seam_carving.build_seam_field))

    def test_private_seam_finders_exposed(self):
        if not HAVE_MODULE:
            self.fail("seam_carving helper is missing")
        self.assertTrue(hasattr(seam_carving, "_find_vertical_seam"))
        self.assertTrue(hasattr(seam_carving, "_find_horizontal_seam"))
        self.assertTrue(hasattr(seam_carving, "_weight_energy"))


@unittest.skipUnless(HAVE_MODULE and HAVE_NUMPY,
                     "needs seam_carving and numpy")
class SeamFinderAnalyticTest(unittest.TestCase):
    """Direct analytic checks of the private min-cost seam finders."""

    def test_low_energy_corridor_selected(self):
        energy = np.ones((6, 6), dtype=np.float32)
        energy[:, 3] = 0.0
        path = seam_carving._find_vertical_seam(energy)
        self.assertEqual(path, [3] * 6)

    def test_preservation_routes_around_energy_spike(self):
        # Raw min-cost seam walks through the 2-energy spike at (1, 0);
        # high preservation amplifies that spike and reroutes around it.
        energy = np.array([[0.0, 1.0, 1.0],
                           [2.0, 2.0, 1.0]], dtype=np.float32)
        raw = seam_carving._find_vertical_seam(energy)
        preserved = seam_carving._find_vertical_seam(
            seam_carving._weight_energy(energy, 1.0))
        self.assertEqual(raw, [0, 0])
        self.assertEqual(preserved, [1, 2])
        self.assertNotEqual(raw, preserved)

    def test_horizontal_seam_matches_transposed_vertical(self):
        energy = np.array([[0.0, 1.0, 1.0],
                           [2.0, 2.0, 1.0],
                           [0.0, 1.0, 1.0]], dtype=np.float32)
        rows = seam_carving._find_horizontal_seam(energy)
        cols = seam_carving._find_vertical_seam(energy.T)
        self.assertEqual(rows, cols)


@unittest.skipUnless(HAVE_MODULE and HAVE_NUMPY and HAVE_CV2,
                     "needs seam_carving, numpy and cv2")
class BuildSeamFieldTest(unittest.TestCase):
    """Black-box behavior of build_seam_field over real uint8 RGB frames."""

    def _frame(self, height=16, width=20, seed=0):
        rng = np.random.default_rng(seed)
        return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)

    def test_zero_passthrough_disabled_axes(self):
        frame = self._frame()
        out = seam_carving.build_seam_field(
            frame, horizontal=0.0, vertical=0.0, strength=1.0,
            preservation=0.5)
        self.assertTrue(np.array_equal(out, np.zeros_like(out)))

    def test_zero_passthrough_zero_strength(self):
        frame = self._frame()
        out = seam_carving.build_seam_field(
            frame, horizontal=1.0, vertical=1.0, strength=0.0,
            preservation=0.5)
        self.assertTrue(np.array_equal(out, np.zeros_like(out)))

    def test_shape_dtype_bounds_finite(self):
        frame = self._frame()
        out = seam_carving.build_seam_field(
            frame, horizontal=1.0, vertical=1.0, strength=0.8,
            preservation=0.5)
        self.assertEqual(out.shape, (frame.shape[0], frame.shape[1], 2))
        self.assertEqual(out.dtype, np.float32)
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertGreaterEqual(out.min(), -1.0)
        self.assertLessEqual(out.max(), 1.0)

    def test_no_input_mutation(self):
        frame = self._frame()
        original = frame.copy()
        seam_carving.build_seam_field(
            frame, horizontal=1.0, vertical=1.0, strength=0.8,
            preservation=0.5)
        self.assertTrue(np.array_equal(frame, original))

    def test_horizontal_vs_vertical_axes(self):
        frame = self._frame()
        horizontal = seam_carving.build_seam_field(
            frame, horizontal=1.0, vertical=0.0, strength=1.0,
            preservation=0.0)
        vertical = seam_carving.build_seam_field(
            frame, horizontal=0.0, vertical=1.0, strength=1.0,
            preservation=0.0)
        self.assertTrue(np.any(horizontal[:, :, 0] != 0.0))
        self.assertTrue(np.all(horizontal[:, :, 1] == 0.0))
        self.assertTrue(np.any(vertical[:, :, 1] != 0.0))
        self.assertTrue(np.all(vertical[:, :, 0] == 0.0))
        self.assertFalse(np.array_equal(horizontal, vertical))

    def test_deterministic(self):
        frame = self._frame()
        a = seam_carving.build_seam_field(frame, 0.7, 0.4, 0.9, 0.3)
        b = seam_carving.build_seam_field(frame, 0.7, 0.4, 0.9, 0.3)
        self.assertTrue(np.array_equal(a, b))

    def test_seams_limited_for_oversized_request(self):
        frame = self._frame(height=8, width=8)
        out = seam_carving.build_seam_field(
            frame, horizontal=1.0, vertical=1.0, strength=1.0,
            preservation=0.0, max_seams=100)
        self.assertEqual(out.shape, (8, 8, 2))
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertGreaterEqual(out.min(), -1.0)
        self.assertLessEqual(out.max(), 1.0)

    def test_tiny_sizes_guard(self):
        tall = self._frame(height=1, width=6)
        out = seam_carving.build_seam_field(tall, 1.0, 1.0, 1.0, 0.0)
        self.assertTrue(np.array_equal(out, np.zeros_like(out)))
        wide = self._frame(height=6, width=1)
        out = seam_carving.build_seam_field(wide, 1.0, 1.0, 1.0, 0.0)
        self.assertTrue(np.array_equal(out, np.zeros_like(out)))

    def test_parameters_clamped(self):
        frame = self._frame(height=8, width=8)
        out = seam_carving.build_seam_field(
            frame, horizontal=3.0, vertical=-1.0, strength=2.0,
            preservation=1.5, max_seams=4)
        self.assertEqual(out.shape, (8, 8, 2))
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertGreaterEqual(out.min(), -1.0)
        self.assertLessEqual(out.max(), 1.0)

    def test_nonfinite_treated_as_zero(self):
        frame = self._frame(height=8, width=8)
        out = seam_carving.build_seam_field(
            frame, horizontal=float("nan"), vertical=0.0,
            strength=float("inf"), preservation=0.5)
        self.assertTrue(np.array_equal(out, np.zeros_like(out)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
