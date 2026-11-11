#!/usr/bin/env python3
"""Block Melt motion compensation, provenance, resets, determinism and real renders."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from block_melt import BlockMelt
import effex
from harness import audio_codec, env_for, ffprobe_streams, make_clip, run, video_dims

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
if HAVE_NUMPY:
    import numpy as np

SHAPE = (64, 128, 3)
# Same luma, different hue: the vectors cannot tell them apart, so the
# moshed canvas keeps the old red picture while the source turns green.
RED, GREEN = (255, 0, 0), (0, 130, 0)
PURE = dict(residual=0, leak=0, intra=0, repeat=1)


def square(x, color, shape=SHAPE):
    frame = np.zeros(shape, np.uint8)
    frame[16:48, x:x + 32] = color
    return frame


def texture(i, shape=SHAPE):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    base = 127 + 90 * np.sin((yy + 3 * i) / 7.0) * np.cos((xx - 5 * i) / 11.0)
    return np.repeat(base[..., None], 3, -1).astype(np.uint8) + np.array([0, 10, 20], np.uint8)


@unittest.skipUnless(HAVE_NUMPY, "install requirements-stateful.txt")
class BlockMeltPixelTests(unittest.TestCase):
    def processor(self, shape=SHAPE, seed=17, **config):
        proc = BlockMelt()
        proc.initialize(config, shape, 30, seed)
        return proc

    def test_shape_dtype_ownership_and_first_frame_is_clean(self):
        proc = self.processor()
        frame = texture(0)
        out = proc.process(frame, 0, 0)
        self.assertEqual((out.shape, out.dtype), (SHAPE, np.uint8))
        self.assertTrue(np.array_equal(out, frame))
        self.assertFalse(np.shares_memory(out, frame))
        out[:] = 0
        second = proc.process(texture(1), 1, 1 / 30)
        self.assertEqual((second.shape, second.dtype), (SHAPE, np.uint8))
        self.assertGreater(int(second.max()), 0)  # caller edits never reach the canvas

    def test_moving_square_drags_the_stale_canvas_block_by_block(self):
        proc = self.processor(block=16, **PURE)
        previous = proc.process(square(16, RED), 0, 0)
        for i in range(1, 7):
            out = proc.process(square(16 + 4 * i, GREEN), i, i / 30)
            # Pure motion compensation: each block is a translated copy of the
            # previous canvas along its own vector (edges clamp).
            h, w, _ = SHAPE
            for by in range(0, h, 16):
                for bx in range(0, w, 16):
                    vy, vx = proc.last_vectors[by // 16, bx // 16]
                    rows = np.clip(np.arange(by, by + 16) + vy, 0, h - 1)
                    cols = np.clip(np.arange(bx, bx + 16) + vx, 0, w - 1)
                    self.assertTrue(np.array_equal(out[by:by + 16, bx:bx + 16],
                                                   previous[np.ix_(rows, cols)]))
            previous = out
        colors = {tuple(c) for c in out.reshape(-1, 3)}
        self.assertEqual(colors, {(0, 0, 0), RED})  # nothing new is drawn, the old red is pushed
        red_x = np.nonzero(np.all(out == RED, axis=-1))[1]
        self.assertGreaterEqual(red_x.max(), 16 + 24 + 31 - 4)  # followed the green square right
        self.assertTrue(np.any(proc.last_vectors[..., 1] < 0))  # cur(p) ~ prev(p + v)

    def test_bloom_repeats_the_vectors_and_moves_further(self):
        reach = []
        for repeat in (1, 3):
            proc = self.processor(**dict(PURE, repeat=repeat))
            proc.process(square(16, RED), 0, 0)
            for i in range(1, 4):
                out = proc.process(square(16 + 4 * i, GREEN), i, i / 30)
            reach.append(np.nonzero(np.all(out == RED, axis=-1))[1].max())
        self.assertGreater(reach[1], reach[0])

    def test_keyframes_reset_to_the_clean_source(self):
        proc = self.processor(keyframe=3, **{k: v for k, v in PURE.items()})
        for i in range(8):
            frame = square(16 + 4 * i, RED if i == 0 else GREEN)
            out = proc.process(frame, i, i / 30)
            if i % 3 == 0:
                self.assertTrue(np.array_equal(out, frame), i)
            elif i in (1, 2):
                self.assertFalse(np.array_equal(out, frame), i)
        self.assertTrue(np.any(np.all(out == GREEN, axis=-1)))  # frame 6 keyframe went green

    def test_leak_100_without_grit_is_the_source(self):
        proc = self.processor(leak=100, grit=0, residual=0)
        for i in range(5):
            frame = texture(i)
            self.assertTrue(np.array_equal(proc.process(frame, i, i / 30), frame))

    def test_grit_coarsens_leaked_blocks_and_residual_adds_detail_back(self):
        proc = self.processor(leak=100, grit=80)
        proc.process(texture(0), 0, 0)
        out = proc.process(texture(1), 1, 1 / 30)
        self.assertFalse(np.array_equal(out, texture(1)))
        self.assertLess(len(np.unique(out)), len(np.unique(texture(1))))
        errors = []
        for residual in (0, 60):
            proc = self.processor(**dict(PURE, residual=residual))
            proc.process(square(16, RED), 0, 0)
            for i in range(1, 4):
                out = proc.process(square(16 + 4 * i, GREEN), i, i / 30)
            errors.append(np.abs(out.astype(int) - square(28, GREEN)).mean())
        self.assertLess(errors[1], errors[0])

    def test_seed_is_deterministic_and_reset_replays(self):
        def render(seed):
            proc = self.processor(seed=seed, leak=30)
            outs = [proc.process(texture(i), i, i / 30) for i in range(6)]
            proc.reset()
            again = [proc.process(texture(i), i, i / 30) for i in range(6)]
            return outs, again
        a, a_again = render(5)
        b, _ = render(5)
        c, _ = render(6)
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, b)))
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, a_again)))
        self.assertTrue(any(not np.array_equal(x, y) for x, y in zip(a, c)))

    def test_index_jump_rewind_and_source_change_start_clean(self):
        for index, stamp, context in ((9, .3, {"source_id": "a"}),
                                      (3, -.1, {"source_id": "a"}),
                                      (3, .1, {"source_id": "b"})):
            proc = self.processor(**PURE)
            for i in range(3):
                proc.process(square(16 + 4 * i, RED), i, i / 30, {"source_id": "a"})
            frame = square(60, GREEN)
            self.assertTrue(np.array_equal(proc.process(frame, index, stamp, context), frame))

    def test_scene_cuts_are_moshed_through(self):
        proc = self.processor(**PURE)
        proc.process(square(16, RED), 0, 0)
        out = proc.process(square(20, GREEN), 1, 1 / 30, {"scene_cut": True})
        self.assertFalse(np.any(np.all(out == GREEN, axis=-1)))

    def test_memory_is_bounded_and_odd_sizes_work(self):
        for block in (8, 16, 32):
            proc = self.processor(shape=(37, 53, 3), block=block, search=32, leak=20)
            for i in range(6):
                out = proc.process(texture(i, (37, 53, 3)), i, i / 30)
                self.assertEqual(out.shape, (37, 53, 3))
            self.assertLessEqual(proc.state_nbytes, 2 * 37 * 53 * 3 + 37 * 53 * 4)
            self.assertEqual(proc.serialize_config()["block"], block)
        tiny = self.processor(shape=(3, 5, 3))
        for i in range(3):
            tiny.process(np.full((3, 5, 3), i * 40, np.uint8), i, i / 30)
        with patch.object(BlockMelt, "MAX_STATE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "resize"):
                self.processor()

    def test_config_is_clamped_and_bad_frames_are_rejected(self):
        proc = self.processor(block="12", search=999, residual=-5, repeat="nan", leak=500)
        config = proc.serialize_config()
        self.assertEqual((config["search"], config["residual"], config["repeat"], config["leak"]),
                         (32, 0, 1, 100))
        self.assertIn(config["block"], (8, 16))
        for frame in (np.zeros(SHAPE, float), np.zeros((10, 40, 3), np.uint8)):
            with self.assertRaises(ValueError):
                proc.process(frame, 0, 0)


class BlockMeltRegistryTests(unittest.TestCase):
    def test_effect_is_grouped_after_datamosh_chainable_and_lists_without_numpy(self):
        codec = dict(effex.EFFECT_GROUPS)["CODEC & BLOCKS"]
        self.assertEqual(codec[codec.index("datamosh") + 1], "blockmelt")
        effect = effex.EFFECT_BY_KEY["blockmelt"]
        self.assertIs(effect.frame_processor, BlockMelt)
        self.assertEqual({p.key for p in effect.params},
                         set(BlockMelt.DEFAULTS))
        for param in effect.params:
            default = BlockMelt.DEFAULTS[param.key]
            self.assertEqual(int(param.default), default, param.key)
        chain, error = effex.parse_chain("blockmelt+crt")
        self.assertFalse(error)
        self.assertIsNone(chain.multi_pass)
        proc = run([sys.executable, "-S", "-c",
                    "import effex; print(effex.EFFECT_BY_KEY['blockmelt'].name)"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"block melt", proc.stdout)


@unittest.skipUnless(HAVE_NUMPY and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "NumPy and FFmpeg required")
class BlockMeltRenderTests(unittest.TestCase):
    def frames_of(self, path):
        pixels = run(["ffmpeg", "-v", "error", "-i", path, "-an",
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        self.assertEqual(pixels.returncode, 0, pixels.stderr.decode())
        return pixels.stdout

    def test_cli_render_options_chain_and_bounds(self):
        with tempfile.TemporaryDirectory(prefix="effex-blockmelt-") as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            self.assertTrue(make_clip(source, seconds=1, size="160x96"))
            env = env_for(folder, folder / "out")
            outputs = []
            for settings in (("leak=100", "grit=0", "residual=0"), ("seed=17", "repeat=3")):
                args = [effex.__file__, "blockmelt", source, "--preset", "ultrafast", "--json"]
                for setting in settings:
                    args += ["--set", setting]
                proc = run(args, env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                data = json.loads(proc.stdout)
                self.assertIn("_BLOCKMELTED", data["output"])
                self.assertEqual(video_dims(data["output"]), (160, 96))
                self.assertEqual(audio_codec(data["output"]), "aac")
                video = next(s for s in ffprobe_streams(data["output"])["streams"]
                             if s["codec_type"] == "video")
                self.assertEqual(int(video["nb_frames"]), 30)
                outputs.append(self.frames_of(data["output"]))
            self.assertNotEqual(outputs[0], outputs[1])
            proc = run([effex.__file__, "blockmelt+crt", source, "--preset", "ultrafast",
                        "--set", "blockmelt.seed=3", "--json"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            data = json.loads(proc.stdout)
            self.assertEqual(video_dims(data["output"]), (160, 96))
            self.assertEqual(audio_codec(data["output"]), "aac")
            dry = run([effex.__file__, "blockmelt", source, "--set", "keyframe=30", "--dry-run"],
                      env_extra=env)
            self.assertEqual(dry.returncode, 0, dry.stderr.decode())
            self.assertIn(b"keyframe every 30", dry.stdout)
            for setting in ("residual=101", "repeat=0", "search=64", "leak=-1", "block=12"):
                proc = run([effex.__file__, "blockmelt", source, "--set", setting, "--dry-run"],
                           env_extra=env)
                self.assertEqual(proc.returncode, 2, setting)


if __name__ == "__main__":
    unittest.main()
