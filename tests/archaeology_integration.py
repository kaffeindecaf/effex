#!/usr/bin/env python3
"""Pixel archaeology (future cut) registry, media pipeline and terminal wiring."""
import importlib.util
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import (EFFEX, audio_codec, env_for, make_clip, run,  # noqa: E402
                     video_dims, video_frames)


@unittest.skipUnless(importlib.util.find_spec("numpy"), "install requirements-stateful.txt")
class ArchaeologyIntegration(unittest.TestCase):
    def test_registered_and_lazy_without_numeric_dependencies(self):
        import effex
        self.assertIn("archaeology", effex.EFFECT_BY_KEY)
        effect = effex.EFFECT_BY_KEY["archaeology"]
        self.assertEqual(effect.name, "pixel archaeology")
        self.assertIsNotNone(effect.frame_processor)
        self.assertTrue(getattr(effect.frame_processor, "requires_timestamps", False))
        members = [key for _label, keys in effex.EFFECT_GROUPS for key in keys]
        self.assertEqual(members.count("archaeology"), 1)
        listed = run([sys.executable, "-S", EFFEX, "--list", "--json"])
        self.assertEqual(listed.returncode, 0, listed.stderr.decode())
        data = json.loads(listed.stdout)
        self.assertIn("archaeology", [e["key"] for e in data["effects"]])

    def test_cli_renders_future_cutouts_and_preserves_audio(self):
        with tempfile.TemporaryDirectory(prefix="effex-archaeology-cli-") as directory:
            root = Path(directory)
            clip = root / "source.mp4"
            self.assertTrue(make_clip(clip, seconds=2, size="160x96"))
            env = env_for(root, root / "out")
            proc = run([EFFEX, "archaeology", clip, "--json", "--preset", "ultrafast",
                        "--set", "gap=6", "--set", "spawn_every=8", "--set",
                        "size=20", "--set", "hold=2", "--set", "melt=4",
                        "--set", "seam=none", "--set", "seed=17"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            payload = json.loads(proc.stdout.decode())
            output = Path(payload["output"])
            self.assertTrue(output.exists())
            self.assertEqual(video_dims(output), (160, 96))
            self.assertEqual(video_frames(output), video_frames(clip))
            self.assertEqual(audio_codec(output), "aac")
            self.assertIsInstance(payload.get("warnings", []), list)
            decoded = run(["ffmpeg", "-v", "error", "-i", str(output),
                           "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())
            self.assertEqual(len(decoded.stdout), video_frames(clip) * 160 * 96 * 3)

    def test_dry_run_describes_the_future_buffer(self):
        with tempfile.TemporaryDirectory(prefix="effex-archaeology-dry-") as directory:
            root = Path(directory)
            clip = root / "source.mp4"
            self.assertTrue(make_clip(clip, seconds=1, size="160x96"))
            env = env_for(root, root / "out")
            proc = run([EFFEX, "archaeology", clip, "--dry-run",
                        "--set", "gap=9", "--set", "lookahead=1.2",
                        "--set", "hold=3", "--set", "melt=5"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            text = proc.stdout.decode()
            self.assertIn("archaeology.gap=9", text)
            self.assertIn("archaeology.mode=smudge", text)
            self.assertIn("stateful future-cut frame pass", text)
            self.assertIn("9 frame(s) ahead", text)
            self.assertIn("1.2s future buffer", text)
            self.assertIn("3 sharp frames then 5 melt frames", text)

    def test_bad_choice_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="effex-archaeology-bad-") as directory:
            root = Path(directory)
            clip = root / "source.mp4"
            self.assertTrue(make_clip(clip, seconds=1, size="160x96"))
            env = env_for(root, root / "out")
            for setting in ("pick=sparkle", "shape=oval", "seam=glow",
                            "mode=warp", "gap=0", "size=41", "lookahead=4"):
                proc = run([EFFEX, "archaeology", clip, "--dry-run",
                            "--set", setting], env_extra=env)
                self.assertEqual(proc.returncode, 2, setting)
                self.assertNotIn(b"Traceback", proc.stderr)

    def test_filter_and_timestamped_chains(self):
        with tempfile.TemporaryDirectory(prefix="effex-archaeology-chains-") as directory:
            root = Path(directory)
            clip = root / "source.mp4"
            self.assertTrue(make_clip(clip, seconds=0.5, size="160x96"))
            env = env_for(root, root / "out")
            chains = ["neon+archaeology+deepfry", "archaeology+sediment",
                      "sediment+archaeology"]
            if importlib.util.find_spec("av"):
                chains += ["inversion+archaeology", "archaeology+inversion"]
            for chain in chains:
                with self.subTest(chain=chain):
                    proc = run([EFFEX, chain, clip, "--json", "--preset", "ultrafast"],
                               env_extra=env)
                    self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                    output = json.loads(proc.stdout)["output"]
                    self.assertEqual(video_dims(output), (160, 96))
                    self.assertEqual(audio_codec(output), "aac")
            if importlib.util.find_spec("av"):
                vfr = root / "vfr.mkv"
                made = run(["ffmpeg", "-v", "error", "-y", "-copyts", "-f", "lavfi", "-i",
                            "testsrc2=s=160x96:r=10:d=1.2", "-vf",
                            "select='not(eq(mod(n,4),3))',setpts=PTS+5/TB",
                            "-fps_mode", "vfr", "-c:v", "ffv1", vfr])
                self.assertEqual(made.returncode, 0, made.stderr.decode())
                proc = run([EFFEX, "archaeology+inversion", vfr, "--json"], env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())

                def pts(path):
                    result = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "frame=pts_time", "-of", "json", path])
                    return [Fraction(f["pts_time"]) for f in json.loads(result.stdout)["frames"]]
                self.assertEqual(pts(json.loads(proc.stdout)["output"]), pts(vfr))


if __name__ == "__main__":
    unittest.main()
