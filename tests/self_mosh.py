#!/usr/bin/env python3
"""Self-mosh planning, codec windows, and real video/audio rendering."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datamosh_stream import self_mosh_segments, transition_ranges
import effex
from effex import strip_ivops

ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    return subprocess.run([str(a) for a in args], capture_output=True)


class PlanningTests(unittest.TestCase):
    def test_shuffle_is_seeded_and_preserves_every_selected_cut(self):
        original = self_mosh_segments(10, 10, 1, 0, 50, 17)
        shuffled = self_mosh_segments(10, 10, 1, 0, 50, 17, shuffle=True)
        self.assertCountEqual(original, shuffled)
        self.assertNotEqual(original, shuffled)
        self.assertEqual(shuffled, self_mosh_segments(10, 10, 1, 0, 50, 17, shuffle=True))
        self.assertNotEqual(shuffled, self_mosh_segments(10, 10, 1, 0, 50, 18, shuffle=True))
        for seed in range(20):
            cuts = self_mosh_segments(2, 10, 1, 0, 0, seed, shuffle=True)
            self.assertEqual([s[0] for s in cuts], [10, 0])
        single = self_mosh_segments(1, 10, 1, 0, 0, 17, shuffle=True)
        self.assertEqual(single, [(0, 10, False)])

    def test_shuffle_is_selectable_in_menu_and_presets_keep_it(self):
        chain, _ = effex.parse_chain("datamosh")
        args = SimpleNamespace(width=0, height=0, suffix="", crf=18, preset="medium")
        clip = effex.Clip(Path("source.mp4"), duration=10, width=96, height=64, fps="10/1")
        keys = iter(["down", "down", "right", "right", "up", "right", "r"])
        with patch.object(effex.sys.stdin, "isatty", return_value=True), \
                patch.object(effex, "read_one_key", side_effect=lambda: next(keys)), \
                patch.object(effex, "draw"):
            opts = effex.screen_effect_options(chain, clip, effex.Tools(), args)
        self.assertEqual(opts["datamosh"]["algorithm"], "shuffle")
        self.assertEqual(opts["datamosh"]["preset"], "standard")

    def test_menu_algorithm_and_preset_are_independent(self):
        chain, _ = effex.parse_chain("datamosh")
        args = SimpleNamespace(width=0, height=0, suffix="", crf=18, preset="medium")
        clip = effex.Clip(Path("source.mp4"), duration=10, width=96, height=64, fps="10/1")
        screens = []
        # second -> preset -> algorithm; change algorithm, then change preset.
        keys = iter(["down", "down", "right", "up", "right", "r"])
        with patch.object(effex.sys.stdin, "isatty", return_value=True), \
                patch.object(effex, "read_one_key", side_effect=lambda: next(keys)), \
                patch.object(effex, "draw", side_effect=screens.append):
            opts = effex.screen_effect_options(chain, clip, effex.Tools(), args)
        self.assertEqual(opts["datamosh"]["algorithm"], "self")
        self.assertEqual(opts["datamosh"]["preset"], "standard")
        self.assertEqual(opts["datamosh"]["cut_length"], 2)
        self.assertEqual(opts["datamosh"]["bloom"], 1)
        final = effex.option_rows(chain, opts)
        visible = [row.param.key for row in final if row.param]
        self.assertIn("transition", visible)
        self.assertNotIn("second", visible)
        self.assertNotIn("hold", visible)

    def test_auto_covers_short_tail_and_fixed_count_spans_source(self):
        cuts = self_mosh_segments(3.1, 10, 1, 0, 0, 7)
        self.assertEqual(cuts, [(0, 10, False), (10, 10, False),
                                (20, 10, False), (30, 1, False)])
        self.assertEqual(self_mosh_segments(10, 10, 1, 3, 0, 7),
                         [(0, 10, False), (45, 10, False), (90, 10, False)])

    def test_reversal_is_seeded_and_extremes_work(self):
        a = self_mosh_segments(10, 10, .5, 0, 50, 12)
        self.assertEqual(a, self_mosh_segments(10, 10, .5, 0, 50, 12))
        self.assertNotEqual(a, self_mosh_segments(10, 10, .5, 0, 50, 13))
        self.assertTrue(any(s[2] for s in a) and not all(s[2] for s in a))
        self.assertTrue(all(s[2] for s in self_mosh_segments(2, 10, .5, 0, 100, 1)))

    def test_bounds_and_transition_clamping(self):
        for args in ((10, 10, .1, 128, 0, 1), (60, 10, .1, 0, 0, 1)):
            with self.assertRaises(ValueError):
                self_mosh_segments(*args)
        cuts = self_mosh_segments(2.3, 10, 1, 0, 0, 1)
        self.assertEqual(transition_ranges(cuts, 10, .5), [(10, 15), (20, 23)])
        self.assertEqual(transition_ranges(cuts, 10, 0), [])
        fractional = self_mosh_segments(2.003, 30000 / 1001, .5, 0, 0, 1)
        self.assertEqual(sum(s[1] for s in fractional), 60)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg and ffprobe required")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="effex-self-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        proc = command("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                       "testsrc2=size=96x64:rate=10:duration=3", "-f", "lavfi", "-i",
                       "sine=frequency=440:duration=3", "-c:v", "libx264",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", self.source)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def effex(self, *settings, dry=False):
        args = [sys.executable, ROOT / "effex.py", "datamosh", self.source,
                "--set", "algorithm=self", "--output-dir", self.root]
        for setting in settings:
            args += ["--set", setting]
        args += ["--dry-run"] if dry else ["--json"]
        return command(*args)

    def test_codec_edits_stay_inside_transition_windows(self):
        rawpath = self.root / "source.m4v"
        proc = command("ffmpeg", "-v", "error", "-y", "-i", self.source, "-an",
                       "-c:v", "mpeg4", "-g", "3", "-bf", "0", "-f", "m4v", rawpath)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        raw = rawpath.read_bytes()
        windows = [(6, 9), (15, 18)]
        moshed = strip_ivops(raw, "heavy", "next", 12, bloom=5, active_ranges=windows)
        marker = b"\x00\x00\x01\xb6"
        before, after = raw.split(marker), moshed.split(marker)
        self.assertEqual(len(before), len(after))
        for frame, (a, b) in enumerate(zip(before[1:], after[1:])):
            if not any(start <= frame < end for start, end in windows):
                self.assertEqual(a, b, f"clean frame {frame} changed")
        self.assertNotEqual(raw, moshed)
        self.assertEqual(strip_ivops(raw, "heavy", "next", 12, bloom=12, active_ranges=[]), raw)

    def test_render_preserves_timeline_decodes_and_cleans_scratch(self):
        proc = self.effex("cut_length=0.6", "transition=0.2", "reverse=50", "seed=17")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        data = json.loads(proc.stdout)
        output = Path(data["output"])
        info = command("ffprobe", "-v", "error", "-show_streams", "-of", "json", output)
        streams = json.loads(info.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        self.assertEqual(int(video["nb_frames"]), 30)
        keyframes = command("ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "frame=key_frame", "-of", "csv=p=0", output)
        retained = [i for i, line in enumerate(keyframes.stdout.splitlines()) if line == b"1"]
        self.assertEqual(retained, [0, 8, 14, 20, 26])
        self.assertEqual((video["width"], video["height"]), (96, 64))
        self.assertAlmostEqual(float(audio["duration"]), 3, delta=.06)
        decoded = command("ffmpeg", "-v", "error", "-i", output, "-f", "null", "-")
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())
        self.assertFalse(decoded.stderr, decoded.stderr.decode())
        self.assertFalse(list(self.root.glob(".self-mosh-*")))
        self.assertEqual(data["options"]["algorithm"], "self")

    def test_fixed_count_reversal_and_zero_transition(self):
        proc = self.effex("cut_length=0.5", "segments=3", "transition=0", "reverse=100")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        output = Path(json.loads(proc.stdout)["output"])
        decoded = command("ffmpeg", "-v", "error", "-i", output, "-an",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
        self.assertEqual(len(decoded.stdout), 15 * 96 * 64 * 3)
        # A fully reversed first cut starts at its last source frame (.4s).
        expected = command("ffmpeg", "-v", "error", "-ss", ".4", "-i", self.source,
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
        pixels = decoded.stdout[:96 * 64 * 3]
        error = sum(abs(a - b) for a, b in zip(pixels, expected.stdout)) / len(pixels)
        self.assertLess(error, 20, f"reversal pixel error={error}")
        self.assertFalse(decoded.stderr, decoded.stderr.decode())

    def test_bad_combinations_rejected_before_render(self):
        for settings, error in ((["second=" + str(self.source)], b"remove --with"),
                                (["audio=mix"], b"self-mosh supports"),
                                (["segments=10", "cut_length=1"], b"exceeds"),
                                (["transition=-1"], b"outside"),
                                (["algorithm=unknown"], b"expected one of")):
            with self.subTest(settings=settings):
                proc = self.effex(*settings, dry=True)
                self.assertEqual(proc.returncode, 2, proc.stderr.decode())
                self.assertIn(error, proc.stderr)
        proc = self.effex("cut_length=.5", "segments=3", "seed=12", dry=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"3 segments", proc.stdout)

    def test_fractional_rate_silent_source_and_short_tail(self):
        proc = command("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                       "testsrc2=size=96x64:rate=30000/1001", "-frames:v", "61",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", self.source)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        proc = self.effex("cut_length=0.5", "transition=0.1", "reverse=100")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        output = Path(json.loads(proc.stdout)["output"])
        info = command("ffprobe", "-v", "error", "-show_streams", "-of", "json", output)
        streams = json.loads(info.stdout)["streams"]
        self.assertEqual(len(streams), 1)
        self.assertEqual(int(streams[0]["nb_frames"]), 61)
        self.assertAlmostEqual(float(streams[0]["duration"]), 61 * 1001 / 30000, delta=.001)

    def test_failure_removes_scratch_cuts(self):
        tools = effex.Tools(ffmpeg=shutil.which("ffmpeg"))
        clip = effex.Clip(self.source, duration=3, width=96, height=64, fps="10/1")
        ctx = effex.EffectContext(clip, 0, 0, "", 18, "medium", {"algorithm": "self"})

        def fail_encode(cmd, *args):
            Path(cmd[-1]).write_bytes(b"partial scratch")
            return 1, "test failure", .1

        result = effex.run_self_datamosh(ctx, clip, tools, SimpleNamespace(),
                                        self.root / "output.mp4", fail_encode, None, False)
        self.assertFalse(result[0])
        self.assertIn("test failure", result[1])
        self.assertFalse(list(self.root.glob(".self-mosh-*")))

    def test_integer_second_joins_melt_without_rollover_donors(self):
        proc = self.effex("cut_length=1", "transition=.5", "preset=standard")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        output = Path(json.loads(proc.stdout)["output"])
        keyframes = command("ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "frame=key_frame", "-of", "csv=p=0", output)
        self.assertEqual([i for i, line in enumerate(keyframes.stdout.splitlines()) if line == b"1"],
                         [0, 15, 25])
        decoded = command("ffmpeg", "-v", "error", "-i", output, "-an",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
        self.assertFalse(decoded.stderr, decoded.stderr.decode())
        self.assertEqual(len(decoded.stdout), 30 * 96 * 64 * 3)

    def test_shuffle_renders_source_cuts_in_planned_order(self):
        settings = ("algorithm=shuffle", "cut_length=.6", "transition=0", "preset=standard", "seed=17")
        outputs = []
        for _ in range(2):
            proc = self.effex(*settings)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            data = json.loads(proc.stdout)
            self.assertEqual(data["options"]["algorithm"], "shuffle")
            self.assertIn("_SHUFFLE_MOSHED", data["output"])
            decoded = command("ffmpeg", "-v", "error", "-i", data["output"], "-an",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
            self.assertFalse(decoded.stderr, decoded.stderr.decode())
            self.assertEqual(len(decoded.stdout), 30 * 96 * 64 * 3)
            outputs.append(decoded.stdout)
        self.assertEqual(outputs[0], outputs[1])
        cuts = self_mosh_segments(3, 10, .6, 0, 0, 17, shuffle=True)
        cursor, frame_size = 0, 96 * 64 * 3
        for start, frames, _ in cuts:
            expected = command("ffmpeg", "-v", "error", "-ss", str(start / 10), "-i", self.source,
                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
            actual = outputs[0][cursor * frame_size:(cursor + 1) * frame_size]
            error = sum(abs(a - b) for a, b in zip(actual, expected.stdout)) / frame_size
            self.assertLess(error, 10, f"shuffled cut at {start}: pixel error {error}")
            cursor += frames
        proc = self.effex("algorithm=shuffle", "cut_length=.6", "transition=.2", "reverse=35", "seed=17")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        output = json.loads(proc.stdout)["output"]
        frames = command("ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "frame=key_frame", "-of", "csv=p=0", output)
        self.assertEqual([i for i, line in enumerate(frames.stdout.splitlines()) if line == b"1"],
                         [0, 8, 14, 20, 26])


if __name__ == "__main__":
    unittest.main()
