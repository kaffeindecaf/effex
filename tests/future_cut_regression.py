#!/usr/bin/env python3
"""FutureCut: future-frame cutout compositor (E2.47a-d) — regression tests.

These tests target the CPU frame processor only. Registry, CLI/menu wiring,
integration renders, docs and benchmarks live elsewhere.
"""
import subprocess
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO  # noqa: E402

try:
    import numpy as np
except ImportError:
    print("  SKIP NumPy not installed; install requirements-stateful.txt")
    sys.exit(0)

try:
    from future_cut import FutureCut  # noqa: E402
except ImportError as exc:
    print(f"  FAIL FutureCut processor is missing: {exc}")
    sys.exit(1)


def packet(frame, index, timestamp=None, context=None):
    from frame_pipeline import FramePacket
    return FramePacket(
        frame, index,
        Fraction(index, 10) if timestamp is None else timestamp,
        Fraction(1, 1000), Fraction(1, 10),
        dict(context or {}),
    )


def make_processor(config=None, shape=(48, 64, 3), seed=0, fps=10.0):
    processor = FutureCut()
    processor.initialize(dict(config or {}), shape, fps, seed)
    return processor


def push_all(processor, frames, fps=10.0):
    out = []
    for i, frame in enumerate(frames):
        out.extend(processor.push(packet(frame, i, Fraction(i, int(fps)))))
    out.extend(processor.flush())
    return out


def _two_speed_textures(shape=(48, 64, 3), n=40):
    """Two textures translating at different speeds (the 'cloud' case).

    A smooth vertical band (slow cloud, ~1 px/frame) and a sharp bright blob
    (fast feature, ~2 px/frame) both roll rightward. No per-pixel noise so the
    descriptors stay coherent and region growing can form one blob.
    """
    h, w = shape[:2]
    frames = []
    for i in range(n):
        xx = np.arange(w, dtype=np.float32)[None, :]
        band = (128 + 60 * np.sin(((xx + i) % w) / w * 2 * np.pi)).astype(np.float32)
        frame = np.empty(shape, np.uint8)
        frame[..., 0] = band
        frame[..., 1] = band
        frame[..., 2] = band
        blob_y = slice(h // 3, h // 3 + 8)
        blob_x0 = (10 + 2 * i) % (w - 12)
        frame[blob_y, blob_x0:blob_x0 + 12] = 255
        frames.append(frame)
    return frames


def label_components(mask):
    """Pure-numpy 8-connected component labelling (BFS over flat indices)."""
    mask = mask.astype(bool)
    if not mask.any():
        return []
    h, w = mask.shape
    remaining = mask.copy()
    stack = list(np.flatnonzero(remaining))
    areas = []
    neighbours = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
                  (0, 1), (1, -1), (1, 0), (1, 1)]
    while stack:
        seed = stack.pop()
        if not remaining.flat[seed]:
            continue
        area = 0
        queue = [seed]
        remaining.flat[seed] = False
        while queue:
            p = queue.pop()
            area += 1
            py, px = divmod(p, w)
            for dy, dx in neighbours:
                ny, nx = py + dy, px + dx
                if 0 <= ny < h and 0 <= nx < w:
                    q = ny * w + nx
                    if remaining.flat[q]:
                        remaining.flat[q] = False
                        queue.append(q)
        areas.append(area)
    return sorted(areas, reverse=True)


