@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title School Agent - Setup

echo.
echo   ============================================================
echo     School Agent - first-time setup
echo   ============================================================
echo.
echo   This checks your computer for everything the app needs and
echo   installs whatever is missing. It only has to run once.
echo.

REM ---------------------------------------------------------------
REM  1. Find a usable Python (3.10 or newer)
REM ---------------------------------------------------------------
set "PY="
call :find_python
if defined PY goto have_python

echo   [1/4] Python is not installed on this computer.
echo         Installing it now - this is a normal Microsoft-signed
echo         installer from python.org, and it installs just for you
echo         (no administrator password needed).
echo.

REM --- Try winget first: cleanest, already on Windows 10/11 ---
where winget >nul 2>&1
if %errorlevel%==0 (
    echo         Installing via winget, please wait...
    winget install -e --id Python.Python.3.12 --scope user --silent ^
        --accept-source-agreements --accept-package-agreements >nul 2>&1
    call :find_python
    if defined PY goto have_python
    echo         winget did not succeed - falling back to a direct download.
)

REM --- Fallback: download the official installer straight from python.org ---
set "PYURL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
set "PYEXE=%TEMP%\school-agent-python-3.12.7.exe"
echo         Downloading Python 3.12.7 from python.org...
curl.exe -L --fail --silent --show-error -o "%PYEXE%" "%PYURL%"
if errorlevel 1 (
    echo.
    echo   Could not download Python automatically ^(no internet?^).
    echo   Install it by hand from https://www.python.org/downloads/
    echo   and be sure to tick "Add python.exe to PATH", then run this again.
    echo.
    pause
    exit /b 1
)
echo         Installing Python, this takes a minute...
"%PYEXE%" /passive InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0
del "%PYEXE%" >nul 2>&1

call :find_python
if not defined PY (
    echo.
    echo   Python was installed but this window can't see it yet.
    echo   Close this window, open a new one, and run setup.bat again.
    echo   ^(Windows only refreshes PATH for new windows.^)
    echo.
    pause
    exit /b 1
)

:have_python
for /f "tokens=*" %%v in ('"!PY!" --version 2^>^&1') do set "PYVER=%%v"
echo   [1/4] Python found: !PYVER!
echo.

REM ---------------------------------------------------------------
REM  2. Private environment for this app
REM ---------------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo   [2/4] App environment already exists - reusing it.
) else (
    echo   [2/4] Creating a private environment for this app...
    "!PY!" -m venv .venv
    if errorlevel 1 (
        echo         Could not create the environment. Try deleting the
        echo         .venv folder if it exists, then run setup.bat again.
        pause
        exit /b 1
    )
)
echo.

REM ---------------------------------------------------------------
REM  3. Libraries
REM ---------------------------------------------------------------
echo   [3/4] Installing the libraries the app needs...
echo         ^(calendar sync, PDF reading, flashcard scheduling, web UI^)
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
REM Stamp what was installed, so start.bat can tell when requirements.txt
REM changes under an existing venv and re-sync instead of starting broken.
".venv\Scripts\python.exe" -c "import hashlib,pathlib; pathlib.Path('.venv/.reqs-stamp').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Some libraries failed to install. The most common cause is no
    echo   internet connection. Check your connection and run this again.
    echo.
    pause
    exit /b 1
)
echo         Done.
echo.

REM ---------------------------------------------------------------
REM  4. Config files
REM ---------------------------------------------------------------
echo   [4/4] Setting up your config...
if not exist "config" mkdir config
if not exist "data" mkdir data
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo         Created .env - this is where an AI key goes, if you use one.
    )
) else (
    echo         .env already exists - left it alone.
)
echo.

echo   ============================================================
echo     Setup complete.
echo   ============================================================
echo.
echo   Starting School Agent now. Your browser will open by itself.
echo   From now on just use start.bat - setup won't run again.
echo.
timeout /t 3 >nul
".venv\Scripts\python.exe" ui\server.py
echo.
echo   School Agent has stopped. You can close this window.
pause
exit /b 0

REM ---------------------------------------------------------------
REM  Helper: locate a Python that is 3.10 or newer
REM ---------------------------------------------------------------
:find_python
set "PY="
REM The py launcher is the most reliable when several versions exist.
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if !errorlevel!==0 (
        for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%p"
        if defined PY exit /b 0
    )
)
where python >nul 2>&1
if %errorlevel%==0 (
    python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if !errorlevel!==0 (
        for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%p"
        if defined PY exit /b 0
    )
)
REM Freshly installed per-user copies aren't on PATH in this window yet.
for %%v in (313 312 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
        exit /b 0
    )
    if exist "%ProgramFiles%\Python%%v\python.exe" (
        set "PY=%ProgramFiles%\Python%%v\python.exe"
        exit /b 0
    )
)
exit /b 1
