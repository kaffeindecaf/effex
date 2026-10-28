#!/usr/bin/env python3
"""Codec blooming must reuse prediction data, not duplicate display frames."""
import shutil
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from effex import (EFFECT_BY_KEY, apply_cli_opts, build_parser, default_opts,
                   parse_chain, strip_ivops)  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg and ffprobe are required")
class BloomTests(unittest.TestCase):
    def test_bloom_does_not_break_byte_aligned_slices_at_clock_rollover(self):
        import subprocess
        with tempfile.TemporaryDirectory(prefix="effex-slice-mosh-") as temp:
            source = Path(temp) / "sliced.m4v"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=640x360:rate=30:duration=3",
                "-c:v", "mpeg4", "-q:v", "18", "-g", "12", "-bf", "0",
                "-f", "m4v", str(source)], check=True)
            source.write_bytes(strip_ivops(source.read_bytes(), "heavy", "chaos",
                                           123, bloom=12, bloom_window=2))
            decoded = subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(source), "-an",
                "-fps_mode", "passthrough", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
            self.assertFalse(decoded.stderr, decoded.stderr.decode()[:300])
            self.assertEqual(len(decoded.stdout), 90 * 640 * 360 * 3)

    def test_final_keyframe_still_has_a_fill_frame(self):
        import subprocess
        with tempfile.TemporaryDirectory(prefix="effex-short-mosh-") as temp:
            source = Path(temp) / "short.m4v"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=64x64:rate=30:duration=0.1",
                "-c:v", "mpeg4", "-g", "2", "-bf", "0", "-f", "m4v", str(source)],
                check=True)
            raw = source.read_bytes()
            for bloom in (1, 12):
                with self.subTest(bloom=bloom):
                    stream = strip_ivops(raw, "heavy", "next", 123, bloom=bloom)
                    self.assertEqual(stream.count(b"\x00\x00\x01\xb6"), 3)

    def test_heavy_is_tuned_and_explicit_knobs_override_presets_in_any_order(self):
        defaults = default_opts(EFFECT_BY_KEY["datamosh"])
        self.assertEqual(defaults["preset"], "heavy")
        self.assertGreaterEqual(defaults["bloom"], 12)
        self.assertGreaterEqual(defaults["grit"], 12)
        chain, problem = parse_chain("datamosh")
        self.assertFalse(problem)
        for settings in (("grit=7", "preset=standard"),
                         ("preset=standard", "grit=7")):
            args = build_parser().parse_args([
                "datamosh", "a.mp4", "--set", settings[0],
                "--set", settings[1]])
            args.size_given = False
            opts, problem = apply_cli_opts(chain, args)
            self.assertFalse(problem)
            self.assertEqual(opts["datamosh"]["grit"], 7)
            self.assertEqual(opts["datamosh"]["bloom"], 1)
            self.assertEqual(opts["datamosh"]["hold"], 150)

    def test_bloom_changes_motion_without_stretching_the_timeline(self):
        for rate in ("30", "30000/1001", "60"):
            with self.subTest(rate=rate):
                self.check_motion_timeline(rate)

    def check_motion_timeline(self, rate):
        import subprocess
        with tempfile.TemporaryDirectory(prefix="effex-bloom-") as temp:
            source = Path(temp) / "source.m4v"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"testsrc2=size=160x96:rate={rate}", "-frames:v", "60",
                "-c:v", "mpeg4", "-g", "15", "-bf", "0", "-f", "m4v",
                str(source)], check=True)
            raw = source.read_bytes()
            normal = strip_ivops(raw, "heavy", "next", 123)
            heavy = strip_ivops(raw, "heavy", "next", 123,
                                bloom=12, bloom_window=2)
            self.assertNotEqual(normal, heavy)
            decoded = []
            for name, stream in (("normal", normal), ("heavy", heavy)):
                path = Path(temp) / (name + ".m4v")
                path.write_bytes(stream)
                result = subprocess.run([
                    "ffmpeg", "-v", "error", "-i", str(path), "-an",
                    "-fps_mode", "passthrough", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
                decoded.append(result.stdout)
                self.assertEqual(len(result.stdout), 60 * 160 * 96 * 3)
                if name == "heavy":
                    self.assertFalse(result.stderr, result.stderr.decode()[:300])
                baked = path.with_suffix(".mp4")
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-f", "m4v", "-i",
                    str(path), "-c:v", "copy", str(baked)],
                    capture_output=True, check=True)
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "frame=pts_time", "-of", "csv=p=0",
                    str(baked)], capture_output=True, check=True)
                times = [float(line) for line in probe.stdout.splitlines()]
                self.assertEqual(len(times), 60)
                self.assertTrue(all(b > a for a, b in zip(times, times[1:])), name)
                self.assertAlmostEqual(times[-1] - times[0],
                                       float(59 / Fraction(rate)), places=5)
            self.assertNotEqual(decoded[0], decoded[1])
            self.assertEqual(heavy, strip_ivops(raw, "heavy", "next", 123,
                                              bloom=12, bloom_window=2))


if __name__ == "__main__":
    unittest.main()
