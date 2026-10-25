#!/usr/bin/env python3
# effex - terminal menu for weird video effects
# v0.11.0 - Made by kaffeindecaf
#
#   interactive:  ./effex.py
#   scripted:     ./effex.py stretch input/clip.mp4
#                 ./effex.py stretch input/clip.mp4 --set target=16:9
#                 ./effex.py pixelize input/clip.mp4 --set chunk=16
#   chained:      ./effex.py stretch+deepfry clip.mp4 --set deepfry.strength=60
#   inspect:      ./effex.py --list   |   ./effex.py --check
#
#   drop videos in input/, results land in output/.
#   zero deps, stdlib only.
#
# every screen is drawn as one full frame (home the cursor, write the frame,
# clear whatever is left under it), so nothing from an earlier screen can
# survive as an artifact.

import argparse
import difflib
import json
import math
import random
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, cast

from stateful_effects import CausalityInversion, ElasticCollapse, TemporalSediment

VERSION = "0.12.0"

ROOT = Path(os.path.dirname(os.path.realpath(__file__)))
# the folders can be pointed elsewhere (env or --input-dir/--output-dir);
# the test suite uses that to run against temp folders
INPUT_DIR = Path(os.environ.get("EFFEX_INPUT_DIR") or (ROOT / "input"))
OUTPUT_DIR = Path(os.environ.get("EFFEX_OUTPUT_DIR") or (ROOT / "output"))

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ts", ".3gp", ".mts", ".m2ts", ".ogv",
}

DEFAULT_W, DEFAULT_H = 1080, 1920
DEFAULT_CRF = 18
DEFAULT_PRESET = "medium"

# ---------------------------------------------------------------------------
# palette - one accent, one muted tone, three semantic colors, bold, reverse
# ---------------------------------------------------------------------------

_C_FROST = "\033[38;5;117m"
_C_DIM = "\033[38;5;240m"
_C_GRN = "\033[0;32m"
_C_AMB = "\033[1;33m"
_C_RED = "\033[0;31m"
_C_B = "\033[1m"
_C_NC = "\033[0m"

C_FROST, C_DIM, C_GRN, C_AMB, C_RED, B, NC = (
    _C_FROST, _C_DIM, _C_GRN, _C_AMB, _C_RED, _C_B, _C_NC,
)
REV = "\033[7m"
REV_OFF = "\033[27m"

_COLOR = True
_QUIET = False

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_CLEAR_EOL = "\033[K"


def apply_palette(enabled: bool) -> None:
    global C_FROST, C_DIM, C_GRN, C_AMB, C_RED, B, NC
    global REV, REV_OFF, _COLOR
    _COLOR = enabled
    C_FROST = _C_FROST if enabled else ""
    C_DIM = _C_DIM if enabled else ""
    C_GRN = _C_GRN if enabled else ""
    C_AMB = _C_AMB if enabled else ""
    C_RED = _C_RED if enabled else ""
    B = _C_B if enabled else ""
    NC = _C_NC if enabled else ""
    REV = "\033[7m" if enabled else ""
    REV_OFF = "\033[27m" if enabled else ""


def vlen(s: str) -> int:
    """visible width of a string, ansi escapes not counted"""
    return len(_ANSI_RE.sub("", s))


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - vlen(s))


def trunc(s: str, width: int) -> str:
    """cut to a visible width; colours are lost, add them back afterwards"""
    plain = _ANSI_RE.sub("", s)
    if len(plain) <= width:
        return plain
    return plain[:max(0, width - 1)] + "\u2026"


def fit(s: str, width: int) -> str:
    return pad(trunc(s, width), width)


# ---------------------------------------------------------------------------
# output helpers - err/warn go to stderr so pipes stay clean
# ---------------------------------------------------------------------------

def _say(text: str = "") -> None:
    print(text)


def ok(text: str) -> None:
    if not _QUIET:
        _say("  " + C_GRN + "\u2713" + NC + " " + text)


def err(text: str) -> None:
    sys.stdout.flush()
    print("  " + C_RED + "\u2717" + NC + " " + text, file=sys.stderr)


def warn(text: str) -> None:
    if not _QUIET:
        sys.stdout.flush()
        print("  " + C_AMB + "\u26a0" + NC + " " + text, file=sys.stderr)


def info(text: str) -> None:
    if not _QUIET:
        _say("  " + C_FROST + "\u2139" + NC + " " + text)


def hint(text: str) -> None:
    if not _QUIET:
        _say("  " + C_DIM + "\u2192 " + text + NC)


def kv(key: str, value: str, key_pad: int = 10) -> str:
    return "  " + pad(B + C_FROST + key + NC, key_pad) + value


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# ffmpeg discovery + install prompt
# ---------------------------------------------------------------------------

INSTALLERS = [
    ("apt-get", ["sudo", "apt-get", "install", "-y", "ffmpeg"],
     "debian / ubuntu / parrot / mint"),
    ("dnf", ["sudo", "dnf", "install", "-y", "ffmpeg"], "fedora / rhel"),
    ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"],
     "arch / manjaro"),
    ("zypper", ["sudo", "zypper", "install", "-y", "ffmpeg"], "opensuse"),
    ("apk", ["sudo", "apk", "add", "ffmpeg"], "alpine"),
    ("brew", ["brew", "install", "ffmpeg"], "macos / linuxbrew"),
]


@dataclass
class Tools:
    ffmpeg: Optional[str] = None
    ffprobe: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg)


def find_tools() -> Tools:
    return Tools(shutil.which("ffmpeg"), shutil.which("ffprobe"))


def detect_installer() -> Optional[tuple]:
    for name, cmd, label in INSTALLERS:
        if shutil.which(name):
            return name, cmd, label
    return None


def install_ffmpeg(tools: Tools, interactive: bool) -> Tools:
    """returns freshly detected Tools (unchanged if install skipped/failed)"""
    hit = detect_installer()
    if not hit:
        err("no package manager found - install ffmpeg manually")
        hint("https://ffmpeg.org/download.html")
        return tools

    name, cmd, label = hit
    shown = " ".join(cmd)

    if not interactive:
        err("ffmpeg is required to process video")
        hint(f"install it with:  {shown}   ({label})")
        return tools

    _say()
    warn("ffmpeg was not found on PATH")
    info(f"detected package manager: {B}{name}{NC}  ({label})")
    _say("  " + C_DIM + "will run: " + NC + C_FROST + shown + NC)
    if not ask_yes_no("install ffmpeg now?", default=True):
        warn("skipped install - effex cannot process anything without ffmpeg")
        hint(f"do it later with:  {shown}")
        return tools

    _say()
    try:
        rc = subprocess.run(cmd).returncode
    except (OSError, KeyboardInterrupt) as exc:
        err(f"install failed: {exc}")
        return tools

    if rc != 0:
        err(f"installer exited with code {rc}")
        return tools

    fresh = find_tools()
    if fresh.ok:
        ok("ffmpeg installed")
    else:
        err("ffmpeg still not on PATH - open a new shell and retry")
    return fresh


def check_tools(interactive: bool) -> Tools:
    tools = find_tools()
    if tools.ok:
        return tools
    return install_ffmpeg(tools, interactive)


# ---------------------------------------------------------------------------
# probing + scanning
# ---------------------------------------------------------------------------

@dataclass
class Clip:
    path: Path
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[str] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    size: int = 0

    @property
    def res(self) -> str:
        return f"{self.width}x{self.height}" if self.width else "?"

    @property
    def audio(self) -> bool:
        return bool(self.acodec)


def probe(path: Path, tools: Tools) -> Clip:
    clip = Clip(path=path)
    try:
        clip.size = path.stat().st_size
    except OSError:
        pass
    if not tools.ffprobe:
        return clip
    cmd = [tools.ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(raw.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return clip

    fmt = data.get("format", {})
    try:
        clip.duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        clip.duration = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and clip.vcodec is None:
            clip.vcodec = stream.get("codec_name")
            clip.width = stream.get("width")
            clip.height = stream.get("height")
            clip.fps = stream.get("r_frame_rate")
        elif stream.get("codec_type") == "audio" and clip.acodec is None:
            clip.acodec = stream.get("codec_name")
    return clip


def scan_videos(directory: Path = INPUT_DIR) -> list:
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir()
             if p.is_file() and p.suffix.lower() in VIDEO_EXTS
             and not p.name.startswith(".")]
    return sorted(files, key=lambda p: p.name.lower())


def count_renders(directory: Path = OUTPUT_DIR) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.iterdir()
               if p.is_file() and not p.name.startswith("."))


def unique_output(path: Path, force: bool) -> Path:
    if force or not path.exists():
        return path
    stem, suffix, n = path.stem, path.suffix, 2
    while True:
        cand = path.with_name(f"{stem}_{n}{suffix}")
        if not cand.exists():
            return cand
        n += 1


def parse_wxh(text: str) -> Optional[tuple]:
    for sep in ("x", "X", ":", "*"):
        if sep in text:
            left, _, right = text.partition(sep)
            try:
                w, h = int(left), int(right)
            except ValueError:
                return None
            return (w, h) if 16 <= w <= 16384 and 16 <= h <= 16384 else None
    return None


# ---------------------------------------------------------------------------
# effects + their options
# ---------------------------------------------------------------------------

TARGETS = {
    "9:16": (1080, 1920, "portrait, phone / reels / tiktok"),
    "16:9": (1920, 1080, "landscape, youtube / desktop"),
    "4:5": (1080, 1350, "instagram feed, tall square"),
    "1:1": (1080, 1080, "square"),
    "3:4": (1080, 1440, "portrait 4:3"),
}

RESAMPLERS = (
    ("bicubic", "bicubic", "smooth, ffmpeg's default"),
    ("bilinear", "bilinear", "softer, slightly blurrier"),
    ("neighbor", "neighbor", "blocky, keeps hard pixel edges"),
    ("lanczos", "lanczos", "sharpest, slowest"),
    ("spline", "spline", "sharp, good for shrinking"),
)


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    help: str
    kind: str            # choice | bool | int | float | percent | multi | size | video
    default: object
    choices: tuple = ()
    minimum: float = 0.0
    maximum: float = 0.0
    step: float = 1.0

    def display(self, value) -> str:
        if self.kind == "bool":
            return "on" if value else "off"
        if self.kind == "percent":
            return f"{value}%"
        if self.kind == "multi":
            return ", ".join(str(v) for v in value) if value else "none"
        if self.kind == "video":
            text = str(value or "none")
            return "none" if text.lower() in ("", "none", "off") else Path(text).name
        if self.kind == "float":
            try:
                return f"{float(value):g}"
            except (TypeError, ValueError):
                return str(value)
        if self.kind == "choice":
            for val, label, _hint in self.choices:
                if val == value:
                    return label
            return str(value)
        return str(value)

    def step_value(self, current, delta: int):
        if self.kind == "bool":
            return not current
        if self.kind == "choice":
            values = [v for v, _l, _h in self.choices]
            if current not in values:
                return values[0]
            return values[(values.index(current) + delta) % len(values)]
        if self.kind in ("int", "percent"):
            try:
                value = int(str(current).rstrip("%"))
            except (TypeError, ValueError):
                value = int(str(self.default))
            step = int(self.step) * (1 if delta > 0 else -1)
            return max(int(self.minimum), min(int(self.maximum), value + step))
        if self.kind == "float":
            try:
                value = float(str(current))
            except (TypeError, ValueError):
                value = float(str(self.default))
            step = self.step * (1 if delta > 0 else -1)
            return max(self.minimum, min(self.maximum,
                                         round(value + step, 2)))
        return current

    def range_text(self) -> str:
        if self.kind == "int":
            return f"{int(self.minimum)}-{int(self.maximum)}"
        if self.kind == "float":
            return f"{self.minimum:g}-{self.maximum:g}"
        if self.kind == "percent":
            return f"{int(self.minimum)}-{int(self.maximum)}%"
        if self.kind == "multi":
            return "pick several"
        return ""


@dataclass
class EffectContext:
    clip: Clip
    width: int
    height: int
    suffix: str
    crf: int
    preset: str
    opts: dict = field(default_factory=dict)
    # the frame the chain is currently working on - a second effect sees what
    # the first one produced instead of the source size
    frame: tuple = (0, 0)


@dataclass
class Plan:
    vf: str
    out_suffix: str
    width: int = 0
    height: int = 0
    notes: list = field(default_factory=list)
    in_extra: list = field(default_factory=list)
    out_extra: list = field(default_factory=list)
    af: str = ""
    abr: bool = False


@dataclass(frozen=True)
class Effect:
    key: str
    name: str
    blurb: str
    tag: str
    build: Optional[Callable[[EffectContext], Plan]] = None
    params: tuple = ()
    run: Optional[Callable] = None
    stages: tuple = ()
    frame_processor: Optional[Callable] = None


@dataclass(frozen=True)
class Chain:
    """one or more effects, applied in order (E3.3).

    the chain is the unit the pipeline works on: a chain of one is exactly the
    old single-effect behaviour, a chain of two or more joins the builders'
    filter chains with commas.
    """
    effects: tuple = ()

    @property
    def single(self) -> bool:
        return len(self.effects) == 1

    @property
    def key(self) -> str:
        return "+".join(e.key for e in self.effects)

    @property
    def name(self) -> str:
        return " + ".join(e.name for e in self.effects)

    @property
    def multi_pass(self) -> Optional["Effect"]:
        return next((e for e in self.effects if e.run is not None), None)

    @property
    def stateful_effects(self) -> list:
        return [e for e in self.effects if e.frame_processor is not None]

    @property
    def rows(self) -> list:
        """every (effect, param) pair in chain order - what the options
        screen walks"""
        return [(e, p) for e in self.effects for p in e.params]

    @property
    def stage_lines(self) -> list:
        lines = []
        for effect in self.effects:
            for line in effect.stages or ():
                lines.append(f"{effect.name}: {line}")
        return lines


def one(effect: Effect) -> Chain:
    return Chain((effect,))


def target_param(key: str = "target", label: str = "shape",
                 default: str = "9:16", keep: bool = False) -> Param:
    choices = []
    if keep:
        choices.append(("keep", "keep source", "leave the frame shape alone"))
    choices += [(k, f"{k}  {w}x{h}", desc)
                for k, (w, h, desc) in TARGETS.items()]
    choices.append(("custom", "custom WxH", "type any size, eg 1920x1080"))
    return Param(key, label, "shape of the frame", "choice", default,
                 tuple(choices))


STRETCH_PARAMS = (
    target_param(default="9:16"),
    Param("resample", "resample", "how pixels blend when scaling",
          "choice", "bicubic", choices=RESAMPLERS),
    Param("rotation", "fix rotation", "strip phone rotation from the file",
          "bool", True),
    Param("flip", "flip", "mirror the frame after the squash", "choice",
          "none", choices=(
              ("none", "none", "leave it"),
              ("h", "horizontal", "mirror left-right"),
              ("v", "vertical", "mirror top-bottom"),
              ("hv", "both", "turn it upside down"),
          )),
)

PIXELIZE_PARAMS = (
    Param("chunk", "block size", "bigger = chunkier, in pixels per block",
          "int", 24, minimum=2, maximum=128, step=2),
    Param("edges", "edges", "block edges: crisp pixels or soft blur",
          "choice", "crisp", choices=(
              ("crisp", "crisp", "hard pixel edges"),
              ("soft", "soft", "bilinear, softer mosaic"),
          )),
    target_param(key="shape", default="keep", keep=True),
)


def resolve_target(value: str, fallback: tuple) -> tuple:
    if value in TARGETS:
        w, h, _desc = TARGETS[value]
        return w, h
    parsed = parse_wxh(str(value))
    return parsed if parsed else fallback


def build_stretch(ctx: EffectContext) -> Plan:
    opts = ctx.opts
    target = str(opts.get("target", "9:16"))
    w, h = resolve_target(target, (ctx.width or DEFAULT_W,
                                   ctx.height or DEFAULT_H))
    resample = str(opts.get("resample", "bicubic"))
    fix_rotation = bool(opts.get("rotation", True))
    flip = str(opts.get("flip", "none"))
    parts = [f"scale=w={w}:h={h}:flags={resample}"
             f":force_original_aspect_ratio=disable", "setsar=1"]
    if "h" in flip:
        parts.append("hflip")
    if "v" in flip:
        parts.append("vflip")
    parts.append(f"setdar={w}/{h}")

    plan = Plan(
        vf=",".join(parts),
        out_suffix=ctx.suffix or "_STRETCHED",
        width=w,
        height=h,
        notes=[
            f"forces {w}x{h} with square pixels",
            "no crop, no letterbox - the frame is squashed to fit",
            f"resample {resample}",
        ],
        in_extra=["-metadata:s:v:0", "rotate=0"] if fix_rotation else [],
    )
    if not fix_rotation:
        plan.notes.append("rotation metadata left alone")
    return plan


def build_pixelize(ctx: EffectContext) -> Plan:
    opts = ctx.opts
    try:
        chunk = int(opts.get("chunk", 24))
    except (TypeError, ValueError):
        chunk = 24
    shape = str(opts.get("shape", "keep"))
    edges = str(opts.get("edges", "crisp"))
    flags = "neighbor" if edges == "crisp" else "bilinear"

    if shape != "keep":
        w, h = resolve_target(shape, (ctx.width or DEFAULT_W,
                                      ctx.height or DEFAULT_H))
        forced = True
    else:
        # keep means "the frame we are handed": in a chain that is the frame
        # the earlier effect produced, not the source size
        w, h = _source_dims(ctx.clip, ctx.frame)
        forced = False

    if w and h:
        even_w, even_h = w - w % 2, h - h % 2
        # a block bigger than a quarter of the short side makes mush
        chunk = max(2, min(chunk, max(2, min(even_w, even_h) // 4)))
        if forced:
            first = (f"scale=w={even_w}:h={even_h}"
                     f":force_original_aspect_ratio=disable,")
        else:
            first = f"scale=w={even_w}:h={even_h},"
        vf = (f"{first}scale=w=iw/{chunk}:h=ih/{chunk}:flags={flags},"
              f"scale=w={even_w}:h={even_h}:flags={flags},setsar=1")
        width, height = even_w, even_h
    else:
        # no ffprobe: shrink by chunk then scale straight back up
        vf = (f"scale=w=iw/{chunk}:h=ih/{chunk}:flags={flags},"
              f"scale=w=iw*{chunk}:h=ih*{chunk}:flags={flags},setsar=1")
        width = height = 0

    return Plan(
        vf=vf,
        out_suffix=ctx.suffix or "_PIXELIZED",
        width=width,
        height=height,
        notes=[
            f"blocks of about {chunk} pixels",
            "hard pixel edges, no blurring" if edges == "crisp"
            else "soft block edges, bilinear blend",
            "frame shape kept" if shape == "keep"
            else f"frame forced to {width}x{height}",
        ],
    )


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(value))))
    except (TypeError, ValueError):
        return default


def _source_dims(clip: Clip, frame: tuple = (0, 0)) -> tuple:
    """even size of the frame a plan works on; (0, 0) when unknown.

    `frame` is the chain's current frame (E3.3): a second effect in a chain
    works on what the first one produced, not on the source.
    """
    if frame and frame[0] and frame[1]:
        w, h = frame
    elif clip.width and clip.height:
        w, h = clip.width, clip.height
    else:
        return 0, 0
    return w - w % 2, h - h % 2


