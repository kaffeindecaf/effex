#!/usr/bin/env python3
"""Psychedelic builders, pixel behavior and real CLI renders (stdlib only)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import effex
from harness import (EFFEX, audio_codec, env_for, ffprobe_streams, make_clip,
                     run, video_dims)


class PsychedelicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="effex-psychedelic-")
        cls.addClassCleanup(cls.folder.cleanup)
        root = Path(cls.folder.name)
        cls.out_dir = root / "out"
        cls.out_dir.mkdir()
        cls.env = env_for(root, cls.out_dir)
        cls.clip = root / "clip.mp4"
        cls.silent = root / "silent.mp4"
        assert make_clip(cls.clip, seconds=1, size="160x90")
        assert make_clip(cls.silent, seconds=1, size="160x90", audio=False)

    def plan(self, key, **opts):
        effect = effex.EFFECT_BY_KEY[key]
        values = effex.default_opts(effect)
        values.update(opts)
        clip = effex.Clip(Path("synthetic.mp4"), width=64, height=64)
        ctx = effex.EffectContext(clip, 64, 64, "", 18, "ultrafast", values)
        return effex.build_plan(effect, ctx)

    def pixels(self, vf, rgb=(230, 150, 70)):
        proc = run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt",
                    "rgb24", "-s", "64x64", "-i", "pipe:0", "-vf", vf,
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                   stdin=bytes(rgb) * (64 * 64))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(len(proc.stdout), 64 * 64 * 3)
        return tuple(proc.stdout[:3])

    def test_lobotomy_crushes_channels_then_kills_then_swaps(self):
        self.assertTrue("lobotomy" in effex.EFFECT_BY_KEY,
                        "channel lobotomy must be registered")
        plan = self.plan("lobotomy", red="4", green="8", blue="2",
                         kill="g", swap="rb")
        self.assertEqual((plan.width, plan.height), (64, 64))
        self.assertEqual(self.pixels(plan.vf), (0, 0, 192))

    def test_neon_cycles_colors_on_static_footage(self):
        self.assertTrue("neon" in effex.EFFECT_BY_KEY,
                        "neon hue cycle must be registered")
        plan = self.plan("neon", amount=120, period=1.0)
        proc = run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=64x64:r=4:d=1", "-vf", plan.vf,
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        size = 64 * 64 * 3
        self.assertEqual(len(proc.stdout), 4 * size)
        first, quarter, half = (proc.stdout[i * size:i * size + 3]
                                for i in (0, 1, 2))
        self.assertGreater(sum(abs(a - b) for a, b in zip(first, quarter)), 100)
        self.assertLess(sum(abs(a - b) for a, b in zip(first, half)), 6)

    def test_lobotomy_channel_controls(self):
        unchanged = dict(red="256", green="256", blue="256",
                         kill="none", swap="none")
        self.assertEqual(self.pixels(self.plan("lobotomy", **unchanged).vf),
                         (230, 150, 70))
        for kill, expected in (("r", (0, 150, 70)), ("g", (230, 0, 70)),
                               ("b", (230, 150, 0))):
            with self.subTest(kill=kill):
                opts = dict(unchanged, kill=kill)
                self.assertEqual(self.pixels(self.plan("lobotomy", **opts).vf),
                                 expected)
        for swap, expected in (("rb", (70, 150, 230)), ("rg", (150, 230, 70)),
                               ("gb", (230, 70, 150)), ("roll", (150, 70, 230))):
            with self.subTest(swap=swap):
                opts = dict(unchanged, swap=swap)
                self.assertEqual(self.pixels(self.plan("lobotomy", **opts).vf),
                                 expected)
        for levels in (2, 4, 8, 16, 32, 64, 128, 256):
            with self.subTest(levels=levels):
                opts = dict(unchanged, red=str(levels))
                expected = (230 // (256 // levels) * (256 // levels), 150, 70)
                self.assertEqual(self.pixels(self.plan("lobotomy", **opts).vf),
                                 expected)

    def test_neon_can_be_disabled(self):
        plan = self.plan("neon", amount=0, saturation=100, punch=0)
        self.assertEqual(plan.vf, "null")
        self.assertEqual(self.pixels(plan.vf), (230, 150, 70))

    def test_neon_saturation_and_punch_change_pixels(self):
        neutral = self.pixels(self.plan("neon", amount=0, saturation=100,
                                       punch=0).vf, (180, 100, 70))
        for opts in (dict(saturation=300, punch=0),
                     dict(saturation=100, punch=100)):
            with self.subTest(opts=opts):
                changed = self.pixels(self.plan("neon", amount=0, **opts).vf,
                                      (180, 100, 70))
                self.assertGreater(sum(abs(a - b) for a, b in
                                       zip(neutral, changed)), 20)

    def test_cli_lists_both_effects_and_all_controls(self):
        proc = run([EFFEX, "--list", "--json"], env_extra=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        effects = {e["key"]: e for e in json.loads(proc.stdout)["effects"]}
        for key in ("lobotomy", "neon"):
            with self.subTest(effect=key):
                self.assertEqual({p["key"] for p in effects[key]["options"]},
                                 {p.key for p in effex.EFFECT_BY_KEY[key].params})

    def test_cli_rejects_invalid_options(self):
        for key, settings in {
            "lobotomy": ("red=1", "green=3", "blue=512", "kill=x", "swap=x"),
            "neon": ("amount=-1", "amount=181", "period=0", "period=31",
                     "period=abc", "saturation=99", "saturation=301",
                     "punch=-1", "punch=101"),
        }.items():
            for setting in settings:
                with self.subTest(effect=key, setting=setting):
                    proc = run([EFFEX, key, self.clip, "--set", setting,
                                "--dry-run"], env_extra=self.env)
                    self.assertEqual(proc.returncode, 2, proc.stderr.decode())

    def test_dry_run_uses_options_and_writes_nothing(self):
        before = set(self.out_dir.iterdir())
        proc = run([EFFEX, "lobotomy+neon", self.clip, "--set", "red=2",
                    "--set", "neon.period=1.5", "--set", "saturation=300%",
                    "--set", "punch=100%", "--dry-run"], env_extra=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        text = proc.stdout.decode()
        for expression in ("floor(val/128)*128", "sin(2*PI*t/1.5)",
                           "s=3", "0.25/0.1 0.75/0.9"):
            self.assertIn(expression, text)
        self.assertEqual(set(self.out_dir.iterdir()), before)

    def test_real_renders_keep_size_timing_audio_and_chain_order(self):
        for key in ("lobotomy", "neon", "lobotomy+neon", "neon+lobotomy"):
            for clip in (self.clip, self.silent):
                with self.subTest(effect=key, source=clip.name):
                    proc = run([EFFEX, key, clip, "--json", "--preset",
                                "ultrafast"], env_extra=self.env)
                    self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                    data = json.loads(proc.stdout)
                    self.assertTrue(data["ok"], data)
                    out = data["output"]
                    self.assertEqual(video_dims(out), (160, 90))
                    self.assertEqual(audio_codec(out),
                                     "aac" if clip == self.clip else None)
                    streams = ffprobe_streams(out)
                    video = next(s for s in streams["streams"]
                                 if s["codec_type"] == "video")
                    self.assertEqual(int(video["nb_frames"]), 30)
                    self.assertAlmostEqual(float(video["duration"]), 1, delta=0.05)
                    if "+" in key:
                        self.assertEqual(data["chain"], key.split("+"))

    def test_builders_use_upstream_frame_dimensions(self):
        chain, error = effex.parse_chain("stretch+lobotomy+neon")
        self.assertFalse(error)
        clip = effex.Clip(self.clip, width=160, height=90)
        ctx = effex.EffectContext(clip, 64, 64, "", 18, "ultrafast",
                                 {"stretch": {"target": "64x64"}})
        plans = effex.chain_plans(chain, ctx)
        self.assertEqual([(p.width, p.height) for _, p in plans], [(64, 64)] * 3)


if __name__ == "__main__":
    unittest.main()
