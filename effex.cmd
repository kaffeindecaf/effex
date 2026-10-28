@echo off
rem effex launcher for windows: python effex.py with every argument passed on.
rem .py files are not executable here, so this shim is what windows users run.
setlocal
set "here=%~dp0"
set "py=python"
where py >nul 2>nul && set "py=py -3"
if exist "%here%.venv\Scripts\python.exe" set py="%here%.venv\Scripts\python.exe"
%py% "%here%effex.py" %*
exit /b %errorlevel%