RGBSPLIT_PARAMS = (
    Param("amount", "split", "how far the color planes drift (chroma steps)",
          "int", 12, minimum=2, maximum=80, step=2),
    Param("axis", "axis", "which way the planes drift", "choice",
          "horizontal", choices=(
              ("horizontal", "horizontal", "red and cyan offset sideways"),
              ("vertical", "vertical", "offset up and down"),
          )),
    Param("channels", "channels", "which planes drift (pick several)",
          "multi", ("r", "b"), choices=(
              ("r", "red", "red plane, drifts left"),
              ("b", "blue", "blue plane, drifts right"),
          )),
)


def build_rgbsplit(ctx: EffectContext) -> Plan:
    amount = _clamp_int(ctx.opts.get("amount", 12), 12, 2, 80)
    axis = str(ctx.opts.get("axis", "horizontal"))
    letter = "h" if axis == "horizontal" else "v"
    channels = ctx.opts.get("channels", ("r", "b")) or ("r", "b")
    sign = {"r": -1, "b": 1}
    parts = [f"c{ch}{letter}={amount * sign[ch]}"
             for ch in ("b", "r") if ch in channels]
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf="chromashift=" + ":".join(parts),
        out_suffix=ctx.suffix or "_RGBSPLIT",
        width=w,
        height=h,
        notes=[
            f"color planes drift {amount} chroma steps apart",
            "classic 3d-glasses red/cyan look" if "b" in channels
            and "r" in channels else "selected planes drift",
            "frame size stays as it was",
        ],
    )


STUTTER_PARAMS = (
    Param("fps", "frame rate", "how choppy the motion gets", "choice", "8",
          choices=(("8", "8 fps", "very choppy"),
                   ("12", "12 fps", "stuttery"),
                   ("15", "15 fps", "mild"))),
    Param("mode", "mode", "how the gap is filled", "choice", "drop",
          choices=(
              ("drop", "drop", "frames vanish - classic stutter"),
              ("hold", "hold", "each kept frame is held to fill the gap"),
          )),
)


def _fps_number(clip: Clip) -> float:
    """'30/1' or '30000/1001' -> a usable number; 30 when unknown"""
    if clip.fps and "/" in clip.fps:
        num, _, den = clip.fps.partition("/")
        try:
            value = float(num) / float(den)
            if 1 <= value <= 240:
                return value
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return 30.0


def build_stutter(ctx: EffectContext) -> Plan:
    rate = str(ctx.opts.get("fps", "8"))
    mode = str(ctx.opts.get("mode", "drop"))
    w, h = _source_dims(ctx.clip, ctx.frame)
    if mode == "hold":
        src = _fps_number(ctx.clip)
        vf = f"fps={rate},minterpolate=fps={src:g}:mi_mode=dup"
        notes = [
            f"keeps {rate} frames per second, each held to fill the gap",
            "full frame rate, so motion stutters instead of chopping",
            "audio keeps its timing",
        ]
    else:
        vf = f"fps={rate}"
        notes = [
            f"drops the frame rate to {rate}",
            "audio keeps its timing, only the picture chops",
            "the file gets smaller along the way",
        ]
    return Plan(
        vf=vf,
        out_suffix=ctx.suffix or "_STUTTER",
        width=w,
        height=h,
        notes=notes,
    )


DEEPFRIED_PARAMS = (
    Param("strength", "strength", "how fried (saturation, contrast, crunch)",
          "percent", 40, minimum=10, maximum=100, step=5),
    Param("brightness", "brightness", "lift or crush the exposure",
          "int", 0, minimum=-30, maximum=30, step=5),
    Param("crisp", "crisp", "how crunchy the sharpening gets",
          "int", 20, minimum=0, maximum=20, step=2),
)


def build_deepfry(ctx: EffectContext) -> Plan:
    strength = _clamp_int(ctx.opts.get("strength", 40), 40, 10, 100)
    bright = _clamp_int(ctx.opts.get("brightness", 0), 0, -30, 30)
    crisp = _clamp_int(ctx.opts.get("crisp", 20), 20, 0, 20)
    sat = min(3.0, 1 + strength * 0.05)      # eq caps saturation at 3.0
    con = 1 + strength * 0.015
    sharp = crisp / 5
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=(f"eq=brightness={bright / 100:.2f}:saturation={sat:.2f}"
            f":contrast={con:.2f},unsharp=5:5:{sharp:.2f}"),
        out_suffix=ctx.suffix or "_DEEPFRIED",
        width=w,
        height=h,
        notes=[
            f"saturation {sat:.1f}x, contrast {con:.2f}x, "
            f"brightness {bright:+d}, sharpen {sharp:.0f}",
            "the 'screenshot of a screenshot' meme look",
        ],
    )


WOBBLE_PARAMS = (
    Param("amount", "amount", "how far the frame rocks, in degrees",
          "int", 4, minimum=1, maximum=15, step=1),
    Param("speed", "speed", "how fast it rocks", "choice", "slow",
          choices=(("slow", "slow", "one rock every 2.5s"),
                   ("medium", "medium", "one rock every 1.2s"),
                   ("fast", "fast", "one rock every 0.6s"))),
    Param("waveform", "waveform", "the shape of the rock", "choice", "sine",
          choices=(
              ("sine", "sine", "smooth rock"),
              ("triangle", "triangle", "crisp, mechanical rock"),
          )),
)

WOBBLE_PERIODS = {"slow": 2.5, "medium": 1.2, "fast": 0.6}


def build_wobble(ctx: EffectContext) -> Plan:
    amount = _clamp_int(ctx.opts.get("amount", 4), 4, 1, 15)
    speed = str(ctx.opts.get("speed", "slow"))
    period = WOBBLE_PERIODS.get(speed, 2.5)
    waveform = str(ctx.opts.get("waveform", "sine"))
    if waveform == "triangle":
        expr = f"{amount}*(2/PI)*asin(sin(2*PI*t/{period}))"
    else:
        expr = f"{amount}*(PI/180)*sin(2*PI*t/{period})"
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=(f"rotate=a='{expr}'"
            f":fillcolor=black"),
        out_suffix=ctx.suffix or "_WOBBLE",
        width=w,
        height=h,
        notes=[
            f"rocks {amount} degrees each way, period {period}s",
            "thin black corners show at the edge of each rock",
        ],
    )


SPIN_PARAMS = (
    Param("period", "seconds per turn", "time for one full rotation",
          "float", 6.0, minimum=0.5, maximum=30.0, step=0.5),
    Param("direction", "direction", "which way it turns", "choice",
          "clockwise", choices=(
              ("clockwise", "clockwise", "to the right"),
              ("counterclockwise", "counterclockwise", "to the left"),
          )),
)


def build_spin(ctx: EffectContext) -> Plan:
    try:
        period = float(ctx.opts.get("period", 6.0))
    except (TypeError, ValueError):
        period = 6.0
    period = max(0.5, min(30.0, period))
    direction = str(ctx.opts.get("direction", "clockwise"))
    sign = "-" if direction == "counterclockwise" else ""
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=f"rotate=a='{sign}2*PI*t/{period:g}':fillcolor=black",
        out_suffix=ctx.suffix or "_SPIN",
        width=w,
        height=h,
        notes=[
            f"one full turn every {period:g} seconds, "
            + ("right" if direction == "clockwise" else "left"),
            "the corners black out as the frame turns",
        ],
    )


POSTERIZE_PARAMS = (
    Param("levels", "levels", "fewer = harsher color bands", "choice", "8",
          choices=(("2", "2 levels", "two-tone, harsh"),
                   ("4", "4 levels", "poster print"),
                   ("8", "8 levels", "chunky bands"),
                   ("16", "16 levels", "subtle banding"))),
    Param("grain", "grain", "sprinkle noise to break up the bands",
          "bool", False),
)


def build_posterize(ctx: EffectContext) -> Plan:
    levels = _clamp_int(ctx.opts.get("levels", 8), 8, 2, 16)
    grain = bool(ctx.opts.get("grain", False))
    q = 256 // levels
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=("format=rgb24,"
            + ("noise=alls=2:allf=t," if grain else "")
            + f"lutrgb=r='floor(val/{q})*{q}':"
            f"g='floor(val/{q})*{q}':"
            f"b='floor(val/{q})*{q}'"),
        out_suffix=ctx.suffix or "_POSTERIZED",
        width=w,
        height=h,
        notes=[
            f"colors crushed into {levels} bands",
            "flat, screen-printed look",
        ],
    )


DATAMOSH_PARAMS = (
    Param("second", "second video", "append another video before the codec melt",
          "video", "none"),
    Param("second_start", "second start", "seconds into the second video",
          "float", 0.0, minimum=0.0, maximum=3600.0, step=0.5),
    Param("second_length", "second length", "seconds to use; 0 means to the end",
          "float", 0.0, minimum=0.0, maximum=3600.0, step=0.5),
    Param("cut", "cut depth", "how many keyframes survive the cut",
          "choice", "heavy",
          choices=(
              ("heavy", "heavy", "only the first keyframe - one long melt"),
              ("medium", "medium", "every 2nd keyframe - melt, reset, melt"),
              ("light", "light", "every 3rd keyframe - short smears"),
              ("dice", "dice", "keyframes survive by the chance below"),
              ("shuffle", "shuffle", "one keyframe, then every motion frame "
               "replays in a random order"),
          )),
    Param("survival", "keyframe survival", "chance a keyframe survives in dice mode",
          "percent", 25, minimum=0, maximum=100, step=5),
    Param("fill", "fill", "what fills each cut keyframe", "choice", "next",
          choices=(
              ("next", "next frame", "drift into the next motion frame"),
              ("prev", "previous frame", "freeze on the last clean frame"),
              ("chaos", "chaos", "a random motion frame from anywhere"),
          )),
    Param("hold", "hold", "frames between keyframes - how long a melt runs",
          "int", 150, minimum=2, maximum=600, step=2),
    Param("grit", "grit", "how crunchy the intermediate encode is",
          "int", 2, minimum=1, maximum=31, step=1),
    Param("seed", "random seed", "0 uses a repeatable seed derived from the input",
          "int", 0, minimum=0, maximum=2147483647, step=1),
    Param("audio", "audio", "audio stream to keep when rendering",
          "choice", "auto", choices=(
              ("auto", "follow the video join", "concatenate audio with the video"),
              ("first", "first video", "keep the primary video's audio"),
              ("second", "second video", "keep the appended video's audio"),
              ("mix", "mix both", "mix both audio tracks from the start"),
              ("none", "none", "render without audio"),
          )),
)

def build_datamosh(ctx: EffectContext) -> Plan:
    opts = ctx.opts
    cut = str(opts.get("cut", "heavy"))
    fill = str(opts.get("fill", "next"))
    hold = _clamp_int(ctx.opts.get("hold", 150), 150, 2, 600)
    second = str(opts.get("second", "none"))
    width, height = _source_dims(ctx.clip, ctx.frame)
    suffix = "_DATAMOSHED"
    if second.lower() not in ("", "none", "off"):
        second_stem = Path(second).stem
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", second_stem).strip("._")
        if safe_stem:
            suffix += "_WITH_" + safe_stem
    notes = [
        "real datamosh - i-vops are cut from the mpeg4 bitstream and "
        "the decoder smears each run of motion vectors across the gap",
        f"{cut} cut, {fill} fill, {hold}-frame keyframe interval",
        "removed keyframes are replaced by a fill frame, so the clip "
        "keeps its length and its audio sync",
    ]
    if second.lower() not in ("", "none", "off"):
        second_start = float(opts.get("second_start", 0.0))
        second_length = float(opts.get("second_length", 0.0))
        length_note = f"{second_length:g}s" if second_length else "to the end"
        notes.append(f"append {Path(second).name} from {second_start:g}s "
                     f"({length_note}) before the melt")
        notes.append(f"audio: {opts.get('audio', 'auto')}")
    if cut == "shuffle":
        notes.append("shuffle: the melt feeds on scrambled motion - "
                     "harder than anything with a clean melt")
    elif cut == "dice":
        notes.append("dice: keyframe survival chance is configurable, "
                     "seeded from the input or a chosen seed")
    return Plan(
        vf=(f"pass 1: mpeg4 keyint={hold} i-vop stream, then {cut} i-vop "
            f"strip with {fill} fill, pass 2: straight remux"),
        out_suffix=suffix,
        width=width,
        height=height,
        notes=notes,
    )


def strip_ivops(raw: bytes, cut: str, fill: str, seed: int,
                survival: int = 25) -> bytes:
    """cut i-vops out of an mpeg4 part 2 (m4v) bitstream.

    cut: heavy keeps only the first i-vop, medium every 2nd, light every
    3rd, dice keeps keyframes at the configured survival percentage, with
    deterministic randomness from seed. every removed keyframe is replaced by a fill frame - next (the following p-vop), prev
    (the last clean p-vop) or chaos (a random p-vop, seeded) - so the frame
    count and the audio sync survive the cut. headers (vos/vol/gov) and
    p-vops pass through untouched."""
    sc = b"\x00\x00\x01"
    units = []
    i = 0
    n = len(raw)
    while i < n:
        if raw.startswith(sc, i):
            end = raw.find(sc, i + 3)
            if end == -1:
                units.append(raw[i:])
                break
            units.append(raw[i:end])
            i = end
        else:
            i += 1

    def ctype(unit: bytes) -> int:
        if unit.startswith(b"\x00\x00\x01\xb6") and len(unit) > 4:
            return (unit[4] >> 6) & 0x3      # 0 = i-vop, 1 = p-vop
        return -1

    rng = random.Random(seed)
    survival = max(0, min(100, int(survival)))
    pvops = [idx for idx, unit in enumerate(units) if ctype(unit) == 1]
    drop = set()
    ivops = 0
    for idx, unit in enumerate(units):
        if ctype(unit) != 0:
            continue
        ivops += 1
        if ivops == 1:
            continue
        elif cut == "medium":
            if (ivops - 1) % 2 != 0:
                drop.add(idx)
        elif cut == "light":
            if (ivops - 1) % 3 != 0:
                drop.add(idx)
        elif cut == "dice":
            if rng.random() * 100 >= survival:
                drop.add(idx)
        else:                                    # heavy
            drop.add(idx)

    if cut == "shuffle" and ivops > 0 and pvops:
        first_i = next(idx for idx, unit in enumerate(units)
                       if ctype(unit) == 0)
        pool = pvops[:]
        pool.extend(rng.choice(pvops) for _ in range(ivops - 1))
        rng.shuffle(pool)
        out = bytearray()
        out += b"".join(units[:first_i + 1])
        out += b"".join(units[idx] for idx in pool)
        return bytes(out)

    out = bytearray()
    for idx, unit in enumerate(units):
        if idx not in drop:
            out += unit
            continue
        if fill == "next":
            for later in units[idx + 1:]:
                if ctype(later) == 1:
                    out += later
                    break
        elif fill == "prev":
            for earlier in reversed(units[:idx]):
                if ctype(earlier) == 1:
                    out += earlier
                    break
        else:                                    # chaos
            if pvops:
                out += units[rng.choice(pvops)]
    return bytes(out)


def resolve_second_video(raw: object, input_dir: Path,
                         primary: Path) -> tuple:
    """resolve and validate the optional datamosh source."""
    text = str(raw or "none").strip()
    if text.lower() in ("", "none", "off"):
        return None, ""
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and not candidate.is_file():
        candidate = input_dir / candidate
    if not candidate.is_file():
        return None, f"no such second video: {text}"
    if candidate.suffix.lower() not in VIDEO_EXTS:
        return None, f"second video has an unsupported extension: {candidate.name}"
    try:
        if candidate.resolve() == primary.resolve():
            return None, "second video must be different from the primary video"
    except OSError:
        pass
    return candidate.resolve(), ""


