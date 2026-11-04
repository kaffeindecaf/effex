# Windows installer: dependencies live in the repo's .venv; the launcher is on PATH.
$ErrorActionPreference = "Stop"
$script:EffexRoot = $PSScriptRoot

function Invoke-EffexCommand($Executable, [string[]]$Arguments, $Description) {
    & $Executable @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit $LASTEXITCODE). Fix the error above and run install.ps1 again."
    }
}

function Update-EffexToolPath {
    $paths = @($env:Path,
        [Environment]::GetEnvironmentVariable("Path", "Machine"),
        [Environment]::GetEnvironmentVariable("Path", "User"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"))
    $env:Path = ($paths | Where-Object { $_ }) -join ";"
}

function Find-EffexPython($Root, [int]$MinimumMinor = 8) {
    $candidates = @((Join-Path $Root ".venv\Scripts\python.exe"), "py", "python",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"))
    foreach ($candidate in $candidates) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        $pythonArgs = @()
        if ($candidate -eq "py") { $pythonArgs += "-3" }
        $pythonArgs += @("-c", "import sys; assert sys.version_info >= (3, $MinimumMinor); print(sys.executable)")
        try {
            $resolved = & $found.Source @pythonArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath "$resolved")) {
                return "$resolved"
            }
        } catch { }
    }
    return $null
}

function Install-EffexTool($Package) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "WinGet is required to download $Package. Install App Installer from Microsoft Store, then rerun install.ps1."
    }
    Invoke-EffexCommand $winget.Source @("install", "--exact", "--id", $Package,
        "--source", "winget", "--accept-package-agreements", "--accept-source-agreements") "Installing $Package"
    Update-EffexToolPath
}

function Add-EffexUserPath($Bin) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { $_ })
    $normalized = @($entries | ForEach-Object { $_.TrimEnd('\') })
    if ($normalized -notcontains $Bin.TrimEnd('\')) {
        [Environment]::SetEnvironmentVariable("Path", (($entries + $Bin) -join ";"), "User")
        Write-Host "note: added $Bin to your user PATH - open a new terminal"
    }
}

function Install-Effex($Root = $script:EffexRoot, $Bin = (Join-Path $env:LOCALAPPDATA "effex\bin")) {
    Write-Host "effex can install Python (if missing), FFmpeg + FFprobe, and all optional packages:"
    Write-Host "NumPy, OpenCV and PyAV. Python packages go into $Root\.venv."
    do {
        $answer = Read-Host "Download/install all dependencies, including optional ones? [y/N]"
        $answer = "$answer".Trim().ToLowerInvariant()
    } while ($answer -notin @("", "y", "yes", "n", "no"))
    $installDependencies = $answer -in @("y", "yes")
    $python = Find-EffexPython $Root

    if ($installDependencies) {
        Update-EffexToolPath
        $python = Find-EffexPython $Root 10
        if (-not $python) {
            Install-EffexTool "Python.Python.3.13"
            $python = Find-EffexPython $Root 10
            if (-not $python) { throw "Python 3.10+ was not found after installation. Open a new terminal and rerun install.ps1." }
        }
        if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
            -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
            Install-EffexTool "Gyan.FFmpeg"
        }
        foreach ($tool in @("ffmpeg", "ffprobe")) {
            $found = Get-Command $tool -ErrorAction SilentlyContinue
            if (-not $found) { throw "$tool was not found after installation. Open a new terminal and rerun install.ps1." }
            Invoke-EffexCommand $found.Source @("-version") "Checking $tool"
        }
        $venv = Join-Path $Root ".venv"
        $venvPython = Join-Path $venv "Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $venv)) {
            Invoke-EffexCommand $python @("-m", "venv", $venv) "Creating .venv"
        }
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw "The existing .venv is incomplete. Repair or move it, then rerun install.ps1."
        }
        $python = $venvPython
        Invoke-EffexCommand $python @("-c", "import sys; assert sys.version_info >= (3, 10), 'Dependencies need Python 3.10+; recreate .venv with a newer Python'") "Checking .venv"
        Invoke-EffexCommand $python @("-m", "ensurepip", "--upgrade") "Preparing pip"
        Invoke-EffexCommand $python @("-m", "pip", "install", "--upgrade", "pip") "Updating pip"
        Invoke-EffexCommand $python @("-m", "pip", "install", "-r", (Join-Path $Root "requirements-collapse.txt"),
            "-r", (Join-Path $Root "requirements-inversion.txt")) "Installing all optional dependencies"
        Invoke-EffexCommand $python @("-c", "import numpy, cv2, av; print('All optional dependencies are ready')") "Checking optional dependencies"
    } elseif (-not $python) {
        throw "Python was not found. Rerun install.ps1 and answer y to install dependencies."
    } else {
        Write-Host "Skipping dependency downloads. Only the effex launcher will be installed."
    }

    New-Item -ItemType Directory -Force -Path $Bin | Out-Null
    # Call the repo launcher so a later .venv creation is picked up automatically.
    $launcher = Join-Path $Root "effex.cmd"
    $shim = Join-Path $Bin "effex.cmd"
    "@echo off`r`ncall `"$launcher`" %*`r`nexit /b %errorlevel%" | Set-Content -LiteralPath $shim -Encoding ASCII
    Add-EffexUserPath $Bin
    Write-Host "effex -> $shim"
}

# Dot-sourcing exposes the helpers for isolated installer tests without installing.
if ($MyInvocation.InvocationName -ne ".") {
    try { Install-Effex } catch { Write-Error $_; exit 1 }
}
