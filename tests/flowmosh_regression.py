#!/usr/bin/env python3
"""flowmosh: flow-warped canvas, keyframes, scene refresh, bounds and rendering."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flow_mosh import FlowMosh
import effex
from harness import audio_codec, env_for, ffprobe_streams, make_clip, run, video_dims

HAVE_CV = (importlib.util.find_spec("numpy") is not None
           and importlib.util.find_spec("cv2") is not None)
if HAVE_CV:
    import cv2
    import numpy as np

SHAPE = (96, 128, 3)


def texture(seed, shape=SHAPE):
    """smooth random RGB texture that optical flow can lock onto"""
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 256, (shape[0] * 2, shape[1] * 2, 3), dtype=np.uint8)
    noise = cv2.GaussianBlur(noise, (0, 0), 3)
    noise = cv2.normalize(noise, None, 0, 255, cv2.NORM_MINMAX)
    return noise


def window(big, dx, dy=0, shape=SHAPE):
    """a frame-sized view into a larger texture, panned by dx/dy pixels"""
    y, x = 20 + dy, 20 + dx
    return np.ascontiguousarray(big[y:y + shape[0], x:x + shape[1]])


def diff(a, b):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


@unittest.skipUnless(HAVE_CV, "install requirements-collapse.txt")
class FlowMoshPixelTests(unittest.TestCase):
    def processor(self, seed=17, shape=SHAPE, **config):
        config.setdefault("detail", "low")
        proc = FlowMosh()
        proc.initialize(config, shape, 30, seed)
        return proc

    def test_first_frame_is_clean_and_shape_dtype_ownership_hold(self):
        proc = self.processor()
        frame = window(texture(1), 0)
        out = proc.process(frame, 0, 0)
        self.assertTrue(np.array_equal(out, frame))
        self.assertFalse(np.shares_memory(out, frame))
        out = proc.process(window(texture(1), 3), 1, 1 / 30)
        self.assertEqual(out.shape, SHAPE)
        self.assertEqual(out.dtype, np.uint8)

    def test_canvas_keeps_stale_pixels_moving_with_new_motion(self):
        old, new = texture(1), texture(2)
        proc = self.processor(bleed=0, keyframe=0, amplify=1.5)
        proc.process(window(old, 0), 0, 0)
        for i in range(1, 10):
            source = window(new, 3 * i)
            out = proc.process(source, i, i / 30)
        # the picture is still the old scene's pixels, not the live frame...
        self.assertGreater(diff(out, source), 15)
        # ...and they were dragged, so they no longer match the first frame either
        self.assertGreater(diff(out, window(old, 0)), 5)

    def test_flow_direction_tracks_a_pan(self):
        big = texture(3)
        proc = self.processor(bleed=0, keyframe=0, amplify=1, blocks=0)
        first = window(big, 0)
        proc.process(first, 0, 0)
        for i in range(1, 6):
            source = window(big, 2 * i)
            out = proc.process(source, i, i / 30)
        core = (slice(16, -16), slice(24, -24))
        # warping the first frame along the pan lands near the real frame
        self.assertLess(diff(out[core], source[core]), diff(first[core], source[core]) * .6)

    def test_keyframe_refresh_resets_canvas_to_source(self):
        old, new = texture(1), texture(2)
        proc = self.processor(bleed=0, keyframe=5)
        proc.process(window(old, 0), 0, 0)
        refreshed = []
        for i in range(1, 16):
            source = window(new, 3 * i)
            out = proc.process(source, i, i / 30)
            refreshed.append(np.array_equal(out, source))
        self.assertTrue(any(refreshed))
        self.assertFalse(all(refreshed))
        first = refreshed.index(True) + 1
        self.assertTrue(4 <= first <= 6, first)  # 5 frames +-25% seeded jitter
        never = self.processor(bleed=0, keyframe=0)
        never.process(window(old, 0), 0, 0)
        for i in range(1, 30):
            source = window(new, 3 * i)
            self.assertFalse(np.array_equal(never.process(source, i, i / 30), source))

    def test_scene_refresh_choice(self):
        old, new = texture(1), texture(2)
        for refresh, expect_clean in (("scene", True), ("never", False)):
            proc = self.processor(bleed=0, keyframe=0, refresh=refresh)
            proc.process(window(old, 0), 0, 0)
            proc.process(window(old, 2), 1, 1 / 30)
            cut = window(new, 0)
            out = proc.process(cut, 2, 2 / 30, {"scene_cut": True})
            self.assertEqual(np.array_equal(out, cut), expect_clean, refresh)

    def test_bleed_and_still_pull_back_to_the_live_frame(self):
        old, new = window(texture(1), 0), window(texture(2), 0)
        full = self.processor(bleed=100, keyframe=0)
        full.process(old, 0, 0)
        self.assertTrue(np.array_equal(full.process(new, 1, 1 / 30), new))
        held = self.processor(bleed=0, keyframe=0, still=0)
        still = self.processor(bleed=0, keyframe=0, still=100)
        for proc in (held, still):
            proc.process(old, 0, 0)
            proc.process(new, 1, 1 / 30)
            proc.process(new, 2, 2 / 30)  # no motion between these two
        self.assertLess(diff(still.process(new, 3, 3 / 30), new), 1)
        self.assertGreater(diff(held.process(new, 3, 3 / 30), new), 15)

    def test_same_seed_is_deterministic(self):
        def render(seed):
            proc = self.processor(seed=seed, keyframe=7, bleed=3)
            old, new = texture(1), texture(2)
            frames = [window(old, 0)] + [window(new, 2 * i, i % 3) for i in range(1, 20)]
            return [proc.process(f, i, i / 30) for i, f in enumerate(frames)]
        a, b = render(5), render(5)
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, b)))

    def test_resets_on_index_jump_and_source_change(self):
        old, new = window(texture(1), 0), window(texture(2), 0)
        for index, context in ((7, {"source_id": "a"}), (1, {"source_id": "b"})):
            proc = self.processor(bleed=0, keyframe=0)
            proc.process(old, 0, 0, {"source_id": "a"})
            self.assertTrue(np.array_equal(proc.process(new, index, index / 30, context), new))
        proc.reset()
        self.assertIsNone(proc._canvas)

    def test_state_is_bounded_and_bad_input_rejected(self):
        proc = self.processor(blocks=8)
        for i in range(40):
            proc.process(window(texture(1), i % 10), i, i / 30)
        self.assertLess(proc.state_nbytes, FlowMosh.MAX_STATE_BYTES)
        self.assertLess(proc.state_nbytes, SHAPE[0] * SHAPE[1] * 3 * 4 * 3)
        self.assertNotIn("canvas", proc.serialize_config())
        for frame in (np.zeros(SHAPE, float), np.zeros((10, 10, 3), np.uint8)):
            with self.assertRaises(ValueError):
                proc.process(frame, 40, 40 / 30)
        with patch.object(FlowMosh, "MAX_STATE_BYTES", 1000):
            with self.assertRaisesRegex(ValueError, "resize"):
                self.processor()
        clamped = self.processor(amplify=99, bleed=-4, refresh="bogus", detail="x")
        cfg = clamped.serialize_config()
        self.assertEqual((cfg["amplify"], cfg["bleed"], cfg["refresh"], cfg["detail"]),
                         (6.0, 0, "never", "medium"))


class FlowMoshRegistryTests(unittest.TestCase):
    def test_effect_is_grouped_chainable_and_lists_without_numpy(self):
        chain, error = effex.parse_chain("flowmosh+deepfry")
        self.assertFalse(error)
        self.assertIsNone(chain.multi_pass)
        group = dict(effex.EFFECT_GROUPS)["CODEC & BLOCKS"]
        self.assertEqual(group.index("flowmosh"), group.index("blockswap") + 1)
        proc = run([sys.executable, "-S", "-c",
                    "import effex; print(effex.EFFECT_BY_KEY['flowmosh'].name)"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"flow mosh", proc.stdout)


@unittest.skipUnless(HAVE_CV and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "NumPy, OpenCV and FFmpeg required")
class FlowMoshRenderTests(unittest.TestCase):
    def test_cli_render_chain_and_option_validation(self):
        with tempfile.TemporaryDirectory(prefix="effex-flowmosh-") as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            self.assertTrue(make_clip(source, seconds=1, size="160x90"))
            env = env_for(folder, folder / "out")
            outputs = []
            for setting in ("bleed=100", "bleed=0"):
                proc = run([effex.__file__, "flowmosh", source, "--preset", "ultrafast",
                            "--json", "--set", setting, "--set", "seed=17"], env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                data = json.loads(proc.stdout)
                self.assertIn("_FLOWMOSHED", data["output"])
                self.assertEqual(video_dims(data["output"]), (160, 90))
                self.assertEqual(audio_codec(data["output"]), "aac")
                streams = ffprobe_streams(data["output"])["streams"]
                video = next(s for s in streams if s["codec_type"] == "video")
                self.assertEqual(int(video["nb_frames"]), 30)
                pixels = run(["ffmpeg", "-v", "error", "-i", data["output"], "-an",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
                self.assertEqual(pixels.returncode, 0, pixels.stderr.decode())
                outputs.append(pixels.stdout)
            self.assertNotEqual(outputs[0], outputs[1])
            proc = run([effex.__file__, "flowmosh+deepfry", source,
                        "--preset", "ultrafast", "--json"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            data = json.loads(proc.stdout)
            self.assertEqual(video_dims(data["output"]), (160, 90))
            self.assertEqual(audio_codec(data["output"]), "aac")
            for setting in ("amplify=7", "blocks=65", "keyframe=-1", "refresh=bogus",
                            "detail=ultra", "bleed=101"):
                proc = run([effex.__file__, "flowmosh", source, "--set", setting,
                            "--dry-run"], env_extra=env)
                self.assertEqual(proc.returncode, 2, setting)


if __name__ == "__main__":
    unittest.main()
