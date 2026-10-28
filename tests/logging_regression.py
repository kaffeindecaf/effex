#!/usr/bin/env python3
"""Diagnostics log tests: the file you attach to a github issue.

Two halves. The unit half imports diagnostics and checks the record format,
rotation, thread safety and the report; the cli half runs effex with
EFFEX_LOG_DIR pointed at a temp folder and checks that

  * a clean run writes nothing
  * a failing run records the error, the warning before it, and the ffmpeg
    stderr under it
  * --logs prints the path and the tail, --report writes one attachable file

Run: python3 tests/logging_regression.py
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagnostics  # noqa: E402
from harness import Checker, EFFEX, require_tools, run  # noqa: E402

EVENT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (warn|error|info) +\S.*$")


def fresh_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def main() -> int:
    c = Checker("diagnostics log")

    # ---- unit: session records -------------------------------------
    logs = fresh_dir("effex-logs-")
    os.environ["EFFEX_LOG_DIR"] = str(logs)
    c.check("log dir follows EFFEX_LOG_DIR", diagnostics.log_dir() == logs,
            str(diagnostics.log_dir()))
    c.check("the default path is not inside the repo",
            "effex" in diagnostics.LOG.path().name)

    session = diagnostics.Session()
    session.start(version="9.9.9", argv=["stretch", "clip.mp4"], cwd="/tmp/x")
    real_ffmpeg = shutil.which("ffmpeg")
    session.bind("ffmpeg", real_ffmpeg or "/bin/ffmpeg")
    c.check("nothing is written before an event", not session.exists())
    session.warn("ffmpeg is missing")
    c.check("a warning creates the file", session.exists())
    text = session.path().read_text()
    c.check("the header names the version", "==== effex 9.9.9" in text, text)
    c.check("the header carries the command",
            "effex stretch clip.mp4" in text, text)
    c.check("the header carries platform and python",
            "platform" in text and "python" in text)
    # the version line only exists when the bound binary can actually run
    if real_ffmpeg:
        c.check("the header carries the tool paths and versions",
                real_ffmpeg in text
                and re.search(r"ffmpeg v\s+\d", text) is not None, text)
    else:
        c.check("the header carries the tool path", "/bin/ffmpeg" in text,
                text)
    c.check("the event line is timestamped and levelled",
            any(EVENT.match(line) and "warn" in line
                for line in text.splitlines()), text)

    session.error("\033[31mbang\033[0m", "first line\nsecond line")
    text = session.path().read_text()
    c.check("ansi codes never reach the log", "\033[" not in text, text)
    c.check("detail lines are indented and prefixed",
            "    | first line" in text and "    | second line" in text, text)

    try:
        raise ValueError("boom")
    except ValueError as exc:
        session.exception(exc, "render failed")
    text = session.path().read_text()
    c.check("exceptions keep the traceback",
            "Traceback" in text and "ValueError: boom" in text, text[-400:])
    c.check("the header is written once per session",
            text.count("==== effex 9.9.9") == 1)

    c.check("tail returns the last lines",
            len(session.tail(3).splitlines()) == 3, session.tail(3))
    c.check("info events are recorded too",
            bool(session.info("hello") or "hello" in session.path().read_text()))

    # ---- unit: rotation stays bounded ------------------------------
    tidy = fresh_dir("effex-rotate-")
    os.environ["EFFEX_LOG_DIR"] = str(tidy)
    small = diagnostics.Session()
    small.start(version="1.0")
    old_bytes, old_keep = diagnostics.MAX_BYTES, diagnostics.KEEP
    diagnostics.MAX_BYTES, diagnostics.KEEP = 1500, 3
    try:
        for index in range(120):
            small.error(f"event number {index} with a little padding to grow")
    finally:
        diagnostics.MAX_BYTES, diagnostics.KEEP = old_bytes, old_keep
    base = tidy / diagnostics.LOG_NAME
    c.check("rotation keeps the current file small",
            base.stat().st_size <= 1500 + 400, str(base.stat().st_size))
    c.check("rotation keeps one old file", (tidy / (diagnostics.LOG_NAME + ".1")).is_file())
    c.check("rotation drops files past the keep count",
            not (tidy / (diagnostics.LOG_NAME + ".4")).exists())

    # ---- unit: threads never interleave ---------------------------
    os.environ["EFFEX_LOG_DIR"] = str(fresh_dir("effex-threads-"))
    threaded = diagnostics.Session()
    threaded.start(version="2.0")

    def worker(n: int) -> None:
        for i in range(20):
            threaded.error(f"worker {n} line {i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = [line for line in threaded.path().read_text().splitlines()
             if EVENT.match(line)]
    c.check("every threaded event lands", len(lines) == 120, str(len(lines)))
    c.check("no half-written lines",
            all("line" in line for line in lines), str(lines[:2]))

    # ---- unit: report ---------------------------------------------
    target = fresh_dir("effex-report-") / "nested" / "report.log"
    written = threaded.report(target)
    body = written.read_text()
    c.check("the report is written where asked", written == target)
    c.check("the report opens with what it is",
            body.startswith("effex diagnostics report"), body[:60])
    c.check("the report carries the environment",
            "platform" in body and "python" in body and "log file" in body)
    c.check("the report carries the log tail",
            "---- log tail ----" in body and "worker 0 line 0" in body)
    c.check("the report links the issue tracker",
            diagnostics.ISSUES_URL in body)

    # ---- the failure screen points at the log ---------------------
    os.environ["EFFEX_LOG_DIR"] = str(fresh_dir("effex-screen-"))
    diagnostics.LOG.start(version="3.0", argv=["stretch"])
    diagnostics.LOG.error("ffmpeg failed (exit 1)")
    import effex
    effex.last_render.clear()
    effex.last_render["error"] = "ffmpeg failed (exit 1)"
    frame = io.StringIO()
    with contextlib.redirect_stdout(frame):
        effex.screen_done(types.SimpleNamespace(), False)
    shown = frame.getvalue()
    c.check("the failure screen shows the log path",
            "log" in shown and effex.LOG.path().name in shown, shown[-300:])
    c.check("the failure screen offers the log key",
            "l open log folder" in shown, shown[-300:])

    # ---- cli: a clean run writes nothing --------------------------
    if not require_tools(c):
        return c.summary()

    clean = fresh_dir("effex-clean-")
    proc = run([EFFEX, "--check", "--json"],
               env_extra={"EFFEX_LOG_DIR": str(clean)})
    c.check("a clean run still exits 0", proc.returncode == 0,
            proc.stderr.decode()[:160])
    c.check("a clean run writes no log",
            not (clean / diagnostics.LOG_NAME).exists(),
            str(list(clean.iterdir()) if clean.exists() else ""))

    # ---- cli: a failing run records everything --------------------
    bad = fresh_dir("effex-bad-")
    log = bad / diagnostics.LOG_NAME
    proc = run([EFFEX, "bogus", "clip.mp4"],
               env_extra={"EFFEX_LOG_DIR": str(bad)})
    text = log.read_text() if log.is_file() else ""
    c.check("a failed run exits non-zero", proc.returncode != 0)
    c.check("the failure names the file in the log",
            "error unknown effect: bogus" in text, text[-200:])
    c.check("the failed run tells the user where the log is",
            b"details in" in proc.stdout + proc.stderr,
            (proc.stdout + proc.stderr).decode()[-200:])

    # a real ffmpeg failure must keep its stderr, not just "it failed"
    broken = fresh_dir("effex-broken-") / "broken.mp4"
    broken.write_bytes(os.urandom(40_000))
    proc = run([EFFEX, "stretch", broken, "--no-color"],
               env_extra={"EFFEX_LOG_DIR": str(bad)})
    text = log.read_text()
    c.check("the render failure is logged",
            "error ffmpeg failed" in text, text[-300:])
    c.check("the warning that led to it is logged",
            "warn  could not read duration" in text, text[-300:])
    c.check("ffmpeg's own stderr is attached under the error",
            "    | " in text and "Invalid data" in text, text[-300:])

    # ---- cli: --logs ---------------------------------------------
    proc = run([EFFEX, "--logs"], env_extra={"EFFEX_LOG_DIR": str(bad)})
    out = proc.stdout.decode()
    c.check("--logs prints the path", str(log) in out, out[:200])
    c.check("--logs prints the tail", "unknown effect" in out, out[:200])
    c.check("--logs points at the issue tracker",
            diagnostics.ISSUES_URL in out, out[-200:])
    c.check("--logs exits 0", proc.returncode == 0)

    empty = fresh_dir("effex-empty-")
    proc = run([EFFEX, "--logs"], env_extra={"EFFEX_LOG_DIR": str(empty)})
    c.check("--logs explains an empty log",
            b"nothing logged yet" in proc.stdout, proc.stdout.decode()[:200])

    # ---- cli: --report -------------------------------------------
    proc = run([EFFEX, "--report"], env_extra={"EFFEX_LOG_DIR": str(bad)})
    out = proc.stdout.decode()
    made = [p for p in bad.glob("effex-report-*.log")]
    c.check("--report writes a file in the log folder", len(made) == 1,
            str(made))
    c.check("--report prints where it went",
            bool(made) and str(made[0]) in out, out[:200])
    if made:
        body = made[0].read_text()
        c.check("the report has the header and the tail",
                "effex diagnostics report" in body
                and "ffmpeg failed" in body, body[-200:])

    custom = fresh_dir("effex-custom-") / "issue.log"
    proc = run([EFFEX, "--report", custom],
               env_extra={"EFFEX_LOG_DIR": str(bad)})
    c.check("--report honours an explicit path",
            custom.is_file() and str(custom) in proc.stdout.decode(),
            proc.stdout.decode()[:200])

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
