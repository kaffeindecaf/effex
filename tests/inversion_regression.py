#!/usr/bin/env python3
"""Causality inversion: real future pixels, bounded queues and timestamped export."""
import json
import importlib.util
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import numpy as np
except ImportError:
    print("  SKIP inversion tests need requirements-inversion.txt")
    sys.exit(0)

import stateful_effects
from harness import EFFEX, audio_codec, env_for, make_clip, run, video_dims


class InversionTests(unittest.TestCase):
    def test_done_screen_keeps_the_underlying_render_error(self):
        import contextlib
        import io
        import effex
        from types import SimpleNamespace
        previous = dict(effex.last_render)
        try:
            effex.last_render.clear()
            effex.last_render["error"] = "Causality Inversion needs requirements-inversion.txt (PyAV)"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                effex.screen_done(SimpleNamespace(), False)
            self.assertIn("requirements-inversion.txt", output.getvalue())
            self.assertIn("PyAV", output.getvalue())
        finally:
            effex.last_render.clear()
            effex.last_render.update(previous)

    def test_plain_launch_selects_the_project_environment_only_when_needed(self):
        import effex
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="effex-python-") as directory:
            root = Path(directory)
            python = root / ".venv" / "bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
            with patch.object(effex.sys, "prefix", sys.base_prefix):
                self.assertEqual(effex.project_python(root), str(python))
                with patch.object(effex.sys, "flags", SimpleNamespace(no_site=True)):
                    self.assertIsNone(effex.project_python(root))
            with patch.object(effex.sys, "prefix", "/some/active/venv"):
                self.assertIsNone(effex.project_python(root))
            python.unlink()
            self.assertIsNone(effex.project_python(root))

    def test_nominal_lookahead_adapts_to_the_memory_budget(self):
        from stateful_effects import CausalityInversion
        shape = (24, 32, 3)
        p = CausalityInversion()
        p.MAX_STATE_BYTES = 65536 + int(np.prod(shape)) * 3
        p.initialize(dict(lookahead=0.5, offset=0.5, strength=100,
                          mode="replace", feather=0), shape, 10, 17)
        self.assertEqual(p.capacity, 3)
        self.assertAlmostEqual(p.effective_lookahead, 0.2)
        self.assertAlmostEqual(p.effective_offset, 0.2)
        self.assertTrue(p.runtime_notes)
        self.assertEqual(p.serialize_config()["offset"], 0.5)
        output = []
        for index in range(8):
            output.extend(p.push(self.packet(np.full(shape, index * 10, np.uint8), index)))
            self.assertLessEqual(p.state_nbytes, p.MAX_STATE_BYTES)
        output.extend(p.flush())
        self.assertEqual([item.index for item in output], list(range(8)))
        self.assertEqual(int(output[0].frame[0, 0, 0]), 20)
        self.assertEqual([item.timestamp for item in output], [Fraction(i, 10) for i in range(8)])

    def processor(self, shape=(24, 32, 3), seed=17, **config):
        self.assertTrue(hasattr(stateful_effects, "CausalityInversion"),
                        "CausalityInversion must be implemented")
        p = stateful_effects.CausalityInversion()
        p.initialize(config, shape, 10, seed)
        return p

    @staticmethod
    def packet(frame, index, timestamp=None):
        from frame_pipeline import FramePacket
        return FramePacket(frame, index, Fraction(index, 10) if timestamp is None else timestamp,
                           Fraction(1, 1000), Fraction(1, 10))

    def test_moving_object_appears_early_without_shifting_output_times(self):
        p = self.processor(lookahead=0.2, offset=0.2, strength=100,
                           mode="replace", region="full", feather=0)
        frames = []
        output = []
        for i in range(6):
            frame = np.zeros((24, 32, 3), np.uint8)
            frame[8:14, 2 + i * 4:6 + i * 4] = 255
            frames.append(frame.copy())
            output.extend(p.push(self.packet(frame, i)))
        output.extend(p.flush())
        self.assertEqual([p.index for p in output], list(range(6)))
        self.assertEqual([p.timestamp for p in output], [Fraction(i, 10) for i in range(6)])
        np.testing.assert_array_equal(output[0].frame, frames[2])
        np.testing.assert_array_equal(output[1].frame, frames[3])
        np.testing.assert_array_equal(output[-2].frame, frames[-1])
        np.testing.assert_array_equal(output[-1].frame, frames[-1])
        self.assertEqual(p.state_nbytes, 0)

    def test_blend_and_replace_have_different_strength_semantics(self):
        results = []
        for mode in ("blend", "replace"):
            p = self.processor(lookahead=0.1, offset=0.1, strength=50, mode=mode,
                               region="full", feather=0)
            first = np.zeros((24, 32, 3), np.uint8)
            future = np.full_like(first, 100)
            self.assertEqual(list(p.push(self.packet(first, 0))), [])
            output = list(p.push(self.packet(future, 1)))
            results.append(output[0].frame)
        self.assertEqual(set(np.unique(results[0])), {50})
        self.assertEqual(set(np.unique(results[1])), {0, 100})

    def test_tearing_is_a_separate_future_pixel_mode(self):
        frame = np.zeros((24, 32, 3), np.uint8)
        future = np.tile(np.arange(32, dtype=np.uint8)[None, :, None], (24, 1, 3))
        p = self.processor(lookahead=0.1, offset=0.1, strength=100, mode="tear",
                           region="full", feather=0)
        list(p.push(self.packet(frame, 0)))
        out = list(p.push(self.packet(future, 1)))[0].frame
        self.assertFalse(np.array_equal(out, future))
        self.assertTrue(set(np.unique(out)).issubset(set(np.unique(future))))

    def test_regions_feather_seed_and_mask_speed(self):
        first = np.zeros((24, 32, 3), np.uint8)
        future = np.full_like(first, 100)
        outputs = []
        for region in ("full", "bands", "blocks", "patch"):
            p = self.processor(lookahead=0.1, offset=0.1, strength=100,
                               mode="blend", region=region, feather=0)
            list(p.push(self.packet(first, 0)))
            out = list(p.push(self.packet(future, 1)))[0].frame
            outputs.append(out)
            if region != "full":
                self.assertEqual(set(np.unique(out)), {0, 100})
        for i in range(len(outputs)):
            for j in range(i):
                self.assertFalse(np.array_equal(outputs[i], outputs[j]))
        p = self.processor(lookahead=0.1, offset=0.1, strength=100,
                           region="bands", feather=50)
        list(p.push(self.packet(first, 0)))
        softened = list(p.push(self.packet(future, 1)))[0].frame
        self.assertTrue(np.any((softened > 0) & (softened < 100)))
        a = self.processor(lookahead=0.1, offset=0.1, region="patch", mask_speed=1)
        b = self.processor(lookahead=0.1, offset=0.1, region="patch", mask_speed=1)
        c = self.processor(lookahead=0.1, offset=0.1, region="patch", mask_speed=1, seed=99)
        collected = [[], [], []]
        for i in range(10):
            source = first if i % 2 == 0 else future
            for index, processor in enumerate((a, b, c)):
                collected[index].extend(processor.push(self.packet(source, i)))
        self.assertTrue(all(np.array_equal(x.frame, y.frame)
                            for x, y in zip(collected[0], collected[1])))
        self.assertTrue(any(not np.array_equal(x.frame, y.frame)
                            for x, y in zip(collected[0], collected[2])))

    def test_alignment_warps_future_toward_current_with_correct_sign(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("aligned mode needs requirements-collapse.txt")
        rng = np.random.default_rng(1)
        current = cv2.GaussianBlur(rng.integers(0, 180, (64, 96, 3), dtype=np.uint8), (3, 3), 0)
        future = np.roll(current, 4, axis=1)
        errors = []
        for mode in ("blend", "align"):
            p = self.processor(shape=current.shape, lookahead=0.1, offset=0.1,
                               strength=100, mode=mode, alignment=100, feather=0)
            list(p.push(self.packet(current, 0)))
            out = list(p.push(self.packet(future, 1)))[0].frame
            errors.append(float(np.abs(out[8:-8, 8:-8].astype(float)
                                       - current[8:-8, 8:-8]).mean()))
        self.assertLess(errors[1], errors[0] * 0.6, errors)

    def test_ownership_passthrough_reset_and_buffer_budget(self):
        shape = (24, 32, 3)
        source = np.zeros(shape, np.uint8)
        p = self.processor(lookahead=0.2, offset=0.2, strength=100, mode="replace")
        list(p.push(self.packet(source, 0)))
        source[:] = 100
        list(p.push(self.packet(source, 1)))
        p.reset()
        self.assertEqual(p.buffered_frames, 0)
        self.assertEqual(p.state_nbytes, 0)
        for config in (dict(strength=0), dict(lookahead=0, offset=0)):
            p = self.processor(**config)
            output = list(p.push(self.packet(source, 0)))
            np.testing.assert_array_equal(output[0].frame, source)
            self.assertFalse(np.shares_memory(output[0].frame, source))
        p = self.processor(lookahead=0.2, offset=0.2)
        p.MAX_STATE_BYTES = 65536 + source.nbytes * 5
        p.initialize(dict(lookahead=0.2, offset=0.2), shape, 10, 17)
        output = []
        for i in range(100):
            output.extend(p.push(self.packet(source, i, Fraction(i, 1000))))
            self.assertLessEqual(p.buffered_frames, 5)
            self.assertLessEqual(p.state_nbytes, p.MAX_STATE_BYTES)
        self.assertGreater(p.capacity_fallbacks, 0)
        output.extend(p.flush())
        self.assertEqual([q.index for q in output], list(range(100)))
        self.assertEqual(p.state_nbytes, 0)
        high_res = self.processor(shape=(2160, 3840, 3), lookahead=1, offset=1)
        self.assertLess(high_res.effective_lookahead, 1)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.processor(shape=(10000, 10000, 3), lookahead=1, offset=1)
        with self.assertRaisesRegex(ValueError, "offset"):
            self.processor(lookahead=0.1, offset=0.5)

    def test_irregular_timestamps_eof_and_metadata(self):
        p = self.processor(lookahead=0.2, offset=0.2, strength=100, mode="replace")
        stamps = [Fraction(n, 100) for n in (0, 3, 11, 27, 31)]
        output, frames = [], []
        for i, stamp in enumerate(stamps):
            frame = np.full((24, 32, 3), i * 20, np.uint8)
            frames.append(frame.copy())
            output.extend(p.push(self.packet(frame, i, stamp)))
            frame[:] = 255  # buffered snapshots must not alias the caller
        output.extend(p.flush())
        self.assertEqual([q.timestamp for q in output], stamps)
        self.assertEqual([q.duration for q in output], [Fraction(1, 10)] * 5)
        np.testing.assert_array_equal(output[0].frame, frames[3])
        np.testing.assert_array_equal(output[-2].frame, frames[-1])
        p = self.processor()
        self.assertEqual(list(p.flush()), [])
        lone = self.packet(frames[0], 0)
        self.assertEqual(list(p.push(lone)), [])
        np.testing.assert_array_equal(list(p.flush())[0].frame, frames[0])

    def test_scene_cuts_do_not_borrow_across_scenes(self):
        from dataclasses import replace
        p = self.processor(lookahead=0.2, offset=0.2, strength=100, mode="replace")
        black = np.zeros((24, 32, 3), np.uint8)
        white = np.full_like(black, 255)
        list(p.push(self.packet(black, 0)))
        out = list(p.push(replace(self.packet(white, 1), context={"scene_cut": True})))
        np.testing.assert_array_equal(out[0].frame, black)
        p.reset()
        for index in range(3):
            list(p.push(self.packet(black, index)))
        # An explicit rewind discards the previous timeline, rather than leaking it.
        self.assertEqual(list(p.push(self.packet(white, 0))), [])
        self.assertEqual([q.index for q in p.flush()], [0])

    def test_streaming_stage_composition_preserves_packet_metadata(self):
        from frame_pipeline import process_packets
        from stateful_effects import TemporalSediment
        packets = [self.packet(np.full((24, 32, 3), i * 20, np.uint8), i) for i in range(5)]
        for reverse in (False, True):
            inversion = self.processor(lookahead=0.1, offset=0.1)
            sediment = TemporalSediment()
            sediment.initialize({"intensity": 0}, (24, 32, 3), 10, 17)
            stages = [inversion, sediment] if not reverse else [sediment, inversion]
            output = list(process_packets(iter(packets), stages))
            self.assertEqual([q.timestamp for q in output], [q.timestamp for q in packets])
            self.assertEqual(len(output), len(packets))

    @unittest.skipUnless(importlib.util.find_spec("av"), "install requirements-inversion.txt")
    def test_default_inversion_renders_high_resolution_instead_of_refusing(self):
        with tempfile.TemporaryDirectory(prefix="effex-large-inversion-") as directory:
            root = Path(directory)
            clip = root / "large.mp4"
            self.assertTrue(make_clip(clip, seconds=0.3, size="2560x1440", audio=False))
            proc = run([EFFEX, "inversion", clip, "--json", "--preset", "ultrafast"],
                       env_extra=env_for(root, root / "out"), timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            data = json.loads(proc.stdout)
            self.assertEqual(video_dims(data["output"]), (2560, 1440))
            self.assertTrue(any("buffer budget" in note for note in data.get("warnings", [])))
            probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=nb_frames", "-of", "json", data["output"]])
            self.assertEqual(int(json.loads(probe.stdout)["streams"][0]["nb_frames"]), 9)

    @unittest.skipUnless(importlib.util.find_spec("av"), "install requirements-inversion.txt")
    def test_real_cli_preserves_vfr_pts_and_render_modes(self):
        import effex
        self.assertTrue("inversion" in effex.EFFECT_BY_KEY, "inversion must be registered")
        with tempfile.TemporaryDirectory(prefix="effex-inversion-") as directory:
            root = Path(directory)
            out = root / "out"
            out.mkdir()
            env = env_for(root, out)
            clip = root / "av.mp4"
            self.assertTrue(make_clip(clip, seconds=1, size="160x90"))
            for setting in ("lookahead=-1", "offset=6", "strength=101", "mode=bad",
                            "region=bad", "mask_speed=5", "alignment=-1", "feather=101",
                            "seed=-1", "lookahead=0.1"):
                invalid = run([EFFEX, "inversion", clip, "--set", setting, "--dry-run"], env_extra=env)
                self.assertEqual(invalid.returncode, 2, invalid.stderr.decode())
                self.assertNotIn(b"Traceback", invalid.stderr)
            modes = ["blend", "replace", "tear"]
            if importlib.util.find_spec("cv2"):
                modes.append("align")
            for mode in modes:
                proc = run([EFFEX, "inversion", clip, "--set", f"mode={mode}",
                            "--set", "region=bands", "--json", "--preset", "ultrafast"], env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                data = json.loads(proc.stdout)
                self.assertEqual(video_dims(data["output"]), (160, 90))
                self.assertEqual(audio_codec(data["output"]), "aac")
            for chain in ("neon+inversion+deepfry", "inversion+sediment",
                          "sediment+inversion", "collapse+inversion", "inversion+collapse",
                          "stretch+inversion"):
                if "collapse" in chain and importlib.util.find_spec("cv2") is None:
                    continue
                args = [EFFEX, chain, clip, "--json", "--preset", "ultrafast"]
                if chain.startswith("stretch"):
                    args += ["--size", "128x72"]
                result = run(args, env_extra=env)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                data = json.loads(result.stdout)
                self.assertEqual(video_dims(data["output"]), (128, 72) if chain.startswith("stretch") else (160, 90))
            def audio_pts(path):
                result = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                              "-show_entries", "packet=pts_time", "-of", "json", path])
                return [p["pts_time"] for p in json.loads(result.stdout)["packets"]]
            copied = run([EFFEX, "inversion", clip, "--json"], env_extra=env)
            self.assertEqual(audio_pts(clip), audio_pts(json.loads(copied.stdout)["output"]))
            for start in (0, 5):
                vfr = root / f"vfr-{start}.mkv"
                made = run(["ffmpeg", "-v", "error", "-y", "-copyts", "-f", "lavfi", "-i",
                            "testsrc2=s=160x90:r=10:d=1.5", "-vf",
                            f"select='not(eq(mod(n,4),3))',setpts=PTS+{start}/TB",
                            "-fps_mode", "vfr", "-c:v", "ffv1", vfr])
                self.assertEqual(made.returncode, 0, made.stderr.decode())
                proc = run([EFFEX, "inversion", vfr, "--json", "--preset", "ultrafast"], env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                destination = json.loads(proc.stdout)["output"]
                def pts(path):
                    result = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "frame=pts_time", "-of", "json", path])
                    return [Fraction(f["pts_time"]) for f in json.loads(result.stdout)["frames"]]
                self.assertEqual(pts(destination), pts(vfr))

    def test_nut_roundtrip_short_writes_and_timestamp_guards(self):
        import io
        import threading
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("timestamped transport needs requirements-inversion.txt")
        from timestamped_frames import _PipeWriter, read_packets, write_packets
        event = threading.Event()
        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(bytes(data[:7]))
        destination = ShortWriter()
        writer = _PipeWriter(destination, event)
        self.assertEqual(writer.write(b"abcdefghijklmnopqrstuvwxyz"), 26)
        self.assertEqual(destination.getvalue(), b"abcdefghijklmnopqrstuvwxyz")
        event.set()
        with self.assertRaises(InterruptedError):
            writer.write(b"cancelled")
        event.clear()
        packets = [self.packet(np.full((24, 32, 3), i * 20, np.uint8), i, Fraction(n, 1000))
                   for i, n in enumerate((5000, 5033, 5117, 5200))]
        destination = ShortWriter()
        self.assertEqual(write_packets(iter(packets), destination, (24, 32, 3), 30, event), 4)
        source = io.BytesIO(destination.getvalue())
        restored = list(read_packets(source, {}, event))
        self.assertEqual([q.timestamp for q in restored], [q.timestamp for q in packets])
        for a, b in zip(restored, packets):
            np.testing.assert_array_equal(a.frame, b.frame)
        with self.assertRaisesRegex(ValueError, "non-monotonic"):
            write_packets(iter([packets[1], packets[0]]), io.BytesIO(), (24, 32, 3), 30, event)
        proc = run([sys.executable, "-S", EFFEX, "--list", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())


    @unittest.skipUnless(importlib.util.find_spec("av"), "install requirements-inversion.txt")
    def test_failure_and_cancellation_remove_partial_output(self):
        import os
        import signal
        import subprocess
        import time
        with tempfile.TemporaryDirectory(prefix="effex-inversion-cancel-") as directory:
            root = Path(directory)
            clip = root / "clip.mp4"
            self.assertTrue(make_clip(clip, seconds=20, size="640x360"))
            bad = root / "render.invalid"
            failed = run([EFFEX, "inversion", clip, "--output", bad, "--json"], timeout=15)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(bad.exists())
            target = root / "cancelled.mp4"
            env = dict(os.environ, **env_for(root, root))
            process = subprocess.Popen([sys.executable, EFFEX, "inversion", clip,
                                        "--output", target, "--json"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                deadline = time.monotonic() + 10
                while not target.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIsNone(process.poll(), "render ended before cancellation could be exercised")
                self.assertTrue(target.exists(), "encoder did not create its partial output")
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse(target.exists(), "cancelled timestamped output was not removed")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
