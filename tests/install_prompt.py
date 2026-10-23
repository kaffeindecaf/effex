#!/usr/bin/env python3
"""ffmpeg missing / install prompt tests, using a fake PATH.

Never uninstall anything: the suite builds a throwaway bin folder holding an
apt-get shim and a sudo shim, points PATH at it, and checks that effex

  * reports the missing binary instead of crashing
  * prints the install command and exits 1 when there is no terminal to ask
  * asks y/n on a terminal, runs the detected command when told yes
  * keeps going (with a warning) when told no

Run: python3 tests/install_prompt.py
"""
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checker, EFFEX, require_tools, run  # noqa: E402
from pty_driver import PtyDriver  # noqa: E402

ENTER = b"\r"


def fake_bin(with_manager: bool, marker: Path) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="effex-fakebin-"))
    if with_manager:
        apt = folder / "apt-get"
        apt.write_text(f"#!/bin/sh\necho ran >> {marker}\necho 'fake apt-get $*'\n"
                       "exit 0\n")
        apt.chmod(apt.stat().st_mode | stat.S_IEXEC)
        sudo = folder / "sudo"
        sudo.write_text('#!/bin/sh\nexec "$@"\n')
        sudo.chmod(sudo.stat().st_mode | stat.S_IEXEC)
    return folder


def main() -> int:
    c = Checker("install prompt")
    if not require_tools(c):
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="effex-install-"))
    marker = tmp / "marker"
    marker.write_text("")

    # ---- no ffmpeg, a package manager, no terminal ---------------------
    fake = fake_bin(True, marker)
    env = {"PATH": str(fake)}
    proc = run([sys.executable, EFFEX, "--check"], env_extra=env)
    text = (proc.stdout + proc.stderr).decode()
    c.check("missing ffmpeg reports an error",
            "not on PATH" in text and "ffmpeg" in text, text[-160:])
    c.check("--check exits 1 without ffmpeg", proc.returncode == 1)
    c.check("piped run prints the install command instead of asking",
            "apt-get install -y ffmpeg" in text and "install ffmpeg now?" not in text)

    proc = run([sys.executable, EFFEX, "--check", "--json"], env_extra=env)
    c.check("--check --json stays parseable without ffmpeg",
            proc.stdout.decode().strip().startswith("{"))
    c.check("--check --json says ok=false",
            '"ok": false' in proc.stdout.decode())

    proc = run([sys.executable, EFFEX, "stretch", tmp / "whatever.mp4"],
               env_extra=env)
    c.check("cli render without ffmpeg exits 1",
            proc.returncode == 1 and
            b"install it with" in proc.stdout + proc.stderr,
            (proc.stdout + proc.stderr).decode()[-160:])

    # ---- interactive decline ------------------------------------------
    d = PtyDriver([sys.executable, str(EFFEX)], env_extra=env, timeout=8)
    c.check("prompt appears", d.wait_for("install ffmpeg now?"))
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
    d.wait_for("install ffmpeg now?")
    d.send(b"y")
    c.check("accept runs the detected installer",
            d.wait_for("still not on PATH", timeout=10))
    c.check("the shim was actually executed",
            "ran" in marker.read_text(), marker.read_text())
    rc = d.expect_exit(6)
    c.check(f"accept path exits cleanly (rc={rc})", rc in (0, 1))
    d.close()

    # ---- no package manager at all ------------------------------------
    empty = fake_bin(False, marker)
    proc = run([sys.executable, EFFEX, "stretch", tmp / "x.mp4"],
               env_extra={"PATH": str(empty)})
    c.check("no package manager falls back to a manual hint",
            b"install ffmpeg manually" in proc.stdout + proc.stderr,
            (proc.stdout + proc.stderr).decode()[-160:])
    c.check("manual hint links upstream",
            b"ffmpeg.org" in proc.stdout + proc.stderr)

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
