#!/usr/bin/env python3
"""Exercise the real PowerShell installer without downloads or registry writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
HARNESS = r'''
$ErrorActionPreference = "Stop"
. $env:EFFEX_INSTALL_SCRIPT
$script:Calls = [Collections.Generic.List[object]]::new()
$script:PythonReady = $env:TEST_BOOTSTRAP -ne "1"
$script:ToolsReady = $env:TEST_MISSING_TOOL -ne "1"
$script:Answers = $env:TEST_ANSWERS -split ","
$script:AnswerIndex = 0
function Read-Host($Prompt) {
    $answer = $script:Answers[$script:AnswerIndex]
    $script:AnswerIndex++
    return $answer
}
function Find-EffexPython($Root, $MinimumMinor) {
    if ($script:PythonReady) { return (Join-Path $Root "base-python.exe") }
    return $null
}
function Get-Command($Name, $ErrorAction) {
    if ($Name -eq "winget") {
        if ($env:TEST_NO_WINGET -eq "1") { return $null }
        return [pscustomobject]@{Source = "mock-winget"}
    }
    if ($Name -eq "ffmpeg" -or $Name -eq "ffprobe") {
        if ($script:ToolsReady -or $Name -eq "ffmpeg") {
            return [pscustomobject]@{Source = "mock-$Name"}
        }
        return $null
    }
    Microsoft.PowerShell.Core\Get-Command $Name -ErrorAction SilentlyContinue
}
function Update-EffexToolPath { }
function Add-EffexUserPath($Bin) {
    $script:Calls.Add([pscustomobject]@{description="PATH"; arguments=@($Bin)})
}
function Invoke-EffexCommand($Executable, [string[]]$Arguments, $Description) {
    $script:Calls.Add([pscustomobject]@{executable=$Executable; arguments=$Arguments; description=$Description})
    if ($Description -eq $env:TEST_FAIL) { throw "simulated command failure" }
    if ($Arguments -contains "Python.Python.3.13") { $script:PythonReady = $true }
    if ($Arguments -contains "Gyan.FFmpeg") { $script:ToolsReady = $true }
    if ($Description -eq "Creating .venv") {
        $folder = Join-Path $Arguments[-1] "Scripts"
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        New-Item -ItemType File -Path (Join-Path $folder "python.exe") | Out-Null
    }
}
$ok = $true
$message = ""
try {
    Install-Effex -Root $env:TEST_ROOT -Bin (Join-Path $env:TEST_ROOT "bin")
    if ($env:TEST_RERUN -eq "1") {
        Install-Effex -Root $env:TEST_ROOT -Bin (Join-Path $env:TEST_ROOT "bin")
    }
} catch {
    $ok = $false
    $message = $_.ToString()
}
@{ok=$ok; message=$message; calls=@($script:Calls.ToArray()); prompts=$script:AnswerIndex} |
    ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $env:TEST_RESULT -Encoding UTF8
'''


@unittest.skipUnless(POWERSHELL, "Windows PowerShell required")
class WindowsInstallerTests(unittest.TestCase):
    def simulate(self, answers="y", **flags):
        temp = tempfile.TemporaryDirectory(prefix="effex installer '")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        harness = folder / "harness.ps1"
        harness.write_text(HARNESS)
        result = folder / "result.json"
        env = dict(os.environ, EFFEX_INSTALL_SCRIPT=str(ROOT / "install.ps1"),
                   TEST_ROOT=str(folder), TEST_RESULT=str(result), TEST_ANSWERS=answers)
        env.update({"TEST_" + key.upper(): value for key, value in flags.items()})
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(harness)], capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        return json.loads(result.read_text(encoding="utf-8-sig")), folder

    def test_yes_installs_missing_tools_and_all_packages(self):
        data, folder = self.simulate(bootstrap="1", missing_tool="1")
        self.assertTrue(data["ok"], data["message"])
        packages = [c["arguments"] for c in data["calls"] if c["description"].startswith("Installing")]
        self.assertTrue(any("Python.Python.3.13" in args for args in packages))
        self.assertTrue(any("Gyan.FFmpeg" in args for args in packages))
        pip = next(c for c in data["calls"] if c["description"] == "Installing all optional dependencies")
        self.assertIn(str(folder / "requirements-collapse.txt"), pip["arguments"])
        self.assertIn(str(folder / "requirements-inversion.txt"), pip["arguments"])
        self.assertEqual(Path(pip["executable"]), folder / ".venv" / "Scripts" / "python.exe")
        self.assertTrue(any(c["description"] == "Checking optional dependencies" for c in data["calls"]))
        shim = (folder / "bin" / "effex.cmd").read_text()
        self.assertIn('call "' + str(folder / "effex.cmd") + '" %*', shim)

    def test_no_and_empty_answer_never_install_dependencies(self):
        for answer in ("n", ""):
            data, folder = self.simulate(answers=answer)
            self.assertTrue(data["ok"], data["message"])
            self.assertEqual([c["description"] for c in data["calls"]], ["PATH"])
            self.assertFalse((folder / ".venv").exists())

    def test_invalid_answer_is_reprompted(self):
        data, _ = self.simulate(answers="maybe,yes")
        self.assertTrue(data["ok"], data["message"])
        self.assertEqual(data["prompts"], 2)

    def test_failure_never_reports_completed_installation(self):
        data, folder = self.simulate(fail="Installing all optional dependencies")
        self.assertFalse(data["ok"])
        self.assertIn("simulated", data["message"])
        self.assertFalse((folder / "bin" / "effex.cmd").exists())

    def test_missing_winget_and_declined_python_are_clear(self):
        data, _ = self.simulate(bootstrap="1", no_winget="1")
        self.assertFalse(data["ok"])
        self.assertIn("WinGet", data["message"])
        data, _ = self.simulate(answers="n", bootstrap="1")
        self.assertFalse(data["ok"])
        self.assertIn("Python was not found", data["message"])

    def test_rerun_reuses_venv(self):
        data, _ = self.simulate(answers="y,y", rerun="1")
        self.assertTrue(data["ok"], data["message"])
        self.assertEqual(sum(c["description"] == "Creating .venv" for c in data["calls"]), 1)

    def test_native_failure_exit_code_is_checked(self):
        script = ". $env:EFFEX_INSTALL_SCRIPT; Invoke-EffexCommand $env:TEST_SHELL @('-NoProfile', '-Command', 'exit 7') 'test command'"
        env = dict(os.environ, EFFEX_INSTALL_SCRIPT=str(ROOT / "install.ps1"), TEST_SHELL=POWERSHELL)
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-Command", script], capture_output=True, env=env, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"exit 7", proc.stderr)


if __name__ == "__main__":
    unittest.main()
