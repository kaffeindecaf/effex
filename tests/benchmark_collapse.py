#!/usr/bin/env python3
"""CPU-only collapse benchmark; each mode/size runs in a fresh process.

python3 tests/benchmark_collapse.py --frames 120 --output benchmarks/collapse.json
RSS is process peak (Linux), including imports, input/output and transient maps.
Timing covers process() only, not FFmpeg, input generation, or initialization.
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


def worker(mode, width, height, frames, pixelate=False):
    import cv2
    import numpy as np
    from stateful_effects import ElasticCollapse

    # Fixed thread count avoids benchmark changes from OpenCV's host defaults.
    cv2.setNumThreads(1)
    y, x = np.indices((height, width))
    frame = np.stack(((x * 2) % 256, (y * 3) % 256,
                      ((x // 20 + y // 20) % 2) * 255), axis=-1).astype(np.uint8)
    del x, y
    processor = ElasticCollapse()
    config = dict(ElasticCollapse.DEFAULTS, mode=mode, seed=17,
                  pixelate=pixelate)
    processor.initialize(config, frame.shape, 30.0, 17)
    times, update_times = [], []
    # Warm up libraries, then reset so measured frame zero includes first setup.
    processor.process(frame, 0, 0.0, {})
    processor.reset()
    for index in range(frames):
        source = np.roll(frame, index * 2, axis=1)
        updates = processor.seam_updates
        start = time.perf_counter()
        result = processor.process(source, index, index / 30, {})
        elapsed = (time.perf_counter() - start) * 1000
        assert result.shape == frame.shape and result.dtype == np.uint8
        times.append(elapsed)
        if processor.seam_updates != updates:
            update_times.append(elapsed)
    return {
        "mode": mode, "pixelate": pixelate,
        "width": width, "height": height, "frames": frames,
        "config": config, "mean_ms": float(np.mean(times)),
        "p50_ms": float(np.percentile(times, 50)),
        "p95_ms": float(np.percentile(times, 95)), "max_ms": max(times),
        "processor_fps": 1000 / float(np.mean(times)),
        "seam_updates": processor.seam_updates,
        "seam_update_mean_ms": float(np.mean(update_times)) if update_times else None,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "persistent_state_mib": processor.state_nbytes / (1024 * 1024),
        "work_shape": processor.work_shape,
        "python": platform.python_version(), "numpy": np.__version__,
        "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("elastic", "seam"))
    parser.add_argument("--pixelate", choices=("off", "on"), default="off",
                        help="benchmark the opt-in pixel grid (default: off, the effect default)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    if args.worker:
        print(json.dumps(worker(args.worker, args.width, args.height, args.frames,
                                pixelate=args.pixelate == "on")))
        return
    pixelate = args.pixelate == "on"
    rows = []
    for mode in ("elastic", "seam"):
        for width, height in ((640, 360), (1280, 720)):
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", mode,
                 "--width", str(width), "--height", str(height), "--frames", str(args.frames),
                 "--pixelate", args.pixelate],
                capture_output=True, text=True, check=True, cwd=REPO,
            )
            row = json.loads(proc.stdout)
            rows.append(row)
            print(f"{mode:7} pixelate={args.pixelate:3} {width}x{height}: "
                  f"{row['mean_ms']:.2f} ms mean, "
                  f"{row['p95_ms']:.2f} ms p95, {row['peak_rss_mib']:.1f} MiB peak RSS")
    assert len(rows) == 4
    report = {
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "logical_cpus": os.cpu_count(),
        "pixelate": pixelate,
        "scope": "synthetic moving RGB; process() only; fresh process per mode/size; not end-to-end render FPS",
        "results": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
