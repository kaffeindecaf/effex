#!/usr/bin/env python3
"""Benchmark bounded inversion at 360p/720p; no FFmpeg time is included."""
import argparse
import json
import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def worker(mode, width, height, frames):
    import cv2
    import numpy as np
    from frame_pipeline import FramePacket
    from stateful_effects import CausalityInversion
    cv2.setNumThreads(1)
    y, x = np.indices((height, width))
    source = np.stack((x % 256, y % 256, ((x // 24 + y // 24) % 2) * 180), axis=-1).astype(np.uint8)
    del x, y
    processor = CausalityInversion()
    processor.initialize({"mode": mode}, source.shape, 30, 17)
    timings, buffered, outputs = [], 0, 0
    for i in range(frames):
        packet = FramePacket(np.roll(source, i * 2, axis=1), i, Fraction(i, 30),
                             Fraction(1, 30), Fraction(1, 30))
        start = time.perf_counter()
        for result in processor.push(packet):
            assert result.frame.shape == source.shape
            outputs += 1
        timings.append((time.perf_counter() - start) * 1000)
        buffered = max(buffered, processor.state_nbytes)
    start = time.perf_counter()
    for _ in processor.flush():
        outputs += 1
    drain_ms = (time.perf_counter() - start) * 1000
    assert outputs == frames and processor.state_nbytes == 0
    return {"mode": mode, "width": width, "height": height, "frames": frames,
            "mean_ms_including_eof": (sum(timings) + drain_ms) / frames,
            "push_p95_ms": float(np.percentile(timings, 95)), "eof_drain_ms": drain_ms,
            "peak_buffer_mib": buffered / 1048576,
            "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "lookahead_seconds": 0.5, "fps": 30, "opencv_threads": 1,
            "python": platform.python_version(), "numpy": np.__version__, "opencv": cv2.__version__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("blend", "replace", "tear", "align"))
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    if args.worker:
        print(json.dumps(worker(args.worker, args.width, args.height, args.frames)))
        return
    rows = []
    for mode in ("blend", "replace", "tear", "align"):
        for width, height in ((640, 360), (1280, 720)):
            result = subprocess.run([sys.executable, __file__, "--worker", mode, "--width", str(width),
                                     "--height", str(height), "--frames", str(args.frames)],
                                    text=True, capture_output=True, check=True)
            row = json.loads(result.stdout)
            rows.append(row)
            print(f"{mode:7} {width}x{height}: {row['mean_ms_including_eof']:.2f} ms/frame, "
                  f"{row['peak_buffer_mib']:.2f} MiB state, {row['peak_process_rss_mib']:.1f} MiB RSS")
    assert len(rows) == 8
    report = {"recorded_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "scope": "processor only, fresh worker per mode/size; includes EOF drain, excludes decode/encode",
              "results": rows}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
