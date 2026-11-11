#!/usr/bin/env python3
"""Reproducible synthetic example and CPU/memory measurement (no extra packages).

python benchmarks/reverse_infection.py --output-dir docs/examples/reverse-infection
"""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from frame_pipeline import FramePacket, process_packets
from reverse_infection import ReverseInfection

ROOT = Path(__file__).resolve().parents[1]


def frames(count, width, height, fps):
    """A red figure enters a static room at 2s, then walks to the right."""
    yy, xx = np.mgrid[:height, :width]
    room = np.empty((height, width, 3), np.uint8)
    room[:] = (25, 34, 42)
    floor = yy > height * .72
    room[floor] = (47, 49, 53)
    room[(xx % max(1, width // 8) == 0) & floor] = (65, 67, 71)
    cv2.rectangle(room, (width // 8, height // 6), (width // 3, height * 3 // 4), (70, 76, 82), 2)
    cv2.rectangle(room, (width * 2 // 3, height // 5), (width * 7 // 8, height // 2), (45, 64, 77), 2)
    scale = height / 180
    for i in range(count):
        frame = room.copy()
        if i >= fps * 2:
            x = int(width * .25 + (i - fps * 2) * width / (fps * 6))
            y = int(height * .56)
            color = (240, 45, 35)
            cv2.circle(frame, (x, y - int(30 * scale)), max(1, int(9 * scale)), color, -1)
            cv2.rectangle(frame, (x - int(9 * scale), y - int(18 * scale)),
                          (x + int(9 * scale), y + int(15 * scale)), color, -1)
            swing = int(6 * scale * np.sin(i * .4))
            for direction in (-1, 1):
                cv2.line(frame, (x + direction * int(6 * scale), y + int(12 * scale)),
                         (x + direction * int(10 * scale) + swing, y + int(38 * scale)),
                         color, max(1, int(5 * scale)))
            cv2.line(frame, (x - int(16 * scale), y + int(5 * scale) + swing),
                     (x + int(16 * scale), y - swing), color, max(1, int(5 * scale)))
            # Contrasting woven detail makes reference-tile copying visible;
            # a flat silhouette cannot reveal whether texture survives growth.
            subject = np.all(frame == color, axis=2)
            weave = ((xx - x) // max(2, round(4 * scale))
                     + (yy - y) // max(2, round(4 * scale))) % 5
            frame[subject & (weave == 0)] = (45, 100, 225)
            frame[subject & (weave == 1)] = (230, 180, 35)
        yield FramePacket(frame, i, Fraction(i, fps), Fraction(1, fps), Fraction(1, fps))


def peak_rss_bytes():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess
        process.restype = wintypes.HANDLE
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not get_info(process(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.PeakWorkingSetSize
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def measure(count, width, height, fps, trace=False):
    if trace:
        tracemalloc.start()
    processor = ReverseInfection()
    processor.initialize({}, (height, width, 3), fps, 42)
    start = time.perf_counter()
    total = sum(1 for _ in process_packets(frames(count, width, height, fps), [processor]))
    elapsed = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1] if trace else None
    if trace:
        tracemalloc.stop()
    return dict(frames=total, seconds=elapsed, fps=total / elapsed,
                peak_traced_bytes=peak, peak_process_rss_bytes=peak_rss_bytes(),
                peak_retained_state_bytes=processor.peak_state_bytes,
                max_buffered_frames=processor.peak_buffered_frames,
                effective_lookahead_frames=processor.effective_lookahead_frames,
                effective_overlap=processor.effective_overlap)


def write_source(path, count, width, height, fps):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0", "-an",
           "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(path)]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as encoder:
        try:
            for packet in frames(count, width, height, fps):
                encoder.stdin.write(packet.frame.tobytes())
        finally:
            encoder.stdin.close()
        if encoder.wait():
            raise RuntimeError("synthetic source encode failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/reverse-infection")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--benchmark-only", action="store_true")
    args = parser.parse_args()
    if min(args.width, args.height, args.frames, args.fps) < 1 or args.width % 2 or args.height % 2:
        parser.error("positive dimensions/frame counts required; dimensions must be even")
    cv2.setNumThreads(1)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Warm OpenCV before timing. Trace allocations separately to avoid charging
    # Python allocation instrumentation to the reported throughput.
    measure(3, args.width, args.height, args.fps)
    timed = measure(args.frames, args.width, args.height, args.fps)
    memory = measure(args.frames, args.width, args.height, args.fps, trace=True)
    report = dict(platform=platform.platform(), python=platform.python_version(),
                  numpy=np.__version__, opencv=cv2.__version__, opencv_threads=1,
                  resolution=[args.width, args.height], config=ReverseInfection.DEFAULTS,
                  throughput=timed, memory=memory,
                  notes="Processor + synthetic generator; excludes video IO. Tracing is a separate pass. "
                        "RSS includes interpreter/imports/native libraries and is process high-water.")
    if not args.benchmark_only:
        source = args.output_dir / "source.mp4"
        effect = args.output_dir / "infected.mp4"
        write_source(source, args.frames, args.width, args.height, args.fps)
        cli = [sys.executable, str(ROOT / "effex.py"), "datamosh", str(source), "--set",
               "algorithm=reverse_infection", "--output", str(effect), "--force", "--json"]
        start = time.perf_counter()
        render = subprocess.run(cli, check=True, capture_output=True, text=True)
        report["cli_render_seconds"] = time.perf_counter() - start
        report["render"] = json.loads(render.stdout)
        compare = args.output_dir / "comparison.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-i", str(effect),
                        "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]", "-map", "[v]",
                        "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(compare)], check=True)
        # Contact sheet uses decoded results from the actual Effex export.
        capture = cv2.VideoCapture(str(effect))
        selected = [max(0, min(args.frames - 1, round(args.fps * t))) for t in (1.25, 1.5, 1.75, 2.)]
        originals = {q.index: q.frame for q in frames(args.frames, args.width, args.height, args.fps)
                     if q.index in selected}
        rows = []
        for i in selected:
            capture.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, bgr = capture.read()
            if not ok:
                raise RuntimeError("cannot read contact-sheet frame")
            row = np.hstack((cv2.cvtColor(originals[i], cv2.COLOR_RGB2BGR), bgr))
            cv2.putText(row, f"source   {i / args.fps:.2f}s", (6, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, .4, (240, 240, 240), 1)
            cv2.putText(row, "Reverse Infection", (args.width + 6, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, .4, (240, 240, 240), 1)
            rows.append(row)
        capture.release()
        cv2.imwrite(str(args.output_dir / "contact-sheet.png"), np.vstack(rows))
    (args.output_dir / "measurements.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
