#!/usr/bin/env python3
"""Stdlib pty driver for keypress smoke tests of terminal apps.

Why not script(1): script(1) gives the child a controlling terminal but the
bytes you pipe in arrive before the app switches to cbreak - they get echoed
and line-buffered, so a menu reads one key and hangs. This driver spawns the
app with a pty slave as stdin/stdout/stderr and writes keys to the master
whenever the app is actually ready for them.

Usage:
    d = PtyDriver(["python3", "effex.py"])
    d.wait_for("main menu")
    d.send(b"p")          # single key
    d.send(b"\\r")         # enter
    d.send(b"\\x1bOB")     # down arrow (application mode)
    d.send(b"q")
    assert d.expect_exit(6) == 0

Arrow keys in a pty are application-mode: down \\x1bOB, up \\x1bOA,
right \\x1bOC, left \\x1bOD.
"""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time


class PtyDriver:
    def __init__(self, argv, rows=44, cols=110, env_extra=None, timeout=8,
                 cwd=None):
        self.master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass
        env = dict(os.environ, TERM="xterm-256color",
                   LC_ALL="C.UTF-8", LANG="C.UTF-8")
        if env_extra:
            env.update(env_extra)
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave,
                                     stderr=slave, env=env, cwd=cwd,
                                     close_fds=True)
        os.close(slave)
        self.buf = b""
        self.timeout = timeout

    def pump(self, seconds=0.4):
        """drain whatever the app has drawn, keeping a bounded buffer"""
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.buf += chunk
            if len(self.buf) > 400_000:
                self.buf = self.buf[-200_000:]

    def text(self):
        return self.buf.decode("utf-8", "replace")

    def wait_for(self, marker, timeout=None):
        """True once the marker shows up in the captured output"""
        end = time.time() + (timeout or self.timeout)
        while time.time() < end:
            if marker in self.text():
                return True
            self.pump(0.2)
        return False

    def send(self, data, pause=0.35):
        os.write(self.master, data if isinstance(data, bytes)
                 else data.encode())
        time.sleep(pause)
        self.pump()

    def expect_exit(self, seconds=6):
        try:
            self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None
        return self.proc.returncode

    def close(self):
        try:
            os.close(self.master)
        except OSError:
            pass


if __name__ == "__main__":
    driver = PtyDriver(sys.argv[1:])
    driver.pump(2.0)
    rc = driver.expect_exit(2)
    print(driver.text()[-4000:])
    print("exit:", rc if rc is not None else "still running (killed)")
    driver.close()
