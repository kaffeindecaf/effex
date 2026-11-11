#!/usr/bin/env python3
"""Reverse Infection behavior, bounded windows and real timestamped CLI renders."""
from dataclasses import replace
from fractions import Fraction
import importlib.util
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
from frame_pipeline import FramePacket, process_packets
from reverse_infection import ReverseInfection

try:
    import numpy as np
    import cv2
except ImportError:
    np = cv2 = None

ROOT = Path(__file__).resolve().parents[1]


def command(*args, timeout=90):
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout)


def packet(frame, index, stamp=None):
    return FramePacket(frame, index, Fraction(index, 24) if stamp is None else stamp,
                       Fraction(1, 24000), Fraction(1, 24), {"source_id": "synthetic"})


def scene(count=54, shape=(64, 96, 3), arrival=20):
    for i in range(count):
        frame = np.full(shape, 15, np.uint8)
        if i >= arrival:
            x = min(shape[1] - 18, 20 + (i - arrival))
            frame[20:44, x:x + 16] = (240, 35, 20)
        yield packet(frame, i)


@unittest.skipUnless(np is not None and cv2 is not None, "install requirements-reverse-infection.txt")
class InfectionTests(unittest.TestCase):
    def processor(self, shape=(64, 96, 3), **config):
        p = ReverseInfection()
        p.initialize(config, shape, 24, 42)
        return p

    def render(self, **config):
        p = self.processor(**config)
        return list(process_packets(scene(), [p]))

    def test_future_pixels_arrive_early_and_continue_through_arrival(self):
        out = self.render()
        # Source is empty until frame 20. Earlier red is future-only material.
        for q in out[4:20]:
            red = q.frame[..., 0].astype(int) - q.frame[..., 1]
            self.assertGreater(np.count_nonzero(red > 15), 20, q.index)
        self.assertEqual([q.index for q in out], list(range(54)))
        self.assertGreater(out[20].frame[28:38, 23:32, 0].mean(), 170)
        # Not a copied future frame or a uniform crossfade: material has grown
        # into spatially distinct regions and accumulated multiple remaps.
        future = list(scene())[24].frame
        ordinary = (15 * .4 + future * .6).astype(np.uint8)
        self.assertFalse(np.array_equal(out[0].frame, future))
        self.assertFalse(np.array_equal(out[0].frame, ordinary))

    def test_spatial_growth_increases_contaminated_area_and_contraction_reduces_it(self):
        areas = []
        for radius in (-1, 0, 3, 6):
            result = self.render(spread_radius=radius, mutation_strength=0)[4].frame
            areas.append(np.count_nonzero(result[..., 0].astype(int) - result[..., 1] > 12))
        self.assertLess(areas[0], areas[1], areas)
        self.assertLess(areas[1], areas[2], areas)
        self.assertLessEqual(areas[2], areas[3], areas)

    def test_seed_is_repeatable_resettable_and_changes_mutation(self):
        p = self.processor(seed=42)
        a = list(process_packets(scene(), [p]))
        p.reset()
        b = list(process_packets(scene(), [p]))
        c = self.render(seed=43)
        self.assertTrue(all(np.array_equal(x.frame, y.frame) for x, y in zip(a, b)))
        self.assertTrue(any(not np.array_equal(x.frame, y.frame) for x, y in zip(a, c)))
        restored = self.processor(**json.loads(json.dumps(p.serialize_config())))
        self.assertEqual(restored.serialize_config(), p.serialize_config())
        derived = ReverseInfection()
        derived.initialize({"seed": 0}, (64, 96, 3), 24, 1701)
        self.assertEqual(derived._seed, 1701)

    def test_flow_sampling_sign_and_vector_scaling(self):
        rng = np.random.default_rng(3)
        current = cv2.GaussianBlur(rng.integers(0, 255, (96, 128, 3), dtype=np.uint8), (3, 3), 0)
        future = np.roll(current, 4, axis=1)
        for scale in (1, .5):
            p = self.processor(shape=current.shape, flow_scale=scale)
            flow = p._full_flow(p._estimate_flow(p._gray(current), p._gray(future)))
            self.assertAlmostEqual(float(np.median(flow[20:-20, 20:-20, 0])), 4, delta=.9)
            aligned = p._warp(future, flow)
            wrong = p._warp(future, -flow)
            error = lambda a: np.abs(a[20:-20, 20:-20].astype(float) - current[20:-20, 20:-20]).mean()
            self.assertLess(error(aligned), error(future) * .65)
            self.assertLess(error(aligned), error(wrong) * .65)

    def test_recurrence_uses_every_adjacent_motion_field(self):
        # Force exactly +1 px/frame: a donor at x=40 must move to x=36 after
        # four backwards samples. This distinguishes propagation from overlay.
        p = self.processor(lookahead_frames=4, window_overlap=0, mutation_strength=0,
                           spread_radius=0, backward_decay=1, intensity=1, infection_strength=1)
        frames = [np.zeros((64, 96, 3), np.uint8) for _ in range(5)]
        frames[-1][25:35, 40:50] = (250, 30, 10)
        with patch.object(p, "_estimate_flow", side_effect=lambda a, b:
                          np.dstack((np.full_like(a, .5, dtype=np.float32),
                                     np.zeros_like(a, dtype=np.float32)))) as flow:
            output = list(process_packets((packet(f, i) for i, f in enumerate(frames)), [p]))
        self.assertEqual(flow.call_count, 4)
        self.assertGreater(output[0].frame[29, 36, 0], 150)
        self.assertEqual(output[0].frame[29, 49, 0], 0)
        self.assertGreater(output[1].frame[29, 37, 0], 150)

    def test_vfr_metadata_ownership_time_horizon_and_large_timestamp_gap(self):
        stamps = [Fraction(i, 100) for i in (500, 503, 511, 519, 535, 610)]
        p = self.processor(lookahead_mode="seconds", lookahead_seconds=.2,
                           mutation_strength=0, spread_radius=0)
        output = []
        for i, stamp in enumerate(stamps):
            frame = np.zeros((64, 96, 3), np.uint8)
            if i >= 4:
                frame[20:40, 30:50] = (250, 30, 20)
            output.extend(p.push(packet(frame, i, stamp)))
            frame[:] = 255  # buffer must own its snapshot
        output.extend(p.flush())
        self.assertEqual([q.timestamp for q in output], stamps)
        self.assertTrue(all(q.duration == Fraction(1, 24) and q.time_base == Fraction(1, 24000)
                            and q.context == {"source_id": "synthetic"} for q in output))
        self.assertEqual(int(output[0].frame.max()), 0)  # red frame exceeds its .2s horizon
        self.assertGreater(int(output[3].frame[..., 0].max()), 10)
        self.assertTrue(all(q.frame.shape == (64, 96, 3) and q.frame.dtype == np.uint8 for q in output))

    def test_short_eof_empty_disable_and_no_brightness_feedback(self):
        for count in (0, 1, 2, 7, 24, 25, 26, 41):
            frames = list(scene(count=count, arrival=2))
            p = self.processor()
            output = list(process_packets(iter(frames), [p]))
            self.assertEqual(len(output), count)
            self.assertEqual(p.buffered_frames, 0)
            if count:
                np.testing.assert_array_equal(output[-1].frame, frames[-1].frame)
                self.assertLessEqual(max(int(q.frame.max()) for q in output), 240)
        for config in (dict(intensity=0), dict(infection_strength=0), dict(lookahead_frames=0),
                       dict(lookahead_mode="seconds", lookahead_seconds=0)):
            original = list(scene())
            output = list(process_packets(iter(original), [self.processor(**config)]))
            for a, b in zip(original, output):
                np.testing.assert_array_equal(a.frame, b.frame)
                self.assertFalse(np.shares_memory(a.frame, b.frame))
        disabled_large = self.processor(shape=(2160, 3840, 3), intensity=0)
        self.assertEqual(disabled_large.state_nbytes, 0)

    def test_scene_cuts_source_changes_and_seek(self):
        p = self.processor()
        black = np.zeros((64, 96, 3), np.uint8)
        white = np.full_like(black, 255)
        for explicit in (False, True):
            p.reset()
            list(p.push(packet(black, 0)))
            cut = replace(packet(white, 1), context={"source_id": "synthetic", "scene_cut": explicit})
            out = list(p.push(cut))
            np.testing.assert_array_equal(out[0].frame, black)
            self.assertEqual([q.index for q in p.flush()], [1])
        list(p.push(packet(black, 10)))
        self.assertEqual(list(p.push(packet(white, 0))), [])
        self.assertEqual([q.index for q in p.flush()], [0])
        list(p.push(packet(black, 0)))
        self.assertEqual(list(p.push(replace(packet(white, 1), context={"source_id": "other"}))), [])
        self.assertEqual([q.index for q in p.flush()], [1])

    def test_capacity_stays_bounded_on_long_stream_and_high_density_pts(self):
        p = ReverseInfection()
        p.MAX_STATE_BYTES = 64 * 96 * 150 + 65536
        p.initialize(dict(lookahead_mode="seconds", lookahead_seconds=5, window_overlap=2),
                     (64, 96, 3), 24, 42)
        count = 0
        for i in range(220):
            count += len(list(p.push(packet(np.zeros((64, 96, 3), np.uint8), i, Fraction(i, 1000)))))
            self.assertLessEqual(p.buffered_frames, p._capacity)
            self.assertLessEqual(p.state_nbytes, p.MAX_STATE_BYTES)
        count += len(list(p.flush()))
        self.assertEqual(count, 220)
        self.assertGreater(p.capacity_fallbacks, 0)
        self.assertLessEqual(p.peak_buffered_frames, p._capacity)
        self.assertTrue(any("budget" in n for n in p.runtime_notes))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.processor(shape=(10000, 10000, 3))

    def test_overlap_joins_do_not_restart_randomness(self):
        # A static future patch should not flicker when a new window begins.
        p = self.processor(lookahead_frames=12, window_overlap=6, mutation_strength=.1,
                           spread_radius=1)
        out = list(process_packets(scene(count=72, arrival=8), [p]))
        jumps = np.array([np.abs(a.frame.astype(float) - b.frame).mean()
                          for a, b in zip(out, out[1:])])
        boundaries = jumps[np.arange(5, 60, 6)]
        self.assertLess(float(boundaries.max()), max(12, float(np.percentile(jumps, 95)) * 3))


