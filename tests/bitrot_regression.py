#!/usr/bin/env python3
"""Bitstream rot must damage only VOP payloads and still decode every frame."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# the repo goes first: tests/datamosh_stream.py would shadow the real module
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bit_rot import (payload_start, rot_stream, start_codes,  # noqa: E402
                     vop_units)
from datamosh_stream import clock_bits  # noqa: E402
from harness import EFFEX, env_for, make_clip, run  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def encode(path: Path, size="320x180", frames=60, keyint=15) -> bytes:
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"testsrc2=size={size}:rate=30", "-frames:v", str(frames),
        "-threads", "4", "-c:v", "mpeg4", "-q:v", "6", "-g", str(keyint),
        "-bf", "0", "-f", "m4v", str(path)], check=True)
    return path.read_bytes()


def frame_count(path: Path) -> int:
    proc = subprocess.run([
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return int(proc.stdout.strip() or 0)


def gray_frames(path: Path) -> bytes:
    return subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=80:-2",
        "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True).stdout


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
class ByteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="effex-bitrot-")
        cls.raw = encode(Path(cls.temp.name) / "clean.m4v")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def rot(self, **kw):
        options = dict(rate=60, mode="mix", target="all", burst=30, hits=4,
                       seed=11)
        options.update(kw)
        return rot_stream(self.raw, **options)

    def test_rate_zero_returns_identical_bytes(self):
        self.assertEqual(rot_stream(self.raw, 0, seed=5), self.raw)

    def test_same_seed_same_bytes_other_seed_differs(self):
        self.assertEqual(self.rot(), self.rot())
        self.assertNotEqual(self.rot(), self.rot(seed=12))
        self.assertNotEqual(self.rot(), self.raw)

    def test_structure_and_headers_survive_every_mode(self):
        width = clock_bits(self.raw)
        units = vop_units(self.raw)
        first_vop = units[0][0]
        for mode in ("flip", "zero", "smear", "mix"):
            for target in ("p", "i", "all"):
                with self.subTest(mode=mode, target=target):
                    out = self.rot(mode=mode, target=target, rate=100, hits=8)
                    self.assertEqual(len(out), len(self.raw))
                    self.assertNotEqual(out, self.raw)
                    # no new or lost 00 00 01: start codes are exactly where they were
                    self.assertEqual(start_codes(out), start_codes(self.raw))
                    # VOS / VOL headers before the first VOP are untouched
                    self.assertEqual(out[:first_vop], self.raw[:first_vop])
                    for start, end, kind in units:
                        head = start + payload_start(self.raw[start:end], width)
                        self.assertEqual(out[start:head], self.raw[start:head])
                        if target == "p" and kind == 0:
                            self.assertEqual(out[start:end], self.raw[start:end])
                        if target == "i" and kind != 0:
                            self.assertEqual(out[start:end], self.raw[start:end])

    def test_zero_runs_never_emulate_start_codes_across_seeds(self):
        expected = start_codes(self.raw)
        for seed in range(1, 25):
            out = self.rot(mode="zero", rate=100, hits=16, seed=seed)
            self.assertEqual(start_codes(out), expected, seed)

    def test_rotted_stream_still_decodes_every_frame(self):
        path = Path(self.temp.name) / "rot.m4v"
        path.write_bytes(self.rot(rate=100, hits=8))
        held = path.with_suffix(".mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "m4v", "-i",
                        str(path), "-c:v", "copy", str(held)], check=True)
        for conceal in ([], ["-ec", "0"]):
            with self.subTest(conceal=conceal):
                proc = subprocess.run([
                    "ffmpeg", "-v", "quiet", "-err_detect", "ignore_err", *conceal,
                    "-i", str(held), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                    capture_output=True)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(len(proc.stdout), 60 * 320 * 180)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
class CliTests(unittest.TestCase):
    def test_render_decodes_fully_and_differs_from_a_clean_encode(self):
        with tempfile.TemporaryDirectory(prefix="effex-bitrot-cli-") as temp:
            in_dir, out_dir = Path(temp) / "in", Path(temp) / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            source = in_dir / "clip.mp4"
            self.assertTrue(make_clip(source, seconds=3, size="320x180"))
            env = env_for(in_dir, out_dir)
            outputs = {}
            for name, rate in (("clean", 0), ("rot", 15)):
                proc = run([EFFEX, "bitrot", source, "--set", f"rate={rate}",
                            "--set", "seed=7", "--suffix", "_" + name,
                            "--json"], env_extra=env)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:400])
                data = json.loads(proc.stdout.decode())
                self.assertEqual(data["effect"], "bitrot")
                outputs[name] = Path(data["output"])
            rot = outputs["rot"]
            decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(rot),
                                     "-f", "null", "-"], capture_output=True)
            self.assertEqual(decode.returncode, 0)
            self.assertFalse(decode.stderr, "baked output should decode cleanly")
            self.assertEqual(frame_count(rot), frame_count(source))
            self.assertEqual(frame_count(outputs["clean"]), frame_count(source))
            streams = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                "-of", "csv=p=0", str(rot)], capture_output=True, text=True).stdout
            self.assertIn("h264", streams)
            self.assertIn("aac", streams)
            clean, broken = gray_frames(outputs["clean"]), gray_frames(rot)
            self.assertEqual(len(clean), len(broken))
            delta = sum(abs(a - b) for a, b in zip(clean, broken)) / len(clean)
            self.assertGreater(delta, 5.0, f"mean pixel delta {delta:.2f}")
            self.assertFalse(list(out_dir.glob(".*_rot_*")),
                             "temporary streams must be removed")

    def test_dry_run_lists_stages_and_effect_is_not_chainable(self):
        with tempfile.TemporaryDirectory(prefix="effex-bitrot-dry-") as temp:
            in_dir, out_dir = Path(temp) / "in", Path(temp) / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            source = in_dir / "clip.mp4"
            self.assertTrue(make_clip(source, seconds=1, size="160x96"))
            env = env_for(in_dir, out_dir)
            proc = run([EFFEX, "bitrot", source, "--dry-run"], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[:300])
            self.assertIn(b"rot_stream()", proc.stdout)
            self.assertIn(b"rate=15%", proc.stdout)
            proc = run([EFFEX, "bitrot+neon", source, "--dry-run"], env_extra=env)
            self.assertEqual(proc.returncode, 2)
            for setting in ("rate=101", "hits=0", "mode=melt", "target=b"):
                proc = run([EFFEX, "bitrot", source, "--set", setting,
                            "--dry-run"], env_extra=env)
                self.assertEqual(proc.returncode, 2, setting)


if __name__ == "__main__":
    unittest.main()