class FutureCutTests(unittest.TestCase):
    def test_module_imports_without_numpy(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import future_cut, sys; "
             "sys.exit(0 if 'numpy' not in sys.modules else 1)"],
            cwd=str(REPO), capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_config_defaults_match_roadmap(self):
        self.assertEqual(FutureCut.DEFAULTS["lookahead"], 0.6)
        self.assertEqual(FutureCut.DEFAULTS["gap"], 18)
        self.assertEqual(FutureCut.DEFAULTS["cutouts"], 1)
        self.assertEqual(FutureCut.DEFAULTS["spawn_every"], 24)
        self.assertEqual(FutureCut.DEFAULTS["size"], 18)
        self.assertEqual(FutureCut.DEFAULTS["pick"], "coherent")
        self.assertEqual(FutureCut.DEFAULTS["min_motion"], 2)
        self.assertEqual(FutureCut.DEFAULTS["max_motion"], 25)
        self.assertEqual(FutureCut.DEFAULTS["similarity"], 60)
        self.assertEqual(FutureCut.DEFAULTS["shape"], "blob")
        self.assertEqual(FutureCut.DEFAULTS["seam"], "poisson")
        self.assertEqual(FutureCut.DEFAULTS["feather"], 35)
        self.assertEqual(FutureCut.DEFAULTS["hold"], 6)
        self.assertEqual(FutureCut.DEFAULTS["melt"], 14)
        self.assertEqual(FutureCut.DEFAULTS["mode"], "smudge")
        self.assertEqual(FutureCut.DEFAULTS["retention"], 70)
        self.assertEqual(FutureCut.DEFAULTS["drift"], 100)
        self.assertEqual(FutureCut.DEFAULTS["seed"], 0)

    def test_config_validation_and_clamping(self):
        p = make_processor({"lookahead": 5.0, "gap": 999, "cutouts": 0,
                            "spawn_every": 0, "size": 900, "min_motion": -5,
                            "max_motion": 999, "similarity": 999,
                            "feather": 999, "hold": 999, "melt": 999,
                            "retention": 999, "drift": 999, "seed": -5},
                           shape=(48, 64, 3), seed=9)
        cfg = p.serialize_config()
        self.assertEqual(cfg["lookahead"], 2.0)
        self.assertEqual(cfg["gap"], 60)
        self.assertEqual(cfg["cutouts"], 1)
        self.assertEqual(cfg["spawn_every"], 4)
        self.assertEqual(cfg["size"], 40)
        self.assertEqual(cfg["min_motion"], 0)
        self.assertEqual(cfg["max_motion"], 100)
        self.assertEqual(cfg["similarity"], 100)
        self.assertEqual(cfg["feather"], 100)
        self.assertEqual(cfg["hold"], 30)
        self.assertEqual(cfg["melt"], 60)
        self.assertEqual(cfg["retention"], 95)
        self.assertEqual(cfg["drift"], 100)
        self.assertEqual(cfg["seed"], 0)

        for key, bad in (("pick", "bogus"), ("shape", "circle"),
                         ("seam", "melt"), ("mode", "codec")):
            with self.assertRaises(ValueError):
                make_processor({key: bad}, shape=(48, 64, 3))
        # bool/int/on-off style values are bad choice strings too
        for key in ("pick", "shape", "seam", "mode"):
            for value in (True, 1, "on", "off", 0):
                with self.assertRaises(ValueError):
                    make_processor({key: value}, shape=(48, 64, 3))

        p = make_processor({"pick": "dissimilar", "shape": "rect",
                            "seam": "none", "mode": "slide"}, shape=(48, 64, 3))
        self.assertEqual(p.serialize_config()["pick"], "dissimilar")

    def test_diagnostics_exposed_to_tests(self):
        p = make_processor(shape=(48, 64, 3))
        self.assertIsInstance(p.spawns, int)
        self.assertIsInstance(p.active_cutouts, int)
        self.assertIsInstance(p.runtime_notes, list)
        self.assertIsInstance(p.capacity, int)
        self.assertGreater(p.capacity, 0)

    def test_metadata_preserved_and_flush_ordering(self):
        shape = (24, 32, 3)
        rng = np.random.default_rng(1)
        frames = [rng.integers(0, 255, shape, dtype=np.uint8) for _ in range(10)]
        p = make_processor({"lookahead": 0.1, "gap": 1}, shape=shape, fps=10.0)
        out = push_all(p, frames, fps=10.0)
        self.assertEqual([q.index for q in out], list(range(10)))
        self.assertEqual([q.timestamp for q in out],
                         [Fraction(i, 10) for i in range(10)])
        self.assertEqual([q.duration for q in out], [Fraction(1, 10)] * 10)
        self.assertEqual(p.state_nbytes, 0)

    def test_output_delayed_by_expected_frames_at_lookahead(self):
        shape = (24, 32, 3)
        rng = np.random.default_rng(2)
        frames = [rng.integers(0, 255, shape, dtype=np.uint8) for _ in range(6)]
        p = make_processor({"lookahead": 0.2, "gap": 1, "mode": "slide",
                            "hold": 1, "melt": 2}, shape=shape, fps=10.0)
        # lookahead 0.2s at 10fps => ceil(2)+1 = 3 frame delay
        partial = []
        for i, frame in enumerate(frames):
            partial.extend(p.push(packet(frame, i, Fraction(i, 10))))
        self.assertEqual([q.index for q in partial], list(range(3)))
        flushed = list(p.flush())
        self.assertEqual([q.index for q in flushed], [3, 4, 5])
        self.assertEqual(p.state_nbytes, 0)

    def test_short_clip_passes_through_with_note(self):
        shape = (24, 32, 3)
        rng = np.random.default_rng(3)
        frames = [rng.integers(0, 255, shape, dtype=np.uint8) for _ in range(4)]
        p = make_processor({"lookahead": 0.6, "gap": 18}, shape=shape, fps=10.0)
        out = push_all(p, frames, fps=10.0)
        self.assertEqual([q.index for q in out], list(range(4)))
        for q, f in zip(out, frames):
            np.testing.assert_array_equal(q.frame, f)
        self.assertTrue(p.runtime_notes)

    def test_no_cv2_path_falls_back_to_block_matching(self):
        import sys as _sys
        shape = (48, 64, 3)
        rng = np.random.default_rng(4)
        frames = [rng.integers(0, 255, shape, dtype=np.uint8) for _ in range(30)]
        saved = _sys.modules.get("cv2")
        _sys.modules["cv2"] = None
        try:
            p = FutureCut()
            p.initialize({"lookahead": 0.3, "gap": 10, "spawn_every": 6},
                         shape, 10.0, 17)
            out = push_all(p, frames, fps=10.0)
            self.assertEqual([q.index for q in out], list(range(30)))
            self.assertTrue(any("approximate" in n.lower()
                                for n in p.runtime_notes))
        finally:
            if saved is None:
                _sys.modules.pop("cv2", None)
            else:
                _sys.modules["cv2"] = saved

    def test_spawned_cutout_pixels_come_from_a_later_frame(self):
        frames = _two_speed_textures(n=40)
        shape = frames[0].shape
        # Single spawn at birth index 0 (seed=0 => phase 0), donor 10 ahead.
        config = {"lookahead": 1.0, "gap": 10, "spawn_every": 100, "cutouts": 1,
                  "size": 20, "pick": "coherent", "shape": "blob", "seam": "none",
                  "feather": 0, "hold": 8, "melt": 4, "mode": "slide",
                  "drift": 0}
        p = make_processor(config, shape=shape, seed=0, fps=10.0)
        out = push_all(p, frames, fps=10.0)
        self.assertEqual([q.index for q in out], list(range(len(frames))))
        self.assertEqual(p.spawns, 1)
        donor = frames[10]  # gap = 10 ahead of the birth frame
        changed_any = False
        for q in out:
            if q.index < 8:  # hold phase: exact future pixels at same coords
                mask = np.any(q.frame != frames[q.index], axis=2)
                if mask.any():
                    changed_any = True
                    np.testing.assert_array_equal(q.frame[mask], donor[mask])
        self.assertTrue(changed_any, "hold frames never showed a cutout")

    def test_changed_pixels_form_one_coherent_blob(self):
        frames = _two_speed_textures(n=40)
        shape = frames[0].shape
        config = {"lookahead": 1.0, "gap": 10, "spawn_every": 100, "cutouts": 1,
                  "size": 20, "pick": "coherent", "shape": "blob", "seam": "none",
                  "feather": 10, "hold": 8, "melt": 4, "mode": "slide",
                  "drift": 0, "similarity": 60}
        p = make_processor(config, shape=shape, seed=0, fps=10.0)
        out = push_all(p, frames, fps=10.0)
        examined = 0
        for q in out:
            if q.index >= 8:
                continue  # melt/after: dissolving is allowed to fragment
            diff = np.any(q.frame != frames[q.index], axis=2)
            if diff.sum() < 4:
                continue
            components = label_components(diff)
            examined += 1
            self.assertGreaterEqual(len(components), 1, f"frame {q.index}")
            self.assertLessEqual(len(components), 3,
                                 f"frame {q.index}: {len(components)} blobs")
            self.assertGreaterEqual(components[0] / diff.sum(), 0.6,
                                    f"frame {q.index} largest blob too small")
        self.assertGreater(examined, 0, "no hold frame had any changed pixels")

    def test_coherent_pick_avoids_regions_that_look_identical_ahead(self):
        """A cutout must carry visible displacement, not a copy of itself.

        Left half: one big soft region that is identical 10 frames later (a
        static surface). Right half: a textured region that moves. A coherent
        cutout has to come from the moving half, otherwise it is invisible.
        """
        h, w = 48, 64
        frames = []
        for i in range(40):
            frame = np.zeros((h, w, 3), np.uint8)
            xx = np.arange(w, dtype=np.float32)[None, :]
            yy = np.arange(h, dtype=np.float32)[:, None]
            soft = (90 + 50 * np.sin(xx / 9.0) * np.cos(yy / 11.0))
            frame[:, :w // 2] = np.repeat(soft[:, :w // 2, None], 3, axis=2)
            stripe = (120 + 110 * (((xx + 3 * i) % 7) < 3)).astype(np.float32)
            frame[:, w // 2:] = np.repeat(stripe[:, w // 2:, None], 3, axis=2)
            frames.append(frame)
        config = {"lookahead": 1.0, "gap": 10, "spawn_every": 100, "size": 12,
                  "pick": "coherent", "seam": "none", "feather": 0,
                  "hold": 6, "melt": 2, "mode": "slide", "drift": 0,
                  "min_motion": 0, "max_motion": 100}
        p = make_processor(config, shape=frames[0].shape, seed=0, fps=10.0)
        out = push_all(p, frames, fps=10.0)
        self.assertEqual(p.spawns, 1)
        changed = np.zeros((h, w), bool)
        for q in out[:6]:
            changed |= np.any(q.frame != frames[q.index], axis=2)
        self.assertTrue(changed.any(), "no cutout was composited")
        cols = np.nonzero(changed)[1]
        self.assertGreater(cols.mean(), w // 2,
                           "cutout came from the region that is identical ahead")
        self.assertGreater(changed[:, w // 2:].sum() / changed.sum(), 0.9)

    def test_blob_area_follows_the_size_setting(self):
        frames = _two_speed_textures(n=40)
        areas = {}
        for size in (10, 25):
            config = {"lookahead": 1.0, "gap": 10, "spawn_every": 100,
                      "size": size, "pick": "coherent", "seam": "none",
                      "feather": 0, "hold": 8, "melt": 2, "mode": "slide",
                      "drift": 0, "similarity": 0, "min_motion": 0,
                      "max_motion": 100}
            p = make_processor(config, shape=frames[0].shape, seed=0, fps=10.0)
            out = push_all(p, frames, fps=10.0)
            self.assertEqual(p.spawns, 1)
            q = out[3]
            changed = np.any(q.frame != frames[q.index], axis=2)
            areas[size] = changed.mean()
        self.assertGreater(areas[25], areas[10] * 1.4,
                           f"size does not scale the blob: {areas}")
        self.assertGreater(areas[25], 0.12, f"25% blob covers only {areas[25]:.3f}")
        self.assertLess(areas[25], 0.45, f"25% blob covers {areas[25]:.3f}")

    def test_high_resolution_lookahead_is_clamped_to_the_budget(self):
        """A 1080p60 clip cannot hold a 2s horizon, so it must shorten it."""
        processor = FutureCut()
        processor.initialize({"lookahead": 2.0, "cutouts": 3},
                             (1080, 1920, 3), 60.0, 0)
        self.assertLessEqual(processor.capacity, FutureCut.MAX_FRAMES)
        self.assertLess(processor.capacity, 40,
                        "1080p must not pretend it can buffer 40 frames")
        self.assertLessEqual(processor._lookahead_frames, processor.capacity - 1)
        self.assertTrue(any("look-ahead limited" in note
                            for note in processor.runtime_notes),
                        processor.runtime_notes)
        self.assertGreaterEqual(processor.capacity, 5,
                                "1080p should still buffer a few frames")
        self.assertLessEqual(processor.capacity * 1920 * 1080 * 3
                             + 3 * 3 * 1920 * 1080 * 3,
                             FutureCut.MAX_STATE_BYTES)

    def test_residue_after_hold_and_melt_is_live_frame(self):
        frames = _two_speed_textures(n=40)
        shape = frames[0].shape
        for mode in ("slide", "smudge"):
            config = {"lookahead": 1.0, "gap": 10, "spawn_every": 100,
                      "cutouts": 1, "size": 20, "pick": "coherent",
                      "shape": "blob", "seam": "none", "feather": 10,
                      "hold": 2, "melt": 6, "mode": mode, "drift": 0}
            p = make_processor(config, shape=shape, seed=0, fps=10.0)
            out = push_all(p, frames, fps=10.0)
            last = out[-1]
            np.testing.assert_array_equal(last.frame, frames[last.index],
                                          f"mode={mode} left residue")

    def test_determinism_same_seed_different_seed(self):
        frames = _two_speed_textures(n=30)
        shape = frames[0].shape
        config = {"lookahead": 1.0, "gap": 10, "spawn_every": 4, "size": 20,
                  "hold": 3, "melt": 6}

        def run(seed):
            p = make_processor({**config, "seed": seed}, shape=shape,
                               seed=seed, fps=10.0)
            return [q.frame for q in push_all(p, frames, fps=10.0)]

        first = run(7)
        second = run(7)
        other = run(8)
        self.assertTrue(all(np.array_equal(a, b)
                            for a, b in zip(first, second)))
        self.assertTrue(any(not np.array_equal(a, b)
                            for a, b in zip(first, other)))

    def test_reset_and_scene_cut_and_index_discontinuity(self):
        shape = (24, 32, 3)
        rng = np.random.default_rng(6)
        frames = [rng.integers(0, 255, shape, dtype=np.uint8) for _ in range(40)]
        p = make_processor({"lookahead": 0.3, "gap": 5}, shape=shape, fps=10.0)
        for i in range(20):
            list(p.push(packet(frames[i], i, Fraction(i, 10))))
        self.assertGreater(p.state_nbytes, 0)
        p.reset()
        self.assertEqual(p.state_nbytes, 0)
        self.assertEqual(p.spawns, 0)
        self.assertEqual(p.active_cutouts, 0)

        # explicit scene_cut drains the old buffer then starts a new scene
        p2 = make_processor({"lookahead": 0.3, "gap": 5}, shape=shape, fps=10.0)
        out = []
        for i in range(10):
            out.extend(p2.push(packet(frames[i], i, Fraction(i, 10))))
        # lookahead 0.3 @10fps => 4-frame delay: frames 0..5 already emitted
        self.assertEqual([q.index for q in out], list(range(6)))
        drained = list(p2.push(packet(frames[10], 10, Fraction(10, 10),
                                      {"scene_cut": True})))
        self.assertEqual([q.index for q in drained], [6, 7, 8, 9])
        tail = list(p2.flush())
        self.assertEqual([q.index for q in tail], [10])
        self.assertEqual([q.index for q in out + drained + tail], list(range(11)))

        # index discontinuity resets the timeline
        p3 = make_processor({"lookahead": 0.3, "gap": 5}, shape=shape, fps=10.0)
        for i in range(5):
            list(p3.push(packet(frames[i], i, Fraction(i, 10))))
        list(p3.push(packet(frames[20], 20, Fraction(20, 10))))  # jump
        self.assertEqual(p3.state_nbytes, 1 * frames[0].nbytes)

    def test_bounded_state_over_long_clip(self):
        shape = (32, 48, 3)
        p = make_processor({"lookahead": 0.3, "gap": 5}, shape=shape, fps=30.0)
        rng = np.random.default_rng(8)
        for i in range(300):
            frame = rng.integers(0, 255, shape, dtype=np.uint8)
            list(p.push(packet(frame, i, Fraction(i, 30))))
            self.assertLessEqual(p.state_nbytes, p.MAX_STATE_BYTES)
        list(p.flush())
        self.assertEqual(p.state_nbytes, 0)

    def test_input_not_mutated(self):
        shape = (24, 32, 3)
        rng = np.random.default_rng(9)
        frames = [rng.integers(0, 255, shape, dtype=np.uint8) for _ in range(20)]
        saved = [f.copy() for f in frames]
        p = make_processor({"lookahead": 0.3, "gap": 5}, shape=shape, fps=10.0)
        out = push_all(p, frames, fps=10.0)
        for q in out:
            self.assertFalse(np.shares_memory(q.frame, frames[q.index]))
        for f, s in zip(frames, saved):
            np.testing.assert_array_equal(f, s)


if __name__ == "__main__":
    unittest.main()
