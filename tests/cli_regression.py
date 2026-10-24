#!/usr/bin/env python3
"""CLI surface tests: exit codes, json, piped safety, option handling.

Run: python3 tests/cli_regression.py      (exit 0 = all green)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (Checker, EFFEX, env_for, ffprobe_streams,  # noqa: E402
                     make_clip, newest_mp4, require_tools, run,
                     temp_folders, video_dims, audio_codec)


def main() -> int:
    c = Checker("cli regression")
    if not require_tools(c):
        return 0

    in_dir, out_dir = temp_folders()
    env = env_for(in_dir, out_dir)
    clip = in_dir / "clip.mp4"
    clip_noaudio = in_dir / "silent.mp4"
    assert make_clip(clip), "could not build the test clip"
    assert make_clip(clip_noaudio, audio=False), "could not build the silent clip"

    # ---- status and listing -------------------------------------------
    proc = run([EFFEX, "--check"], env_extra=env)
    c.check("--check exits 0 when ffmpeg is present", proc.returncode == 0,
            proc.stderr.decode()[:120])
    c.check("--check reports ffmpeg", "ffmpeg" in proc.stdout.decode())

    proc = run([EFFEX, "--list"], env_extra=env)
    text = proc.stdout.decode()
    c.check("--list exits 0", proc.returncode == 0)
    c.check("--list shows the effects and their options",
            all(word in text for word in ("stretch", "pixelize", "rgb split",
                                          "stutter", "deepfry", "wobble",
                                          "spin", "posterize", "datamosh",
                                          "zoom", "vhs", "trails",
                                          "slow + pitch", "crt",
                                          "channelswap", "blockswap",
                                          "dropout", "crunch",
                                          "timescratch", "block size",
                                          "temporal sediment", "deposit rate",
                                          "excavation chance")),
            text[:200])
    c.check("--list shows option defaults", "default" in text)

    proc = run([EFFEX, "--list", "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("--list --json parses", isinstance(data, dict))
    c.check("--list --json lists choices",
            bool(data["effects"][0]["options"][0]["choices"]))
    c.check("--list --json lists the input videos", len(data["videos"]) == 2,
            str(data["videos"]))

    # ---- piped runs must never wait for input -------------------------
    proc = run([EFFEX], stdin=b"")
    c.check("no args with no tty prints usage instead of hanging",
            proc.returncode == 0 and b"usage: effex" in proc.stdout)
    c.check("piped usage carries no ansi escapes",
            b"\033[" not in proc.stdout)

    proc = run([EFFEX, "--check", "--json"], env_extra=env)
    c.check("--check --json parses", json.loads(proc.stdout.decode())["ok"])

    # ---- rendering ----------------------------------------------------
    proc = run([EFFEX, "stretch", clip, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    out = Path(data["output"])
    c.check("default render exits 0", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("default options are 9:16 portrait",
            (data["width"], data["height"]) == (1080, 1920), str(data))
    c.check("output lands in the output folder", out.parent == out_dir)
    c.check("output is really 1080x1920", video_dims(out) == (1080, 1920),
            str(video_dims(out)))
    c.check("audio survived as aac", audio_codec(out) == "aac")

    proc = run([EFFEX, "stretch", clip, "--set", "target=16:9",
                "--set", "resample=lanczos", "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("--set target=16:9 wins", (data["width"], data["height"])
            == (1920, 1080), str(data))
    c.check("--set resample is reported", data["options"]["resample"]
            == "lanczos")
    c.check("16:9 output is really 1920x1080",
            video_dims(data["output"]) == (1920, 1080))

    proc = run([EFFEX, "stretch", clip, "--size", "720x1280", "--json"],
               env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("--size is shorthand for target", (data["width"], data["height"])
            == (720, 1280))
    c.check("custom size output is really 720x1280",
            video_dims(data["output"]) == (720, 1280))

    frames = {s["codec_type"]: s for s in
              ffprobe_streams(data["output"]).get("streams", [])}
    c.check("square pixels forced (sar 1:1)",
            frames["video"].get("sample_aspect_ratio") == "1:1",
            str(frames["video"].get("sample_aspect_ratio")))
    c.check("display ratio follows the target",
            frames["video"].get("display_aspect_ratio") == "9:16",
            str(frames["video"].get("display_aspect_ratio")))

    proc = run([EFFEX, "stretch", clip_noaudio, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("a source with no audio still renders", proc.returncode == 0)
    c.check("no audio stream is invented", audio_codec(data["output"]) is None)

    # ---- the six newer effects, one dry-run each + real renders ------
    dry_runs = {
        "rgbsplit": ("chromashift=cbh=12:crh=-12", []),
        "stutter": ("fps=8", []),
        "deepfry": ("eq=brightness=0.00:saturation=3.00:contrast=1.60,"
                   "unsharp=5:5:4.00", []),
        "wobble": ("rotate=a='4*(PI/180)*sin(2*PI*t/2.5)':fillcolor=black", []),
        "spin": ("rotate=a='2*PI*t/6'", []),
        "posterize": ("lutrgb", ["floor(val/32)*32"]),
    }
    for effect, (main, extras) in dry_runs.items():
        proc = run([EFFEX, effect, clip, "--dry-run"], env_extra=env)
        cmdline = proc.stdout.decode()
        c.check(f"{effect} --dry-run prints its real chain",
                proc.returncode == 0 and main in cmdline, cmdline[:200])
        for extra in extras:
            c.check(f"{effect} chain carries {extra}", extra in cmdline,
                    cmdline[:200])

    proc = run([EFFEX, "rgbsplit", clip, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("rgbsplit renders", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("rgbsplit keeps the frame", video_dims(data["output"])
            == (640, 360), str(video_dims(data["output"])))

    proc = run([EFFEX, "stutter", clip, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("stutter renders", proc.returncode == 0)
    rate = {s["codec_type"]: s for s in
            ffprobe_streams(data["output"]).get("streams", [])}
    c.check("stutter output really is 8 fps",
            rate.get("video", {}).get("r_frame_rate") == "8/1",
            str(rate.get("video", {}).get("r_frame_rate")))
    c.check("stutter keeps the audio", audio_codec(data["output"]) == "aac")

    proc = run([EFFEX, "wobble", clip, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("wobble renders", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("wobble keeps the frame", video_dims(data["output"])
            == (640, 360))

    proc = run([EFFEX, "posterize", clip, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("posterize renders", proc.returncode == 0)
    c.check("posterize keeps the frame", video_dims(data["output"])
            == (640, 360))

    # ---- option validation for the new kinds --------------------------
    proc = run([EFFEX, "rgbsplit", clip, "--set", "amount=1"], env_extra=env)
    c.check("rgbsplit amount range is enforced", proc.returncode == 2 and
            b"outside 2-80" in proc.stderr, proc.stderr.decode()[:160])

    proc = run([EFFEX, "wobble", clip, "--set", "amount=99"], env_extra=env)
    c.check("wobble amount range is enforced", proc.returncode == 2 and
            b"outside 1-15" in proc.stderr)

    proc = run([EFFEX, "wobble", clip, "--set", "speed=warp"], env_extra=env)
    c.check("wobble speed rejects unknown values", proc.returncode == 2)

    proc = run([EFFEX, "posterize", clip, "--set", "levels=2", "--dry-run"],
               env_extra=env)
    c.check("posterize levels change the quantization",
            "floor(val/128)*128" in proc.stdout.decode())

    # ---- pixelize -----------------------------------------------------
    proc = run([EFFEX, "pixelize", clip, "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    out = Path(data["output"])
    c.check("pixelize renders", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("pixelize keeps the source frame by default",
            video_dims(out) == (640, 360), str(video_dims(out)))
    c.check("pixelize keeps the audio", audio_codec(out) == "aac")

    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=48", "--dry-run"],
               env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("--dry-run shows the block downscale", "iw/48" in cmdline,
            cmdline[:200])
    c.check("--dry-run shows the neighbor upscale", "flags=neighbor" in cmdline)

    proc = run([EFFEX, "pixelize", clip, "--set", "shape=16:9", "--json"],
               env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("pixelize can force a shape", video_dims(data["output"])
            == (1920, 1080), str(video_dims(data["output"])))

    proc = run([EFFEX, "pixelize", clip, "--size", "720x1280", "--json"],
               env_extra=env)
    data = json.loads(proc.stdout.decode())
    c.check("--size works on pixelize too", (data["width"], data["height"])
            == (720, 1280), str(data)[:160])
    c.check("--size output is really 720x1280",
            video_dims(data["output"]) == (720, 1280))

    # the block size has to actually change the picture: a chunkier render
    # throws away far more detail, so the file lands much smaller
    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=2", "--crf", "20",
                "--preset", "ultrafast", "--json"], env_extra=env)
    fine = Path(json.loads(proc.stdout.decode())["output"])
    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=96", "--crf", "20",
                "--preset", "ultrafast", "--json"], env_extra=env)
    coarse = Path(json.loads(proc.stdout.decode())["output"])
    ratio = coarse.stat().st_size / max(1, fine.stat().st_size)
    c.check("bigger blocks really mean a softer picture", ratio < 0.75,
            f"coarse/fine = {ratio:.2f}")
    c.check("both block sizes kept the frame size",
            video_dims(fine) == (640, 360) == video_dims(coarse))

    # ---- datamosh -----------------------------------------------------
    proc = run([EFFEX, "datamosh", clip, "--dry-run"], env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("datamosh --dry-run shows its two passes",
            proc.returncode == 0 and "pass 1" in cmdline
            and "pass 2" in cmdline, cmdline[:200])
    c.check("datamosh --dry-run names the codec surgery",
            "strip_ivops" in cmdline and "mpeg4" in cmdline)

    proc = run([EFFEX, "datamosh", clip, "--set", "cut=heavy",
                "--set", "hold=15", "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    heavy = Path(data["output"])
    c.check("datamosh renders", proc.returncode == 0,
            proc.stderr.decode()[:200])
    c.check("datamosh keeps the frame size", video_dims(heavy) == (640, 360))
    c.check("datamosh keeps the audio", audio_codec(heavy) == "aac")

    proc = run([EFFEX, "datamosh", clip, "--set", "cut=light",
                "--set", "hold=15", "--json"], env_extra=env)
    light = Path(json.loads(proc.stdout.decode())["output"])
    c.check("light cut renders", proc.returncode == 0,
            proc.stderr.decode()[:200])

    def keyframes(path: Path) -> int:
        proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "frame=key_frame", "-of", "csv=p=0",
                    str(path)])
        return proc.stdout.decode().count("1")

    def video_frames(path: Path) -> int:
        proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=nb_frames", "-of", "csv=p=0",
                    str(path)])
        try:
            return int(proc.stdout.decode().strip() or 0)
        except ValueError:
            return 0

    heavy_kf, light_kf = keyframes(heavy), keyframes(light)
    c.check("a heavy cut removes more keyframes than a light one",
            heavy_kf < light_kf, f"heavy={heavy_kf} light={light_kf}")
    c.check("the heavy cut keeps only the first keyframe",
            heavy_kf == 1, f"keyframes={heavy_kf}")
    c.check("the mosh keeps the frame count (holes are filled)",
            video_frames(heavy) == video_frames(clip),
            f"{video_frames(heavy)} vs {video_frames(clip)}")

    proc = run([EFFEX, "datamosh", clip, "--set", "cut=bad"], env_extra=env)
    c.check("datamosh rejects a bad cut depth", proc.returncode == 2 and
            b"heavy, medium, light, dice, shuffle" in proc.stderr)

    proc = run([EFFEX, "datamosh", clip, "--set", "hold=1"], env_extra=env)
    c.check("datamosh hold range is enforced", proc.returncode == 2 and
            b"outside 2-600" in proc.stderr)

    # ---- harder datamosh + circuit bending ----------------------------
    proc = run([EFFEX, "datamosh", clip, "--set", "cut=dice",
                "--set", "fill=chaos", "--dry-run"], env_extra=env)
    c.check("dice survival and shuffle are documented in the plan",
            "dice uses configured survival" in proc.stdout.decode()
            and "survival=25%" in proc.stdout.decode()
            and "shuffle scrambles" in proc.stdout.decode())

    proc = run([EFFEX, "datamosh", clip, "--set", "cut=shuffle",
                "--set", "hold=15", "--json"], env_extra=env)
    shuffled = Path(json.loads(proc.stdout.decode())["output"])
    c.check("shuffle cut renders", proc.returncode == 0,
            proc.stderr.decode()[:200])
    c.check("shuffle keeps only the first keyframe",
            keyframes(shuffled) == 1, f"keyframes={keyframes(shuffled)}")
    c.check("shuffle still decodes every frame",
            video_frames(shuffled) == video_frames(clip))

    proc = run([EFFEX, "datamosh", clip, "--set", "cut=dice",
                "--set", "hold=15", "--json"], env_extra=env)
    dice = Path(json.loads(proc.stdout.decode())["output"])
    c.check("dice cut renders", proc.returncode == 0,
            proc.stderr.decode()[:200])
    dice_kf = keyframes(dice)
    c.check("dice keeps some but not all keyframes",
            1 <= dice_kf <= 5, f"keyframes={dice_kf}")

    proc = run([EFFEX, "datamosh", clip, "--set", "cut=heavy",
                "--set", "fill=prev", "--set", "hold=15", "--json"],
               env_extra=env)
    prev_fill = Path(json.loads(proc.stdout.decode())["output"])
    c.check("prev fill renders", proc.returncode == 0,
            proc.stderr.decode()[:200])
    c.check("prev fill keeps the frame count",
            video_frames(prev_fill) == video_frames(clip))

    proc = run([EFFEX, "datamosh", clip, "--set", "hold=2", "--json"],
               env_extra=env)
    c.check("hold 2 (melt every other frame) renders",
            proc.returncode == 0, proc.stderr.decode()[:200])

    proc = run([EFFEX, "channelswap", clip, "--dry-run"], env_extra=env)
    c.check("channelswap dry-run carries the swap matrix",
            "colorchannelmixer=rr=0:rb=1:bb=0:br=1"
            in proc.stdout.decode())
    proc = run([EFFEX, "channelswap", clip, "--set", "swap=roll",
                "--set", "bleed=10", "--dry-run"], env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("roll swap and bleed land in the chain",
            "rr=0:rg=1" in cmdline and "chromashift=cbh=10:crh=-10" in cmdline)
    proc = run([EFFEX, "channelswap", clip, "--set", "swap=invert",
                "--dry-run"], env_extra=env)
    c.check("invert is the negative lut",
            "lutrgb=r=negval:g=negval:b=negval" in proc.stdout.decode())

    red = out_dir / "red.mp4"
    proc = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-r", "10",
                str(red)])
    c.check("red probe clip exists", proc.returncode == 0 and red.exists())
    proc = run([EFFEX, "channelswap", red, "--json"], env_extra=env)
    bent = Path(json.loads(proc.stdout.decode())["output"])
    proc = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i",
                str(bent), "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"])
    pixel = proc.stdout[:3]
    c.check("red really becomes blue after the swap",
            pixel[0] < 32 and pixel[2] > 200, f"rgb={list(pixel)}")

    proc = run([EFFEX, "blockswap", clip, "--dry-run"], env_extra=env)
    c.check("blockswap dry-run carries the swaprect",
            "swaprect" in proc.stdout.decode())
    static = out_dir / "static.mp4"
    proc = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                "color=c=gray:s=128x128:d=3,geq=r='128+X':g='128':b='128-Y'",
                "-r", "10", str(static)])
    c.check("static probe clip exists",
            proc.returncode == 0 and static.exists())
    proc = run([EFFEX, "blockswap", static, "--json"], env_extra=env)
    moved = Path(json.loads(proc.stdout.decode())["output"])

    def frame_hash(path: Path, at: str) -> str:
        proc = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss",
                    at, "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                    "-pix_fmt", "rgb24", "-"])
        return proc.stdout.hex()[:64]

    h1, h2 = frame_hash(moved, "0.4"), frame_hash(moved, "2.2")
    c.check("the blocks really travel (frames differ)",
            h1 != h2, f"{h1} vs {h2}")
    c.check("blockswap keeps the frame size", video_dims(moved) == (128, 128))

    proc = run([EFFEX, "dropout", clip, "--set", "chance=30", "--dry-run"],
               env_extra=env)
    c.check("dropout dry-run carries the random select",
            "select='gt(random(0),0.3)'" in proc.stdout.decode())
    c.check("dropout prevents CFR from recreating removed frames",
            "-fps_mode vfr" in proc.stdout.decode())
    proc = run([EFFEX, "dropout", clip, "--set", "chance=30", "--json"],
               env_extra=env)
    dropped = Path(json.loads(proc.stdout.decode())["output"])
    n = video_frames(dropped)
    c.check("about a third of the frames vanish", 40 <= n <= 85, f"n={n}")
    dur = float(ffprobe_streams(dropped).get("format", {}).get("duration",
                                                               0) or 0)
    c.check("the clip keeps its length (frames hold)", 2.9 <= dur <= 3.2,
            f"duration={dur}")
    c.check("dropout keeps the audio", audio_codec(dropped) == "aac")

    # ---- crunch + timescratch -----------------------------------------
    proc = run([EFFEX, "crunch", clip, "--set", "res=50", "--dry-run"],
               env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("crunch halves the picture", "scale=320:180" in cmdline,
            cmdline[:200])
    c.check("crunch caps the bitrate",
            "b:v" in cmdline and "800k" in cmdline)
    c.check("crunch drops crf (abr mode)", "crf" not in cmdline)

    proc = run([EFFEX, "crunch", clip, "--set", "res=50", "--json"],
               env_extra=env)
    half = Path(json.loads(proc.stdout.decode())["output"])
    c.check("crunch renders", proc.returncode == 0,
            proc.stderr.decode()[:200])
    c.check("crunch really shrinks the picture",
            video_dims(half) == (320, 180))

    proc = run([EFFEX, "crunch", clip, "--set", "bitrate=200",
                "--preset", "ultrafast", "--json"], env_extra=env)
    low = Path(json.loads(proc.stdout.decode())["output"])
    proc = run([EFFEX, "crunch", clip, "--set", "bitrate=2000",
                "--preset", "ultrafast", "--json"], env_extra=env)
    high = Path(json.loads(proc.stdout.decode())["output"])
    ratio = low.stat().st_size / max(1, high.stat().st_size)
    c.check("the bitrate knob really turns the bitrate down",
            ratio < 0.5, f"200k/2000k = {ratio:.2f}")

    proc = run([EFFEX, "timescratch", clip, "--dry-run"], env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("timescratch dry-run shows the scrub",
            "split into segments" in cmdline and "reverse" in cmdline
            and "concat" in cmdline, cmdline[:200])

    proc = run([EFFEX, "timescratch", clip, "--json"], env_extra=env)
    t1 = Path(json.loads(proc.stdout.decode())["output"])
    c.check("timescratch renders", proc.returncode == 0,
            proc.stderr.decode()[:200])
    c.check("timescratch keeps the audio", audio_codec(t1) == "aac")
    d1 = float(ffprobe_streams(t1).get("format", {}).get("duration", 0) or 0)
    c.check("the scratch stretches time", 0.5 <= d1 <= 30.0,
            f"duration={d1}")
    c.check("timescratch keeps the frame size",
            video_dims(t1) == (640, 360))

    proc = run([EFFEX, "timescratch", clip, "--json"], env_extra=env)
    t2 = Path(json.loads(proc.stdout.decode())["output"])
    d2 = float(ffprobe_streams(t2).get("format", {}).get("duration", 0) or 0)
    c.check("the same clip scratches the same way (seeded)",
            abs(d1 - d2) < 0.05, f"{d1} vs {d2}")

    proc = run([EFFEX, "timescratch", clip, "--set", "curve=swing",
                "--set", "cuts=light", "--json"], env_extra=env)
    swing = Path(json.loads(proc.stdout.decode())["output"])
    d_swing = float(ffprobe_streams(swing).get("format", {}).get("duration",
                                                                0) or 0)
    c.check("swing scratches differently than sine",
            abs(d_swing - d1) > 0.05, f"{d_swing} vs {d1}")

    proc = run([EFFEX, "timescratch", clip_noaudio, "--json"], env_extra=env)
    c.check("timescratch on a silent clip skips the audio chain",
            proc.returncode == 0 and
            audio_codec(json.loads(proc.stdout.decode())["output"]) is None)

    # ---- new kinds + refined options ----------------------------------
    proc = run([EFFEX, "spin", clip, "--set", "period=1.5", "--dry-run"],
               env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("spin takes a fractional period", proc.returncode == 0 and
            "2*PI*t/1.5" in cmdline, cmdline[:200])

    proc = run([EFFEX, "spin", clip, "--set", "period=0.25"], env_extra=env)
    c.check("spin period range is enforced (floats)", proc.returncode == 2
            and b"outside 0.5-30" in proc.stderr)

    proc = run([EFFEX, "spin", clip, "--set", "period=abc"], env_extra=env)
    c.check("spin rejects non numeric periods", proc.returncode == 2)

    proc = run([EFFEX, "deepfry", clip, "--set", "strength=55%",
                "--dry-run"], env_extra=env)
    c.check("deepfry accepts a percent with the sign",
            proc.returncode == 0
            and "saturation=3.00" in proc.stdout.decode())

    proc = run([EFFEX, "deepfry", clip, "--set", "strength=5%"], env_extra=env)
    c.check("percent range is enforced", proc.returncode == 2 and
            b"outside 10-100%" in proc.stderr)

    proc = run([EFFEX, "deepfry", clip, "--set", "strength=abc"], env_extra=env)
    c.check("deepfry rejects non numeric strength", proc.returncode == 2 and
            b"expected a percent" in proc.stderr)

    proc = run([EFFEX, "rgbsplit", clip, "--set", "channels=b",
                "--dry-run"], env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("multi selection drops the unselected plane",
            "chromashift=cbh=12" in cmdline and "crh" not in cmdline,
            cmdline[:200])

    proc = run([EFFEX, "rgbsplit", clip, "--set", "channels=x"], env_extra=env)
    c.check("multi rejects unknown values", proc.returncode == 2 and
            b"not one of r, b" in proc.stderr)

    proc = run([EFFEX, "stretch", clip, "--set", "flip=hv", "--dry-run"],
               env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("stretch flip adds both mirrors", "hflip" in cmdline and
            "vflip" in cmdline, cmdline[:200])

    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=48",
                "--set", "edges=soft", "--crf", "20", "--preset",
                "ultrafast", "--json"], env_extra=env)
    soft = Path(json.loads(proc.stdout.decode())["output"])
    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=48",
                "--set", "edges=crisp", "--crf", "20", "--preset",
                "ultrafast", "--json"], env_extra=env)
    crisp = Path(json.loads(proc.stdout.decode())["output"])
    ratio = soft.stat().st_size / max(1, crisp.stat().st_size)
    c.check("soft edges really keep more detail than crisp ones",
            ratio > 1.15, f"soft/crisp = {ratio:.2f}")

    proc = run([EFFEX, "stutter", clip, "--set", "mode=hold", "--json"],
               env_extra=env)
    hold = Path(json.loads(proc.stdout.decode())["output"])
    rate = {s2["codec_type"]: s2 for s2 in
            ffprobe_streams(hold).get("streams", [])}
    c.check("hold mode fills the gaps back to the source rate",
            rate.get("video", {}).get("r_frame_rate") == "30/1",
            str(rate.get("video", {}).get("r_frame_rate")))
    c.check("hold mode keeps the audio", audio_codec(hold) == "aac")

    proc = run([EFFEX, "deepfry", clip, "--set", "strength=100",
                "--set", "brightness=25", "--set", "crisp=0", "--dry-run"],
               env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("deepfry caps the eq saturation at its hard limit",
            "saturation=3.00" in cmdline, cmdline[:200])
    c.check("deepfry brightness and crisp land in the chain",
            "brightness=0.25" in cmdline and "unsharp=5:5:0.00" in cmdline)

    proc = run([EFFEX, "deepfry", clip, "--set", "strength=100", "--json"],
               env_extra=env)
    c.check("deepfry at full strength still renders",
            proc.returncode == 0, proc.stderr.decode()[:160])

    proc = run([EFFEX, "wobble", clip, "--set", "waveform=triangle",
                "--dry-run"], env_extra=env)
    c.check("wobble triangle wave lands in the chain",
            "asin(sin" in proc.stdout.decode())

    proc = run([EFFEX, "spin", clip, "--set", "direction=counterclockwise",
                "--dry-run"], env_extra=env)
    c.check("spin direction flips the sign", "-2*PI" in proc.stdout.decode())

    proc = run([EFFEX, "posterize", clip, "--set", "grain=on", "--dry-run"],
               env_extra=env)
    c.check("posterize grain adds noise",
            "noise=alls" in proc.stdout.decode())
    proc = run([EFFEX, "posterize", clip, "--dry-run"], env_extra=env)
    c.check("posterize default stays clean",
            "noise=alls" not in proc.stdout.decode())

    proc = run([EFFEX, "datamosh", clip, "--set", "grit=12", "--json"],
               env_extra=env)
    c.check("datamosh grit renders", proc.returncode == 0,
            proc.stderr.decode()[:160])

    proc = run([EFFEX, "--list"], env_extra=env)
    text = proc.stdout.decode()
    c.check("--list shows percent defaults", "40%" in text)
    c.check("--list shows multi defaults", "r, b" in text)

    # ---- batch 2026-09-24: zoom, vhs, trails, slowmo, crt ------------
    dry_runs2 = {
        "zoom": ("zoompan=z='min(zoom+0.0015,1.35)'", []),
        "vhs": ("chromashift=cbh=4:crh=-4,eq=saturation=0.65:contrast=1.1,"
                "noise=alls=14:allf=t", ["sin(2*PI*t/2)"]),
        "trails": ("tmix=frames=10:weights='10 9 8 7 6 5 4 3 2 1'", []),
        "slowmo": ("setpts=2*PTS", ["atempo=0.5"]),
        "crt": ("drawgrid=w=iw:h=4:t=2:c=black@0.6,noise=alls=6:allf=t", []),
    }
    for effect, (main, extras) in dry_runs2.items():
        proc = run([EFFEX, effect, clip, "--dry-run"], env_extra=env)
        cmdline = proc.stdout.decode()
        c.check(f"{effect} --dry-run prints its real chain",
                proc.returncode == 0 and main in cmdline, cmdline[:200])
        for extra in extras:
            c.check(f"{effect} chain carries {extra}", extra in cmdline,
                    cmdline[:200])

    proc = run([EFFEX, "slowmo", clip, "--set", "factor=4", "--dry-run"],
               env_extra=env)
    c.check("slowmo quadruple chains two atempos",
            "atempo=2,atempo=2" in proc.stdout.decode())

    proc = run([EFFEX, "vhs", clip, "--set", "bleed=0", "--set", "noise=0",
                "--set", "tracking=0", "--set", "fade=off", "--dry-run"],
               env_extra=env)
    c.check("a fully stripped vhs falls back to a pass-through",
            "null" in proc.stdout.decode())

    proc = run([EFFEX, "zoom", clip, "--json"], env_extra=env)
    zoomed = Path(json.loads(proc.stdout.decode())["output"])
    c.check("zoom renders", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("zoom keeps the frame size", video_dims(zoomed) == (640, 360))

    proc = run([EFFEX, "vhs", clip, "--json"], env_extra=env)
    vhs_out = Path(json.loads(proc.stdout.decode())["output"])
    c.check("vhs renders", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("vhs keeps the frame size", video_dims(vhs_out) == (640, 360))

    proc = run([EFFEX, "vhs", clip, "--set", "noise=50", "--set", "bleed=0",
                "--set", "tracking=0", "--set", "fade=off", "--crf", "23",
                "--preset", "ultrafast", "--json"], env_extra=env)
    noisy = Path(json.loads(proc.stdout.decode())["output"])
    proc = run([EFFEX, "vhs", clip, "--set", "noise=0", "--set", "bleed=0",
                "--set", "tracking=0", "--set", "fade=off", "--crf", "23",
                "--preset", "ultrafast", "--json"], env_extra=env)
    clean = Path(json.loads(proc.stdout.decode())["output"])
    nratio = noisy.stat().st_size / max(1, clean.stat().st_size)
    c.check("tape grain really costs bits", nratio > 1.5,
            f"noisy/clean = {nratio:.2f}")

    proc = run([EFFEX, "trails", clip, "--set", "trail=16", "--crf", "20",
                "--preset", "ultrafast", "--json"], env_extra=env)
    t16 = Path(json.loads(proc.stdout.decode())["output"])
    proc = run([EFFEX, "trails", clip, "--set", "trail=2", "--crf", "20",
                "--preset", "ultrafast", "--json"], env_extra=env)
    t2 = Path(json.loads(proc.stdout.decode())["output"])
    ratio = t16.stat().st_size / max(1, t2.stat().st_size)
    c.check("a longer trail really smears more detail away", ratio < 0.75,
            f"trail16/trail2 = {ratio:.2f}")

    proc = run([EFFEX, "slowmo", clip, "--json"], env_extra=env)
    slow = Path(json.loads(proc.stdout.decode())["output"])
    d_slow = float(ffprobe_streams(slow).get("format", {}).get("duration",
                                                               0) or 0)
    c.check("half speed doubles the duration", 5.5 <= d_slow <= 6.5,
            f"duration {d_slow}")
    c.check("half speed keeps the audio", audio_codec(slow) == "aac")

    proc = run([EFFEX, "slowmo", clip, "--set", "factor=2", "--json"],
               env_extra=env)
    fast = Path(json.loads(proc.stdout.decode())["output"])
    d_fast = float(ffprobe_streams(fast).get("format", {}).get("duration",
                                                               0) or 0)
    c.check("double speed halves the duration", 1.2 <= d_fast <= 1.8,
            f"duration {d_fast}")

    proc = run([EFFEX, "slowmo", clip_noaudio, "--json"], env_extra=env)
    c.check("slowmo on a silent clip skips the audio chain",
            proc.returncode == 0 and
            audio_codec(json.loads(proc.stdout.decode())["output"]) is None)

    proc = run([EFFEX, "crt", clip, "--set", "roll=15", "--set", "curve=on",
                "--json"], env_extra=env)
    crt_out = Path(json.loads(proc.stdout.decode())["output"])
    c.check("crt renders", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("crt keeps the frame size", video_dims(crt_out) == (640, 360))
    proc = run([EFFEX, "crt", clip, "--set", "roll=15",
                "--set", "curve=on", "--dry-run"], env_extra=env)
    c.check("crt curve and roll land in the chain",
            "lenscorrection" in proc.stdout.decode() and
            "sin(2*PI*t/1.2)" in proc.stdout.decode())

    # ---- chaining (E3.3) ----------------------------------------------
    proc = run([EFFEX, "stretch+deepfry", clip, "--dry-run"], env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("chain runs both effects in one command",
            proc.returncode == 0 and "setdar=1080/1920,eq=" in cmdline,
            cmdline[:200])
    c.check("chain uses the first effect's rotation fix",
            "rotate=0" in cmdline)

    proc = run([EFFEX, "stretch+pixelize", clip, "--dry-run"], env_extra=env)
    c.check("a later effect works on the frame the earlier one produced",
            "setdar=1080/1920,scale=w=1080:h=1920,scale=w=iw/24"
            in proc.stdout.decode(), proc.stdout.decode()[:240])

    proc = run([EFFEX, "stretch+deepfry", clip, "--size", "720x1280",
                "--dry-run"], env_extra=env)
    c.check("--size lands on the first effect with a shape option",
            "scale=w=720:h=1280" in proc.stdout.decode())

    proc = run([EFFEX, "stretch+deepfry", clip, "--set", "strength=60",
                "--dry-run"], env_extra=env)
    bare = proc.stdout.decode()
    proc = run([EFFEX, "stretch+deepfry", clip, "--set",
                "deepfry.strength=60", "--dry-run"], env_extra=env)
    qualified = proc.stdout.decode()
    c.check("a bare key reaches the one effect that has it",
            proc.returncode == 0 and "contrast=1.90" in bare, bare[:200])
    c.check("a qualified key reads the same",
            "contrast=1.90" in qualified and
            bare.split("-vf ")[-1] == qualified.split("-vf ")[-1])

    proc = run([EFFEX, "zoom+blockswap", clip, "--set", "speed=fast",
                "--dry-run"], env_extra=env)
    c.check("a shared key must be qualified", proc.returncode == 2)
    c.check("the ambiguity names both qualified forms",
            b"zoom.speed=..." in proc.stderr and
            b"blockswap.speed=..." in proc.stderr, proc.stderr.decode()[:200])

    proc = run([EFFEX, "datamosh+deepfry", clip, "--dry-run"], env_extra=env)
    c.check("a multi-pass effect is refused in a chain",
            proc.returncode == 2 and b"multi-pass" in proc.stderr,
            proc.stderr.decode()[:200])

    proc = run([EFFEX, "stretch+stretch", clip, "--dry-run"], env_extra=env)
    c.check("the same effect twice is refused",
            proc.returncode == 2 and b"twice" in proc.stderr)

    proc = run([EFFEX, "strecth", clip, "--dry-run"], env_extra=env)
    c.check("a typo in the effect name is offered a fix",
            proc.returncode == 2 and b"did you mean stretch?" in proc.stderr,
            proc.stderr.decode()[:200])

    proc = run([EFFEX, "stretch+deepfry", clip, "--crf", "23", "--preset",
                "ultrafast", "--json"], env_extra=env)
    data = json.loads(proc.stdout.decode())
    chained = Path(data["output"])
    c.check("a chain render reports both effects",
            data.get("chain") == ["stretch", "deepfry"] and
            data["effect"] == "stretch+deepfry", str(data.get("chain")))
    c.check("chained options are keyed per effect",
            data["options"]["deepfry"]["strength"] == 40)
    c.check("the chain names the output after both effects",
            chained.name.endswith("_STRETCHED+DEEPFRIED.mp4"), chained.name)
    c.check("a chained render reaches ffmpeg at the first effect's size",
            video_dims(chained) == (1080, 1920), str(video_dims(chained)))
    c.check("a chain keeps the audio", audio_codec(chained) == "aac")

    proc = run([EFFEX, "stretch+slowmo", clip, "--crf", "23", "--preset",
                "ultrafast", "--json"], env_extra=env)
    both = Path(json.loads(proc.stdout.decode())["output"])
    d_both = float(ffprobe_streams(both).get("format", {})
                   .get("duration", 0) or 0)
    c.check("a chain merges the audio chains too (slowmo inside a chain)",
            5.5 <= d_both <= 6.5 and audio_codec(both) == "aac",
            f"duration {d_both}")

    proc = run([EFFEX, "stretch+deepfry+rgbsplit", clip, "--crf", "23",
                "--preset", "ultrafast", "--json"], env_extra=env)
    triple = json.loads(proc.stdout.decode())
    c.check("three effects chain in the order written",
            triple.get("chain") == ["stretch", "deepfry", "rgbsplit"] and
            Path(triple["output"]).name.endswith(
                "_STRETCHED+DEEPFRIED+RGBSPLIT.mp4"),
            Path(triple["output"]).name)

    proc = run([EFFEX, "stretch", clip, "--crf", "23", "--preset",
                "ultrafast", "--json"], env_extra=env)
    solo = json.loads(proc.stdout.decode())
    c.check("a single effect still reports a flat option dict",
            "resample" in solo["options"] and "chain" not in solo)

    # ---- option validation --------------------------------------------
    proc = run([EFFEX, "stretch", clip, "--set", "nope=1"], env_extra=env)
    c.check("unknown option is rejected", proc.returncode == 2)
    c.check("unknown option error names the real ones",
            b"target, resample, rotation" in proc.stderr,
            proc.stderr.decode()[:200])

    proc = run([EFFEX, "stretch", clip, "--set", "target=bogus"],
               env_extra=env)
    c.check("bad choice value is rejected", proc.returncode == 2)

    proc = run([EFFEX, "stretch", clip, "--set", "target=99999x1"],
               env_extra=env)
    c.check("absurd size is rejected", proc.returncode == 2)

    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=1"], env_extra=env)
    c.check("out of range block size is rejected", proc.returncode == 2)
    c.check("range error names the limits",
            b"outside 2-128" in proc.stderr, proc.stderr.decode()[:160])

    proc = run([EFFEX, "pixelize", clip, "--set", "chunk=abc"], env_extra=env)
    c.check("non numeric block size is rejected", proc.returncode == 2)

    proc = run([EFFEX, "pixelize", clip, "--set", "nope=1"], env_extra=env)
    c.check("unknown pixelize option names the real ones",
            b"chunk, edges, shape" in proc.stderr, proc.stderr.decode()[:200])

    proc = run([EFFEX, "stretch", clip, "--set", "rotation=off", "--dry-run"],
               env_extra=env)
    cmdline = proc.stdout.decode()
    c.check("--dry-run prints the ffmpeg command",
            "scale=w=1080:h=1920" in cmdline, cmdline[:200])
    c.check("--dry-run keeps rotation metadata when asked",
            "rotate=0" not in cmdline)

    proc = run([EFFEX, "stretch", clip, "--set", "rotation=on", "--dry-run"],
               env_extra=env)
    c.check("rotation fixes are added by default", "rotate=0" in
            proc.stdout.decode())

    proc = run([EFFEX, "stretch", clip, "--dry-run"], env_extra=env)
    before = len(list(out_dir.glob("*.mp4")))
    run([EFFEX, "stretch", clip, "--json"], env_extra=env)
    after = len(list(out_dir.glob("*.mp4")))
    c.check("--dry-run writes nothing", after == before + 1,
            f"{before} -> {after}")

    # ---- failures ------------------------------------------------------
    proc = run([EFFEX, "stretch", in_dir / "nope.mp4"], env_extra=env)
    c.check("missing file exits 1", proc.returncode == 1)

    proc = run([EFFEX, "nosucheffect", clip], env_extra=env)
    c.check("unknown effect exits 2", proc.returncode == 2)
    c.check("unknown effect hints at the real names",
            b"known effects" in proc.stdout + proc.stderr)

    fixed = out_dir / "fixed.mp4"
    proc = run([EFFEX, "stretch", clip, "--output", fixed, "--json"],
               env_extra=env)
    c.check("--output writes the exact path",
            proc.returncode == 0 and fixed.is_file())
    proc = run([EFFEX, "stretch", clip, "--output", fixed], env_extra=env)
    c.check("--output refuses to clobber without --force",
            proc.returncode == 1 and b"already exists" in proc.stderr)
    proc = run([EFFEX, "stretch", clip, "--output", fixed, "--force"],
               env_extra=env)
    c.check("--force clobbers", proc.returncode == 0)

    first = newest_mp4(out_dir)
    run([EFFEX, "stretch", clip, "--json"], env_extra=env)
    second = newest_mp4(out_dir)
    c.check("repeat render never overwrites", first != second,
            f"{first} -> {second}")

    proc = run([EFFEX, "stretch", clip, clip_noaudio, "--json"], env_extra=env)
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    c.check("batch of two renders twice", len(lines) == 2 and
            proc.returncode == 0, f"{len(lines)} json lines")

    # ---- colour discipline --------------------------------------------
    proc = run([EFFEX, "--list"], env_extra={**env, "NO_COLOR": "1"})
    c.check("NO_COLOR strips ansi", b"\033[" not in proc.stdout)
    proc = run([EFFEX, "--list", "--no-color"], env_extra=env)
    c.check("--no-color strips ansi", b"\033[" not in proc.stdout)

    print(f"  renders produced: {len(list(out_dir.glob('*.mp4')))}")
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
