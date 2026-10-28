#!/usr/bin/env python3
"""Platform + installer tests: no windows box needed, the tables are data.

effex.py is imported as a module so the pure helpers can be checked directly:

  - host_os() agrees with python's own platform report
  - each OS gets its own package managers, and only unix ones use sudo
  - the command for a missing binary is the one that platform expects
  - a missing ffprobe with ffmpeg present is still a failed check
  - windows key codes decode to the menu's names
  - the venv interpreter path follows the platform layout

Run: python3 tests/platform_support.py
"""
import os
import platform
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import effex  # noqa: E402
from harness import Checker, have  # noqa: E402


def main() -> int:
    c = Checker("platform + installers")

    # ---- host detection ------------------------------------------------
    expected = {"Windows": "windows", "Darwin": "macos",
                "Linux": "linux"}[platform.system()]
    c.check("host_os matches platform.system", effex.host_os() == expected,
            effex.host_os())
    c.check("linux_distro is a bare id or empty",
            isinstance(effex.linux_distro(), str)
            and effex.linux_distro() == effex.linux_distro().strip().lower(),
            effex.linux_distro())

    # ---- per-OS installer tables ---------------------------------------
    managers = {os_name: [i.manager for i in effex.installers_for(os_name)]
                for os_name in ("windows", "macos", "linux")}
    c.check("windows uses winget, choco or scoop",
            managers["windows"] == ["winget", "choco", "scoop"],
            str(managers["windows"]))
    c.check("macos uses homebrew", "brew" in managers["macos"],
            str(managers["macos"]))
    c.check("linux covers the five distro managers",
            {"apt-get", "dnf", "pacman", "zypper", "apk"}
            <= set(managers["linux"]), str(managers["linux"]))
    c.check("no windows command asks for sudo",
            all(command[0] != "sudo"
                for installer in effex.installers_for("windows")
                for command in effex.install_commands(
                    installer, ["ffmpeg", "ffprobe"])))
    distro_managers = [i for i in effex.installers_for("linux")
                       if i.manager != "brew"]
    c.check("distro managers all ask for sudo",
            all(command[0] == "sudo" for installer in distro_managers
                for command in effex.install_commands(installer, ["ffmpeg"])))

    # ---- detection picks what is actually there ------------------------
    picked = effex.detect_installer("windows",
                                    which=lambda name: name == "choco")
    c.check("detect_installer honours PATH",
            picked is not None and picked.manager == "choco",
            str(picked))
    c.check("detect_installer returns None with no manager",
            effex.detect_installer("windows", which=lambda name: False) is None)

    # ---- the command each platform would run ---------------------------
    winget = next(i for i in effex.installers_for("windows")
                  if i.manager == "winget")
    commands = effex.install_commands(winget, ["ffmpeg", "ffprobe"])
    c.check("winget installs ffmpeg and ffprobe with one package",
            len(commands) == 1 and "Gyan.FFmpeg" in commands[0],
            str(commands))
    c.check("winget accepts the agreements unattended",
            "--accept-package-agreements" in commands[0]
            and "--accept-source-agreements" in commands[0])
    c.check("winget command starts with winget", commands[0][0] == "winget")

    apt = next(i for i in effex.installers_for("linux")
               if i.manager == "apt-get")
    c.check("apt command is the documented one",
            effex.install_commands(apt, ["ffmpeg", "ffprobe"])
            == [["sudo", "apt-get", "install", "-y", "ffmpeg"]],
            str(effex.install_commands(apt, ["ffmpeg", "ffprobe"])))
    c.check("install_text joins several commands",
            effex.install_text(winget, ["ffmpeg"]) ==
            "winget install -e --id Gyan.FFmpeg "
            "--accept-package-agreements --accept-source-agreements",
            effex.install_text(winget, ["ffmpeg"]))
    c.check("no missing binary, no command",
            effex.install_commands(apt, []) == [])

    # ---- requirements model -------------------------------------------
    broken = effex.Tools(None, None)
    reqs = effex.check_requirements(broken)
    labels = [r.label for r in effex.missing_requirements(reqs)]
    c.check("nothing installed means ffmpeg and ffprobe are missing",
            labels == ["ffmpeg", "ffprobe"], str(labels))
    c.check("python counts as installed", len(reqs) == 6, str(len(reqs)))
    c.check("optional modules never count as required",
            all(not r.required for r in reqs[3:]))
    c.check("a broken check says so",
            effex.requirement_summary(reqs).startswith("2 missing"),
            effex.requirement_summary(reqs))
    lines = effex.requirement_lines(reqs, 60)
    c.check("missing rows read not on PATH",
            any("not on PATH" in line for line in lines), str(lines))
    c.check("rows keep the binary name", any("ffprobe" in line
                                             for line in lines))

    fixed = effex.Tools("/usr/bin/ffmpeg", "/usr/bin/ffprobe")
    good = effex.check_requirements(fixed)
    c.check("both binaries present means nothing required is missing",
            effex.missing_requirements(good) == [])
    c.check("summary reports the required count",
            effex.requirement_summary(good) == "all 3 installed",
            effex.requirement_summary(good))
    c.check("a healthy panel has no missing marker",
            all("not on PATH" not in line
                for line in effex.requirement_lines(good, 60)))

    # ffprobe alone missing is a real gap (two-video mashing needs it)
    half = effex.Tools("/usr/bin/ffmpeg", None)
    c.check("ffmpeg without ffprobe fails the tools check", not half.ok)
    c.check("ffmpeg without ffprobe is named",
            [r.label for r in effex.missing_requirements(
                effex.check_requirements(half))] == ["ffprobe"])

    # ---- pip hints only mention files that are actually missing --------
    fake_reqs = [
        effex.Requirement("numpy", "numpy", False, "", "sediment",
                          pip_file="requirements-stateful.txt"),
        effex.Requirement("cv2", "opencv", False, "", "collapse",
                          pip_file="requirements-collapse.txt"),
        effex.Requirement("ffmpeg", "ffmpeg", True, "/bin/ffmpeg"),
    ]
    hint = effex.pip_hint(fake_reqs)
    c.check("pip hint covers the missing optional modules",
            "requirements-stateful.txt" in hint
            and "requirements-collapse.txt" in hint, hint)
    c.check("pip hint skips present requirements",
            "av" not in hint and hint.startswith("python -m pip install"))

    # ---- windows specifics (pure data / decode paths) ------------------
    if effex.is_windows():
        c.check("windows tool dirs only lists real folders",
                all(Path(folder).is_dir()
                    for folder in effex.windows_tool_dirs()))
        c.check("refresh_windows_path reads the registry path",
                effex.refresh_windows_path() is True)
    else:
        c.check("windows tool dirs are empty off windows",
                effex.windows_tool_dirs() == [])
        c.check("refresh_windows_path is a no-op off windows",
                effex.refresh_windows_path() is False)
    c.check("prepare_console never raises",
            effex.prepare_console() is None)

    keys = {"H": "up", "P": "down", "M": "right", "K": "left",
            "G": "home", "O": "end", "I": "pgup", "Q": "pgdn"}
    c.check("windows arrow codes decode",
            all(effex.decode_windows_key("\xe0", code) == name
                for code, name in keys.items()),
            str({code: effex.decode_windows_key("\xe0", code)
                 for code in keys}))
    c.check("the NUL prefix decodes the same way",
            effex.decode_windows_key("\x00", "H") == "up")
    c.check("a lone escape is esc", effex.decode_windows_key("\x1b") == "esc")
    c.check("plain letters pass through", effex.decode_windows_key("q") == "q")
    c.check("enter passes through", effex.decode_windows_key("\r") == "\r")
    c.check("unknown windows codes fall back to esc",
            effex.decode_windows_key("\xe0", "Z") == "esc")

    # ---- venv layout per platform -------------------------------------
    for parts, is_win in ((("Scripts", "python.exe"), True),
                          (("bin", "python3"), False)):
        root = Path(tempfile.mkdtemp(prefix="effex-venv-"))
        target = root / ".venv" / parts[0]
        target.mkdir(parents=True)
        binary = target / parts[1]
        binary.write_text("")
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        found = effex.venv_python_path(root, windows=is_win)
        c.check(f"venv path uses the {'windows' if is_win else 'posix'} layout",
                found is not None and found.endswith(os.path.join(*parts)),
                str(found))
        if not is_win:
            c.check("posix layout is not accepted as windows",
                    effex.venv_python_path(root, windows=True) is None,
                    str(effex.venv_python_path(root, windows=True)))
    c.check("no venv, no re-exec",
            effex.venv_python_path(Path(tempfile.mkdtemp(prefix="effex-novenv-")),
                                   windows=True) is None)

    # ---- which_tool agrees with shutil.which on this host --------------
    name = "ffmpeg" if have("ffmpeg") else "sh"
    found = effex.which_tool(name)
    c.check("which_tool finds a binary on PATH",
            found is not None and Path(found).is_file(),
            f"{found} vs {shutil.which(name)}")

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
