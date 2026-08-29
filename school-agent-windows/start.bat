@echo off
setlocal
cd /d "%~dp0"
title School Agent

REM If anything is missing, hand off to setup.bat, which installs Python
REM itself if the computer doesn't have it. Keeping the install logic in
REM one place means this file stays the boring "just run it" entry point.
if not exist ".venv\Scripts\python.exe" goto bootstrap

REM Verify the environment still works - a half-deleted .venv or an OS
REM upgrade that moved Python underneath it both look "installed" but fail.
".venv\Scripts\python.exe" -c "import flask, yaml, icalendar" >nul 2>&1
if errorlevel 1 goto bootstrap

REM Dependencies can change under an existing install (a new release adds a
REM package). The import check above only proves the ORIGINAL three are there,
REM so it happily starts an environment that is missing something newer.
REM Stamp the venv with a hash of requirements.txt and re-sync when it moves.
".venv\Scripts\python.exe" -c "import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(); s=pathlib.Path('.venv/.reqs-stamp'); sys.exit(0 if s.exists() and s.read_text().strip()==h else 1)" >nul 2>&1
if errorlevel 1 (
    echo Updating dependencies...
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 goto bootstrap
    ".venv\Scripts\python.exe" -c "import hashlib,pathlib; pathlib.Path('.venv/.reqs-stamp').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())"
)

echo Starting School Agent - your browser will open automatically...
".venv\Scripts\python.exe" ui\server.py
echo.
echo School Agent has stopped. You can close this window.
pause
exit /b 0

:bootstrap
echo.
echo Something needed for School Agent is missing - running setup first.
echo.
call setup.bat
exit /b %errorlevel%