def run_datamosh(ctx: EffectContext, clip: Clip, tools: Tools, args,
                 dst: Path, run_encode, on_progress, animate) -> tuple:
    """encode one or two videos into an mpeg4 stream, edit its i-vops, remux."""
    opts = ctx.opts
    cut = str(opts.get("cut", "heavy"))
    fill = str(opts.get("fill", "next"))
    hold = _clamp_int(opts.get("hold", 150), 150, 2, 600)
    grit = _clamp_int(opts.get("grit", 2), 2, 1, 31)
    survival = _clamp_int(opts.get("survival", 25), 25, 0, 100)
    chosen_seed = _clamp_int(opts.get("seed", 0), 0, 0, 2147483647)
    second, problem = resolve_second_video(opts.get("second", "none"),
                                           Path(args.input_dir), clip.path)
    if problem:
        return False, problem, 0.0
    second_clip = probe(second, tools) if second else None
    if second and (not second_clip or not second_clip.width or
                   not second_clip.height or not second_clip.duration):
        return False, "ffprobe could not read the second video's video stream", 0.0

    second_start = float(opts.get("second_start", 0.0))
    requested_length = float(opts.get("second_length", 0.0))
    second_length = 0.0
    if second_clip:
        available = float(second_clip.duration or 0.0) - second_start
        second_length = requested_length if requested_length else available
        if second_length <= 0 or second_length > available + 0.01:
            return False, "second video start/length is outside its duration", 0.0

    seed = chosen_seed or (20260925 + (clip.size % 10000))
    if chosen_seed == 0 and second_clip:
        seed += second_clip.size % 10000

    raw = Path(args.output_dir) / f".{clip.path.stem}_mosh_{os.getpid()}.m4v"
    stderr_parts = []
    elapsed = 0.0
    first_duration = clip.duration or 0.0
    try:
        cmd1 = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-i", str(clip.path)]
        pass_duration = first_duration
        if second_clip:
            width, height = _source_dims(clip)
            if not width or not height:
                return False, "ffprobe is required to combine two videos", elapsed
            fps = _fps_number(clip)
            cmd1 += ["-ss", f"{second_start:.6f}"]
            if requested_length:
                cmd1 += ["-t", f"{second_length:.6f}"]
            cmd1 += ["-i", str(second)]
            normalize = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"fps={fps:g},setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
            )
            graph = (f"[0:v:0]{normalize}[v0];[1:v:0]{normalize}[v1];"
                     "[v0][v1]concat=n=2:v=1:a=0[moshsrc]")
            cmd1 += ["-filter_complex", graph, "-map", "[moshsrc]", "-an",
                     "-r", f"{fps:g}"]
            cmd1 += ["-force_key_frames", f"{first_duration:.6f}"]
            pass_duration += second_length
        else:
            cmd1 += ["-an"]
        cmd1 += ["-c:v", "mpeg4", "-q:v", str(grit), "-g", str(hold),
                 "-bf", "0", "-f", "m4v", "-progress", "pipe:1",
                 "-nostats", "-loglevel", "error", str(raw)]
        rc, stderr, took = run_encode(cmd1, pass_duration, on_progress,
                                      animate)
        elapsed += took
        stderr_parts.append(stderr)
        if rc != 0:
            return False, "\n".join(stderr_parts), elapsed
        if not raw.exists() or raw.stat().st_size == 0:
            return False, "pass 1 produced no stream", elapsed
        raw.write_bytes(strip_ivops(raw.read_bytes(), cut, fill, seed,
                                    survival))

        cmd2 = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-f", "m4v", "-i", str(raw)]
        audio_mode = str(opts.get("audio", "auto"))
        audio_inputs = {}
        audio_paths = {}
        next_audio_input = 1

        def add_audio_input(label: str, source: Clip) -> None:
            nonlocal next_audio_input
            if source.audio:
                if source.path not in audio_paths:
                    audio_paths[source.path] = next_audio_input
                    cmd2.extend(["-i", str(source.path)])
                    next_audio_input += 1
                audio_inputs[label] = audio_paths[source.path]
            else:
                audio_inputs[label] = None

        if audio_mode == "auto" and second_clip:
            add_audio_input("first", clip)
            add_audio_input("second", second_clip)
        elif audio_mode in ("first", "auto"):
            add_audio_input("first", clip)
        elif audio_mode == "second" and second_clip:
            add_audio_input("second", second_clip)
        elif audio_mode == "mix":
            add_audio_input("first", clip)
            if second_clip:
                add_audio_input("second", second_clip)

        if audio_mode == "auto" and second_clip \
                and (clip.audio or second_clip.audio):
            audio_graph = []
            labels = []
            segments = [("first", clip.audio, first_duration, 0.0),
                        ("second", bool(second_clip.audio), second_length,
                         second_start)]
            for number, (label, has_audio, duration, start) in enumerate(segments):
                out_label = f"a{number}"
                labels.append(f"[{out_label}]")
                input_index = audio_inputs.get(label)
                if has_audio and input_index is not None:
                    trim = (f"atrim=start={start:g}:duration={duration:g},"
                            if number else f"atrim=duration={duration:g},")
                    audio_graph.append(
                        f"[{input_index}:a:0]{trim}apad,atrim=duration={duration:g},"
                        f"asetpts=PTS-STARTPTS,aresample=48000,"
                        f"aformat=channel_layouts=stereo[{out_label}]"
                    )
                else:
                    audio_graph.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000:"
                        f"d={duration:g}[{out_label}]"
                    )
            audio_graph.append("".join(labels) + "concat=n=2:v=0:a=1[aout]")
            cmd2 += ["-filter_complex", ";".join(audio_graph),
                     "-map", "0:v:0", "-map", "[aout]",
                     "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        elif audio_mode == "mix" and audio_inputs.get("first") \
                and audio_inputs.get("second"):
            first_audio = audio_inputs["first"]
            second_audio = audio_inputs["second"]
            audio_graph = (
                f"[{first_audio}:a:0]apad,atrim=duration={pass_duration:g}[a0];"
                f"[{second_audio}:a:0]apad,atrim=duration={pass_duration:g}[a1];"
                "[a0][a1]amix=inputs=2:duration=longest:normalize=0[aout]"
            )
            cmd2 += ["-filter_complex", audio_graph,
                     "-map", "0:v:0", "-map", "[aout]",
                     "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        else:
            if audio_mode == "second":
                selected = audio_inputs.get("second")
            elif audio_mode == "mix":
                selected = audio_inputs.get("first") or audio_inputs.get("second")
            else:
                selected = audio_inputs.get("first")
            if selected and second_clip and audio_mode == "second":
                delay_ms = max(0, int(round(first_duration * 1000)))
                audio_filter = (
                    f"[{selected}:a:0]atrim=start={second_start:g}:"
                    f"duration={second_length:g},asetpts=PTS-STARTPTS,"
                    f"adelay={delay_ms}|{delay_ms},apad,"
                    f"atrim=duration={pass_duration:g}[asecond]"
                )
                cmd2 += ["-filter_complex", audio_filter,
                         "-map", "0:v:0", "-map", "[asecond]",
                         "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            elif selected and second_clip:
                audio_filter = (
                    f"[{selected}:a:0]apad,"
                    f"atrim=duration={pass_duration:g}[aselected]"
                )
                cmd2 += ["-filter_complex", audio_filter,
                         "-map", "0:v:0", "-map", "[aselected]",
                         "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            elif selected:
                cmd2 += ["-map", "0:v:0", "-map", f"{selected}:a:0?",
                         "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            else:
                cmd2 += ["-map", "0:v:0", "-an"]
        cmd2 += ["-c:v", "copy", "-movflags", "+faststart",
                 "-progress", "pipe:1", "-nostats", "-loglevel", "error",
                 str(dst)]
        rc, stderr, took = run_encode(cmd2, pass_duration, on_progress,
                                      animate)
        elapsed += took
        stderr_parts.append(stderr)
        if rc != 0:
            return False, "\n".join(stderr_parts), elapsed
        return True, "\n".join(stderr_parts), elapsed
    finally:
        try:
            if raw.exists():
                raw.unlink()
        except OSError:
            pass


ZOOM_PARAMS = (
    Param("zoom", "max zoom", "how far in it creeps (percent of the frame)",
          "int", 135, minimum=110, maximum=300, step=10),
    Param("speed", "speed", "how fast it creeps", "choice", "slow",
          choices=(("slow", "slow", "a slow push"),
                   ("fast", "fast", "a quick punch in"))),
)

ZOOM_STEP = {"slow": 0.0015, "fast": 0.006}


def build_zoom(ctx: EffectContext) -> Plan:
    zoom = _clamp_int(ctx.opts.get("zoom", 135), 135, 110, 300)
    speed = str(ctx.opts.get("speed", "slow"))
    step = ZOOM_STEP.get(speed, 0.0015)
    cap = zoom / 100
    w, h = _source_dims(ctx.clip, ctx.frame)
    if not w or not h:
        w, h = ctx.width or DEFAULT_W, ctx.height or DEFAULT_H
    return Plan(
        vf=(f"zoompan=z='min(zoom+{step:g},{cap:g})':d=1"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}"),
        out_suffix="_ZOOMED",
        width=w,
        height=h,
        notes=[
            f"creeps from 100% up to {zoom}%",
            "the classic slow push-in",
            f"{speed} mode adds {step:g} zoom per frame",
        ],
    )


VHS_PARAMS = (
    Param("bleed", "chroma bleed", "how far the color smears sideways",
          "int", 4, minimum=0, maximum=40, step=2),
    Param("noise", "noise", "how much tape grain", "int", 14,
          minimum=0, maximum=60, step=2),
    Param("tracking", "tracking", "vertical roll tears, in pixels",
          "int", 12, minimum=0, maximum=40, step=2),
    Param("fade", "fade", "wash the colors out like an old tape",
          "bool", True),
)


def build_vhs(ctx: EffectContext) -> Plan:
    bleed = _clamp_int(ctx.opts.get("bleed", 4), 4, 0, 40)
    noise = _clamp_int(ctx.opts.get("noise", 14), 14, 0, 60)
    tracking = _clamp_int(ctx.opts.get("tracking", 12), 12, 0, 40)
    fade = bool(ctx.opts.get("fade", True))
    parts = []
    if bleed:
        parts.append(f"chromashift=cbh={bleed}:crh=-{bleed}")
    if fade:
        parts.append("eq=saturation=0.65:contrast=1.1")
    if noise:
        parts.append(f"noise=alls={noise}:allf=t")
    w, h = _source_dims(ctx.clip, ctx.frame)
    if tracking and h:
        crop_h = (h - tracking) - (h - tracking) % 2
        parts.append(f"crop=w=iw:h={crop_h}:x=0"
                     f":y='{tracking}*abs(sin(2*PI*t/2))'"
                     f",pad=w=iw:h={h}:x=0:y=0")
    if not parts:
        parts = ["null"]
    return Plan(
        vf=",".join(parts),
        out_suffix="_VHS",
        width=w,
        height=h,
        notes=[
            "tape grain, color bleed and tracking tears",
            "the family camcorder from the attic look",
            "fade washes the color, tracking rocks the frame",
        ],
    )


TRAILS_PARAMS = (
    Param("trail", "trail", "how many frames bleed into each other",
          "int", 10, minimum=2, maximum=20, step=2),
    Param("mode", "mode", "even blend or a fading tail", "choice", "fade",
          choices=(("fade", "fade", "recent frames weigh more"),
                   ("even", "even", "every frame weighs the same"))),
)


def build_trails(ctx: EffectContext) -> Plan:
    trail = _clamp_int(ctx.opts.get("trail", 10), 10, 2, 20)
    mode = str(ctx.opts.get("mode", "fade"))
    if mode == "even":
        weights = " ".join(["1"] * trail)
    else:
        weights = " ".join(str(trail - i) for i in range(trail))
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=f"tmix=frames={trail}:weights='{weights}'",
        out_suffix="_TRAILS",
        width=w,
        height=h,
        notes=[
            f"each frame bleeds {trail} frames of history",
            "dragged-across-the-screen smears",
        ],
    )


SLOWMO_PARAMS = (
    Param("factor", "speed", "how slow or fast it plays", "choice", "0.5",
          choices=(("0.25", "quarter speed", "very dreamy"),
                   ("0.5", "half speed", "slow motion"),
                   ("2", "double speed", "hyperlapse"),
                   ("4", "quadruple speed", "chaos"))),
)


def build_slowmo(ctx: EffectContext) -> Plan:
    factor = float(str(ctx.opts.get("factor", "0.5")))
    if factor < 1:
        af = ",".join(["atempo=0.5"] * round(math.log(factor, 0.5)))
    else:
        af = ",".join(["atempo=2"] * round(math.log(factor, 2)))
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=f"setpts={1 / factor:g}*PTS",
        af=af,
        out_suffix="_TEMPO",
        width=w,
        height=h,
        notes=[
            f"plays at {factor:g}x, pitch warps with atempo",
            "the duration changes to match",
        ],
    )


CRT_PARAMS = (
    Param("lines", "scanlines", "dark lines across the screen", "bool", True),
    Param("curve", "curve", "bulge the screen like a tube", "bool", False),
    Param("noise", "noise", "how much electric fuzz", "int", 6,
          minimum=0, maximum=30, step=2),
    Param("roll", "roll", "a slow vertical crawl, in pixels", "int", 0,
          minimum=0, maximum=30, step=2),
)


def build_crt(ctx: EffectContext) -> Plan:
    lines = bool(ctx.opts.get("lines", True))
    curve = bool(ctx.opts.get("curve", False))
    noise = _clamp_int(ctx.opts.get("noise", 6), 6, 0, 30)
    roll = _clamp_int(ctx.opts.get("roll", 0), 0, 0, 30)
    parts = []
    if curve:
        parts.append("lenscorrection=k1=0.02:k2=0.06")
    if lines:
        parts.append("drawgrid=w=iw:h=4:t=2:c=black@0.6")
    if noise:
        parts.append(f"noise=alls={noise}:allf=t")
    w, h = _source_dims(ctx.clip, ctx.frame)
    if roll and h:
        crop_h = (h - roll) - (h - roll) % 2
        parts.append(f"crop=w=iw:h={crop_h}:x=0"
                     f":y='{roll}*abs(sin(2*PI*t/1.2))'"
                     f",pad=w=iw:h={h}:x=0:y=0")
    if not parts:
        parts = ["null"]
    return Plan(
        vf=",".join(parts),
        out_suffix="_CRT",
        width=w,
        height=h,
        notes=[
            "scanlines, electric fuzz and a slow roll",
            "the tube tv from the basement look",
        ],
    )


CHANNELSWAP_PARAMS = (
    Param("swap", "swap", "which channels trade places", "choice", "rb",
          choices=(
              ("rb", "red <-> blue", "fire turns electric"),
              ("gb", "green <-> blue", "skin turns alien"),
              ("rg", "red <-> green", "plants turn neon"),
              ("roll", "rotate", "red becomes green becomes blue"),
              ("invert", "invert", "the negative, circuit-fried look"),
          )),
    Param("bleed", "bleed", "how far the color smears sideways",
          "int", 0, minimum=0, maximum=30, step=2),
)

CHANNELSWAP_CCM = {
    "rb": "colorchannelmixer=rr=0:rb=1:bb=0:br=1",
    "gb": "colorchannelmixer=gg=0:gb=1:bb=0:bg=1",
    "rg": "colorchannelmixer=rr=0:rg=1:gg=0:gr=1",
    "roll": "colorchannelmixer=rr=0:rg=1:gg=0:gb=1:bb=0:br=1",
    "invert": "lutrgb=r=negval:g=negval:b=negval",
}


def build_channelswap(ctx: EffectContext) -> Plan:
    swap = str(ctx.opts.get("swap", "rb"))
    bleed = _clamp_int(ctx.opts.get("bleed", 0), 0, 0, 30)
    parts = [CHANNELSWAP_CCM.get(swap, CHANNELSWAP_CCM["rb"])]
    if bleed:
        parts.append(f"chromashift=cbh={bleed}:crh=-{bleed}")
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=",".join(parts),
        out_suffix="_RGBBENT",
        width=w,
        height=h,
        notes=[
            "channels trade places - pure circuit bend",
            "add bleed to smear the wreckage sideways",
        ],
    )


LOBOTOMY_LEVELS = tuple(
    (str(n), f"{n} levels", "unchanged" if n == 256 else "fewer = harsher bands")
    for n in (2, 4, 8, 16, 32, 64, 128, 256)
)
LOBOTOMY_PARAMS = (
    Param("red", "red levels", "quantization levels for the red channel",
          "choice", "8", choices=LOBOTOMY_LEVELS),
    Param("green", "green levels", "quantization levels for the green channel",
          "choice", "4", choices=LOBOTOMY_LEVELS),
    Param("blue", "blue levels", "quantization levels for the blue channel",
          "choice", "8", choices=LOBOTOMY_LEVELS),
    Param("kill", "kill channel", "zero a source channel before swapping",
          "choice", "g", choices=(
              ("none", "none", "keep all channels"),
              ("r", "red", "remove red"),
              ("g", "green", "remove green"),
              ("b", "blue", "remove blue"),
          )),
    Param("swap", "swap", "trade channels after crushing and killing",
          "choice", "rb", choices=(
              ("none", "none", "keep channel order"),
          ) + tuple(c for c in CHANNELSWAP_PARAMS[0].choices if c[0] != "invert")),
)


def build_lobotomy(ctx: EffectContext) -> Plan:
    parts = ["format=rgb24"]
    channels = []
    for key, default in (("red", 8), ("green", 4), ("blue", 8)):
        levels = _clamp_int(ctx.opts.get(key, default), default, 2, 256)
        q = 256 // levels
        channels.append(f"{key[0]}='floor(val/{q})*{q}'")
    parts.append("lutrgb=" + ":".join(channels))
    kill = str(ctx.opts.get("kill", "g"))
    if kill in ("r", "g", "b"):
        parts.append(f"colorchannelmixer={kill}{kill}=0")
    swap = str(ctx.opts.get("swap", "rb"))
    if swap in ("rb", "rg", "gb", "roll"):
        parts.append(CHANNELSWAP_CCM[swap])
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=",".join(parts), out_suffix=ctx.suffix or "_LOBOTOMIZED",
        width=w, height=h,
        notes=["crush each RGB channel, kill a source channel, then swap",
               "256 levels with no kill or swap leaves colors unchanged"],
    )


NEON_PARAMS = (
    Param("amount", "hue swing", "maximum hue rotation in degrees",
          "int", 120, minimum=0, maximum=180, step=10),
    Param("period", "cycle seconds", "seconds for one full back-and-forth cycle",
          "float", 4.0, minimum=0.5, maximum=30.0, step=0.5),
    Param("saturation", "saturation", "100% is unchanged; higher crushes color",
          "percent", 200, minimum=100, maximum=300, step=10),
    Param("punch", "curve punch", "contrast curve strength; 0% disables it",
          "percent", 50, minimum=0, maximum=100, step=5),
)


def build_neon(ctx: EffectContext) -> Plan:
    amount = _clamp_int(ctx.opts.get("amount", 120), 120, 0, 180)
    try:
        period = float(ctx.opts.get("period", 4.0))
    except (TypeError, ValueError):
        period = 4.0
    period = max(0.5, min(30.0, period))
    saturation = _clamp_int(ctx.opts.get("saturation", 200), 200, 100, 300) / 100
    punch = _clamp_int(ctx.opts.get("punch", 50), 50, 0, 100) / 100
    parts = []
    if amount or saturation != 1:
        parts.append(f"hue=h='{amount}*sin(2*PI*t/{period:g})':s={saturation:g}")
    if punch:
        low, high = 0.25 - 0.15 * punch, 0.75 + 0.15 * punch
        parts.append(f"curves=all='0/0 0.25/{low:g} 0.75/{high:g} 1/1'")
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=",".join(parts) or "null", out_suffix=ctx.suffix or "_NEON",
        width=w, height=h,
        notes=[f"hue swings +/-{amount} degrees over {period:g} seconds",
               "saturated color and an adjustable S-curve contrast punch"],
    )


BLOCKSWAP_PARAMS = (
    Param("size", "block size", "how big the swapped blocks are "
           "(percent of the frame)", "int", 25, minimum=10, maximum=60,
           step=5),
    Param("speed", "speed", "how fast the blocks travel", "choice", "slow",
          choices=(("slow", "slow", "a lazy drift"),
                   ("fast", "fast", "a nervous slide"))),
)

BLOCKSWAP_PERIOD = {"slow": 2.0, "fast": 0.5}


