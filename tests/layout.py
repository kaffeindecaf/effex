#!/usr/bin/env python3
"""Layout tests: every frame must fit the terminal and stay aligned.

The menu draws whole frames, so these tests can parse the frames straight out
of the pty stream and measure them:

  - nothing may be wider than the terminal (a wrapped line is what leaves
    artifacts behind on the next redraw)
  - the screen must be cleared before the first frame is drawn
  - list rows must all be the same width so columns line up
  - a frame must not move the cursor itself (no cursor-up tricks inside it)

Run: python3 tests/layout.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (Checker, EFFEX, env_for, make_clip,  # noqa: E402
                     require_tools, temp_folders)
from pty_driver import PtyDriver  # noqa: E402

SGR = re.compile(r"\033\[[0-9;]*m")
ENTER = b"\r"

# sizes to check: a phone-ish pty, the classic 80x24, and something wide
SIZES = [(60, 20), (80, 24), (120, 40)]


def vis(line: str) -> int:
    """visible width; the pty rewrites newlines to crlf, so drop the cr"""
    return len(plain_of(line))


def plain_of(line: str) -> str:
    return SGR.sub("", line).rstrip("\r")


def frames(text: str) -> list:
    """split the pty stream into frames on the home-cursor sequence"""
    return [part.split("\033[J")[0].rstrip("\n")
            for part in text.split("\033[H")[1:]]


def frame_with(text: str, marker: str) -> str:
    for frame in reversed(frames(text)):
        if marker in plain_of(frame):
            return frame
    return ""


def body_of(frame: str) -> list:
    """the rows between the two rules, without blanks or the more-marker"""
    plain = [plain_of(line) for line in frame.split("\n")]
    rules = [i for i, line in enumerate(plain)
             if line.strip() and set(line.strip()) <= {"\u2500"}]
    if len(rules) < 2:
        return []
    middle = plain[rules[0] + 1:rules[-1]]
    return [line for line in middle
            if line.strip() and not line.strip().startswith("\u2026")]


def lines(frame: str) -> list:
    return frame.split("\n")


def scenario_fit(c: Checker, cols: int, rows: int) -> PtyDriver:
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "a_long_video_name_for_testing_columns.mp4", seconds=1)
    make_clip(in_dir / "b.mp4", seconds=1)
    env = env_for(in_dir, out_dir)

    d = PtyDriver([sys.executable, str(EFFEX)], rows=rows, cols=cols,
                  env_extra=env, timeout=10)
    c.check(f"{cols}x{rows}: menu comes up", d.wait_for("main menu"))
    return d


def check_frame(c: Checker, label: str, frame: str, cols: int) -> None:
    body = lines(frame)
    widest = max((vis(line) for line in body), default=0)
    c.check(f"{label}: every line fits {cols} columns", widest <= cols,
            f"widest line is {widest}")
    c.check(f"{label}: no tabs in the frame", "\t" not in frame)
    c.check(f"{label}: frame does not move the cursor itself",
            not any(code in frame for code in ("\033[1A", "\033[2J",
                                               "\033[K", "\033[A")))
    c.check(f"{label}: has a header and a footer rule",
            plain_of(frame).count("\u2500" * 10) >= 2)


def check_rows_aligned(c: Checker, label: str, frame: str) -> None:
    """list rows are padded to one width, so their columns line up"""
    rows = body_of(frame)
    widths = {vis(line) for line in rows}
    c.check(f"{label}: rows share one width", len(widths) == 1,
            f"{len(rows)} rows, widths seen: {sorted(widths)}")


def scenario_sizes(c: Checker) -> None:
    for cols, rows in SIZES:
        d = scenario_fit(c, cols, rows)
        stream = d.text()
        c.check(f"{cols}x{rows}: screen is cleared before the first frame",
                stream.startswith("\033[H"), repr(stream[:24]))

        menu = frame_with(stream, "main menu")
        check_frame(c, f"{cols}x{rows} main menu", menu, cols)
        check_rows_aligned(c, f"{cols}x{rows} main menu", menu)

        d.send(b"p")
        d.wait_for("pick a video")
        picker = frame_with(d.text(), "pick a video")
        check_frame(c, f"{cols}x{rows} video list", picker, cols)
        check_rows_aligned(c, f"{cols}x{rows} video list", picker)
        c.check(f"{cols}x{rows}: long file names are cut, not wrapped",
                "a_long_video_name_for_testing_columns.mp4" in
                SGR.sub("", picker)
                or "\u2026" in SGR.sub("", picker))

        d.send(ENTER)
        d.wait_for("pick an effect")
        effects = frame_with(d.text(), "pick an effect")
        check_frame(c, f"{cols}x{rows} effect list", effects, cols)

        d.send(ENTER)
        d.wait_for("options")
        options = frame_with(d.text(), "options")
        check_frame(c, f"{cols}x{rows} options", options, cols)

        d.send(b"q")
        d.send(b"q")
        d.send(b"q")
        d.send(b"q")
        rc = d.expect_exit(6)
        c.check(f"{cols}x{rows}: clean exit (rc={rc})", rc == 0)
        c.check(f"{cols}x{rows}: no traceback", "Traceback" not in d.text())
        d.close()


def scenario_columns(c: Checker) -> None:
    """the meta columns of the video list start at the same x on every row"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "aa.mp4", seconds=1)
    make_clip(in_dir / "bbbbbbbbbbbbbb.mp4", seconds=1)
    env = env_for(in_dir, out_dir)

    d = PtyDriver([sys.executable, str(EFFEX)], rows=30, cols=110,
                  env_extra=env, timeout=10)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    picker = frame_with(d.text(), "pick a video")

    rows = body_of(picker)
    res_starts = [m.start() for line in rows
                  for m in [re.search(r"\d{2,5}x\d{2,5}", line)] if m]
    time_starts = [m.start() for line in rows
                   for m in [re.search(r"\d{2}:\d{2}", line)] if m]
    c.check("video rows found for the column check", len(res_starts) == 2,
            str(res_starts))
    c.check("resolution column starts at the same x on every row",
            len(res_starts) == 2 and len(set(res_starts)) == 1,
            str(res_starts))
    c.check("duration column starts at the same x on every row",
            len(time_starts) == 2 and len(set(time_starts)) == 1,
            str(time_starts))
    d.send(b"q")
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    d.close()


def main() -> int:
    c = Checker("layout")
    if not require_tools(c):
        return 0
    scenario_sizes(c)
    scenario_columns(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
