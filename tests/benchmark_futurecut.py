#!/usr/bin/env python3
"""CPU-only FutureCut benchmark; each size/mode runs in a fresh process.

python3 tests/benchmark_futurecut.py --frames 120 --output benchmarks/futurecut.json
RSS is process peak (Linux), including imports, buffered frames and masks.
Timing covers push() only, not FFmpeg, decode/encode or initialization.
"""
import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def worker(mode, width, height, frames):
    import cv2
    import numpy as np
    from fractions import Fraction
    from frame_pipeline import FramePacket
    from future_cut import FutureCut

    # Fixed thread count avoids benchmark changes from OpenCV's host defaults.
    cv2.setNumThreads(1)
    y, x = np.indices((height, width))
    frame = np.stack(((x * 2) % 256, (y * 3) % 256,
                      ((x // 20 + y // 20) % 2) * 255), axis=-1).astype(np.uint8)
    del x, y

    fps = 30.0
    processor = FutureCut()
    processor.initialize({"gap": 12, "spawn_every": 24, "mode": mode},
                         frame.shape, fps, 17)

    def packet(index):
        source = np.roll(frame, index * 2, axis=1)
        return FramePacket(source, index, Fraction(index * 1000, int(fps * 1000)),
                           Fraction(1, 1000), Fraction(1000, int(fps * 1000)), {})

    # Warm up libraries, then reset so measured frame zero includes first setup.
    list(processor.push(packet(0)))
    processor.reset()

    times = []
    spawned = 0
    peak_state = 0
    for index in range(frames):
        before = processor.spawns
        start = time.perf_counter()
        out = list(processor.push(packet(index)))
        elapsed = (time.perf_counter() - start) * 1000
        for item in out:
            assert item.frame.shape == frame.shape and item.frame.dtype == np.uint8
        times.append(elapsed)
        peak_state = max(peak_state, processor.state_nbytes)
        if processor.spawns != before:
            spawned += 1
    list(processor.flush())
    return {
        "mode": mode,
        "width": width, "height": height, "frames": frames,
        "config": processor.serialize_config(),
        "mean_ms": float(np.mean(times)),
        "p50_ms": float(np.percentile(times, 50)),
        "p95_ms": float(np.percentile(times, 95)), "max_ms": max(times),
        "processor_fps": 1000 / float(np.mean(times)),
        "spawns": spawned,
        "capacity": processor.capacity,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "persistent_state_mib": peak_state / (1024 * 1024),
        "python": platform.python_version(), "numpy": np.__version__,
        "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("smudge", "slide"))
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    if args.worker:
        print(json.dumps(worker(args.worker, args.width, args.height, args.frames)))
        return
    rows = []
    for mode in ("smudge", "slide"):
        for width, height in ((640, 360), (1280, 720), (1920, 1080)):
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", mode,
                 "--width", str(width), "--height", str(height),
                 "--frames", str(args.frames)],
                capture_output=True, text=True, check=True, cwd=REPO,
            )
            row = json.loads(proc.stdout)
            rows.append(row)
            print(f"{mode:6} {width}x{height}: {row['mean_ms']:.2f} ms mean, "
                  f"{row['p95_ms']:.2f} ms p95, {row['peak_rss_mib']:.1f} MiB peak RSS, "
                  f"{row['spawns']} spawns")
    assert len(rows) == 6
    report = {
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "scope": "synthetic moving RGB; push() only; fresh process per mode/size; "
                 "not end-to-end render FPS",
        "results": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