def build_blockswap(ctx: EffectContext) -> Plan:
    size = _clamp_int(ctx.opts.get("size", 25), 25, 10, 60)
    speed = str(ctx.opts.get("speed", "slow"))
    period = BLOCKSWAP_PERIOD.get(speed, 2.0)
    w, h = _source_dims(ctx.clip, ctx.frame)
    if not w or not h:
        w, h = ctx.width or DEFAULT_W, ctx.height or DEFAULT_H
    bw = max(2, int(w * size / 100) // 2 * 2)
    bh = max(2, int(h * size / 100) // 2 * 2)
    span = f"(w-{bw})/2"
    return Plan(
        vf=(f"swaprect=w={bw}:h={bh}"
            f":x1='{span}*(1+sin(2*PI*t/{period:g}))'"
            f":y1='(h-{bh})/2'"
            f":x2='{span}*(1-sin(2*PI*t/{period:g}))'"
            f":y2='(h-{bh})/2'"),
        out_suffix="_BLOCKSWAP",
        width=w,
        height=h,
        notes=[
            "two blocks slide through each other",
            f"{size}% blocks, {speed} travel",
        ],
    )


DROPOUT_PARAMS = (
    Param("chance", "drop chance", "percent of frames that vanish",
          "int", 8, minimum=1, maximum=50, step=1),
)


def _clip_fps(clip: Clip) -> float:
    try:
        if clip.fps and "/" in str(clip.fps):
            num, den = str(clip.fps).split("/", 1)
            return float(num) / float(den)
    except (TypeError, ValueError):
        pass
    return 30.0


def build_dropout(ctx: EffectContext) -> Plan:
    chance = _clamp_int(ctx.opts.get("chance", 8), 8, 1, 50)
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=f"select='gt(random(0),{chance / 100:g})'",
        out_suffix="_DROPOUT",
        # FFmpeg 6 defaults to CFR here and recreates frames that select removed.
        out_extra=["-fps_mode", "vfr"],
        width=w,
        height=h,
        notes=[
            f"every frame rolls a {chance}% chance to vanish",
            "the picture freezes in place wherever a frame drops - the "
            "classic dropout hold",
            "the audio runs straight through, untouched",
        ],
    )


CRUNCH_PARAMS = (
    Param("res", "resolution", "how far to shrink the picture",
          "choice", "100",
          choices=(("100", "keep", "no resizing"),
                   ("75", "75%", "three quarters"),
                   ("50", "half", "half the width and height"),
                   ("25", "quarter", "postage stamp"))),
    Param("bitrate", "bitrate", "how much data the video gets, in kbit/s",
          "int", 800, minimum=100, maximum=5000, step=100),
)


def build_crunch(ctx: EffectContext) -> Plan:
    res = int(str(ctx.opts.get("res", "100")))
    bitrate = _clamp_int(ctx.opts.get("bitrate", 800), 800, 100, 5000)
    w, h = _source_dims(ctx.clip, ctx.frame)
    vf = "null"
    out_w, out_h = 0, 0
    if res < 100 and w and h:
        out_w = max(2, int(w * res / 100) // 2 * 2)
        out_h = max(2, int(h * res / 100) // 2 * 2)
        vf = f"scale={out_w}:{out_h}"
    return Plan(
        vf=vf,
        out_suffix="_CRUNCHED",
        width=out_w,
        height=out_h,
        out_extra=["-b:v", f"{bitrate}k"],
        abr=True,
        notes=[
            f"video capped at {bitrate} kbit/s",
            f"picture scaled to {res}%" if res < 100 else "picture untouched",
            "the poor man's shipping cut",
        ],
    )


TIMESCRATCH_PARAMS = (
    Param("curve", "curve", "the math that generates the scratches",
          "choice", "sine",
          choices=(("sine", "sine", "speeds swing on a sine wave"),
                   ("random", "random", "every segment rolls its own speed"),
                   ("swing", "swing", "alternates forward and backward"),
                   ("walk", "walk", "a random walk up and down the speeds"))),
    Param("speed", "speed", "how wild the speeds get", "choice", "rough",
          choices=(("gentle", "gentle", "up to 2x, down to half"),
                   ("rough", "rough", "up to 4x"),
                   ("extreme", "extreme", "up to 8x"))),
    Param("cuts", "cuts", "how many scratches across the clip", "choice",
          "medium",
          choices=(("light", "light", "one scratch per second"),
                   ("medium", "medium", "two per second"),
                   ("heavy", "heavy", "four per second"))),
)

CURVE_MAX = {"gentle": 2.0, "rough": 4.0, "extreme": 8.0}
CUTS_PER_SEC = {"light": 1.0, "medium": 2.0, "heavy": 4.0}


def _tempo_chain(speed: float) -> str:
    """an atempo chain that hits `speed` exactly (atempo is 0.5-2 per stage)."""
    if speed >= 1:
        k = int(math.log(speed, 2))
        r = speed / (2 ** k)
        chain = [f"atempo={r:g}"] + ["atempo=2"] * k
    else:
        k = int(math.log(speed, 0.5))
        r = speed / (0.5 ** k)
        chain = ["atempo=0.5"] * k + [f"atempo={r:g}"]
    return ",".join(chain)


def _scratch_speeds(curve: str, cap: float, count: int, seed: int) -> list:
    """one signed speed multiplier per segment; negative = backward."""
    rng = random.Random(seed)
    if curve == "sine":
        return [cap * math.sin(2 * math.pi * i / max(3, count))
                for i in range(count)]
    if curve == "swing":
        return [cap * ((-1) ** i) * (1 + i % 3) / 3 for i in range(count)]
    if curve == "walk":
        speeds = []
        cur = 1.0
        for _ in range(count):
            cur += rng.uniform(-cap / 3, cap / 3)
            cur = max(-cap, min(cap, cur))
            speeds.append(cur)
        return speeds
    return [rng.uniform(-cap, cap) for _ in range(count)]


def _clamp_scratch(speeds: list) -> list:
    out = []
    for sp in speeds:
        if abs(sp) < 0.15:
            sp = 0.15 if sp >= 0 else -0.15
        out.append(sp)
    return out


def build_timescratch(ctx: EffectContext) -> Plan:
    curve = str(ctx.opts.get("curve", "sine"))
    wild = str(ctx.opts.get("speed", "rough"))
    cuts = str(ctx.opts.get("cuts", "medium"))
    w, h = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf=(f"scrub: cut into {cuts} segments, {curve} speeds up to "
            f"{CURVE_MAX[wild]:g}x, reverse+retime, concat"),
        out_suffix="_TIMESCRATCHED",
        width=w,
        height=h,
        notes=[
            "the timeline-cursor scrub: segments speed up, slow down and "
            "run backward",
            f"{curve} curve, {wild} speeds, {cuts} cuts",
            "backward runs reverse the picture and the audio",
            "the output runs longer or shorter than the source - the "
            "scratch stretches time",
        ],
    )


def run_timescratch(ctx, clip, tools, args, dst, run_encode, on_progress,
                    animate) -> tuple:
    """real timescratch: split the clip into segments, give each a signed
    speed from the chosen curve, reverse+retime the backward ones, then
    concat. every segment is a real re-encode so the scrubbed stream stays
    monotonic. returns (ok, stderr, elapsed)."""
    opts = ctx.opts
    curve = str(opts.get("curve", "sine"))
    wild = str(opts.get("speed", "rough"))
    cuts = str(opts.get("cuts", "medium"))
    cap = CURVE_MAX.get(wild, 4.0)
    cps = CUTS_PER_SEC.get(cuts, 2.0)
    dur = clip.duration or 30.0
    count = max(2, min(60, int(round(dur * cps))))
    seg = dur / count
    seed = 20260925 + (clip.size % 10000)
    speeds = _clamp_scratch(_scratch_speeds(curve, cap, count, seed))
    fps = _clip_fps(clip)
    work = Path(args.output_dir) / f".{clip.path.stem}_scratch_{os.getpid()}"
    stderr_parts = []
    elapsed = 0.0
    try:
        work.mkdir(parents=True, exist_ok=True)
        parts = []
        for i, sp in enumerate(speeds):
            start = i * seg
            vf = (f"reverse,setpts=N/{fps:g}/TB" if sp < 0 else "null") \
                 + f",setpts=PTS*{1 / abs(sp):g}"
            af = _tempo_chain(abs(sp))
            if sp < 0:
                af = "areverse," + af
            part = work / f"seg{i:02d}.mp4"
            cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
                   "-ss", f"{start:.3f}", "-to", f"{start + seg:.3f}",
                   "-i", str(clip.path), "-vf", vf]
            if clip.audio:
                cmd += ["-af", af]
            cmd += ["-c:v", "libx264", "-crf", str(ctx.crf),
                    "-preset", str(ctx.preset), "-pix_fmt", "yuv420p",
                    "-r", f"{fps:g}"]
            if clip.audio:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-an"]
            cmd += ["-progress", "pipe:1", "-nostats", "-loglevel",
                    "error", str(part)]
            rc, stderr, took = run_encode(cmd, seg, on_progress, animate)
            elapsed += took
            stderr_parts.append(stderr)
            if rc != 0:
                return False, "\n".join(stderr_parts), elapsed
            parts.append(part)
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-c", "copy", "-movflags", "+faststart",
               "-progress", "pipe:1", "-nostats", "-loglevel", "error",
               str(dst)]
        rc, stderr, took = run_encode(cmd, dur, on_progress, animate)
        elapsed += took
        stderr_parts.append(stderr)
        if rc != 0:
            return False, "\n".join(stderr_parts), elapsed
        return True, "\n".join(stderr_parts), elapsed
    finally:
        shutil.rmtree(work, ignore_errors=True)


TEMPORAL_SEDIMENT_PARAMS = (
    Param("intensity", "intensity", "how much accumulated history shows", "percent",
          65, minimum=0, maximum=100, step=5),
    Param("deposit", "deposit rate", "how strongly new frames enter history",
          "percent", 70, minimum=0, maximum=100, step=5),
    Param("decay", "decay rate", "how quickly old layers fade", "percent",
          35, minimum=0, maximum=100, step=5),
    Param("layers", "memory layers", "number of decay speeds to retain",
          "choice", "3", choices=(
              ("1", "1 layer", "one history speed"),
              ("2", "2 layers", "short and long memory"),
              ("3", "3 layers", "fast, medium and long memory"),
          )),
    Param("motion", "motion sensitivity", "how strongly movement deposits pixels",
          "percent", 50, minimum=0, maximum=100, step=5),
    Param("excavation", "excavation chance", "chance per frame to reveal an older patch",
          "percent", 5, minimum=0, maximum=100, step=5),
    Param("excavation_size", "excavation size", "patch width and height as a frame percent",
          "percent", 15, minimum=5, maximum=50, step=5),
    Param("color", "color memory", "how stored colors are treated", "choice",
          "keep", choices=(
              ("keep", "keep", "retain the original colors"),
              ("warm", "warm", "tint old material amber"),
              ("cool", "cool", "tint old material blue"),
              ("mono", "mono", "turn old material grayscale"),
          )),
    Param("seed", "random seed", "0 derives a repeatable seed from the clip",
          "int", 0, minimum=0, maximum=2147483647, step=1),
)


def build_temporal_sediment(ctx: EffectContext) -> Plan:
    width, height = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf="stateful temporal sediment frame pass",
        out_suffix=ctx.suffix or "_SEDIMENTED",
        width=width,
        height=height,
        notes=[
            "builds persistent, bounded frame memory with separate decay layers",
            "motion changes where new pixels settle; excavation exposes the oldest layer",
            "history state resets on a clip change or a detected scene cut",
        ],
    )


COLLAPSE_PARAMS = (
    Param("intensity", "intensity", "how strongly the displacement is applied",
          "percent", 100, minimum=0, maximum=100, step=5),
    Param("horizontal", "horizontal scale", "horizontal deformation amount",
          "percent", 100, minimum=0, maximum=100, step=5),
    Param("vertical", "vertical scale", "vertical deformation amount",
          "percent", 100, minimum=0, maximum=100, step=5),
    Param("preserve", "content preservation", "protect strong edges from collapse",
          "percent", 75, minimum=0, maximum=100, step=5),
    Param("mode", "mode", "progressive content-aware scaling or elastic warping",
          "choice", "seam", choices=(
              ("seam", "content-aware scale",
               "linearly shrink by seams, then resize to the original frame"),
              ("elastic", "elastic warp", "smooth content-weighted CPU remapping"),
          )),
    Param("seam_strength", "shrink amount", "maximum content-aware shrink by the end of the clip",
          "percent", 100, minimum=0, maximum=100, step=5),
    Param("motion", "motion influence", "amplify elastic deformation in moving regions",
          "percent", 50, minimum=0, maximum=100, step=5),
    Param("smoothness", "field smoothness", "larger, smoother deformation regions",
          "percent", 70, minimum=0, maximum=100, step=5),
    Param("speed", "field speed", "elastic field cycles per second; 0 holds it still",
          "float", 0.35, minimum=0, maximum=4, step=0.1),
    Param("coherence", "temporal coherence", "smooth field changes over elapsed time",
          "percent", 80, minimum=0, maximum=100, step=5),
    Param("seed", "random seed", "0 derives repeatable elastic phases from the clip",
          "int", 0, minimum=0, maximum=2147483647, step=1),
)


def build_collapse(ctx: EffectContext) -> Plan:
    width, height = _source_dims(ctx.clip, ctx.frame)
    mode = str(ctx.opts.get("mode", "seam"))
    return Plan(
        vf=f"stateful elastic collapse ({mode}) frame pass",
        out_suffix=ctx.suffix or "_COLLAPSED", width=width, height=height,
        notes=[
            "content-aware mode linearly shrinks over the clip and remaps to original dimensions",
            "seam analysis is capped at 192px; target scales remove at most half of either axis",
            "elastic mode remains available for seeded smooth fields and CPU OpenCV remapping",
            "needs requirements-collapse.txt; no stateful preview or original VFR PTS",
        ],
    )


INVERSION_PARAMS = (
    Param("lookahead", "look-ahead secs", "buffer horizon in seconds; adds processing latency",
          "float", 0.5, minimum=0, maximum=5, step=0.1),
    Param("offset", "future offset", "seconds to borrow ahead; cannot exceed look-ahead",
          "float", 0.5, minimum=0, maximum=5, step=0.1),
    Param("strength", "strength", "blend opacity or replacement coverage",
          "percent", 65, minimum=0, maximum=100, step=5),
    Param("mode", "mode", "how future pixels enter the current image", "choice", "blend",
          choices=(("blend", "blend", "mix current and future pixels"),
                   ("replace", "replace", "replace seeded regions instead of mixing"),
                   ("tear", "region tearing", "shift bands from the future frame"),
                   ("align", "motion aligned", "warp future toward current with CPU flow"))),
    Param("region", "region mask", "which regions may borrow future pixels", "choice", "full",
          choices=(("full", "full frame", "every pixel"),
                   ("bands", "bands", "moving horizontal stripes"),
                   ("blocks", "blocks", "moving checker regions"),
                   ("patch", "moving patch", "a drifting circular patch"))),
    Param("mask_speed", "mask speed", "mask and tear cycles per second; 0 holds still",
          "float", 0.25, minimum=0, maximum=4, step=0.1),
    Param("alignment", "motion alignment", "flow gain in aligned mode; 0 disables alignment",
          "percent", 100, minimum=0, maximum=100, step=5),
    Param("feather", "edge feather", "soften region mask boundaries",
          "percent", 10, minimum=0, maximum=100, step=5),
    Param("seed", "random seed", "0 derives repeatable masks from the source",
          "int", 0, minimum=0, maximum=2147483647, step=1),
)


def build_inversion(ctx: EffectContext) -> Plan:
    lookahead = float(ctx.opts.get("lookahead", 0.5))
    offset = float(ctx.opts.get("offset", 0.5))
    if offset > lookahead:
        raise ValueError("inversion offset must not exceed lookahead seconds")
    width, height = _source_dims(ctx.clip, ctx.frame)
    return Plan(
        vf="stateful causality inversion timestamped frame pass",
        out_suffix=ctx.suffix or "_INVERTED", width=width, height=height,
        notes=[f"buffers up to {lookahead:g}s to borrow {offset:g}s into the future",
               "preserves video PTS, dimensions and compatible unfiltered audio packets",
               "EOF/cuts clamp to latest frame; 128 MiB/128-frame buffer budget",
               "requires requirements-inversion.txt; aligned mode also needs headless OpenCV"],
    )


EFFECTS = [
    Effect(key="stretch", name="stretch",
           blurb="squash the frame into another shape",
           tag="scale=W:H:force_original_aspect_ratio=disable,setsar=1",
           build=build_stretch, params=STRETCH_PARAMS),
    Effect(key="pixelize", name="pixelize",
           blurb="turn the picture into fat blocks",
           tag="scale down, scale back up with neighbor",
           build=build_pixelize, params=PIXELIZE_PARAMS),
    Effect(key="rgbsplit", name="rgb split",
           blurb="drift the color planes apart",
           tag="chromashift=cbh: -crh",
           build=build_rgbsplit, params=RGBSPLIT_PARAMS),
    Effect(key="stutter", name="stutter",
           blurb="chop the frame rate down",
           tag="fps=N",
           build=build_stutter, params=STUTTER_PARAMS),
    Effect(key="deepfry", name="deepfry",
           blurb="saturate and crunch it to death",
           tag="eq=saturation=:contrast=,unsharp",
           build=build_deepfry, params=DEEPFRIED_PARAMS),
    Effect(key="wobble", name="wobble",
           blurb="rock the frame side to side",
           tag="rotate=a=sin(2*PI*t)",
           build=build_wobble, params=WOBBLE_PARAMS),
    Effect(key="spin", name="spin",
           blurb="spin the whole frame",
           tag="rotate=a=2*PI*t",
           build=build_spin, params=SPIN_PARAMS),
    Effect(key="posterize", name="posterize",
           blurb="crush the colors into bands",
           tag="lutrgb=floor(val/Q)*Q",
           build=build_posterize, params=POSTERIZE_PARAMS),
    Effect(key="datamosh", name="datamosh",
           blurb="codec-level melt - the decoder smears the motion",
           tag="two passes - mpeg4 i-vop removal, then a straight remux",
           build=build_datamosh, params=DATAMOSH_PARAMS,
           run=run_datamosh,
           stages=(
               "pass 1: ffmpeg -i <clip> -an -c:v mpeg4 -q:v 2 "
               "-g <hold> -bf 0 -f m4v <clip>_mosh.m4v",
               "cut: strip_ivops() removes i-vops from the bitstream - "
               "heavy keeps 1, medium every 2nd, light every 3rd, dice "
               "uses configured survival, shuffle scrambles every motion frame; "
               "each hole takes the next, previous or a random motion "
               "frame",
               "pass 2: ffmpeg -f m4v -i <clip>_mosh.m4v [-i <clip> for "
               "the audio] -c:v copy -c:a aac <output>.mp4",
           )),
    Effect(key="zoom", name="zoom",
           blurb="creep slowly into the frame",
           tag="zoompan=z='min(zoom+step,cap)':d=1",
           build=build_zoom, params=ZOOM_PARAMS),
    Effect(key="vhs", name="vhs",
           blurb="tape grain, color bleed, tracking tears",
           tag="chromashift,noise=alls,eq,crop roll",
           build=build_vhs, params=VHS_PARAMS),
    Effect(key="trails", name="trails",
           blurb="frames bleed into each other",
           tag="tmix=frames=N",
           build=build_trails, params=TRAILS_PARAMS),
    Effect(key="slowmo", name="slow + pitch",
           blurb="speed the clip up or down, audio pitches with it",
           tag="setpts=1/F*PTS,atempo",
           build=build_slowmo, params=SLOWMO_PARAMS),
    Effect(key="crt", name="crt",
           blurb="scanlines, fuzz and a slow roll",
           tag="drawgrid,lenscorrection,noise",
           build=build_crt, params=CRT_PARAMS),
    Effect(key="channelswap", name="channelswap",
           blurb="circuit-bent rgb - channels trade places",
           tag="lutrgb channel swap, chromashift bleed",
           build=build_channelswap, params=CHANNELSWAP_PARAMS),
    Effect(key="lobotomy", name="channel lobotomy",
           blurb="crush, kill and swap individual color channels",
           tag="lutrgb,colorchannelmixer",
           build=build_lobotomy, params=LOBOTOMY_PARAMS),
    Effect(key="neon", name="neon hue cycle",
           blurb="swing through saturated acid colors with a contrast punch",
           tag="hue=h=sin(2*PI*t/P),curves",
           build=build_neon, params=NEON_PARAMS),
    Effect(key="blockswap", name="blockswap",
           blurb="two blocks slide through each other",
           tag="swaprect moving blocks",
           build=build_blockswap, params=BLOCKSWAP_PARAMS),
    Effect(key="dropout", name="dropout",
           blurb="frames vanish at random - the picture holds",
           tag="select=gt(random(0),C)",
           build=build_dropout, params=DROPOUT_PARAMS),
    Effect(key="crunch", name="crunch",
           blurb="turn the bitrate and resolution down",
           tag="scale, -b:v cap",
           build=build_crunch, params=CRUNCH_PARAMS),
    Effect(key="timescratch", name="timescratch",
           blurb="drag the timeline cursor - forward, backward, slow, fast",
           tag="cut into segments, reverse+retime, concat",
           build=build_timescratch, params=TIMESCRATCH_PARAMS,
           run=run_timescratch,
           stages=(
               "cut: the clip is split into segments and each gets a signed "
               "speed from the curve",
               "segment: ffmpeg -ss S -to E -i <clip> -vf "
               "[reverse,setpts=N/FPS/TB,]setpts=PTS*K -af atempo[,areverse] "
               "-c:v libx264 -c:a aac segNN.mp4",
               "pass 2: ffmpeg -f concat -i list.txt -c copy <output>.mp4",
           )),
    Effect(key="collapse", name="elastic collapse",
           blurb="progressive content-aware scaling or elastic warp",
           tag="stateful seam carving and CPU remap",
           build=build_collapse, params=COLLAPSE_PARAMS,
           frame_processor=ElasticCollapse,
           stages=(
               "decode through earlier FFmpeg filters to RGB frames",
               "evolve content-aware elastic field or bounded low-resolution seam map",
               "smooth displacement, CPU remap, encode with later filters and audio",
           )),
    Effect(key="inversion", name="causality inversion",
           blurb="borrow real pixels from buffered future frames",
           tag="bounded look-ahead with timestamp-preserving RGB transport",
           build=build_inversion, params=INVERSION_PARAMS,
           frame_processor=CausalityInversion,
           stages=("decode timestamped RGB frames through earlier FFmpeg filters",
                   "buffer future frames, mix selected regions, clamp unavailable future at EOF",
                   "encode on original video PTS with later filters and source audio")),
    Effect(key="sediment", name="temporal sediment",
           blurb="deposit frames into bounded, decaying visual history",
           tag="stateful Python frame processor",
           build=build_temporal_sediment, params=TEMPORAL_SEDIMENT_PARAMS,
           frame_processor=TemporalSediment,
           stages=(
               "decode the source through any earlier FFmpeg filters to RGB frames",
               "update bounded float history, apply sediment and excavation masks",
               "encode processed frames and any later FFmpeg filters with the source audio",
           )),
]

