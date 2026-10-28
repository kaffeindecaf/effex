#!/usr/bin/env python3
"""Shared helpers for the effex test suite (stdlib only)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
EFFEX = REPO / "effex.py"


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def tools_ready() -> bool:
    return have("ffmpeg") and have("ffprobe")


class Checker:
    """tiny check reporter: prints PASS/FAIL, exit code from summary()"""

    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = ""):
        if ok:
            self.passed += 1
            print(f"  PASS {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))
        return ok

    def summary(self) -> int:
        total = self.passed + self.failed
        state = "ok" if self.failed == 0 else "FAILED"
        print(f"  {self.title}: {self.passed}/{total} {state}")
        return 0 if self.failed == 0 else 1


def run(args, stdin: bytes = b"", env_extra: Optional[dict] = None,
        timeout: int = 300, cwd=None):
    """run effex (or anything) with a piped, non-tty stdin.

    windows cannot execute a .py file directly (no shebang support), so the
    interpreter is put in front there; on unix the shebang path stays tested
    """
    argv = [str(a) for a in args]
    if os.name == "nt" and argv and argv[0].lower().endswith(".py"):
        argv = [sys.executable] + argv
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(argv, input=stdin, env=env,
                          capture_output=True, timeout=timeout,
                          cwd=str(cwd or REPO))
    return proc


def temp_folders(prefix: str = "effex-test-"):
    """isolated input/ and output/ so tests never touch the real folders"""
    base = Path(tempfile.mkdtemp(prefix=prefix))
    in_dir = base / "in"
    out_dir = base / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    return in_dir, out_dir


def env_for(in_dir, out_dir) -> dict:
    return {"EFFEX_INPUT_DIR": str(in_dir), "EFFEX_OUTPUT_DIR": str(out_dir),
            "EFFEX_LOG_DIR": str(Path(in_dir).parent / "logs"), "NO_COLOR": "1"}


def make_clip(path: Path, seconds: float = 3, size: str = "640x360",
              audio: bool = True) -> bool:
    """lavfi test clip - fast, deterministic, no assets in the repo"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:"
                                f"duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:a", "aac", "-b:a", "96k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path)]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def ffprobe_streams(path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         "-show_format", str(path)], capture_output=True, text=True)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {}


def video_dims(path):
    for stream in ffprobe_streams(path).get("streams", []):
        if stream.get("codec_type") == "video":
            return stream.get("width"), stream.get("height")
    return None, None


def audio_codec(path):
    for stream in ffprobe_streams(path).get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream.get("codec_name")
    return None


def newest_mp4(folder) -> Optional[Path]:
    files = [p for p in Path(folder).glob("*.mp4")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def video_frames(path) -> int:
    result = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=nb_frames", "-of", "csv=p=0",
                  str(path)])
    try:
        return int(result.stdout.decode().strip() or 0)
    except ValueError:
        return 0


def skip(message: str) -> int:
    print(f"  SKIP {message}")
    return 0


def require_tools(checker: Checker) -> bool:
    """returns False (and records a skip) when ffmpeg is not on PATH"""
    if tools_ready():
        return True
    print("  SKIP ffmpeg/ffprobe not on PATH - run ./effex.py --check")
    return False


if __name__ == "__main__":
    sys.exit(0 if tools_ready() else 1)
