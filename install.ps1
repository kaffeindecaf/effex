# effex installer for windows: put an `effex` shim on PATH so it runs anywhere.
# idempotent - run it again after moving the repo and it repoints the shim.
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = Join-Path $env:LOCALAPPDATA "effex\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

$python = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Host "python is not on PATH - install it from https://python.org first"
        exit 1
    }
    $python = $found.Source
}

$script = Join-Path $here "effex.py"
$shim = Join-Path $bin "effex.cmd"
@"
@echo off
"$python" "$script" %*
"@ | Set-Content -Path $shim -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    Write-Host "note: added $bin to your user PATH - open a new terminal"
}
Write-Host "effex -> $shim"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Host "note: ffmpeg is missing - run effex and answer y, or install it with"
    Write-Host "      winget install -e --id Gyan.FFmpeg"
}