EFFECT_BY_KEY = {e.key: e for e in EFFECTS}
EFFECT_GROUPS = (
    ("SHAPE & MOTION", (
        "stretch", "pixelize", "zoom", "wobble", "spin", "stutter", "slowmo",
        "collapse",
    )),
    ("COLOR & TEXTURE", (
        "rgbsplit", "deepfry", "posterize", "channelswap", "vhs", "crt",
        "lobotomy", "neon",
    )),
    ("TIME & MEMORY", (
        "trails", "timescratch", "sediment", "dropout", "inversion",
    )),
    ("CODEC & BLOCKS", (
        "datamosh", "blockswap", "crunch",
    )),
)
_grouped_effect_keys = tuple(key for _title, keys in EFFECT_GROUPS
                             for key in keys)
if (len(_grouped_effect_keys) != len(set(_grouped_effect_keys))
        or set(_grouped_effect_keys) != set(EFFECT_BY_KEY)):
    raise ValueError("EFFECT_GROUPS must list each registered effect exactly once")


def build_plan(effect: Effect, ctx: EffectContext) -> Plan:
    """effects without a builder are a registry bug, not a crash risk"""
    builder = effect.build
    plan = builder(ctx) if builder else None
    if plan is None:
        raise ValueError(f"{effect.name} has no builder")
    return plan


def default_opts(effect: Effect) -> dict:
    return {p.key: p.default for p in effect.params}


# ---------------------------------------------------------------------------
# chaining (E3.3) - chains, their options, their merged plan
# ---------------------------------------------------------------------------

CHAIN_SPLIT = re.compile(r"\s*[+,]\s*")


def parse_chain(text: str) -> tuple:
    """'stretch+deepfry' -> (Chain, ''); anything wrong -> (None, why)"""
    keys = [k for k in CHAIN_SPLIT.split(text.strip()) if k]
    if not keys:
        return None, "no effect given"
    effects = []
    for key in keys:
        effect = EFFECT_BY_KEY.get(key)
        if effect is None:
            near = difflib.get_close_matches(key, list(EFFECT_BY_KEY), 1, 0.7)
            tail = f" - did you mean {near[0]}?" if near else ""
            return None, f"unknown effect: {key}{tail}"
        if effect in effects:
            return None, f"{effect.name} is in the chain twice"
        effects.append(effect)
    if len(effects) > 1:
        stuck = [e for e in effects if e.run is not None]
        if stuck:
            names = ", ".join(e.name for e in stuck)
            return None, (f"{names} runs its own multi-pass pipeline and "
                          "cannot be chained yet - render it on its own")
    return Chain(tuple(effects)), ""


def default_chain_opts(chain: Chain) -> dict:
    """one option dict per effect in the chain"""
    return {e.key: default_opts(e) for e in chain.effects}


def opts_of(opts: dict, effect: Effect) -> dict:
    """this effect's slice of a chain option dict, defaults filled in"""
    mine = opts.get(effect.key)
    if not isinstance(mine, dict):
        mine = {}
    merged = default_opts(effect)
    merged.update(mine)
    return merged


def missing_option(effect: Effect, key: str) -> str:
    known = ", ".join(p.key for p in effect.params) or "none"
    return f"{effect.name} has no option {key!r} (has: {known})"


def chain_plans(chain: Chain, ctx: EffectContext) -> list:
    """[(effect, plan)] in chain order.

    each effect is handed the frame the previous one produced, so
    stretch+pixelize pixelates the stretched frame instead of the source.
    """
    plans = []
    frame = _source_dims(ctx.clip, ctx.frame)
    for effect in chain.effects:
        sub = EffectContext(
            clip=ctx.clip, width=ctx.width, height=ctx.height,
            suffix=ctx.suffix, crf=ctx.crf, preset=ctx.preset,
            opts=opts_of(ctx.opts, effect), frame=frame,
        )
        plan = build_plan(effect, sub)
        if plan.width and plan.height:
            frame = (plan.width, plan.height)
        plans.append((effect, plan))
    return plans


def plan_for_chain(chain: Chain, ctx: EffectContext) -> Plan:
    """the merged plan: every chain joined with commas, in chain order"""
    plans = chain_plans(chain, ctx)
    frame = (0, 0)
    for _effect, plan in plans:
        if plan.width and plan.height:
            frame = (plan.width, plan.height)
    return merge_plans(plans, ctx, frame)


def merge_plans(plans: list, ctx: EffectContext, frame: tuple) -> Plan:
    """one Plan out of many: filter chains joined, notes tagged per effect"""
    vf = ",".join(p.vf for _e, p in plans if p.vf)
    af = ",".join(p.af for _e, p in plans if p.af)
    if ctx.suffix:
        suffix = ctx.suffix
    else:
        suffix = "+".join(p.out_suffix.strip("_") for _e, p in plans
                          if p.out_suffix)
        suffix = f"_{suffix}" if suffix else ""

    chained = len(plans) > 1
    notes = []
    if chained:
        notes.append("chain: " + " then ".join(e.name for e, _p in plans))
    for effect, plan in plans:
        for note in plan.notes:
            notes.append(f"{effect.name}: {note}" if chained else note)
    out = []
    for _e, plan in plans:
        out += plan.out_extra
    inn = []
    for _e, plan in plans:
        inn += plan.in_extra
    return Plan(
        vf=vf,
        out_suffix=suffix,
        width=frame[0] if frame else 0,
        height=frame[1] if frame else 0,
        notes=notes,
        in_extra=inn,
        out_extra=out,
        af=af,
        abr=any(p.abr for _e, p in plans),
    )


def coerce_param(param: Param, raw: str) -> tuple:
    """returns (value, error_text)"""
    text = raw.strip()
    if param.kind == "bool":
        low = text.lower()
        if low in ("on", "yes", "true", "1", "y"):
            return True, ""
        if low in ("off", "no", "false", "0", "n"):
            return False, ""
        return None, f"{param.key}: expected on or off"
    if param.kind == "choice":
        for value, _label, _hint in param.choices:
            if text == value:
                return value, ""
        known = ", ".join(v for v, _l, _h in param.choices)
        return None, f"{param.key}: expected one of {known}"
    if param.kind == "int":
        try:
            value = int(text)
        except ValueError:
            return None, (f"{param.key}: expected a whole number "
                          f"{int(param.minimum)}-{int(param.maximum)}")
        if not param.minimum <= value <= param.maximum:
            return None, (f"{param.key}: {value} is outside "
                          f"{int(param.minimum)}-{int(param.maximum)}")
        return value, ""
    if param.kind == "float":
        try:
            value = float(text)
        except ValueError:
            return None, (f"{param.key}: expected a number "
                          f"{param.minimum:g}-{param.maximum:g}")
        if not param.minimum <= value <= param.maximum:
            return None, (f"{param.key}: {value:g} is outside "
                          f"{param.minimum:g}-{param.maximum:g}")
        return value, ""
    if param.kind == "percent":
        try:
            value = int(text.rstrip("%").strip())
        except ValueError:
            return None, f"{param.key}: expected a percent like 40 or 40%"
        if not param.minimum <= value <= param.maximum:
            return None, (f"{param.key}: {value}% is outside "
                          f"{int(param.minimum)}-{int(param.maximum)}%")
        return value, ""
    if param.kind == "multi":
        picked = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if not any(part == v for v, _l, _h in param.choices):
                known = ", ".join(v for v, _l, _h in param.choices)
                return None, f"{param.key}: {part!r} is not one of {known}"
            if part not in picked:
                picked.append(part)
        return picked, ""
    if param.kind == "video":
        return ("none" if text.lower() in ("", "none", "off") else text, "")
    if param.kind == "size":
        return (text, "") if parse_wxh(text) else \
            (None, f"{param.key}: expected WxH like 1920x1080")
    return text, ""


def shape_key(effect: Effect) -> Optional[str]:
    for key in ("target", "shape"):
        if any(p.key == key for p in effect.params):
            return key
    return None


def apply_cli_opts(chain: Chain, args) -> tuple:
    """merge --set / --size into fresh per-effect option dicts.

    returns (opts, error) where opts is {effect_key: {param: value}}. with one
    effect in the chain a bare `--set key=value` behaves exactly as before; in
    a longer chain a key that lives in more than one effect has to be written
    as `effect.key=value`.
    """
    opts = default_chain_opts(chain)
    for raw in args.set or []:
        if "=" not in raw:
            return opts, f"--set wants key=value, got {raw!r}"
        left, _, value = raw.partition("=")
        head, dot, tail = left.strip().partition(".")
        if dot:
            effect = next((e for e in chain.effects if e.key == head), None)
            if effect is None:
                return opts, (f"no {head} in this chain (has: "
                              + ", ".join(e.key for e in chain.effects) + ")")
            param = next((p for p in effect.params if p.key == tail), None)
            if param is None:
                return opts, missing_option(effect, tail)
        else:
            hits = [(e, p) for e, p in chain.rows if p.key == left]
            if not hits:
                if chain.single:
                    return opts, missing_option(chain.effects[0], left)
                return opts, (f"no option {left!r} anywhere in this chain "
                              + "(has: "
                              + "; ".join(e.key + ": "
                                          + (", ".join(p.key for p in e.params)
                                             or "none")
                                          for e in chain.effects) + ")")
            if len(hits) > 1:
                forms = " / ".join(f"{e.key}.{left}=..." for e, _p in hits)
                return opts, (f"{left!r} exists in more than one effect - "
                              f"qualify it: {forms}")
            effect, param = hits[0]
        coerced, problem = coerce_param(param, value)
        if problem:
            return opts, problem
        opts[effect.key][param.key] = coerced
    if args.size_given:
        # the request shape belongs to the first effect that has one - a second
        # shape option further down the chain is reachable with its own --set
        effect = next((e for e in chain.effects if shape_key(e)), None)
        if effect is None:
            return opts, (f"{chain.name} has no shape option for --size")
        opts[effect.key][shape_key(effect)] = f"{args.width}x{args.height}"
    if "inversion" in opts:
        mine = opts["inversion"]
        if mine["offset"] > mine["lookahead"]:
            return opts, "inversion offset must not exceed lookahead seconds"
    return opts, ""


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------

def build_command(chain: Chain, ctx: EffectContext, clip: Clip, tools: Tools,
                  dst: Path) -> tuple:
    plan = plan_for_chain(chain, ctx)
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(clip.path)]
    cmd += plan.in_extra
    cmd += ["-vf", plan.vf]
    if plan.af and clip.audio:
        cmd += ["-af", plan.af]
    cmd += ["-map", "0:v:0"]
    if clip.audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", ctx.preset]
    if not plan.abr:
        cmd += ["-crf", str(ctx.crf)]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd += plan.out_extra
    cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error", str(dst)]
    return cmd, plan


def resolve_output(clip: Clip, args, plan: Plan) -> Path:
    if getattr(args, "output", ""):
        return Path(args.output).expanduser()
    return unique_output(
        Path(args.output_dir) / f"{clip.path.stem}{plan.out_suffix}.mp4",
        got_force(args),
    )


def _bar(fraction: float, cells: int = 24) -> str:
    frac = min(max(fraction, 0.0), 1.0)
    filled = int(frac * cells)
    return (C_FROST + "\u2588" * filled + C_DIM + "\u2591" * (cells - filled)
            + NC)


def run_encode(cmd: list, duration: Optional[float], on_progress,
               animate: bool, input_writer: Optional[Callable] = None) -> tuple:
    """Run the common FFmpeg encoder, optionally streaming frames into stdin."""
    start = time.time()
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE if input_writer is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    out = proc.stdout
    errp = proc.stderr
    if out is None or errp is None:  # pragma: no cover - pipes always requested
        raise RuntimeError("ffmpeg pipes unavailable")
    stderr_lines = []
    feed_errors = []
    feed_stop = threading.Event()
    feeder = None

    def drain():
        for line in errp:
            stderr_lines.append(line.decode("utf-8", "replace").rstrip())

    pump = threading.Thread(target=drain, daemon=True)
    pump.start()

    if input_writer is not None:
        stream = proc.stdin
        if stream is None:
            proc.terminate()
            proc.wait()
            pump.join(timeout=2)
            raise RuntimeError("ffmpeg input pipe unavailable")

        def feed():
            try:
                input_writer(stream, feed_stop)
            except Exception as exc:
                feed_errors.append(str(exc))
                feed_stop.set()
            finally:
                try:
                    stream.close()
                except (BrokenPipeError, OSError) as exc:
                    if not feed_errors:
                        feed_errors.append(str(exc))

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

    try:
        for raw_line in out:
            if not animate:
                continue
            line = raw_line.decode("ascii", "ignore")
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):
                try:
                    done = int(value) / 1_000_000
                except ValueError:
                    continue
                on_progress(done, duration, time.time() - start)
            elif key == "progress" and value == "end":
                on_progress(duration or 0, duration, time.time() - start)
        proc.wait()
    except KeyboardInterrupt:
        feed_stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if feeder is not None:
            feeder.join(timeout=5)
        pump.join(timeout=2)
        raise

    if feeder is not None:
        feeder.join(timeout=10)
        if feeder.is_alive():
            feed_stop.set()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            feeder.join(timeout=2)
            feed_errors.append("frame producer did not stop after FFmpeg exited")
    pump.join(timeout=5)
    returncode = proc.returncode or 0
    if feed_errors and returncode == 0:
        returncode = 1
    error_text = "\n".join(stderr_lines + feed_errors)
    return returncode, error_text, time.time() - start


def run_stateful_chain(chain: Chain, ctx: EffectContext, clip: Clip, tools: Tools,
                       args, dst: Path, run_encode_fn: Callable,
                       on_progress, animate) -> tuple:
    """stream RGB frames through bounded stateful processors between FFmpeg filters."""
    stateful_positions = [i for i, effect in enumerate(chain.effects)
                          if effect.frame_processor is not None]
    if not stateful_positions:
        return False, "no stateful frame processor in this chain", 0.0
    first, last = stateful_positions[0], stateful_positions[-1]
    if any(chain.effects[i].frame_processor is None
           for i in range(first, last + 1)):
        return (False,
                "stateful effects must be adjacent in a chain for now; "
                "place FFmpeg effects before or after the stateful group",
                0.0)
    if not tools.ffprobe or not clip.width or not clip.height:
        return False, "ffprobe is required for stateful frame effects", 0.0
    try:
        import numpy as np
    except ImportError:
        return False, ("Stateful frame effects need NumPy; install "
                       "requirements-stateful.txt"), 0.0

    plans = chain_plans(chain, ctx)
    merged = merge_plans(plans, ctx, (0, 0))
    width, height = _source_dims(clip)
    for _effect, plan in plans[:first]:
        if plan.width and plan.height:
            width, height = plan.width, plan.height
    if not width or not height:
        return False, "could not determine the stateful frame dimensions", 0.0
    fps = _fps_number(clip)
    fps_text = f"{fps:g}"
    processors = []
    try:
        for index in stateful_positions:
            effect, _plan = plans[index]
            factory = effect.frame_processor
            processor = factory()
            opts = opts_of(ctx.opts, effect)
            if factory is ElasticCollapse:
                opts["_clip_duration"] = clip.duration
            seed = int(opts.get("seed", 0) or 0)
            if not seed:
                seed = 20261023 + (clip.size % 1000003)
            processor.initialize(opts, (height, width, 3), fps, seed)
            processors.append(processor)
    except Exception as exc:
        return False, f"could not initialize {chain.name}: {exc}", 0.0

    timestamped = any(getattr(p, "requires_timestamps", False) for p in processors)
    if timestamped:
        try:
            import av  # noqa: F401 - dependency check before launching FFmpeg
        except ImportError:
            return False, "Causality Inversion needs requirements-inversion.txt (PyAV)", 0.0

    prefix = ",".join(p.vf for e, p in plans[:first]
                      if p.vf and e.frame_processor is None)
    suffix = ",".join(p.vf for e, p in plans[last + 1:]
                      if p.vf and e.frame_processor is None)
    decode_filters = [prefix] if prefix else []
    decode_filters += [f"fps={fps_text}", "format=rgb24"]
    decode_cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-i",
                  str(clip.path), "-map", "0:v:0", "-vf",
                  ",".join(decode_filters), "-an", "-fps_mode", "passthrough",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                  f"{width}x{height}", "pipe:1"]

    encode_cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
                  "-f", "rawvideo", "-pixel_format", "rgb24",
                  "-video_size", f"{width}x{height}", "-framerate", fps_text,
                  "-i", "pipe:0"]
    if timestamped:
        decode_cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                      "-nostats", "-copyts", "-i", str(clip.path), "-map", "0:v:0",
                      "-vf", ",".join(([prefix] if prefix else []) + ["format=rgb24"]),
                      "-an", "-c:v", "rawvideo", "-enc_time_base", "demux",
                      "-fps_mode", "passthrough", "-avoid_negative_ts", "disabled",
                      "-f", "nut", "pipe:1"]
        encode_cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-copyts",
                      "-f", "nut", "-i", "pipe:0"]
    if clip.audio:
        encode_cmd += ["-i", str(clip.path)]
    encode_cmd += merged.in_extra
    if suffix:
        encode_cmd += ["-vf", suffix]
    encode_cmd += ["-map", "0:v:0"]
    if clip.audio:
        if merged.af:
            encode_cmd += ["-af", merged.af]
        if timestamped and not merged.af and clip.acodec in ("aac", "mp3", "ac3", "eac3", "alac", "opus"):
            encode_cmd += ["-map", "1:a:0?", "-c:a", "copy"]
        else:
            encode_cmd += ["-map", "1:a:0?", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        encode_cmd += ["-an"]
    encode_cmd += ["-c:v", "libx264", "-preset", ctx.preset]
    if not merged.abr:
        encode_cmd += ["-crf", str(ctx.crf)]
    encode_cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if timestamped:
        encode_cmd += ["-fps_mode", "passthrough", "-enc_time_base", "demux",
                       "-avoid_negative_ts", "disabled"]
    encode_cmd += merged.out_extra
    encode_cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error",
                   str(dst)]

    def read_frame(pipe, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = pipe.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def stop(proc) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def feed_frames(encoder_input, stop_event) -> None:
        decoder = None
        decoder_errors = []
        error_thread = None

        def drain_decoder(pipe):
            for line in iter(pipe.readline, b""):
                decoder_errors.append(line.decode("utf-8", "replace").rstrip())
                del decoder_errors[:-40]

        try:
            decoder = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, bufsize=0)
            decode_out, decode_err = decoder.stdout, decoder.stderr
            if decode_out is None or decode_err is None:
                raise RuntimeError("FFmpeg decoder pipes unavailable")
            error_thread = threading.Thread(
                target=drain_decoder, args=(decode_err,), daemon=True)
            error_thread.start()
            index = 0
            if timestamped:
                from frame_pipeline import process_packets
                from timestamped_frames import read_packets, write_packets
                packets = read_packets(decode_out, {"clip": clip, "fps": fps}, stop_event)
                index = write_packets(process_packets(packets, processors), encoder_input,
                                      (height, width, 3), fps, stop_event)
            frame_bytes = width * height * 3
            while not timestamped and not stop_event.is_set():
                raw = read_frame(decode_out, frame_bytes)
                if stop_event.is_set():
                    break
                if not raw:
                    break
                if len(raw) != frame_bytes:
                    raise RuntimeError("decoder returned a partial RGB frame")
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (height, width, 3))
                timestamp = index / fps
                for processor in processors:
                    frame = processor.process(
                        frame, index, timestamp,
                        {"clip": clip, "fps": fps, "frame_index": index},
                    )
                if (not isinstance(frame, np.ndarray)
                        or frame.shape != (height, width, 3)
                        or frame.dtype != np.uint8):
                    raise RuntimeError(
                        "stateful processor returned an invalid RGB frame")
                payload = memoryview(np.ascontiguousarray(frame)).cast("B")
                while payload and not stop_event.is_set():
                    written = encoder_input.write(payload)
                    if written is None or written <= 0:
                        if stop_event.is_set():
                            break
                        raise BrokenPipeError(
                            "FFmpeg encoder stopped reading frames")
                    payload = payload[written:]
                index += 1
            if stop_event.is_set():
                stop(decoder)
                return
            decode_rc = decoder.wait()
            error_thread.join(timeout=5)
            stderr = "\n".join(decoder_errors)
            if decode_rc != 0:
                raise RuntimeError(stderr or f"FFmpeg decoder exited {decode_rc}")
            if index == 0:
                raise RuntimeError("decoder produced no video frames")
        except Exception as exc:
            stop(decoder)
            if error_thread is not None:
                error_thread.join(timeout=2)
            detail = f"stateful frame decode failed: {exc}"
            if decoder_errors:
                detail += "\n" + "\n".join(decoder_errors)
            raise RuntimeError(detail) from exc
        finally:
            for processor in processors:
                processor.reset()
            if decoder is not None:
                for pipe in (decoder.stdin, decoder.stdout, decoder.stderr):
                    if pipe is not None and not pipe.closed:
                        try:
                            pipe.close()
                        except OSError:
                            pass
                if decoder.poll() is None:
                    stop(decoder)

    returncode, stderr, elapsed = run_encode_fn(
        encode_cmd, clip.duration, on_progress, animate,
        input_writer=feed_frames,
    )
    return returncode == 0, stderr, elapsed


