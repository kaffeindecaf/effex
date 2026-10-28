#!/usr/bin/env python3
"""requirements panel + install prompt tests, using a fake PATH.

Never uninstall anything: the suite builds a throwaway bin folder holding an
apt-get shim, a sudo shim and (optionally) an ffmpeg shim that execs the real
binary, points PATH at it, and checks that effex

  * reports every missing requirement instead of crashing
  * prints the install command and exits 1 when there is no terminal to ask
  * asks y/n on a terminal, runs the detected command when told yes
  * keeps going (with a warning) when told no
  * shows the requirement panel in the main menu either way

Run: python3 tests/install_prompt.py
"""
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, EFFEX, require_tools, run  # noqa: E402
from pty_driver import PtyDriver  # noqa: E402

ENTER = b"\r"


def shim(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def fake_bin(marker: Path, with_manager: bool = True,
             with_ffmpeg: bool = False) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="effex-fakebin-"))
    if with_manager:
        shim(folder, "apt-get", f"echo ran >> {marker}\necho 'fake apt-get $*'\n")
        shim(folder, "sudo", 'exec "$@"\n')
    if with_ffmpeg:
        real = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        shim(folder, "ffmpeg", f'exec {real} "$@"\n')
    return folder


def main() -> int:
    c = Checker("requirements + install prompt")
    if not require_tools(c):
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="effex-install-"))
    marker = tmp / "marker"
    marker.write_text("")
    logs = tmp / "logs"   # keep test failures out of the real log folder

    # ---- nothing installed, a package manager, no terminal -------------
    fake = fake_bin(marker)
    env = {"PATH": str(fake), "EFFEX_LOG_DIR": str(logs)}
    proc = run([sys.executable, EFFEX, "--check"], env_extra=env)
    text = (proc.stdout + proc.stderr).decode()
    c.check("missing ffmpeg reports an error",
            "not on PATH" in text and "ffmpeg" in text, text[-160:])
    c.check("missing ffprobe is reported too", "ffprobe" in text)
    c.check("--check exits 1 without ffmpeg", proc.returncode == 1)
    c.check("piped run prints the install command instead of asking",
            "apt-get install -y ffmpeg" in text
            and "now?" not in text, text[-200:])
    c.check("one command covers ffmpeg and ffprobe (same package)",
            text.count("apt-get install -y ffmpeg") == 1, text[-200:])

    proc = run([sys.executable, EFFEX, "--check", "--json"], env_extra=env)
    payload = proc.stdout.decode()
    c.check("--check --json stays parseable without ffmpeg",
            payload.strip().startswith("{"))
    c.check("--check --json says ok=false", '"ok": false' in payload)
    c.check("--check --json lists the requirements",
            '"requirements"' in payload and '"ffprobe"' in payload)

    proc = run([sys.executable, EFFEX, "stretch", tmp / "whatever.mp4"],
               env_extra=env)
    c.check("cli render without ffmpeg exits 1",
            proc.returncode == 1 and
            b"install it with" in proc.stdout + proc.stderr,
            (proc.stdout + proc.stderr).decode()[-160:])

    # ---- interactive decline ------------------------------------------
    d = PtyDriver([sys.executable, str(EFFEX)], env_extra=env, timeout=8)
    c.check("prompt appears",
            d.wait_for("install ffmpeg + ffprobe now?"))
    d.send(b"n")
    c.check("decline is acknowledged",
            d.wait_for("cannot continue without ffmpeg"))
    rc = d.expect_exit(6)
    c.check(f"declining exits cleanly (rc={rc})", rc == 0 or rc == 1)
    c.check("decline ran nothing", marker.read_text().strip() == "",
            marker.read_text())
    d.close()

    # ---- interactive accept -------------------------------------------
    d = PtyDriver([sys.executable, str(EFFEX)], env_extra=env, timeout=8)
    d.wait_for("install ffmpeg + ffprobe now?")
    d.send(b"y")
    c.check("accept runs the detected installer",
            d.wait_for("still not on PATH", timeout=10))
    c.check("the shim was actually executed",
            "ran" in marker.read_text(), marker.read_text())
    rc = d.expect_exit(6)
    c.check(f"accept path exits cleanly (rc={rc})", rc in (0, 1))
    d.close()

    # ---- no package manager at all ------------------------------------
    empty = fake_bin(marker, with_manager=False)
    proc = run([sys.executable, EFFEX, "stretch", tmp / "x.mp4"],
               env_extra={"PATH": str(empty), "EFFEX_LOG_DIR": str(logs)})
    text = (proc.stdout + proc.stderr).decode()
    c.check("no package manager falls back to a manual hint",
            "install ffmpeg + ffprobe manually" in text, text[-200:])
    c.check("manual hint links upstream", "ffmpeg.org" in text)

    # ---- ffmpeg present, ffprobe missing ------------------------------
    marker.write_text("")
    partial = fake_bin(marker, with_ffmpeg=True)
    env = {"PATH": str(partial), "EFFEX_LOG_DIR": str(logs)}
    proc = run([sys.executable, EFFEX, "--check"], env_extra=env)
    text = (proc.stdout + proc.stderr).decode()
    c.check("ffprobe-only gap still fails the check", proc.returncode == 1,
            text[-200:])
    c.check("ffprobe-only gap names the binary",
            "ffprobe" in text and "ffmpeg ok" not in text)

    d = PtyDriver([sys.executable, str(EFFEX)], env_extra=env, timeout=10)
    c.check("startup asks only for the missing one",
            d.wait_for("install ffprobe now?"), d.text()[-300:])
    d.send(b"n")
    c.check("menu banner counts the missing requirement",
            d.wait_for("1 of 3 missing") and d.wait_for("ffprobe"),
            d.text()[-400:])
    c.check("menu shows the requirement panel",
            d.wait_for("requirements") and d.wait_for("python"),
            d.text()[-400:])
    d.send(b"c")
    c.check("requirements screen offers the install",
            d.wait_for("install ffprobe now?"))
    d.send(b"n")
    c.check("requirements screen marks ffprobe missing",
            d.wait_for("not on PATH"), d.text()[-400:])
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"menu exits cleanly (rc={rc})", rc == 0)
    c.check("declined install never ran the shim",
            marker.read_text().strip() == "", marker.read_text())
    d.close()

    # ---- everything installed: the menu says so -----------------------
    d = PtyDriver([sys.executable, str(EFFEX)],
                 env_extra={"EFFEX_LOG_DIR": str(logs)}, timeout=10)
    c.check("main menu reports all requirements installed",
            d.wait_for("all 3 installed"), d.text()[-400:])
    c.check("main menu lists ffmpeg and ffprobe rows",
            d.wait_for("ffmpeg") and d.wait_for("ffprobe"))
    d.send(b"q")
    d.expect_exit(6)
    d.close()

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
