#!/usr/bin/env python3
"""Drop a generated test clip into input/ so there is something to play with.

    python3 tests/make_clip.py                          # 3s 640x360 with audio
    python3 tests/make_clip.py --seconds 10 --size 1280x720 demo.mp4
    python3 tests/make_clip.py --no-audio silent.mp4
    python3 tests/make_clip.py --landscape              # 16:9 source, handy for
                                                        # seeing the stretch

The clip is made by ffmpeg's lavfi sources, so nothing is downloaded.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO, make_clip  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="build a lavfi test clip into input/ so there is "
                    "something to play with")
    p.add_argument("name", nargs="?", default="test_clip.mp4",
                   help="file name inside input/ (default test_clip.mp4)")
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--size", default="640x360", help="source size, default 640x360")
    p.add_argument("--landscape", action="store_true",
                   help="use 1280x720 source instead of --size")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--out-dir", default=None,
                   help="target folder (default input/)")
    args = p.parse_args()

    size = "1280x720" if args.landscape else args.size
    folder = Path(args.out_dir) if args.out_dir else (REPO / "input")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (args.name if args.name.endswith(".mp4")
                       else args.name + ".mp4")

    if target.exists():
        print(f"  {target} already exists, pick another name")
        return 1
    if not make_clip(target, seconds=args.seconds, size=size,
                     audio=not args.no_audio):
        print("  ffmpeg could not build the clip (is ffmpeg installed?)")
        return 1
    print(f"  wrote {target}  ({size}, {args.seconds:g}s, "
          f"{'no audio' if args.no_audio else 'with audio'})")
    print("  start ./effex.py and it will show up in the list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
