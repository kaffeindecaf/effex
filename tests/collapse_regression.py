#!/usr/bin/env python3
"""Elastic collapse processor and shared-renderer integration tests."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import numpy as np
    import cv2  # noqa: F401 - optional dependency gate
except ImportError:
    print("  SKIP collapse tests need requirements-collapse.txt")
    sys.exit(0)
import effex
import stateful_effects
from harness import EFFEX, audio_codec, env_for, ffprobe_streams, make_clip, run, video_dims


class CollapseTests(unittest.TestCase):
    def processor(self, shape=(64, 96, 3), **config):
        self.assertTrue(hasattr(stateful_effects, "ElasticCollapse"),
                        "ElasticCollapse processor must exist")
        processor = stateful_effects.ElasticCollapse()
        processor.initialize(config, shape, 30, 17)
        return processor

    @staticmethod
    def frame(shape=(64, 96, 3)):
        y, x = np.indices(shape[:2])
        return np.stack(((x * 3) % 256, (y * 4) % 256,
                         ((x // 6 + y // 6) % 2) * 255), axis=-1).astype(np.uint8)

    def test_elastic_deformation_preserves_rgb_shape_without_mutation(self):
        frame = self.frame()
        original = frame.copy()
        processor = self.processor(mode="elastic", intensity=100, preserve=0)
        out = processor.process(frame, 0, 0.0, {})
        self.assertEqual(out.shape, frame.shape)
        self.assertEqual(out.dtype, np.uint8)
        np.testing.assert_array_equal(frame, original)
        self.assertTrue(out.flags.c_contiguous)
        self.assertGreater(float(np.abs(out.astype(float) - frame).mean()), 4)
        # A uniform zoom cannot create a row-dependent horizontal displacement.
        field = processor.displacement
        self.assertGreater(float(np.std(field[:, field.shape[1] // 2, 0])), 0.005)

    def test_strength_scales_displacement_and_pixel_change(self):
        frame = self.frame()
        low = self.processor(mode="elastic", intensity=15, preserve=0)
        high = self.processor(mode="elastic", intensity=100, preserve=0)
        a = low.process(frame, 0, 0.0, {})
        b = high.process(frame, 0, 0.0, {})
        self.assertGreater(float(np.linalg.norm(high.displacement)),
                           5 * float(np.linalg.norm(low.displacement)))
        self.assertGreater(float(np.abs(b.astype(float) - frame).mean()),
                           float(np.abs(a.astype(float) - frame).mean()))

    def test_off_and_disabled_axes_are_exact_passthrough(self):
        frame = self.frame()
        for config in (dict(intensity=0), dict(horizontal=0, vertical=0)):
            with self.subTest(config=config):
                p = self.processor(**config)
                for i in range(3):
                    np.testing.assert_array_equal(p.process(frame, i, i / 30, {}), frame)

    def test_seed_repeatability_and_reset(self):
        frame = self.frame()
        a = self.processor(mode="elastic")
        b = self.processor(mode="elastic")
        first = a.process(frame, 0, 0.0, {})
        b.process(frame, 0, 0.0, {})
        for i in range(8):
            np.testing.assert_array_equal(a.process(frame, i + 1, (i + 1) / 30, {}),
                                          b.process(frame, i + 1, (i + 1) / 30, {}))
        a.reset()
        np.testing.assert_array_equal(a.process(frame, 0, 0.0, {}), first)
        b.initialize({"mode": "elastic"}, frame.shape, 30, 99)
        self.assertFalse(np.array_equal(b.process(frame, 0, 0.0, {}), first))

    def test_temporal_coherence_reduces_static_flicker(self):
        frame = self.frame()
        measurements = []
        for coherence in (0, 95):
            p = self.processor(mode="elastic", coherence=coherence, speed=4,
                               preserve=0, motion=0)
            outputs = [p.process(frame, i, i / 30, {}).astype(np.float32)
                       for i in range(30)]
            measurements.append(float(np.abs(np.diff(outputs, axis=0)).mean()))
        self.assertLess(measurements[1], measurements[0] * 0.5, measurements)

    def test_content_preservation_reduces_edge_displacement(self):
        frame = self.frame()
        a = self.processor(mode="elastic", preserve=0)
        b = self.processor(mode="elastic", preserve=100)
        a.process(frame, 0, 0.0, {})
        b.process(frame, 0, 0.0, {})
        self.assertLess(float(np.linalg.norm(b.displacement)),
                        0.85 * float(np.linalg.norm(a.displacement)))

    def test_axes_motion_speed_and_smoothness_controls(self):
        frame = self.frame()
        p = self.processor(mode="elastic", horizontal=100, vertical=0)
        p.process(frame, 0, 0.0, {})
        self.assertEqual(float(np.abs(p.displacement[:, :, 1]).max()), 0)
        fields = []
        for motion in (0, 100):
            p = self.processor(mode="elastic", motion=motion, preserve=0,
                               coherence=0, speed=0)
            p.process(frame, 0, 0.0, {})
            p.process(np.roll(frame, 5, axis=1), 1, 1 / 30, {})
            fields.append(float(np.linalg.norm(p.displacement)))
        self.assertGreater(fields[1], fields[0])
        gradients = []
        for smoothness in (0, 100):
            p = self.processor(mode="elastic", smoothness=smoothness,
                               preserve=0, speed=0, motion=0)
            first = p.process(frame, 0, 0.0, {})
            np.testing.assert_array_equal(p.process(frame, 1, 1.0, {}), first)
            gradients.append(float(np.abs(np.diff(p.displacement, axis=0)).mean()))
        self.assertLess(gradients[1], gradients[0])

    def test_scene_cut_rewind_and_discontinuity_reset(self):
        frame = self.frame()
        for context, index, stamp, source in (
                ({"scene_cut": True}, 2, 2.0, frame),
                ({}, 2, -1.0, frame), ({}, 9, 2.0, frame),
                ({}, 2, 2.0, np.full_like(frame, 255))):
            with self.subTest(context=context, index=index, stamp=stamp):
                p = self.processor()
                p.process(np.zeros_like(frame) if source[0, 0, 0] == 255 else frame,
                          0, 0.0, {})
                p.process(np.zeros_like(frame) if source[0, 0, 0] == 255 else frame,
                          1, 1.0, {})
                fresh = self.processor().process(source, 0, 0.0, {})
                np.testing.assert_array_equal(p.process(source, index, stamp, context), fresh)

    def test_timestamp_cadence_not_frame_count_drives_field(self):
        frame = self.frame()
        a = self.processor(mode="elastic", coherence=0, motion=0)
        b = self.processor(mode="elastic", coherence=0, motion=0)
        a.process(frame, 0, 0.0, {})
        b.process(frame, 0, 0.0, {})
        a.process(frame, 1, 1.0, {})
        for i in range(1, 31):
            b.process(frame, i, i / 30, {})
        np.testing.assert_allclose(a.displacement, b.displacement, atol=1e-6)

    def test_bounded_state_tiny_frames_and_input_validation(self):
        p = self.processor(shape=(720, 1280, 3))
        frame = self.frame((720, 1280, 3))
        for i in range(20):
            p.process(frame, i, None, {})
            self.assertLessEqual(p.state_nbytes, p.MAX_STATE_BYTES)
        size = p.state_nbytes
        p.process(frame, 20, float("nan"), {})
        self.assertEqual(p.state_nbytes, size)
        self.assertLessEqual(max(p.work_shape), 192)
        self.assertEqual(set(p.serialize_config()), set(p.DEFAULTS))
        for shape in ((1, 1, 3), (1, 8, 3), (8, 1, 3)):
            p = self.processor(shape=shape)
            self.assertEqual(p.process(np.zeros(shape, np.uint8), 0, None, {}).shape, shape)
        with self.assertRaises(ValueError):
            self.processor().process(np.zeros((64, 96, 3), np.float32), 0, None, {})

    def test_collapse_is_registered_with_all_controls(self):
        self.assertTrue("collapse" in effex.EFFECT_BY_KEY, "collapse must be registered")
        effect = effex.EFFECT_BY_KEY["collapse"]
        self.assertEqual({p.key for p in effect.params}, set(stateful_effects.ElasticCollapse.DEFAULTS))
        self.assertIs(effect.frame_processor, stateful_effects.ElasticCollapse)
        self.assertEqual(effex.default_opts(effect)["mode"], "seam")

    def test_seam_mode_is_distinct_and_zero_strength_disables_it(self):
        frame = self.frame()
        seam = self.processor(mode="seam", seam_strength=100)
        elastic = self.processor(mode="elastic")
        out = seam.process(frame, 0, 0.0, {})
        self.assertFalse(np.array_equal(out, elastic.process(frame, 0, 0.0, {})))
        self.assertEqual(out.shape, frame.shape)
        self.assertEqual(out.dtype, np.uint8)
        self.assertGreater(float(np.abs(out.astype(float) - frame).mean()), 0)
        off = self.processor(mode="seam", seam_strength=0)
        np.testing.assert_array_equal(off.process(frame, 0, 0.0, {}), frame)

    def test_content_aware_scale_grows_linearly_over_clip_duration(self):
        frame = self.frame()
        processor = self.processor(
            mode="seam", intensity=100, horizontal=100, vertical=0,
            preserve=0, seam_strength=100, coherence=0, _clip_duration=2,
        )
        first = processor.process(frame, 0, 0.0, {})
        first_field = processor.displacement.copy()
        middle = processor.process(frame, 30, 1.0, {})
        middle_field = processor.displacement.copy()
        final = processor.process(frame, 60, 2.0, {})
        final_field = processor.displacement.copy()

        np.testing.assert_array_equal(first, frame)
        np.testing.assert_allclose(first_field, 0)
        np.testing.assert_allclose(middle_field, final_field * 0.5, atol=1e-6)
        self.assertEqual(middle.shape, frame.shape)
        self.assertEqual(final.shape, frame.shape)
        self.assertGreater(float(np.abs(final.astype(float) - frame).mean()), 4)

    def test_seam_updates_are_rate_limited_and_deterministic(self):
        frame = self.frame()
        a, b = self.processor(mode="seam"), self.processor(mode="seam")
        for i in range(30):
            source = np.roll(frame, i % 5, axis=1)
            np.testing.assert_array_equal(a.process(source, i, i / 30, {}),
                                          b.process(source, i, i / 30, {}))
        self.assertLessEqual(a.seam_updates, 5)
        self.assertGreaterEqual(a.seam_updates, 4)
        self.assertLessEqual(a.state_nbytes, a.MAX_STATE_BYTES)

    def test_seam_decisions_are_temporally_smoothed(self):
        frame = self.frame()
        rng = np.random.default_rng(8)
        sources = [np.clip(frame.astype(np.int16) + rng.integers(-8, 9, frame.shape),
                           0, 255).astype(np.uint8) for _ in range(10)]
        changes = []
        for coherence in (0, 95):
            p = self.processor(mode="seam", coherence=coherence, seam_strength=100)
            fields = []
            for i, source in enumerate(sources):
                p.process(source, i, i / 5, {})
                fields.append(p.displacement)
            changes.append(float(np.abs(np.diff(fields, axis=0)).mean()))
        self.assertGreater(changes[0], 0)
        self.assertLess(changes[1], changes[0] * 0.5, changes)

    def test_cli_seam_scale_starts_at_source_and_grows_toward_clip_end(self):
        with tempfile.TemporaryDirectory(prefix="effex-collapse-progress-") as directory:
            root = Path(directory)
            out_dir = root / "out"
            out_dir.mkdir()
            clip = root / "clip.mp4"
            self.assertTrue(make_clip(clip, seconds=1, size="160x90", audio=False))
            env = env_for(root, out_dir)
            proc = run([
                EFFEX, "collapse", clip,
                "--set", "mode=seam", "--set", "intensity=100",
                "--set", "horizontal=100", "--set", "vertical=100",
                "--set", "preserve=0", "--set", "seam_strength=100",
                "--json", "--preset", "ultrafast",
            ], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            output = Path(json.loads(proc.stdout)["output"])

            def frames(path):
                decoded = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(path), "-vsync", "0",
                     "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                    capture_output=True, check=False)
                self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())
                return np.frombuffer(decoded.stdout, np.uint8).reshape((-1, 90, 160, 3))

            source_frames = frames(clip)
            output_frames = frames(output)
            differences = np.abs(output_frames.astype(np.int16)
                                 - source_frames.astype(np.int16)).mean(axis=(1, 2, 3))
            self.assertEqual(len(output_frames), len(source_frames))
            self.assertLess(float(differences[0]), 8.0, differences.tolist())
            self.assertGreater(float(differences[-1]),
                               float(differences[0]) + 3.0, differences.tolist())

    def test_cli_bounds_and_real_renders(self):
        with tempfile.TemporaryDirectory(prefix="effex-collapse-") as directory:
            root = Path(directory)
            out_dir = root / "out"
            out_dir.mkdir()
            clip, silent = root / "clip.mp4", root / "silent.mp4"
            self.assertTrue(make_clip(clip, seconds=1, size="160x90"))
            self.assertTrue(make_clip(silent, seconds=1, size="160x90", audio=False))
            env = env_for(root, out_dir)
            for setting in ("intensity=101", "horizontal=-1", "vertical=101",
                            "preserve=101", "mode=unknown", "seam_strength=101",
                            "motion=-1", "smoothness=101", "speed=-1", "speed=5",
                            "coherence=101", "seed=-1"):
                with self.subTest(invalid=setting):
                    proc = run([EFFEX, "collapse", clip, "--set", setting,
                                "--dry-run"], env_extra=env)
                    self.assertEqual(proc.returncode, 2, proc.stderr.decode())
            for mode in ("elastic", "seam"):
                for chain, source in (("collapse", clip), ("collapse", silent),
                                      ("neon+collapse+deepfry", clip),
                                      ("collapse+sediment", clip),
                                      ("sediment+collapse", clip),
                                      ("stretch+collapse", clip)):
                    with self.subTest(mode=mode, chain=chain, source=source.name):
                        args = [EFFEX, chain, source, "--set", f"collapse.mode={mode}",
                                "--set", "collapse.seed=17", "--json", "--preset", "ultrafast"]
                        if chain.startswith("stretch"):
                            args += ["--size", "128x72"]
                        proc = run(args, env_extra=env)
                        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                        data = json.loads(proc.stdout)
                        self.assertTrue(data["ok"], data)
                        self.assertEqual(video_dims(data["output"]),
                                         (128, 72) if chain.startswith("stretch") else (160, 90))
                        self.assertEqual(audio_codec(data["output"]),
                                         "aac" if source == clip else None)
                        stream = next(s for s in ffprobe_streams(data["output"])["streams"]
                                      if s["codec_type"] == "video")
                        self.assertEqual(int(stream["nb_frames"]), 30)
                        self.assertAlmostEqual(float(stream["duration"]), 1, delta=0.05)

    def test_optional_dependency_error_and_stdlib_import(self):
        from unittest.mock import patch
        with patch.dict(sys.modules, {"cv2": None}):
            with self.assertRaisesRegex(RuntimeError, "requirements-collapse.txt"):
                self.processor()
        proc = run([sys.executable, "-S", EFFEX, "--list", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("collapse", {e["key"] for e in json.loads(proc.stdout)["effects"]})


if __name__ == "__main__":
    unittest.main()