def drop_partial(dst: Path, source: Path) -> None:
    """a cancelled or failed render must not leave a half file behind"""
    try:
        if dst.exists() and dst.resolve() != source.resolve():
            dst.unlink()
    except OSError:
        pass


def render_preview(chain: Chain, clip: Clip, tools: Tools, args,
                   opts: dict) -> tuple:
    """a ~3 second taste from the middle of the clip, into output/.

    returns (ok: bool, name_or_error: str). uses the same pipeline as a full
    render, just with a seek and a cap on the duration.
    """
    if chain.multi_pass is not None or chain.stateful_effects:
        return False, "preview is not available for stateful or multi-pass effects"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ctx = EffectContext(clip=clip, width=args.width, height=args.height,
                        suffix=args.suffix, crf=args.crf, preset=args.preset,
                        opts=opts)
    plan = plan_for_chain(chain, ctx)
    mid = (clip.duration / 2 - 1.5) \
        if (clip.duration and clip.duration > 4) else 0.0
    take = min(3.0, clip.duration) if clip.duration else 3.0
    dst = unique_output(
        Path(args.output_dir) / f"{clip.path.stem}{plan.out_suffix}"
                                f"_PREVIEW.mp4",
        False,
    )

    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y",
           "-ss", f"{mid:.2f}", "-i", str(clip.path)]
    cmd += plan.in_extra
    cmd += ["-t", f"{take:.2f}", "-vf", plan.vf]
    if plan.af and clip.audio:
        cmd += ["-af", plan.af]
    cmd += ["-map", "0:v:0"]
    if clip.audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", ctx.preset,
        *([] if plan.abr else ["-crf", str(ctx.crf)]),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(dst),
    ]

    animate = (not _QUIET) and sys.stdout.isatty()
    last = {"len": 0}

    def on_progress(done, total, elapsed):
        if not animate:
            return
        if total:
            frac = done / total
            stamp = f"{human_time(done)} / {human_time(take)}"
            pct = f"{int(frac * 100):3d}%"
        else:
            frac, stamp, pct = 0.0, human_time(elapsed), "  ?"
        row = f"  {_bar(frac)}  {B}{pct}{NC}  {C_DIM}{stamp}{NC}"
        sys.stdout.write("\r" + row + _CLEAR_EOL)
        sys.stdout.flush()
        last["len"] = vlen(row)

    try:
        rc, stderr, _elapsed = run_encode(cmd, take, on_progress, animate)
    except KeyboardInterrupt:
        if animate:
            sys.stdout.write("\r" + " " * last["len"] + "\r")
            sys.stdout.flush()
        drop_partial(dst, clip.path)
        return False, "preview cancelled"
    if animate:
        sys.stdout.write("\r" + " " * last["len"] + "\r")
        sys.stdout.flush()

    if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
        drop_partial(dst, clip.path)
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else "?"
        return False, f"preview failed: {tail}"
    return True, dst.name


last_render: dict = {}


def process(chain: Chain, clip: Clip, tools: Tools, args,
            opts_override: Optional[dict] = None) -> bool:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if opts_override is not None:
        opts = dict(opts_override)
    else:
        opts, problem = apply_cli_opts(chain, args)
        if problem:
            err(problem)
            return False
    ctx = EffectContext(clip=clip, width=args.width, height=args.height,
                        suffix=args.suffix, crf=args.crf, preset=args.preset,
                        opts=opts)
    plan = plan_for_chain(chain, ctx)
    dst = resolve_output(clip, args, plan)

    if getattr(args, "output", "") and dst.exists() and not got_force(args):
        err(f"{dst} already exists - add --force to overwrite")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not clip.duration or clip.duration <= 0:
        clip = probe(clip.path, tools)
    render_duration = float(clip.duration or 0.0)

    pass_effect = chain.multi_pass
    runner: Optional[Callable] = None
    if pass_effect is not None:
        # a multi-pass effect never shares a chain (parse_chain refuses it),
        # but its run callback reads its options flat off the context
        runner = pass_effect.run
        ctx.opts = opts_of(opts, pass_effect)
        if pass_effect.key == "datamosh":
            second_path, _problem = resolve_second_video(
                ctx.opts.get("second", "none"), Path(args.input_dir), clip.path)
            if second_path:
                second_clip = probe(second_path, tools)
                start = float(ctx.opts.get("second_start", 0.0))
                length = float(ctx.opts.get("second_length", 0.0))
                render_duration += length or max(
                    0.0, float(second_clip.duration or 0.0) - start)
    elif chain.stateful_effects:
        def stateful_runner(run_ctx, run_clip, run_tools, run_args, run_dst,
                            run_encode, on_progress, animate):
            return run_stateful_chain(chain, run_ctx, run_clip, run_tools,
                                      run_args, run_dst, run_encode,
                                      on_progress, animate)
        runner = stateful_runner
    cmd: list = []
    if runner is not None:
        if args.dry_run:
            for line in chain.stage_lines:
                _say("  " + line)
            if pass_effect is not None and pass_effect.key == "datamosh":
                for param in pass_effect.params:
                    value = opts_of(opts, pass_effect)[param.key]
                    _say(f"  {param.key}={param.display(value)}")
                second_raw = opts_of(opts, pass_effect).get("second", "none")
                second_path, _problem = resolve_second_video(
                    second_raw, Path(args.input_dir), clip.path)
                if second_path:
                    _say("  second source: " + str(second_path))
            elif chain.stateful_effects:
                for effect in chain.stateful_effects:
                    values = opts_of(opts, effect)
                    for param in effect.params:
                        _say(f"  {effect.key}.{param.key}="
                             f"{param.display(values[param.key])}")
            return True
    else:
        cmd, plan = build_command(chain, ctx, clip, tools, dst)
        if args.dry_run:
            _say("  " + " ".join(cmd))
            return True

    animate = (not args.json) and (not _QUIET) and sys.stdout.isatty()
    fps_value = _fps_number(clip)

    def draw_render_monitor(done: float, total: Optional[float],
                            elapsed: float) -> None:
        duration = total or render_duration or None
        fraction = (max(0.0, min(1.0, done / duration))
                    if duration else 0.0)
        percent = int(fraction * 100)
        cells = max(12, min(34, term_cols() - 32))
        filled = min(cells, int(round(fraction * cells)))
        cursor = ">" if filled < cells else ""
        bar = ("[" + C_FROST + "=" * filled + cursor + NC
               + C_DIM + "." * (cells - filled - len(cursor)) + NC + "]")
        if duration:
            total_frames = int(round(duration * fps_value))
            current_frames = min(total_frames,
                                 max(0, int(round(done * fps_value))))
            frames_text = f"~{current_frames:,} / ~{total_frames:,} frames"
        else:
            frames_text = f"~{int(max(0.0, done) * fps_value):,} frames"
        speed = done / elapsed if elapsed > 0 and done > 0 else 0.0
        remaining = ((max(0.0, duration - done) / speed)
                     if duration and speed > 0 else None)
        if not duration:
            remaining_text = "unknown"
        else:
            remaining_text = human_time(remaining) if remaining is not None \
                else "estimating"
        if elapsed < 0.35 and done <= 0:
            activity = "initializing FFmpeg and effect stages"
        elif duration and done >= duration - 0.25:
            activity = "flushing the video and audio streams"
        else:
            activity = "processing frames and writing the render"
        engine = ("stateful CPU frame pass" if chain.stateful_effects
                  else "effect-specific passes" if chain.multi_pass is not None
                  else "FFmpeg filters")
        flow = trunc("[SOURCE] -> [EFFECTS] -> [ENCODE] -> [OUTPUT]",
                     term_cols() - 6)
        full_body = [
            kv("input", trunc(clip.path.name, term_cols() - 20), 10),
            kv("effect", chain.name, 10),
            kv("output", trunc(dst.name, term_cols() - 20), 10),
            "",
            "  " + B + C_FROST + flow.strip() + NC,
            kv("pipeline", engine + "  ·  " + args.preset, 10),
            "",
            "  " + bar + f"  {percent:3d}%",
            kv("frames", frames_text, 10),
            kv("elapsed", human_time(elapsed), 10),
            kv("remaining", "~" + remaining_text, 10),
            kv("speed", f"{speed:.2f}x realtime" if speed else "warming up", 10),
            "",
            kv("now", activity, 10),
            kv("source", f"{clip.res}  ·  {fps_value:g} fps", 10),
            kv("source audio", clip.acodec or "none", 10),
            kv("settings", option_line(chain, opts), 10),
        ]
        if term_rows() < 17:
            body = [full_body[4], full_body[7], full_body[8],
                    full_body[9] + "   " + full_body[10].strip(),
                    full_body[13]]
        elif term_rows() < 23 or term_cols() < 70:
            body = [full_body[i] for i in
                    (0, 1, 2, 3, 4, 7, 8, 9, 10, 13, 14, 15)]
        else:
            body = full_body
        draw(build_frame(f"rendering  ·  {chain.name}", body,
                         ["ctrl+c to cancel"],
                         note=trunc(clip.path.name, 28)))

    def on_progress(done: float, total: Optional[float], elapsed: float) -> None:
        if animate:
            draw_render_monitor(done, total, elapsed)

    if not args.json:
        draw_render_monitor(0.0, clip.duration or render_duration or None, 0.0)

    if runner is not None:
        try:
            okk, stderr, elapsed = runner(ctx, clip, tools, args, dst,
                                          run_encode, on_progress, animate)
        except KeyboardInterrupt:
            drop_partial(dst, clip.path)
            warn("cancelled, partial file removed")
            return False
        except Exception as exc:
            drop_partial(dst, clip.path)
            err(f"{chain.name} failed: {exc}")
            return False
        if not okk:
            drop_partial(dst, clip.path)
            err("ffmpeg failed, partial file removed")
            for line in stderr.splitlines()[-6:]:
                print("    " + C_DIM + line + NC, file=sys.stderr)
            return False
    else:
        try:
            rc, stderr, elapsed = run_encode(cmd, clip.duration, on_progress,
                                             animate)
        except KeyboardInterrupt:
            drop_partial(dst, clip.path)
            warn("cancelled, partial file removed")
            return False

        if rc != 0:
            drop_partial(dst, clip.path)
            err(f"ffmpeg failed (exit {rc}), partial file removed")
            for line in stderr.splitlines()[-6:]:
                print("    " + C_DIM + line + NC, file=sys.stderr)
            return False

    if not dst.exists() or dst.stat().st_size == 0:
        err("ffmpeg reported success but no output was written")
        return False

    out_size = dst.stat().st_size
    speed = (render_duration / elapsed) if (render_duration and elapsed > 0) \
        else None
    last_render.update({"path": str(dst), "name": dst.name, "size": out_size,
                        "seconds": elapsed, "speed": speed})

    if args.json:
        payload = {
            "ok": True,
            "effect": chain.effects[-1].key if chain.single else chain.key,
            "options": opts[chain.effects[0].key] if chain.single else opts,
            "input": str(clip.path),
            "output": str(dst),
            "bytes": out_size,
            "seconds": round(elapsed, 2),
            "width": plan.width,
            "height": plan.height,
        }
        if not chain.single:
            payload["chain"] = [e.key for e in chain.effects]
        _say(json.dumps(payload))
        return True

    ok(f"wrote {B}{dst.name}{NC}  {C_DIM}({human_size(out_size)}, "
       f"{elapsed:.1f}s" + (f", {speed:.2f}x realtime" if speed else "")
       + ")" + NC)
    if not _QUIET:
        hint(str(dst))
    return True


# ---------------------------------------------------------------------------
# terminal: geometry, key input
# ---------------------------------------------------------------------------

def terminal_size() -> os.terminal_size:
    """Use the live TTY size so stale COLUMNS/LINES env vars cannot clip frames."""
    if sys.stdout.isatty():
        try:
            return os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            pass
    return shutil.get_terminal_size((84, 24))


def term_cols() -> int:
    return max(1, terminal_size().columns)


def term_rows() -> int:
    return max(1, terminal_size().lines)


def _read_key(fd: int) -> str:
    raw = os.read(fd, 1)
    if not raw:
        raise EOFError("stdin closed")
    ch = raw.decode("utf-8", "ignore")
    if ch != "\x1b":
        return ch
    import select
    seq = ""
    while select.select([fd], [], [], 0.03)[0]:
        nxt = os.read(fd, 1)
        if not nxt:
            break
        seq += nxt.decode("utf-8", "ignore")
        if len(seq) >= 2:
            break
    return {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
            "OA": "up", "OB": "down", "OC": "right", "OD": "left"}.get(
                seq, "esc")


class RawSession:
    """holds the terminal in cbreak + no-echo for the whole interactive run.

    toggling modes per keypress is wrong: bytes typed while the tty is back in
    canonical mode get line-buffered, so an escape sequence sent as ESC O C can
    be delivered as garbage (a down arrow reading as enter). one session, and a
    temporary hand-back only for cooked line edits.
    """

    def __init__(self, fd: int = 0):
        self.fd = fd
        self.saved = None
        self.active = False

    def enter(self) -> None:
        if self.active or not sys.stdin.isatty():
            return
        import termios
        import tty
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        new = termios.tcgetattr(self.fd)
        new[3] &= ~termios.ECHO
        termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
        self.active = True

    def leave(self) -> None:
        import termios
        if self.active and self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        self.active = False

    @contextmanager
    def cooked(self):
        """hand the terminal back so a normal line-editing prompt works"""
        was_active = self.active
        if was_active:
            self.leave()
        try:
            yield
        finally:
            if was_active:
                self.enter()


TERM = RawSession()


def read_one_key() -> str:
    """one keypress.

    inside the menu session the terminal is already in cbreak, so just read.
    outside it (the ffmpeg install prompt, which must leave the tty cooked so
    sudo can ask for a password) take cbreak for exactly this one key.
    """
    if TERM.active or not sys.stdin.isatty():
        return _read_key(0)
    TERM.enter()
    try:
        return _read_key(0)
    finally:
        TERM.leave()


def wait_key() -> None:
    """hold a one-shot frame on screen until a key arrives"""
    if not sys.stdin.isatty():
        return
    try:
        read_one_key()
    except (EOFError, KeyboardInterrupt):
        pass


def ask_text(prompt: str, default: str = "") -> Optional[str]:
    """line-input prompt; hands the terminal back to cooked mode to do it"""
    tail = C_DIM + f"[{default}] " + NC if default else ""
    try:
        with TERM.cooked():
            typed = input(f"  {B}{prompt}{NC} {tail}").strip()
    except (EOFError, KeyboardInterrupt):
        _say()
        return None
    return typed or default


