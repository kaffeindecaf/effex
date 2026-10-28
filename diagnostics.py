"""effex diagnostics: one bounded log file you can attach to a github issue.

Nothing here imports effex, so effex.py and the tests share it without cycles.
Every write is guarded: a broken log must never break a render. Only warnings,
errors and tracebacks are recorded - a clean run writes nothing, so the log
stays readable when something does go wrong.

    EFFEX_LOG_DIR   override where the log lives (tests, portable installs)
    effex --logs    print the path and the last lines
    effex --report  write a report file (environment + log tail) to attach
"""
import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

MAX_BYTES = 512 * 1024
KEEP = 5
ISSUES_URL = "https://github.com/kaffeindecaf/effex/issues/new"
LOG_NAME = "effex.log"

_ANSI = re.compile(r"\033\[[0-9;]*m")
_VERSION = re.compile(r"version\s+(\S+)")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def log_dir() -> Path:
    """per-user log folder: state dir on linux, Library/Logs on macos,
    LOCALAPPDATA on windows."""
    override = os.environ.get("EFFEX_LOG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / "effex" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "effex"
    state = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state")
    return Path(state) / "effex" / "logs"


def tool_version(binary: Optional[str]) -> str:
    """first version token of `ffmpeg -version`, or empty when unknown."""
    if not binary:
        return ""
    try:
        raw = subprocess.run([binary, "-version"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (raw.stdout or "").splitlines()[:1]
    match = _VERSION.search(first[0]) if first else None
    return match.group(1) if match else ""


class Session:
    """one process' diagnostics file: header, timestamped events, rotation."""

    def __init__(self, version: str = "?", issues_url: str = ISSUES_URL):
        self.version = version
        self.issues_url = issues_url
        self.fields: dict = {}
        self.wrote = False
        self._argv: list = []
        self._started = time.time()
        self._header_written = False
        self._lock = threading.RLock()

    # ---- state -----------------------------------------------------
    def start(self, version: Optional[str] = None,
              argv: Optional[list] = None,
              cwd: Optional[str] = None) -> "Session":
        if version:
            self.version = version
        self._argv = list(argv if argv is not None else sys.argv[1:])
        self._started = time.time()
        self.fields.update({
            "command": ("effex " + " ".join(self._argv)).strip(),
            "cwd": cwd or os.getcwd(),
            "platform": f"{platform.platform()} ({_os_name()})",
            "python": f"{platform.python_version()} ({sys.executable})",
        })
        return self

    def bind(self, key: str, value: str) -> None:
        self.fields[key] = str(value)

    def path(self) -> Path:
        return log_dir() / LOG_NAME

    def exists(self) -> bool:
        return self.path().is_file()

    # ---- writing ---------------------------------------------------
    def warn(self, message: str, detail: str = "") -> None:
        self.event("warn", message, detail)

    def error(self, message: str, detail: str = "") -> None:
        self.event("error", message, detail)

    def info(self, message: str, detail: str = "") -> None:
        self.event("info", message, detail)

    def exception(self, exc: BaseException, context: str = "") -> None:
        text = "".join(traceback.format_exception(type(exc), exc,
                                                  exc.__traceback__))
        label = context or type(exc).__name__
        self.event("error", f"{label}: {exc}", text.rstrip())

    def event(self, level: str, message: str, detail: str = "") -> None:
        """one record; never raises. a failed write is silently dropped."""
        with self._lock:
            try:
                target = self.path()
                target.parent.mkdir(parents=True, exist_ok=True)
                self._rotate(target)
                chunks = []
                if not self._header_written:
                    chunks.append(self.header())
                    self._header_written = True
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                chunks.append(f"{stamp} {level:<5} {_one_line(message)}")
                for line in str(detail or "").splitlines():
                    chunks.append("    | " + _one_line(line))
                with target.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write("\n".join(chunks) + "\n")
                self.wrote = True
            except Exception:
                return

    def header(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        lines = [f"==== effex {self.version}  {stamp} ===="]
        for key, value in self.fields.items():
            lines.append(f"{key:<9} {value}")
        for key in ("ffmpeg", "ffprobe"):
            value = self.fields.get(key, "")
            if value:
                version = tool_version(value)
                lines.append(f"{key + ' v':<9} {version or 'unknown'}")
        return "\n".join(lines)

    def _rotate(self, target: Path) -> None:
        try:
            if not target.is_file() or target.stat().st_size <= MAX_BYTES:
                return
        except OSError:
            return
        for index in range(KEEP, 0, -1):
            src = target if index == 1 else target.with_name(
                f"{target.name}.{index - 1}")
            dst = target.with_name(f"{target.name}.{index}")
            if src.exists():
                try:
                    os.replace(src, dst)
                except OSError:
                    pass

    # ---- reading ---------------------------------------------------
    def tail(self, lines: int = 40) -> str:
        target = self.path()
        if not target.is_file():
            return ""
        try:
            with target.open("r", encoding="utf-8", errors="replace") as fh:
                content = fh.readlines()
        except OSError:
            return ""
        return "".join(content[-max(1, lines):]).rstrip()

    def report(self, destination: Optional[Path] = None) -> Path:
        """a single self-contained file: what broke, where, with what tools."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if destination is None:
            destination = self.path().with_name(f"effex-report-{stamp}.log")
        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        head = [
            "effex diagnostics report",
            f"generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"issues    {self.issues_url}",
            "",
            self.header(),
            "",
            f"log file  {self.path()}",
            "---- log tail ----",
        ]
        body = self.tail(200) or "(nothing was logged in this file)"
        destination.write_text("\n".join(head) + "\n" + body + "\n",
                               encoding="utf-8", errors="replace")
        return destination


def _os_name() -> str:
    name = platform.system().lower()
    return {"windows": "windows", "darwin": "macos"}.get(name, "linux")


def _one_line(text: str) -> str:
    """log lines stay one line per event so `grep error` works"""
    return strip_ansi(str(text)).replace("\r", "").strip()


LOG = Session()
