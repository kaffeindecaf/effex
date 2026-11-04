#!/usr/bin/env python3
"""Scheduled codec blooms must recover to unmodified prediction between bursts."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import effex
from datamosh_stream import pulse_ranges

ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=30)


class PulsePlanningTests(unittest.TestCase):
    def test_windows_are_frame_aligned_and_tail_is_clipped(self):
        self.assertEqual(pulse_ranges(4, 10, 1, .4, .5),
                         [(5, 9), (15, 19), (25, 29), (35, 39)])
        self.assertEqual(pulse_ranges(2.2, 10, 1, .5, 1), [(10, 15), (20, 22)])
        self.assertEqual(pulse_ranges(4, 10, 1, .4, 4), [])
        self.assertEqual(pulse_ranges(3600, 10, .1, 0, 0), [])
        self.assertEqual(pulse_ranges(2.003, 30000 / 1001, 1, .5, 1), [(30, 45)])

    def test_clean_gap_and_burst_count_are_bounded(self):
        for args in ((4, 10, 1, 1, 0), (4, 10, .1, .01, 0), (100, 10, .2, .1, 0)):
            with self.assertRaises(ValueError):
                pulse_ranges(*args)

    def test_menu_shows_pulse_controls_and_preserves_algorithm_on_preset_change(self):
        chain, _ = effex.parse_chain("datamosh")
        clip = effex.Clip(Path("source.mp4"), duration=4, width=96, height=64, fps="10/1")
        args = SimpleNamespace(width=0, height=0, suffix="", crf=18, preset="medium")
        keys = iter(["down", "down", "right", "right", "right", "up", "right", "r"])
        with patch.object(effex.sys.stdin, "isatty", return_value=True), \
                patch.object(effex, "read_one_key", side_effect=lambda: next(keys)), \
                patch.object(effex, "draw"):
            opts = effex.screen_effect_options(chain, clip, effex.Tools(), args)
        self.assertEqual(opts["datamosh"]["algorithm"], "pulse")
        self.assertEqual(opts["datamosh"]["preset"], "standard")
        self.assertEqual(opts["datamosh"]["pulse_interval"], 2)
        visible = [row.param.key for row in effex.option_rows(chain, opts) if row.param]
        for key in ("pulse_interval", "pulse_length", "pulse_start", "bloom"):
            self.assertIn(key, visible)
        for key in ("second", "hold", "cut_length", "reverse", "cut"):
            self.assertNotIn(key, visible)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class PulseRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="effex-pulse-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        made = command("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                       "testsrc2=size=96x64:rate=10:duration=4", "-f", "lavfi", "-i",
                       "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", self.source)
        self.assertEqual(made.returncode, 0, made.stderr.decode())

    def cli(self, *settings, dry=False):
        args = [sys.executable, ROOT / "effex.py", "datamosh", self.source,
                "--output-dir", self.root, "--set", "algorithm=pulse"]
        for setting in settings:
            args += ["--set", setting]
        return command(*args, "--dry-run" if dry else "--json")

    def test_decoded_gaps_match_clean_encode_and_bursts_change_pixels(self):
        tools = effex.Tools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
        clip = effex.probe(self.source, tools)
        opts = effex.default_opts(effex.EFFECT_BY_KEY["datamosh"])
        opts.update(algorithm="pulse", pulse_interval=1, pulse_start=.5, seed=17)
        for length in (.4, 0):
            with self.subTest(length=length):
                opts["pulse_length"] = length
                ctx = effex.EffectContext(clip, 0, 0, "", 18, "medium", opts)
                baseline = self.root / "baseline.m4v"

                def run_encode(cmd, *_):
                    proc = command(*cmd)
                    if cmd[-1].endswith(".m4v") and proc.returncode == 0:
                        baseline.write_bytes(Path(cmd[-1]).read_bytes())
                    return proc.returncode, proc.stderr.decode(), .1

                output = self.root / "pulse.mp4"
                ok, error, _ = effex.run_pulse_datamosh(
                    ctx, clip, tools, SimpleNamespace(), output, run_encode, None, False)
                self.assertTrue(ok, error)
                self.assertFalse(error.strip(), error)
                clean = command("ffmpeg", "-v", "error", "-r", "10", "-f", "m4v", "-i", baseline,
                                "-an", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
                result = command("ffmpeg", "-v", "error", "-i", output, "-an",
                                 "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
                self.assertFalse(result.stderr, result.stderr.decode())
                frame_bytes = 96 * 64 * 3
                self.assertEqual(len(result.stdout), 40 * frame_bytes)
                self.assertEqual(len(clean.stdout), len(result.stdout))
                windows = pulse_ranges(4, 10, 1, length, .5)
                for frame in range(40):
                    if not any(a <= frame < b for a, b in windows):
                        at = slice(frame * frame_bytes, (frame + 1) * frame_bytes)
                        self.assertEqual(result.stdout[at], clean.stdout[at], f"gap frame {frame}")
                if length:
                    self.assertNotEqual(result.stdout, clean.stdout)
                else:
                    self.assertEqual(result.stdout, clean.stdout)
                info = command("ffprobe", "-v", "error", "-show_streams", "-of", "json", output)
                streams = json.loads(info.stdout)["streams"]
                video = next(s for s in streams if s["codec_type"] == "video")
                audio = next(s for s in streams if s["codec_type"] == "audio")
                self.assertEqual(int(video["nb_frames"]), 40)
                self.assertAlmostEqual(float(video["duration"]), 4, delta=.001)
                self.assertAlmostEqual(float(audio["duration"]), 4, delta=.04)
                keys = command("ffprobe", "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "frame=key_frame", "-of", "csv=p=0", output)
                self.assertEqual([i for i, k in enumerate(keys.stdout.splitlines()) if k == b"1"],
                                 [0, 9, 19, 29, 39] if length else [0])
                self.assertFalse(list(self.root.glob(".pulse-mosh-*")))

    def test_cli_dry_run_json_and_invalid_options(self):
        proc = self.cli("pulse_interval=1", "pulse_length=.4", "pulse_start=.5", dry=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"4 bursts", proc.stdout)
        for settings, message in ((("pulse_interval=0",), b"outside"),
                                  (("pulse_interval=1", "pulse_length=1"), b"clean frame"),
                                  (("pulse_length=-1",), b"outside"),
                                  (("pulse_start=-1",), b"outside"),
                                  (("second=" + str(self.source),), b"remove --with"),
                                  (("audio=mix",), b"requires a second video")):
            with self.subTest(settings=settings):
                proc = self.cli(*settings, dry=True)
                self.assertEqual(proc.returncode, 2, proc.stderr.decode())
                self.assertIn(message, proc.stderr)
        proc = self.cli("audio=none")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        data = json.loads(proc.stdout)
        self.assertEqual(data["options"]["algorithm"], "pulse")
        self.assertIn("_PULSE_MOSHED", data["output"])
        streams = command("ffprobe", "-v", "error", "-show_streams", "-of", "json", data["output"])
        self.assertEqual(len(json.loads(streams.stdout)["streams"]), 1)

    def test_fractional_rate_short_tail_and_silent_input(self):
        made = command("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                       "testsrc2=size=96x64:rate=30000/1001", "-frames:v", "61",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", self.source)
        self.assertEqual(made.returncode, 0, made.stderr.decode())
        proc = self.cli("pulse_interval=1", "pulse_length=.4", "pulse_start=.8")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        output = json.loads(proc.stdout)["output"]
        info = command("ffprobe", "-v", "error", "-show_streams", "-of", "json", output)
        streams = json.loads(info.stdout)["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(int(streams[0]["nb_frames"]), 61)
        self.assertAlmostEqual(float(streams[0]["duration"]), 61 * 1001 / 30000, delta=.001)
        decoded = command("ffmpeg", "-v", "error", "-i", output, "-f", "null", "-")
        self.assertFalse(decoded.stderr, decoded.stderr.decode())

    def test_failed_encode_cleans_temporary_stream(self):
        clip = effex.Clip(self.source, duration=4, width=96, height=64, fps="10/1")
        ctx = effex.EffectContext(clip, 0, 0, "", 18, "medium", {"algorithm": "pulse"})

        def fail(cmd, *_):
            Path(cmd[-1]).write_bytes(b"partial")
            return 1, "failed test encode", .1

        result = effex.run_pulse_datamosh(ctx, clip, effex.Tools(ffmpeg="ffmpeg"), SimpleNamespace(),
                                         self.root / "out.mp4", fail, None, False)
        self.assertFalse(result[0])
        self.assertIn("failed test encode", result[1])
        self.assertFalse(list(self.root.glob(".pulse-mosh-*")))


if __name__ == "__main__":
    unittest.main()