def ask_yes_no(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        try:
            raw = input(f"  {question} [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _say()
            return False
        if not raw:
            return default
        return raw[0] == "y"

    tags = "y/n" if default else "y/N"
    sys.stdout.write(f"  {B}{question}{NC} {C_DIM}[{tags}]{NC} ")
    sys.stdout.flush()
    answer = default
    try:
        while True:
            key = read_one_key().lower()
            if key in ("\r", "\n"):
                answer = default
                break
            if key == "y":
                answer = True
                break
            if key in ("n", "q", "\x03", "esc"):
                answer = False
                break
    except (EOFError, KeyboardInterrupt):
        _say()
        return False
    _say((C_GRN + "yes" + NC) if answer else (C_RED + "no" + NC))
    return answer


# ---------------------------------------------------------------------------
# frames - one clean screen per state, written in a single pass
# ---------------------------------------------------------------------------

def build_frame(crumb: str, body: list, keys: list, note: str = "",
                notice: str = "") -> str:
    """the complete screen. every line fits inside the terminal."""
    cols = term_cols()
    width = cols - 4
    rule = C_DIM + "\u2500" * width + NC
    left = B + C_FROST + "effex" + NC + C_DIM + " \u203a " + NC + crumb
    right = C_DIM + (note or f"v{VERSION}") + NC
    gap = width - vlen(left) - vlen(right)
    if gap < 2:
        right = ""
        gap = max(1, width - vlen(left))
    head = "  " + left + " " * gap + right
    lines = [head, "  " + rule, ""]
    lines += body
    if notice:
        lines.append("")
        lines.append("  " + trunc(notice, width - 2))
    lines.append("")
    lines.append("  " + rule)
    footer = "   ".join(keys)
    lines.append("  " + C_DIM + trunc(footer, width) + NC)
    return "\n".join(lines)


def draw(frame: str) -> None:
    """home the cursor, draw the frame, wipe whatever is left under it"""
    if not sys.stdout.isatty():
        _say(frame)
        return
    sys.stdout.write("\033[H" + frame + "\n\033[J")
    sys.stdout.flush()


def start_frame(title: str, body: list, keys: list, note: str = "",
                notice: str = "") -> None:
    draw(build_frame(title, body, keys, note=note, notice=notice))


def row(text: str, selected: bool) -> str:
    """a list row.

    padded to the same width whether or not it is highlighted: that keeps the
    columns lined up and lets the reverse bar cover a whole row.
    """
    width = term_cols() - 4
    if vlen(text) > width:
        text = trunc(text, width)
    text = pad(text, width)
    if selected:
        return "  " + REV + B + text + NC + REV_OFF
    return "  " + text


def multi_pick(crumb: str, param: Param, current: list) -> Optional[list]:
    """checkbox list: space toggles, enter confirms, q/esc keeps the
    current selection untouched"""
    if not sys.stdin.isatty():
        return current
    picked = [v for v in current if v in (c for c, _l, _h in param.choices)]
    items = [(v, lbl, hint) for v, lbl, hint in param.choices]
    idx, top = 0, 0
    page = max(4, term_rows() - 8)

    while True:
        if idx < top:
            top = idx
        elif idx >= top + page:
            top = idx - page + 1
        visible = items[top:top + page]
        body = []
        for n, (v, lbl, hint) in enumerate(visible):
            mark = C_GRN + "\u2713" + NC if v in picked \
                else C_DIM + "\u00b7" + NC
            text = ("  " + mark + " " + fit(lbl, 14) + " " + C_DIM
                    + trunc(hint, term_cols() - 24) + NC)
            body.append(row(text, (top + n) == idx))
        if len(items) > page:
            body.append("  " + C_DIM
                        + f"\u2026 {len(items) - top - len(visible)} more"
                        + NC)
        draw(build_frame(crumb, body,
                         ["\u2191\u2193 move", "space toggle",
                          "enter done", "q keep"],
                         note=param.help))
        try:
            key = read_one_key()
        except EOFError:
            return None
        if key in ("up", "k"):
            idx = (idx - 1) % len(items)
        elif key in ("down", "j"):
            idx = (idx + 1) % len(items)
        elif key == " ":
            v = items[idx][0]
            picked = ([x for x in picked if x != v] if v in picked
                      else picked + [v])
        elif key in ("\r", "\n"):
            return picked
        elif key in ("q", "esc", "\x03"):
            return None


def option_line(chain: Chain, opts: dict) -> str:
    """one line of settings; a chain tags each effect's values with its name"""
    parts = []
    for effect in chain.effects:
        mine = opts_of(opts, effect)
        text = " \u00b7 ".join(f"{p.label} {p.display(mine[p.key])}"
                               for p in effect.params)
        if not text:
            continue
        parts.append(text if chain.single else f"{effect.name}: {text}")
    return trunc(" \u00b7 ".join(parts), term_cols() - 18)


def clip_meta(clip: Clip, cols: int) -> str:
    bits = [clip.res, human_time(clip.duration), human_size(clip.size)]
    if cols >= 96:
        bits.insert(1, (clip.vcodec or "?")
                    + (f" + {clip.acodec}" if clip.acodec else " (no audio)"))
    return "  ".join(bits)


# ---------------------------------------------------------------------------
# list screen
# ---------------------------------------------------------------------------

@dataclass
class Item:
    label: str
    hint: str = ""
    meta: str = ""
    value: object = None


def select(crumb: str, items: list, row_fn: Callable, keys: list,
           note: str = "", back_hint: str = "back",
           rescan: Optional[Callable] = None, notice: str = "",
           extra: Optional[dict] = None,
           jump: Optional[dict] = None,
           intro: Optional[list] = None) -> object:
    """arrow-key list. returns the picked value, or None to go back"""
    if not sys.stdin.isatty():
        return _select_numbered(crumb, items)

    idx, top = 0, 0
    page = (max(1, term_rows() - 8 - len(intro)) if intro
            else max(4, term_rows() - 8))

    while True:
        if idx < top:
            top = idx
        elif idx >= top + page:
            top = idx - page + 1
        visible = items[top:top + page]
        body = [row(line, False) for line in (intro or [])] + [
            row_fn(item, top + n, (top + n) == idx)
            for n, item in enumerate(visible)
        ]
        if len(items) > page:
            body.append("  " + C_DIM
                        + f"\u2026 {len(items) - top - len(visible)} more" + NC)
        footer = ["\u2191\u2193 move", "enter select"]
        if rescan:
            footer.append("r rescan")
        footer.append(f"q {back_hint}")
        footer += keys
        draw(build_frame(crumb, body, footer, note=note, notice=notice))
        try:
            key = read_one_key()
        except EOFError:
            return None

        if key in ("up", "k"):
            idx = (idx - 1) % len(items)
        elif key in ("down", "j"):
            idx = (idx + 1) % len(items)
        elif key in ("\r", "\n", " "):
            return items[idx].value
        elif key in ("q", "esc", "\x03"):
            return None
        elif extra and key in extra:
            return extra[key]
        elif rescan and key == "r":
            fresh = rescan()
            if fresh:
                items[:] = fresh
                idx = min(idx, len(items) - 1)
        elif key.isdigit() and 1 <= int(key) <= min(9, len(items)):
            return items[int(key) - 1].value
        elif jump and key in jump:
            return items[jump[key]].value


def _select_numbered(crumb: str, items: list) -> object:
    """plain fallback for a non-tty, so scripts never block on a menu"""
    _say(f"  {crumb}")
    for i, it in enumerate(items, 1):
        extra = it.meta or it.hint
        _say(f"  {i:>2}. {it.label}" + (f"  {extra}" if extra else ""))
    while True:
        try:
            raw = input("  number (empty = back): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1].value
        warn("pick one of the listed numbers")


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------

def screen_main(tools: Tools, args) -> str:
    videos = len(scan_videos())
    bits = ["ffmpeg ok" if tools.ffmpeg else "ffmpeg missing",
            f"{videos} in input/", f"{count_renders()} in output/"]
    items = [
        Item("process a video", hint="pick a clip, build an effect chain, render",
             value="process"),
        Item("effects", hint="browse effect options and descriptions",
             value="effects"),
        Item("system check", hint="ffmpeg, ffprobe, folders", value="check"),
        Item("quit", hint="leave effex", value="quit"),
    ]
    intro = [
        B + C_FROST + "[SOURCE] -> [EFFECTS] -> [OUTPUT]" + NC,
        C_DIM + "pick a clip, shape a look, render it to output/" + NC,
        "",
    ]
    note = "  ·  ".join(bits)
    if not videos:
        note = "input/ is empty \u00b7 " + note

    def row_fn(it: Item, index: int, sel: bool) -> str:
        return row("  " + fit(it.label, 16) + "  " + C_DIM + it.hint + NC,
                   sel)

    picked = select("main menu", items, row_fn, keys=["? help"], note=note,
                    back_hint="quit", extra={"?": "help"},
                    jump={"p": 0, "e": 1, "c": 2, "q": 3},
                    intro=intro)
    return "quit" if picked is None else str(picked)


def screen_help(args) -> None:
    width = term_cols() - 6
    body = [
        kv("workflow", "pick a video, pick an effect, set its options, "
                       "confirm, render", 12),
        "",
        kv("keys", "\u2191\u2193 move \u00b7 enter select \u00b7 q back",
           12),
        kv("", "\u2190\u2192 change a value in place", 12),
        kv("", "space chains an effect (chip 1, 2, 3 = run order)", 12),
        kv("", "c clears the chain, enter runs what you chipped", 12),
        kv("", "numbers quick-pick an effect; [/] jumps between effect groups", 12),
        kv("", "p / e / c jump to a main menu entry", 12),
        kv("r", "rescan input/ while picking a video", 12),
        "",
        kv("cli", "./effex.py stretch clip.mp4 --set target=16:9", 12),
        kv("", "./effex.py pixelize clip.mp4 --set chunk=16", 12),
        kv("chain", "./effex.py stretch+deepfry clip.mp4", 12),
        kv("", "--set deepfry.strength=60   (key=value when both effects "
           "have it)", 12),
        kv("folders", f"{INPUT_DIR.name}/ in, {OUTPUT_DIR.name}/ out", 12),
        kv("", "EFFEX_INPUT_DIR / EFFEX_OUTPUT_DIR override both", 12),
    ]
    start_frame("help", [trunc(line, width) for line in body], ["enter back"])
    wait_key()


def screen_effects_info(args) -> None:
    width = term_cols() - 6
    body = []
    for effect in EFFECTS:
        body.append("  " + B + C_FROST + effect.name + NC + "   "
                    + C_DIM + effect.blurb + NC)
        if effect.run is not None:
            body.append("      " + C_DIM + "own passes - cannot be chained"
                        + NC)
        elif effect.frame_processor is not None:
            body.append("      " + C_DIM + "stateful CPU frame pass - bounded history"
                        + NC)
        for param in effect.params:
            extra = f"  {param.range_text()}" if param.range_text() else ""
            body.append("      " + fit(param.label, 13) + "  default "
                        + C_FROST + param.display(param.default) + NC
                        + C_DIM + extra + NC)
        body.append("")
    body.append("  " + C_DIM + "these options open right after you pick an "
                "effect" + NC)
    body.append("  " + C_DIM + "space chains effects: 1, 2, 3 is the order "
                "they run in" + NC)
    start_frame("effects", [trunc(line, width) for line in body],
                ["enter back"])
    wait_key()


def screen_check(tools: Tools, interactive: bool) -> Tools:
    width = term_cols() - 6
    body = []
    if tools.ffmpeg:
        body.append("  " + C_GRN + "\u2713" + NC + " ffmpeg   " + C_DIM
                    + trunc(tools.ffmpeg, width - 12) + NC)
    else:
        body.append("  " + C_RED + "\u2717" + NC + " ffmpeg   not on PATH")
    if tools.ffprobe:
        body.append("  " + C_GRN + "\u2713" + NC + " ffprobe  " + C_DIM
                    + trunc(tools.ffprobe, width - 12) + NC)
    else:
        body.append("  " + C_AMB + "\u26a0" + NC + " ffprobe  missing, "
                    "durations show as --:--")
    if not tools.ok:
        TERM.leave()
        tools = install_ffmpeg(tools, interactive)
        TERM.enter()
    for folder in (INPUT_DIR, OUTPUT_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        count = len(scan_videos(folder)) if folder == INPUT_DIR \
            else count_renders(folder)
        body.append("  " + C_GRN + "\u2713" + NC + " " + fit(folder.name, 8)
                    + C_DIM + trunc(str(folder), width - 24) + NC
                    + f"   {count} file{'' if count == 1 else 's'}")
    start_frame("system check", body, ["enter back"])
    wait_key()
    return tools


def screen_pick_video(tools: Tools) -> Optional[Path]:
    def build() -> list:
        rows = []
        for path in scan_videos():
            clip = probe(path, tools)
            rows.append(Item(path.name, meta=clip_meta(clip, term_cols()),
                             value=path))
        return rows

    items = build()
    if not items:
        while True:
            start_frame("pick a video", [
                "  " + C_AMB + "no videos in " + trunc(str(INPUT_DIR),
                                                        term_cols() - 22) + NC,
                "",
                "  " + C_DIM + "drop a video in that folder, then press r" + NC,
            ], ["r rescan", "q main menu"],
                notice="mp4 mov mkv webm avi m4v flv ts 3gp all work")
            if not sys.stdin.isatty():
                return None
            try:
                key = read_one_key()
            except (EOFError, KeyboardInterrupt):
                return None
            if key == "r":
                items = build()
                if items:
                    break
            else:
                return None

    def row_fn(it: Item, index: int, sel: bool) -> str:
        cols = term_cols()
        name_w = 34 if cols >= 96 else 24 if cols >= 76 else 16
        return row("  " + fit(it.label, name_w) + "  " + C_DIM + it.meta + NC,
                   sel)

    picked = select("pick a video", items, row_fn, keys=[],
                    note=f"{len(items)} in input/", back_hint="main menu",
                    rescan=build)
    return picked if isinstance(picked, Path) else None


NOT_CHAINABLE = "runs its own multi-pass pipeline - nothing to chain it with"


def screen_pick_effects(clip: Clip) -> Optional[Chain]:
    """pick a visual effect by section, or chain several with space."""
    grouped_effects = []
    items = []
    visual_rows = []
    selectable_positions = []
    group_starts = []
    for title, keys in EFFECT_GROUPS:
        group_effects = [EFFECT_BY_KEY[key] for key in keys]
        group_starts.append(len(grouped_effects))
        visual_rows.append(("group", title, len(group_effects)))
        for effect in group_effects:
            ordinal = len(grouped_effects)
            grouped_effects.append(effect)
            items.append(Item(effect.name, hint=effect.blurb, value=effect))
            selectable_positions.append(len(visual_rows))
            visual_rows.append(("effect", ordinal))

    note = (f"{trunc(clip.path.name, 30)} \u00b7 {clip.res}"
            + C_DIM + "  \u00b7 space chains several" + NC)

    def row_fn(it: Item, index: int, sel: bool, mark: str = "") -> str:
        cols = term_cols()
        picked_effect = it.value if isinstance(it.value, Effect) else None
        count = len(picked_effect.params) if picked_effect else 0
        marker = C_FROST + ">" + NC if sel else " "
        text = ("  " + mark + " " + marker + " "
                + C_FROST + f"{index + 1:02d}" + NC
                + " " + fit(it.label, 15) + "  "
                + C_DIM + fit(it.hint, 34 if cols >= 96 else 0) + NC)
        if cols >= 96:
            text += C_DIM + f"{count} option"
            text += "" if count == 1 else "s"
            if picked_effect is not None and picked_effect.run is not None:
                text += "  \u00b7 own passes"
            elif picked_effect is not None and picked_effect.frame_processor is not None:
                text += "  \u00b7 stateful"
            text += NC
        return row(text, sel)

    if not sys.stdin.isatty():
        picked = select("pick an effect", items, row_fn, keys=[], note=note,
                        back_hint="videos")
        return one(picked) if isinstance(picked, Effect) else None

    idx, top = 0, 0
    order: list = []
    notice = ""
    page = max(4, term_rows() - 8)

    def marked(effect: Effect) -> str:
        if effect not in order:
            return C_DIM + "\u00b7" + NC
        return C_FROST + B + str(order.index(effect) + 1) + NC

    def group_for(effect_index: int) -> int:
        return max(i for i, start in enumerate(group_starts)
                   if start <= effect_index)

    while True:
        selected_row = selectable_positions[idx]
        anchor = selected_row - 1 if idx in group_starts else selected_row
        if anchor < top:
            top = max(0, anchor)
        if selected_row >= top + page:
            top = selected_row - page + 1
        visible = visual_rows[top:top + page]
        body = []
        for visual_index, entry in enumerate(visible, top):
            if entry[0] == "group":
                _kind, title, count = entry
                body.append("  " + B + C_FROST + title + NC + "  "
                            + C_DIM + f"{count} effects" + NC)
                continue
            _kind, effect_index = entry
            item = items[effect_index]
            body.append(row_fn(item, effect_index,
                               effect_index == idx,
                               marked(cast(Effect, item.value))))
        if len(visual_rows) > page:
            body.append("  " + C_DIM
                        + f"\u2026 {len(visual_rows) - top - len(visible)} more rows"
                        + NC)
        chain_note = (C_DIM + "chain " + NC + C_FROST
                      + " \u2192 ".join(e.name for e in order) + NC
                      + C_DIM + f"   {len(order)} effect"
                      + ("" if len(order) == 1 else "s") + NC) if order else ""
        banner = "   ".join(part for part in (chain_note, notice) if part)
        draw(build_frame("pick an effect", body,
                         ["\u2191\u2193 move", "[/] group", "1-9 pick",
                          "space chain", "enter ok", "c clear", "q videos"],
                         note=note, notice=banner))
        try:
            key = read_one_key()
        except EOFError:
            return None
        notice = ""
        if key in ("up", "k"):
            idx = (idx - 1) % len(grouped_effects)
        elif key in ("down", "j"):
            idx = (idx + 1) % len(grouped_effects)
        elif key in ("[", "{"):
            idx = group_starts[(group_for(idx) - 1) % len(group_starts)]
        elif key in ("]", "}"):
            idx = group_starts[(group_for(idx) + 1) % len(group_starts)]
        elif key == " ":
            effect = grouped_effects[idx]
            if effect in order:
                order = [e for e in order if e is not effect]
            elif effect.run is not None:
                notice = C_AMB + f"{effect.name} {NOT_CHAINABLE}" + NC
            elif len(order) >= 9:
                notice = C_AMB + "nine effects is the limit for one chain" + NC
            else:
                order = order + [effect]
        elif key == "c":
            order = []
        elif key in ("\r", "\n"):
            if order:
                return Chain(tuple(order))
            return one(grouped_effects[idx])
        elif key in ("q", "esc", "\x03"):
            return None
        elif key.isdigit() and 1 <= int(key) <= min(9, len(grouped_effects)):
            return one(grouped_effects[int(key) - 1])


@dataclass
class OptRow:
    kind: str                            # head | param | act
    effect: Optional[Effect] = None
    param: Optional[Param] = None
    action: str = ""


def option_rows(chain: Chain) -> list:
    """the options screen as rows: a header per effect (a longer chain only),
    then every option, then render and back"""
    rows = []
    for effect in chain.effects:
        if not chain.single:
            rows.append(OptRow("head", effect=effect))
        for param in effect.params:
            rows.append(OptRow("param", effect=effect, param=param))
    rows.append(OptRow("act", action="render"))
    rows.append(OptRow("act", action="back"))
    return rows


def step_row(rows: list, idx: int, delta: int) -> int:
    """next selectable row - chain headers are labels, not rows you can pick"""
    for _ in range(len(rows)):
        idx = (idx + delta) % len(rows)
        if rows[idx].kind != "head":
            return idx
    return idx


def screen_effect_options(chain: Chain, clip: Clip, tools: Tools, args,
                          preset_opts: Optional[dict] = None,
                          initial_notice: str = "") -> Optional[dict]:
    """the options screen. returns the option dict, None when backing out.

    a chain renders one labelled block per effect, in the order the effects
    run; the render and back rows sit under the last block.
    """
    opts = default_chain_opts(chain)
    for effect in chain.effects:
        given = (preset_opts or {}).get(effect.key)
        if isinstance(given, dict):
            opts[effect.key].update(given)
    if not chain.rows or not sys.stdin.isatty():
        return opts

    def make_ctx() -> EffectContext:
        return EffectContext(clip=clip, width=args.width, height=args.height,
                             suffix=args.suffix, crf=args.crf,
                             preset=args.preset, opts=opts)

    rows = option_rows(chain)
    idx = next((i for i, r in enumerate(rows) if r.kind != "head"), 0)
    notice = initial_notice

    while True:
        cols = term_cols()
        width = cols - 6
        body = []
        for i, entry in enumerate(rows):
            if entry.kind == "head":
                body.append("  " + B + C_FROST + fit(entry.effect.name, 13)
                            + NC + "  " + C_DIM
                            + trunc(entry.effect.blurb, width - 18) + NC)
                continue
            if entry.kind == "act":
                first = entry.action == "render"
                label = "render now" if first else "back"
                tail = "start the encode" if first else (
                    "pick another effect" if chain.single
                    else "pick another chain")
                body.append(row("  " + C_FROST + fit(label, 13) + NC
                                + "  " + " " * 24 + C_DIM + tail + NC,
                                i == idx))
                continue
            param = entry.param
            mine = opts[entry.effect.key]
            extra = f"   {param.range_text()}" if param.range_text() else ""
            text = ("  " + fit(param.label, 13) + "  "
                    + fit(param.display(mine[param.key]), 24)
                    + C_DIM + trunc(param.help + extra, width - 44) + NC)
            body.append(row(text, i == idx))
        try:
            plan = plan_for_chain(chain, make_ctx())
            out_name = clip.path.stem + plan.out_suffix + ".mp4"
            preview_lines = []
            if chain.single:
                text = f"{plan.width}x{plan.height}   {plan.vf}"
                if plan.af:
                    text += "   + af " + plan.af
                preview_lines.append("  " + C_DIM + "filter   " + NC
                                     + trunc(text, width - 10))
            else:
                # a chain is too long for one line - show each effect's own
                # chain so you can see what actually runs
                preview_lines.append(
                    "  " + C_DIM + "filter   " + NC + C_FROST
                    + f"{plan.width}x{plan.height}" + NC + C_DIM
                    + f"   {len(chain.effects)} chained" + NC)
                for effect, sub in chain_plans(chain, make_ctx()):
                    line = sub.vf + (f"   + af {sub.af}" if sub.af else "")
                    preview_lines.append(
                        "      " + C_DIM + fit(effect.name, 11) + NC
                        + trunc(line, width - 22) + NC)
        except Exception as exc:      # a broken combination must not kill it
            out_name = "\u2014"
            preview_lines = ["  " + C_DIM + "filter   "
                             + trunc(f"options not usable: {exc}",
                                     width - 10) + NC]
        body.append("")
        body.append("  " + C_DIM + "output   " + NC + C_FROST
                    + trunc(out_name, width - 10) + NC)
        body += preview_lines

        keys = ["\u2191\u2193 row", "\u2190\u2192 change",
                "enter edit", "r render", "q back"]
        if chain.multi_pass is None and not chain.stateful_effects:
            keys.insert(-1, "p preview")
        draw(build_frame(f"{chain.name if chain.single else chain.key} options",
                         body, keys,
                         note=trunc(clip.path.name, 28), notice=notice))
        try:
            key = read_one_key()
        except EOFError:
            return None
        notice = ""

        if key == "r":
            return opts
        if key == "p" and chain.multi_pass is None and not chain.stateful_effects:
            try:
                made, name = render_preview(chain, clip, tools, args, opts)
            except Exception as exc:      # never let a preview kill the menu
                made, name = False, str(exc)
            notice = ((C_GRN + f"preview wrote {name}" + NC) if made
                      else (C_AMB + name + NC))
            continue
        if key in ("q", "esc", "\x03", "b"):
            return None
        if key in ("up", "k"):
            idx = step_row(rows, idx, -1)
            continue
        if key in ("down", "j"):
            idx = step_row(rows, idx, 1)
            continue
        if key.isdigit():
            params = [i for i, e in enumerate(rows) if e.kind == "param"]
            if 1 <= int(key) <= len(params):
                idx = params[int(key) - 1]
                continue

        entry = rows[idx]
        if entry.kind == "act":
            if key in ("\r", "\n", " "):
                return opts if entry.action == "render" else None
            continue

        effect, param = entry.effect, entry.param
        mine = opts[effect.key]
        if key in ("left", "h"):
            mine[param.key] = param.step_value(mine[param.key], -1)
            continue
        if key in ("right", "l"):
            mine[param.key] = param.step_value(mine[param.key], 1)
            continue
        if key not in ("\r", "\n", " "):
            continue

        if param.kind == "bool":
            mine[param.key] = not bool(mine[param.key])
        elif param.kind in ("int", "float", "percent"):
            typed = ask_text(f"{param.label} ({param.range_text()}):",
                             str(mine[param.key]))
            if typed:
                coerced, problem = coerce_param(param, typed)
                if problem:
                    notice = C_AMB + problem + NC
                else:
                    mine[param.key] = coerced
        elif param.kind == "multi":
            picked = multi_pick(f"{effect.name} \u203a {param.label}", param,
                                mine[param.key])
            if picked is not None:
                mine[param.key] = picked
        elif param.kind == "choice":
            before = mine[param.key]
            picked = select(
                f"{effect.name} \u203a {param.label}",
                [Item(label, hint=hint, value=value)
                 for value, label, hint in param.choices],
                lambda it, i, sel: row("  " + fit(it.label, 18) + "  "
                                       + C_DIM + it.hint + NC, sel),
                keys=[], note=param.help, back_hint="keep current")
            if picked is None:
                continue
            if picked == "custom":
                typed = ask_text("size (WxH):", "1920x1080")
                if typed and parse_wxh(typed):
                    mine[param.key] = typed
                else:
                    mine[param.key] = before
                    if typed:
                        notice = (C_AMB + f"{typed!r} is not a WxH size, kept "
                                  "the old shape" + NC)
            else:
                mine[param.key] = picked
        elif param.kind == "video":
            choices = [Item("none", hint="datamosh only the primary clip",
                            value="none")]
            primary = clip.path.resolve()
            for path in scan_videos(Path(args.input_dir)):
                if path.resolve() == primary:
                    continue
                choices.append(Item(path.name, meta=clip_meta(
                    probe(path, tools), term_cols()), value=path))
            picked = select(
                f"{effect.name} \u203a {param.label}", choices,
                lambda it, i, sel: row("  " + fit(it.label, 24) + "  "
                                       + C_DIM + it.meta + it.hint + NC, sel),
                keys=[], note=param.help, back_hint="keep current")
            if picked is not None:
                mine[param.key] = str(picked) if isinstance(picked, Path) \
                    else "none"
        elif param.kind == "size":
            typed = ask_text("size (WxH):", str(mine[param.key]))
            if typed:
                coerced, problem = coerce_param(param, typed)
                if problem:
                    notice = C_AMB + problem + NC
                else:
                    mine[param.key] = coerced


def screen_confirm(chain: Chain, clip: Clip, args, opts: dict) -> bool:
    ctx = EffectContext(clip=clip, width=args.width, height=args.height,
                        suffix=args.suffix, crf=args.crf, preset=args.preset,
                        opts=opts)
    plan = plan_for_chain(chain, ctx)
    dst = resolve_output(clip, args, plan)
    cols = term_cols()
    width = cols - 6
    encode = f"libx264  crf {args.crf}  preset {args.preset}"
    audio_description = "aac 192k stereo" if clip.audio else "none in source"
    pass_effect = chain.multi_pass
    if pass_effect is not None and pass_effect.key == "datamosh":
        mosh_opts = opts_of(opts, pass_effect)
        encode = (f"mpeg4  grit {mosh_opts.get('grit', 2)}  "
                  f"gop {mosh_opts.get('hold', 150)}  -> video copy")
        audio_description = str(mosh_opts.get("audio", "auto"))
    elif chain.stateful_effects:
        encode = f"CPU frame pass  ->  libx264  crf {args.crf}  preset {args.preset}"
    body = [
        kv("input", trunc(str(clip.path), width - 8)),
        kv("effect", chain.name if chain.single
           else f"{chain.key}   {C_DIM}{chain.name}{NC}"),
        kv("options", trunc(option_line(chain, opts), width - 8)),
        kv("target", f"{plan.width}x{plan.height}"),
        kv("output", trunc(str(dst), width - 8)),
        kv("encode", encode + f"   audio {audio_description}"),
        "",
        "  " + C_DIM + trunc(plan.vf, width - 2) + NC,
    ]
    for note in plan.notes:
        body.append("  " + C_DIM + "\u00b7 " + trunc(note, width - 4) + NC)
    draw(build_frame("confirm", body, ["y render", "n back"],
                     note=f"{chain.name} \u00b7 {clip.res}"))
    return ask_yes_no("render this video?", default=True)


def open_folder(path: Path) -> bool:
    """hand the folder to the desktop file manager; False when none exists"""
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        return False
    try:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def screen_done(args, ok_render: bool) -> None:
    if not ok_render or not last_render:
        body = ["  " + C_AMB + "no output file was written" + NC]
        notice = C_RED + "render failed or was cancelled" + NC
        keys = ["enter main menu"]
    else:
        speed = last_render.get("speed")
        body = [
            "  " + C_GRN + "\u2713" + NC + " wrote " + B
            + str(last_render["name"]) + NC,
            "",
            kv("size", human_size(last_render["size"])),
            kv("time", f"{last_render['seconds']:.1f}s"
                       + (f"   {speed:.2f}x realtime" if speed else "")),
            kv("path", trunc(str(last_render["path"]), term_cols() - 14)),
        ]
        notice = ""
        keys = ["enter main menu", "o open output folder"]
    while True:
        start_frame("done", body, keys, notice=notice)
        if not sys.stdin.isatty():
            return
        try:
            key = read_one_key()
        except (EOFError, KeyboardInterrupt):
            return
        if key in ("\r", "\n", "q", "esc", "\x03"):
            return
        if key == "o" and last_render:
            notice = (C_GRN + "opened " + str(OUTPUT_DIR) + NC) \
                if open_folder(OUTPUT_DIR) \
                else (C_AMB + "no file manager opener found - the folder "
                      f"is {OUTPUT_DIR}" + NC)


# ---------------------------------------------------------------------------
# menu loop
# ---------------------------------------------------------------------------

def interactive_menu(args) -> int:
    tools = check_tools(interactive=True)
    if not tools.ffmpeg:
        err("cannot continue without ffmpeg")
        return 1

    TERM.enter()
    try:
        while True:
            choice = screen_main(tools, args)
            if choice == "quit":
                return 0
            if choice == "help":
                screen_help(args)
            elif choice == "effects":
                screen_effects_info(args)
            elif choice == "check":
                tools = screen_check(tools, interactive=True)
            elif choice == "process":
                clip_path = screen_pick_video(tools)
                if clip_path is None:
                    continue
                clip = probe(clip_path, tools)
                chain = screen_pick_effects(clip)
                if chain is None:
                    continue
                opts = None
                notice = ""
                while True:
                    opts = screen_effect_options(chain, clip, tools, args,
                                                 preset_opts=opts,
                                                 initial_notice=notice)
                    if opts is None:
                        break
                    input_problem = validate_datamosh_inputs(
                        chain, opts, [clip.path], args, tools)
                    if input_problem:
                        notice = C_AMB + input_problem + NC
                        continue
                    if screen_confirm(chain, clip, args, opts):
                        last_render.clear()
                        rendered = process(chain, clip, tools, args,
                                           opts_override=opts)
                        screen_done(args, rendered)
                        break
                    notice = (C_AMB + "nothing was rendered - y renders, "
                              "n came back here so you can change options"
                              + NC)
    except (KeyboardInterrupt, EOFError):
        _say()
        return 0
    finally:
        TERM.leave()


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def got_force(args) -> bool:
    return bool(getattr(args, "force", False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="effex",
        description="terminal menu for weird video effects",
        epilog="no arguments = interactive menu. "
               "example: effex stretch input/clip.mp4 --set target=16:9\n"
               "         effex stretch+deepfry clip.mp4 "
               "--set deepfry.strength=60",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("effect", nargs="?",
                   help="effect key, or several chained with + "
                        "(eg stretch+deepfry) - see --list")
    p.add_argument("files", nargs="*",
                   help="video files (default: pick from the menu)")
    p.add_argument("--list", action="store_true",
                   help="list effects with their options, and videos in input/")
    p.add_argument("--check", action="store_true",
                   help="check ffmpeg/ffprobe and folders, exit 1 if missing")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="set an effect option, eg --set target=16:9")
    p.add_argument("--size", default=None, metavar="WxH",
                   help="shorthand for --set target=WxH")
    p.add_argument("--crf", type=int, default=DEFAULT_CRF,
                   help=f"x264 quality, lower = better (default {DEFAULT_CRF})")
    p.add_argument("--preset", default=DEFAULT_PRESET,
                   help=f"x264 preset (default {DEFAULT_PRESET})")
    p.add_argument("--suffix", default="",
                   help="output name suffix (default: the effect's own)")
    p.add_argument("--input-dir", default=str(INPUT_DIR),
                   help="video source folder")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR),
                   help="render target folder")
    p.add_argument("--output", default="",
                   help="exact output path (single file only)")
    p.add_argument("--force", action="store_true",
                   help="overwrite instead of adding _2, _3")
    p.add_argument("--dry-run", action="store_true",
                   help="print the ffmpeg command, render nothing")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="only errors and results")
    p.add_argument("--no-color", action="store_true", help="strip ansi colors")
    p.add_argument("--version", action="version", version=f"effex {VERSION}")
    return p


def effect_json(effect: Effect) -> dict:
    return {
        "key": effect.key,
        "name": effect.name,
        "blurb": effect.blurb,
        "filter": effect.tag,
        "chainable": effect.run is None,
        "options": [
            {
                "key": p.key,
                "label": p.label,
                "kind": p.kind,
                "default": p.default,
                "min": p.minimum if p.kind in ("int", "float", "percent")
                else None,
                "max": p.maximum if p.kind in ("int", "float", "percent")
                else None,
                "step": p.step if p.kind in ("int", "float", "percent")
                else None,
                "help": p.help,
                "choices": [{"value": v, "label": lbl, "hint": h}
                            for v, lbl, h in p.choices],
            }
            for p in effect.params
        ],
    }


def cmd_list(args, tools: Tools) -> int:
    clips = scan_videos(Path(args.input_dir))
    if args.json:
        _say(json.dumps({
            "effects": [effect_json(e) for e in EFFECTS],
            "videos": [str(p) for p in clips],
            "input_dir": str(args.input_dir),
            "output_dir": str(args.output_dir),
            "ffmpeg": tools.ffmpeg,
            "ffprobe": tools.ffprobe,
        }))
        return 0
    _say()
    _say("  " + B + C_FROST + "effects" + NC)
    for effect in EFFECTS:
        _say(f"  {B}{effect.name}{NC}  {C_DIM}{effect.blurb}{NC}")
        if effect.run is not None:
            _say("      " + C_DIM + "own multi-pass pipeline - render it "
                 "on its own, it cannot be chained" + NC)
        for param in effect.params:
            mark = ("default, " + param.range_text()) if param.range_text() \
                else "default"
            line = (f"      {param.label:<13} "
                    f"{C_FROST}{param.display(param.default)}{NC}"
                    + C_DIM + f"  ({mark})" + NC
                    + f"   {C_DIM}{param.help}{NC}")
            _say(line)
        _say("      " + C_DIM + "cli: --set " + effect.params[0].key
             + "=..." + NC)
    _say()
    _say("  " + B + C_FROST + "chains" + NC
         + C_DIM + "   several effects, one encode, run in the order written"
         + NC)
    _say("  " + C_DIM + "cli: effex stretch+deepfry clip.mp4" + NC)
    _say("  " + C_DIM + "options: --set key=value, or --set effect.key=value "
         "when two effects share the key" + NC)
    _say()
    _say("  " + B + C_FROST + "input" + NC
         + f"  {len(clips)} video(s) in {args.input_dir}")
    for path in clips:
        clip = probe(path, tools)
        _say("  " + pad(trunc(path.name, 36), 38) + C_DIM
             + clip_meta(clip, 0) + NC)
    _say()
    _say("  " + C_DIM + f"output folder: {args.output_dir}" + NC)
    return 0


def cmd_check(args, tools: Tools) -> int:
    if args.json:
        # machine mode: one json object on stdout, nothing else
        payload = {"ok": tools.ok, "ffmpeg": tools.ffmpeg,
                   "ffprobe": tools.ffprobe}
        if not tools.ok:
            hit = detect_installer()
            payload["hint"] = " ".join(hit[1]) if hit else None
        _say(json.dumps(payload))
        return 0 if tools.ok else 1
    tools = screen_check(tools, interactive=sys.stdin.isatty())
    return 0 if tools.ok else 1


def validate_datamosh_inputs(chain: Chain, opts: dict, targets: list,
                             args, tools: Tools) -> str:
    effect = next((item for item in chain.effects if item.key == "datamosh"),
                  None)
    if effect is None:
        return ""
    mine = opts_of(opts, effect)
    raw_second = mine.get("second", "none")
    audio_mode = str(mine.get("audio", "auto"))
    if str(raw_second).lower() in ("", "none", "off"):
        if audio_mode in ("second", "mix"):
            return f"audio={audio_mode} requires a second video"
        return ""
    if not tools.ffprobe:
        return "ffprobe is required to validate a second datamosh video"
    for primary in targets:
        second_path, problem = resolve_second_video(
            raw_second, Path(args.input_dir), primary)
        if problem:
            return problem
        if second_path is None:
            return "second video was not resolved"
        second_clip = probe(second_path, tools)
        duration = float(second_clip.duration or 0.0)
        if not second_clip.width or not second_clip.height or duration <= 0:
            return f"second video has no readable video stream: {second_path.name}"
        start = float(mine.get("second_start", 0.0))
        length = float(mine.get("second_length", 0.0))
        remaining = duration - start
        if remaining <= 0:
            return (f"second_start={start:g} is past the duration of "
                    f"{second_path.name} ({duration:g}s)")
        if length and length > remaining + 0.01:
            return (f"second_length={length:g}s exceeds the remaining "
                    f"{remaining:g}s of {second_path.name}")
        if audio_mode == "second" and not second_clip.audio:
            return f"audio=second selected, but {second_path.name} has no audio"
    return ""


def cmd_run(args, tools: Tools) -> int:
    chain, problem = parse_chain(args.effect)
    if chain is None:
        err(problem)
        hint("known effects: " + ", ".join(e.key for e in EFFECTS))
        hint("chain several with + : effex stretch+deepfry clip.mp4")
        return 2

    opts, problem = apply_cli_opts(chain, args)
    if problem:
        err(problem)
        hint("--list shows the options every effect takes")
        return 2

    if not args.files:
        if not sys.stdin.isatty():
            err("no input files given and stdin is not a terminal")
            return 2
        TERM.enter()
        try:
            path = screen_pick_video(tools)
        finally:
            TERM.leave()
        if path is None:
            return 0
        targets = [path]
    else:
        targets = []
        for raw in args.files:
            path = Path(raw)
            if not path.is_file():
                if (Path(args.input_dir) / raw).is_file():
                    path = Path(args.input_dir) / raw
                else:
                    err(f"no such file: {raw}")
                    return 1
            targets.append(path)

    input_problem = validate_datamosh_inputs(chain, opts, targets, args, tools)
    if input_problem:
        err(input_problem)
        return 2

    if args.output and len(targets) > 1:
        err("--output works with a single file only")
        return 2

    if len(targets) > 1 and sys.stdin.isatty() and not args.quiet:
        _say()
        info(f"{len(targets)} files, {chain.name}, "
             f"options {option_line(chain, opts)}")
        if not ask_yes_no("render all of them?", default=True):
            warn("nothing rendered")
            return 0

    failures = 0
    for path in targets:
        clip = probe(path, tools)
        if not clip.duration:
            warn(f"could not read duration of {path.name} - is it a video?")
        if not process(chain, clip, tools, args, opts_override=opts):
            failures += 1
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.size_given = args.size is not None
    if args.size_given:
        parsed = parse_wxh(args.size)
        if not parsed:
            err(f"--size wants WxH like 1920x1080, got {args.size!r}")
            return 2
        args.width, args.height = parsed
    else:
        args.width, args.height = DEFAULT_W, DEFAULT_H

    apply_palette(not (args.no_color or os.environ.get("NO_COLOR")
                       or not sys.stdout.isatty()))
    global _QUIET
    _QUIET = args.quiet

    tools = find_tools()

    if args.check:
        return cmd_check(args, tools)
    if args.list:
        return cmd_list(args, tools)

    if args.effect:
        if not tools.ok:
            tools = check_tools(interactive=sys.stdin.isatty())
            if not tools.ffmpeg:
                err("ffmpeg is required")
                return 1
        if sys.stdin.isatty():
            TERM.enter()
            try:
                return cmd_run(args, tools)
            finally:
                TERM.leave()
        return cmd_run(args, tools)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.print_help()
        _say()
        _say("  " + C_DIM + "the interactive menu needs a terminal - in "
             "scripts use `effex --list` or `effex <effect> <file>`" + NC)
        return 0

    return interactive_menu(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        try:
            sys.stderr.close()
        except Exception:
            pass
        os._exit(0)
    except KeyboardInterrupt:
        _say()
        sys.exit(130)
