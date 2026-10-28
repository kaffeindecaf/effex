#!/usr/bin/env python3
"""Real end-to-end checks for the datamosh composition controls."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (Checker, EFFEX, audio_codec, ffprobe_streams, run,
                     video_dims)  # noqa: E402


def make_color(path: Path, color: str, audio: bool = False) -> bool:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=10:d=1"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
            "yuv420p", str(path)]
    return run(cmd).returncode == 0


def make_motion(path: Path, audio: bool = False) -> bool:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i",
           "testsrc2=size=96x96:rate=10:duration=1,hue=h=120"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=660:duration=1",
                "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt",
            "yuv420p", str(path)]
    return run(cmd).returncode == 0


def keyframe_count(path: Path) -> int:
    proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "frame=key_frame", "-of", "csv=p=0",
                str(path)])
    return proc.stdout.decode().count("1")


def frame_rgb_mean(path: Path, at: str) -> tuple:
    proc = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", at,
                "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"])
    pixels = proc.stdout
    if not pixels:
        return 0.0, 0.0, 0.0
    count = len(pixels) // 3
    return tuple(sum(pixels[i::3]) / count for i in range(3))


def main() -> int:
    c = Checker("datamosh features")
    with tempfile.TemporaryDirectory(prefix="effex-datamosh-") as temp:
        root = Path(temp)
        in_dir, out_dir = root / "in", root / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        first, second = in_dir / "red.mp4", in_dir / "motion.mp4"
        c.check("red source fixture builds", make_color(first, "red", audio=True))
        c.check("moving second source fixture builds",
                make_motion(second, audio=True))
        env = {"EFFEX_INPUT_DIR": str(in_dir),
               "EFFEX_OUTPUT_DIR": str(out_dir), "NO_COLOR": "1"}

        proc = run([EFFEX, "datamosh", first, "--with", str(second),
                    "--json"], env_extra=env)
        c.check("--with joins two clips using the heavy preset by default",
                proc.returncode == 0, proc.stderr.decode()[:300])
        if proc.returncode == 0:
            heavy = json.loads(proc.stdout.decode())
            c.check("JSON reports the actual tuned heavy settings",
                    heavy["options"]["preset"] == "heavy" and
                    heavy["options"]["bloom"] == 12 and
                    heavy["options"]["grit"] == 18)
            output = Path(heavy["output"])
            video = next(s for s in ffprobe_streams(output)["streams"]
                         if s["codec_type"] == "video")
            c.check("heavy two-clip blooming preserves all frame slots",
                    int(video["nb_frames"]) == 20)
            decode = run(["ffmpeg", "-v", "error", "-i", str(output),
                          "-an", "-f", "null", "-"])
            c.check("heavy output decodes without damaged-header errors",
                    decode.returncode == 0 and not decode.stderr,
                    decode.stderr.decode()[:300])
            proc = run([EFFEX, "datamosh", first, "--with", str(second),
                        "--set", "preset=standard", "--json"], env_extra=env)
            c.check("standard preset remains available for the old look",
                    proc.returncode == 0, proc.stderr.decode()[:300])
            if proc.returncode == 0:
                standard = json.loads(proc.stdout.decode())
                c.check("heavy changes decoded pixels, not just metadata",
                        frame_rgb_mean(Path(heavy["output"]), "1.7") !=
                        frame_rgb_mean(Path(standard["output"]), "1.7"))

        proc = run([EFFEX, "pixelize", first, "--with", str(second),
                    "--dry-run"], env_extra=env)
        c.check("--with is explicitly datamosh-only", proc.returncode == 2
                and b"datamosh" in proc.stderr)
        proc = run([EFFEX, "datamosh", first, "--with", str(second),
                    "--set", "second=other.mp4", "--dry-run"], env_extra=env)
        c.check("conflicting secondary inputs are not silently ignored",
                proc.returncode == 2 and b"conflict" in proc.stderr)
        for setting in ("bloom=0", "bloom=61", "bloom_window=0",
                        "bloom_window=13", "preset=unknown"):
            proc = run([EFFEX, "datamosh", first, "--set", setting,
                        "--dry-run"], env_extra=env)
            c.check(f"invalid {setting} is rejected", proc.returncode == 2)

        proc = run([EFFEX, "datamosh", first, "--set", "second=motion.mp4",
                    "--set", "hold=4", "--json"], env_extra=env)
        c.check("a second video joins the datamosh stream",
                proc.returncode == 0,
                proc.stderr.decode()[:300])
        if proc.returncode == 0:
            data = json.loads(proc.stdout.decode())
            output = Path(data["output"])
            duration = float(ffprobe_streams(output).get("format", {})
                             .get("duration", 0) or 0)
            c.check("the combined render includes both full clips",
                    1.8 <= duration <= 2.2, f"duration={duration}")
            c.check("the combined render keeps the first clip's frame size",
                    video_dims(output) == (64, 64), str(video_dims(output)))
            c.check("automatic audio follows both clips",
                    audio_codec(output) == "aac")
            audio_streams = [stream for stream in
                             ffprobe_streams(output).get("streams", [])
                             if stream.get("codec_type") == "audio"]
            audio_duration = float(audio_streams[0].get("duration", 0) or 0) \
                if audio_streams else 0.0
            c.check("automatic audio is as long as the joined video",
                    1.8 <= audio_duration <= 2.2,
                    f"audio duration={audio_duration}")
            red_mean = frame_rgb_mean(output, "0.5")
            blue_mean = frame_rgb_mean(output, "1.8")
            c.check("motion vectors carry the second clip into the first",
                    blue_mean[2] > 10,
                    f"red={red_mean}, second={blue_mean}")

        for mode in ("first", "second", "mix"):
            proc = run([EFFEX, "datamosh", first, "--set", "second=motion.mp4",
                        "--set", f"audio={mode}", "--json"], env_extra=env)
            selected = json.loads(proc.stdout.decode()) \
                if proc.returncode == 0 else {}
            selected_streams = ffprobe_streams(
                selected.get("output", "")).get("streams", [])
            selected_audio = next((stream for stream in selected_streams
                                   if stream.get("codec_type") == "audio"), {})
            selected_duration = float(selected_audio.get("duration", 0) or 0)
            c.check(f"audio mode {mode} renders a joined-length AAC stream",
                    proc.returncode == 0 and
                    audio_codec(selected.get("output", "")) == "aac" and
                    1.8 <= selected_duration <= 2.2,
                    f"duration={selected_duration}, "
                    f"error={proc.stderr.decode()[:160]}")

        proc = run([EFFEX, "datamosh", first, "--set", "second=missing.mp4",
                    "--dry-run"], env_extra=env)
        c.check("a missing secondary source is rejected clearly",
                proc.returncode == 2 and b"no such second video" in proc.stderr.lower(),
                proc.stderr.decode()[:240])

        proc = run([EFFEX, "datamosh", first, "--set", "second=red.mp4",
                    "--dry-run"], env_extra=env)
        c.check("the primary clip cannot be selected as the second input",
                proc.returncode == 2 and b"different from the primary" in proc.stderr,
                proc.stderr.decode()[:240])

        proc = run([EFFEX, "datamosh", first, "--set", "second=motion.mp4",
                    "--set", "second_start=0.5", "--set", "second_length=0.25",
                    "--json"], env_extra=env)
        trimmed = json.loads(proc.stdout.decode()) if proc.returncode == 0 else {}
        trimmed_duration = float(ffprobe_streams(trimmed.get("output", ""))
                                 .get("format", {}).get("duration", 0) or 0) \
            if trimmed else 0.0
        c.check("second source can be trimmed before the melt",
                proc.returncode == 0 and 1.15 <= trimmed_duration <= 1.4,
                f"rc={proc.returncode}, duration={trimmed_duration}, "
                f"error={proc.stderr.decode()[:180]}")

        proc = run([EFFEX, "datamosh", first, "--set", "second=motion.mp4",
                    "--set", "audio=none", "--json"], env_extra=env)
        silent = json.loads(proc.stdout.decode()) if proc.returncode == 0 else {}
        c.check("audio can be explicitly removed from a two-video mosh",
                proc.returncode == 0 and
                audio_codec(silent.get("output", "")) is None,
                proc.stderr.decode()[:200])

        proc = run([EFFEX, "datamosh", first, "--set", "second=motion.mp4",
                    "--set", "second_start=1.5", "--dry-run"], env_extra=env)
        c.check("a second start past EOF is rejected before encoding",
                proc.returncode == 2 and b"past the duration" in proc.stderr,
                proc.stderr.decode()[:240])

        proc = run([EFFEX, "datamosh", first, "--set", "cut=dice",
                    "--set", "survival=80", "--set", "seed=1234",
                    "--dry-run"], env_extra=env)
        c.check("dice survival and seed are configurable",
                proc.returncode == 0 and b"survival" in proc.stdout.lower()
                and b"seed" in proc.stdout.lower(),
                proc.stdout.decode()[:300])

        proc = run([EFFEX, "datamosh", first, "--set", "survival=101"],
                   env_extra=env)
        c.check("survival range is enforced", proc.returncode == 2
                and b"outside 0-100%" in proc.stderr,
                proc.stderr.decode()[:200])

        kept = {}
        for chance in (0, 100):
            proc = run([EFFEX, "datamosh", first, "--set", "cut=dice",
                        "--set", f"survival={chance}", "--set", "hold=4",
                        "--set", "seed=1234", "--json"], env_extra=env)
            if proc.returncode == 0:
                kept[chance] = keyframe_count(
                    Path(json.loads(proc.stdout.decode())["output"]))
        c.check("survival percentage changes the number of retained I-frames",
                kept.get(100, 0) > kept.get(0, 0), str(kept))

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