class IntegrationTests(unittest.TestCase):
    def test_selector_parameters_validation_and_menu(self):
        effect = effex.EFFECT_BY_KEY["datamosh"]
        chain = effex.one(effect)
        opts = effex.default_chain_opts(chain)
        opts["datamosh"]["algorithm"] = "reverse_infection"
        visible = {r.param.key for r in effex.option_rows(chain, opts) if r.param}
        self.assertTrue(effex.REVERSE_INFECTION_KEYS.issubset(visible))
        self.assertIn("seed", visible)
        self.assertFalse(visible & {"preset", "grit", "bloom", "second", "pulse_length", "reverse"})
        self.assertTrue(effex.is_reverse_infection(chain, opts))
        for key, default in ReverseInfection.DEFAULTS.items():
            self.assertEqual(opts["datamosh"][key], default)
        opts["datamosh"]["algorithm"] = "classic"
        self.assertFalse(effex.REVERSE_INFECTION_KEYS & {r.param.key for r in effex.option_rows(chain, opts) if r.param})
        for key, (low, high) in ReverseInfection.BOUNDS.items():
            for value in (low - 1, high + 1, float("nan"), float("inf")):
                with self.assertRaisesRegex(ValueError, key):
                    ReverseInfection.validate_config({key: value})

    @unittest.skipUnless(np is not None and cv2 is not None and importlib.util.find_spec("av")
                         and shutil.which("ffmpeg") and shutil.which("ffprobe"), "video dependencies required")
    def test_real_cli_vfr_audio_preview_and_validation(self):
        with tempfile.TemporaryDirectory(prefix="effex-reverse-") as directory:
            root = Path(directory)
            source = root / "vfr.mkv"
            made = command("ffmpeg", "-v", "error", "-y", "-copyts", "-f", "lavfi", "-i",
                           "testsrc2=s=96x64:r=12:d=4", "-f", "lavfi", "-i", "sine=d=4",
                           "-vf", "select='not(eq(mod(n,4),3))',setpts=PTS+2/TB",
                           "-af", "asetpts=PTS+2/TB", "-fps_mode", "vfr", "-c:v", "ffv1", "-c:a", "aac", source)
            self.assertEqual(made.returncode, 0, made.stderr.decode())
            dst = root / "infected.mp4"
            args = [sys.executable, ROOT / "effex.py", "datamosh", source,
                    "--set", "algorithm=reverse_infection", "--output", dst, "--json", "--preset", "ultrafast"]
            result = command(*args)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            data = json.loads(result.stdout)
            self.assertEqual(Path(data["output"]), dst)
            probe = command("ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "json", dst)
            self.assertEqual(json.loads(probe.stdout)["streams"][0], {"width": 96, "height": 64})
            def pts(path, kind="v"):
                result = command("ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
                                 "-show_entries", "packet=pts_time", "-of", "json", path)
                return sorted(Fraction(p["pts_time"]) for p in json.loads(result.stdout)["packets"])
            self.assertEqual(pts(source), pts(dst))
            source_audio, dest_audio = pts(source, "a"), pts(dst, "a")
            self.assertEqual(len(source_audio), len(dest_audio))
            # Matroska rounds AAC timestamps to milliseconds; MP4 restores the
            # sample clock even when compressed packets are copied unchanged.
            self.assertTrue(all(abs(a - b) <= Fraction(1, 1000)
                                for a, b in zip(source_audio, dest_audio)))
            for invalid in ("intensity=nan", "backward_decay=1.1", "block_size=0", "seed=-1",
                            "lookahead_mode=bad", "audio=mix", "second=other.mp4"):
                failed = command(*(args + ["--set", invalid, "--dry-run", "--force"]))
                self.assertNotEqual(failed.returncode, 0, invalid)
                self.assertNotIn(b"Traceback", failed.stderr)
            chain = effex.one(effex.EFFECT_BY_KEY["datamosh"])
            opts = effex.default_chain_opts(chain)
            opts["datamosh"].update(algorithm="reverse_infection", audio="none")
            cli = SimpleNamespace(width=0, height=0, suffix="", crf=18, preset="ultrafast",
                                  output_dir=str(root), input_dir=str(root))
            tools = effex.Tools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
            ok, name = effex.render_preview(chain, effex.probe(source, tools), tools, cli, opts)
            self.assertTrue(ok, name)
            preview_pts = pts(root / name)
            self.assertLess(float(preview_pts[-1] - preview_pts[0]), 3)
            self.assertGreater(len(preview_pts), 10)
            self.assertLess(len(preview_pts), len(pts(source)))
            opts["datamosh"]["audio"] = "auto"
            ok, name = effex.render_preview(chain, effex.probe(source, tools), tools, cli, opts)
            self.assertTrue(ok, name)
            self.assertLessEqual(pts(root / name, "a")[-1], pts(root / name)[-1] + Fraction(1, 10))


if __name__ == "__main__":
    unittest.main()
