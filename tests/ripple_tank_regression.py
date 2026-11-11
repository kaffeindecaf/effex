#!/usr/bin/env python3
"""Ripple Tank wave behavior, pixel provenance, bounds, resets and shared rendering."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ripple_tank import RippleTank
import effex
from harness import audio_codec, env_for, ffprobe_streams, make_clip, run, video_dims

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
if HAVE_NUMPY:
    import numpy as np

SHAPE = (24, 48, 3)


def textured(shape=SHAPE):
    h, w, _ = shape
    frame = np.zeros(shape, np.uint8)
    frame[..., 0] = (np.arange(w, dtype=np.uint16)[None, :] * 5 % 256).astype(np.uint8)
    frame[..., 1] = (np.arange(h, dtype=np.uint16)[:, None] * 9 % 256).astype(np.uint8)
    frame[..., 2] = 128
    return frame


def moving(i, shape=SHAPE):
    frame = textured(shape)
    x = 4 + 3 * i
    frame[8:16, x:x + 6] = 255
    return frame


@unittest.skipUnless(HAVE_NUMPY, "install requirements-stateful.txt")
class RipplePixelTests(unittest.TestCase):
    def processor(self, shape=SHAPE, seed=17, **config):
        proc = RippleTank()
        proc.initialize(config, shape, 10, seed)
        return proc

    def test_still_footage_without_rain_stays_untouched(self):
        proc = self.processor(rain=0)
        frame = textured()
        for i in range(10):
            output = proc.process(frame, i, i / 10)
            self.assertTrue(np.array_equal(output, frame))
            self.assertFalse(np.shares_memory(output, frame))
        self.assertFalse(np.any(proc._height))

    def test_motion_makes_waves_that_keep_ringing_after_it_stops(self):
        proc = self.processor(rain=0, strength=100, threshold=0, prism=0, caustics=0, depth=30)
        self.assertTrue(np.array_equal(proc.process(moving(0), 0, 0), moving(0)))
        changed = []
        for i in range(1, 6):
            changed.append(not np.array_equal(proc.process(moving(i), i, i / 10), moving(i)))
        self.assertTrue(any(changed))
        still = moving(5)
        for i in range(6, 9):
            output = proc.process(still, i, i / 10)
        self.assertFalse(np.array_equal(output, still))  # the water is still moving

    def test_pure_refraction_only_moves_existing_pixels(self):
        proc = self.processor(rain=8, strength=100, threshold=0, prism=0, caustics=0, depth=40)
        for i in range(12):
            frame = moving(i)
            output = proc.process(frame, i, i / 10)
        colors = {tuple(c) for c in frame.reshape(-1, 3)}
        self.assertTrue(all(tuple(c) in colors for c in output.reshape(-1, 3)))

    def test_prism_splits_channels_and_caustics_change_brightness(self):
        def last(**config):
            proc = self.processor(rain=8, strength=100, threshold=0, depth=40, **config)
            for i in range(12):
                output = proc.process(moving(i), i, i / 10)
            return output
        plain = last(prism=0, caustics=0)
        split = last(prism=100, caustics=0)
        lit = last(prism=0, caustics=100)
        self.assertTrue(np.array_equal(plain[..., 1], split[..., 1]))
        self.assertFalse(np.array_equal(plain[..., 0], split[..., 0]))
        self.assertFalse(np.array_equal(plain, lit))

    def test_rain_is_repeatable_per_seed(self):
        def run_rain(seed):
            proc = self.processor(seed=seed, rain=20, strength=0)
            frame = textured()
            return [proc.process(frame, i, i / 10) for i in range(15)]
        a, b, other = run_rain(5), run_rain(5), run_rain(6)
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, b)))
        self.assertTrue(any(not np.array_equal(x, y) for x, y in zip(a, other)))
        self.assertTrue(any(not np.array_equal(x, textured()) for x in a))

    def test_zero_controls_disable_the_effect(self):
        for config in (dict(strength=0, rain=0), dict(depth=0, caustics=0)):
            proc = self.processor(**config)
            for i in range(6):
                frame = moving(i)
                output = proc.process(frame, i, i / 10)
                self.assertTrue(np.array_equal(output, frame))
                self.assertFalse(np.shares_memory(output, frame))

    def test_surface_stays_bounded_under_maximum_settings(self):
        proc = self.processor(strength=100, threshold=0, rain=20, ring=100, speed=.7)
        rng = np.random.default_rng(3)
        for i in range(300):
            proc.process(rng.integers(0, 256, SHAPE, dtype=np.uint8), i, i / 10)
        self.assertTrue(np.all(np.isfinite(proc._height)))
        self.assertLessEqual(float(np.abs(proc._height).max()), RippleTank.HEIGHT_LIMIT)

    def test_reset_on_cuts_rewind_source_change_and_index_jump(self):
        for index, stamp, context in ((6, .6, {"scene_cut": True, "source_id": "a"}),
                                      (6, -.1, {"source_id": "a"}),
                                      (6, .6, {"source_id": "b"}),
                                      (20, .6, {"source_id": "a"})):
            proc = self.processor(rain=20, strength=100, threshold=0)
            for i in range(6):
                proc.process(moving(i), i, i / 10, {"source_id": "a"})
            self.assertTrue(np.any(proc._height))
            frame = moving(6)
            self.assertTrue(np.array_equal(proc.process(frame, index, stamp, context), frame))

    def test_large_frames_use_a_bounded_grid(self):
        proc = self.processor(shape=(1080, 1920, 3))
        self.assertLessEqual(max(proc._sim), RippleTank.SIM_CELLS)
        self.assertLess(proc.state_nbytes, 2 * 1024 * 1024)
        self.assertEqual(proc.serialize_config()["depth"], 14)

    def test_bad_frame_data_is_rejected(self):
        proc = self.processor()
        for frame in (np.zeros(SHAPE, float), np.zeros((10, 48, 3), np.uint8)):
            with self.assertRaises(ValueError):
                proc.process(frame, 0, 0)
        with self.assertRaises(RuntimeError):
            RippleTank().process(np.zeros(SHAPE, np.uint8), 0, 0)


class RippleRegistryTests(unittest.TestCase):
    def test_effect_is_grouped_chainable_and_lists_without_numpy(self):
        chain, error = effex.parse_chain("neon+ripple+deepfry")
        self.assertFalse(error)
        self.assertIsNone(chain.multi_pass)
        self.assertIn("ripple", dict(effex.EFFECT_GROUPS)["SHAPE & MOTION"])
        proc = run([sys.executable, "-S", "-c",
                    "import effex; print(effex.EFFECT_BY_KEY['ripple'].name)"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"ripple tank", proc.stdout)


@unittest.skipUnless(HAVE_NUMPY and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "NumPy and FFmpeg required")
class RippleRenderTests(unittest.TestCase):
    def test_cli_render_audio_chaining_and_bad_options(self):
        with tempfile.TemporaryDirectory(prefix="effex-ripple-") as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            self.assertTrue(make_clip(source, seconds=1, size="160x90"))
            env = env_for(folder, folder / "out")
            proc = run([effex.__file__, "ripple", source, "--preset", "ultrafast", "--json",
                        "--set", "rain=10", "--set", "seed=9"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            data = json.loads(proc.stdout)
            self.assertIn("_RIPPLED", data["output"])
            self.assertEqual(data["options"]["rain"], 10)
            self.assertEqual(video_dims(data["output"]), (160, 90))
            self.assertEqual(audio_codec(data["output"]), "aac")
            video = next(s for s in ffprobe_streams(data["output"])["streams"]
                         if s["codec_type"] == "video")
            self.assertEqual(int(video["nb_frames"]), 30)
            proc = run([effex.__file__, "neon+ripple+deepfry", source,
                        "--preset", "ultrafast", "--json"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertEqual(video_dims(json.loads(proc.stdout)["output"]), (160, 90))
            for setting in ("strength=101", "rain=21", "speed=0", "depth=-1", "ring=nan"):
                proc = run([effex.__file__, "ripple", source, "--set", setting, "--dry-run"],
                           env_extra=env)
                self.assertEqual(proc.returncode, 2, proc.stderr.decode())


if __name__ == "__main__":
    unittest.main()
