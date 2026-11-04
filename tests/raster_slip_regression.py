#!/usr/bin/env python3
"""Raster Slip pixel provenance, bounds, reset behavior and shared rendering."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raster_slip import RasterSlip
import effex
from harness import audio_codec, env_for, ffprobe_streams, make_clip, run, video_dims

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
if HAVE_NUMPY:
    import numpy as np


@unittest.skipUnless(HAVE_NUMPY, "install requirements-stateful.txt")
class RasterPixelTests(unittest.TestCase):
    def processor(self, shape=(20, 40, 3), seed=17, **config):
        proc = RasterSlip()
        proc.initialize(config, shape, 10, seed)
        return proc

    def test_bands_borrow_distinct_real_past_frames_and_leave_live_gaps(self):
        proc = self.processor(strength=100, coverage=40, shift=0, split=0, speed=0, delay=.8)
        for i in range(26):
            current = np.full((20, 40, 3), i * 4, np.uint8)
            output = proc.process(current, i, i / 10, {})
        values = np.unique(output)
        self.assertIn(100, values)  # Live bands are still at the current time.
        self.assertGreater(len(values[values < 100]), 1)
        self.assertTrue(all(v % 4 == 0 for v in values))  # No fabricated/interpolated history.
        self.assertGreaterEqual(int(values.min()), 68)
        self.assertTrue(np.all(output == output[:, :1, :1]))

    def test_channel_offsets_wrap_inside_the_slipped_bands(self):
        frame = np.zeros((20, 40, 3), np.uint8)
        frame[:] = np.arange(40, dtype=np.uint8)[None, :, None] * 5
        proc = self.processor(strength=100, coverage=100, shift=0, split=5, speed=0)
        self.assertTrue(np.array_equal(proc.process(frame, 0, 0), frame))
        result = proc.process(frame, 1, .1)
        self.assertTrue(np.array_equal(result[:, :, 0], np.roll(frame[:, :, 0], 2, axis=1)))
        self.assertTrue(np.array_equal(result[:, :, 1], frame[:, :, 1]))
        self.assertTrue(np.array_equal(result[:, :, 2], np.roll(frame[:, :, 2], -2, axis=1)))

    def test_inputs_and_history_are_owned_and_zero_controls_disable(self):
        for config in (dict(strength=0), dict(coverage=0), dict(delay=0)):
            proc = self.processor(**config)
            for i in range(4):
                frame = np.full((20, 40, 3), i * 20, np.uint8)
                output = proc.process(frame, i, i / 10)
                self.assertTrue(np.array_equal(output, frame))
                self.assertFalse(np.shares_memory(output, frame))
                self.assertFalse(proc._history)
        proc = self.processor(strength=100, coverage=100, shift=0, split=0)
        first = np.full((20, 40, 3), 30, np.uint8)
        output = proc.process(first, 0, 0)
        output[:] = 0
        first[:] = 99
        second = np.full_like(first, 60)
        result = proc.process(second, 1, .1)
        self.assertTrue(np.all(result == 30))
        self.assertTrue(np.all(second == 60))

    def test_strength_blends_and_sideways_slip_changes_spatial_detail(self):
        first = np.zeros((20, 40, 3), np.uint8)
        first[:, :20] = 80
        full = self.processor(strength=100, coverage=100, shift=30, split=0, speed=0)
        half = self.processor(strength=50, coverage=100, shift=30, split=0, speed=0)
        for proc in (full, half):
            proc.process(first, 0, 0)
        current = np.full_like(first, 40)
        changed = full.process(current, 1, .1)
        blended = half.process(current, 1, .1)
        self.assertFalse(np.array_equal(changed, first))
        self.assertTrue(np.array_equal(blended, (changed.astype(np.uint16) + current) // 2))

    def test_moving_band_pattern_and_seed_are_repeatable(self):
        def masks(seed, speed):
            proc = self.processor(seed=seed, speed=speed, strength=100, shift=0, split=0)
            output = []
            for i in range(8):
                frame = np.full((20, 40, 3), i * 10, np.uint8)
                result = proc.process(frame, i, i / 10)
                output.append(np.any(result != frame, axis=(1, 2)))
            return output
        a, b, other = masks(17, .5), masks(17, .5), masks(18, .5)
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, b)))
        self.assertTrue(any(not np.array_equal(x, y) for x, y in zip(a, other)))
        self.assertFalse(np.array_equal(a[1], a[-1]))
        pinned = masks(17, 0)
        self.assertTrue(all(np.array_equal(pinned[1], value) for value in pinned[2:]))

    def test_reset_on_cuts_time_rewind_source_change_and_index_jump(self):
        for index, stamp, context, level in (
                (3, .3, {"scene_cut": True, "source_id": "a"}, 60),
                (3, -.1, {"source_id": "a"}, 60),
                (3, .3, {"source_id": "b"}, 60),
                (10, .3, {"source_id": "a"}, 60),
                (3, .3, {"source_id": "a"}, 255)):
            proc = self.processor(strength=100, coverage=100)
            for i in range(3):
                proc.process(np.full((20, 40, 3), i * 10, np.uint8), i, i / 10, {"source_id": "a"})
            frame = np.full((20, 40, 3), level, np.uint8)
            self.assertTrue(np.array_equal(proc.process(frame, index, stamp, context), frame))
        proc.reset()
        self.assertFalse(proc._history)
        self.assertIsNone(proc._sample)

    def test_memory_is_bounded_and_clamped_delay_is_reported(self):
        with patch.object(RasterSlip, "MAX_STATE_BYTES", 16_000):
            proc = self.processor(shape=(16, 32, 3), delay=2)
            self.assertLess(proc.effective_delay, 2)
            self.assertTrue(proc.runtime_notes)
            for i in range(100):
                proc.process(np.full((16, 32, 3), i % 100, np.uint8), i, i / 10)
                self.assertLessEqual(proc.state_nbytes, 16_000)
        config = proc.serialize_config()
        self.assertEqual(config["delay"], 2)
        self.assertNotIn("history", config)
        with patch.object(RasterSlip, "MAX_STATE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "resize"):
                self.processor()

    def test_bad_frame_data_is_rejected_and_reinitialize_changes_shape(self):
        proc = self.processor()
        for frame in (np.zeros((20, 40, 3), float), np.zeros((10, 40, 3), np.uint8)):
            with self.assertRaises(ValueError):
                proc.process(frame, 0, 0)
        proc.initialize({}, (10, 12, 3), float("nan"), 17)
        frame = np.zeros((10, 12, 3), np.uint8)
        self.assertTrue(np.array_equal(proc.process(frame, 0, None), frame))


class RasterRegistryTests(unittest.TestCase):
    def test_effect_is_grouped_chainable_and_lists_without_numpy(self):
        chain, error = effex.parse_chain("neon+rasterslip+sediment+deepfry")
        self.assertFalse(error)
        self.assertIsNone(chain.multi_pass)
        self.assertIn("rasterslip", dict(effex.EFFECT_GROUPS)["TIME & MEMORY"])
        proc = run([sys.executable, "-S", "-c", "import effex; print(effex.EFFECT_BY_KEY['rasterslip'].name)"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"raster slip", proc.stdout)


@unittest.skipUnless(HAVE_NUMPY and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "NumPy and FFmpeg required")
class RasterRenderTests(unittest.TestCase):
    def test_cli_options_real_render_audio_chaining_and_silent_input(self):
        with tempfile.TemporaryDirectory(prefix="effex-raster-") as temp:
            folder = Path(temp)
            source, silent = folder / "source.mp4", folder / "silent.mp4"
            self.assertTrue(make_clip(source, seconds=1, size="160x90"))
            self.assertTrue(make_clip(silent, seconds=.3, size="160x90", audio=False))
            env = env_for(folder, folder / "out")
            outputs = []
            for settings in (("strength=0",), ("strength=100", "seed=17")):
                args = [effex.__file__, "rasterslip", source, "--preset", "ultrafast", "--json"]
                for setting in settings:
                    args += ["--set", setting]
                proc = run(args, env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                data = json.loads(proc.stdout)
                self.assertIn("_RASTER_SLIPPED", data["output"])
                self.assertEqual(data["options"]["strength"], int(settings[0].split("=")[1]))
                self.assertEqual(video_dims(data["output"]), (160, 90))
                self.assertEqual(audio_codec(data["output"]), "aac")
                streams = ffprobe_streams(data["output"])["streams"]
                video = next(s for s in streams if s["codec_type"] == "video")
                self.assertEqual(int(video["nb_frames"]), 30)
                self.assertAlmostEqual(float(video["duration"]), 1, delta=.04)
                pixels = run(["ffmpeg", "-v", "error", "-i", data["output"], "-an",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
                self.assertEqual(pixels.returncode, 0, pixels.stderr.decode())
                self.assertFalse(pixels.stderr, pixels.stderr.decode())
                outputs.append(pixels.stdout)
            self.assertNotEqual(outputs[0], outputs[1])
            proc = run([effex.__file__, "neon+rasterslip+sediment+deepfry", source,
                        "--preset", "ultrafast", "--json"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            data = json.loads(proc.stdout)
            self.assertEqual(video_dims(data["output"]), (160, 90))
            self.assertEqual(audio_codec(data["output"]), "aac")
            proc = run([effex.__file__, "rasterslip", silent, "--json"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertIsNone(audio_codec(json.loads(proc.stdout)["output"]))
            for setting in ("strength=101", "band_size=0", "delay=nan", "shift=51", "split=-1", "speed=4"):
                proc = run([effex.__file__, "rasterslip", source, "--set", setting, "--dry-run"], env_extra=env)
                self.assertEqual(proc.returncode, 2, proc.stderr.decode())


if __name__ == "__main__":
    unittest.main()
