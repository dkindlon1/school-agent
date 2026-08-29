#!/usr/bin/env bash
# First-time setup for macOS. Finds or installs Python, builds a private
# environment for the app, installs its libraries, and starts it.
#
# You should not normally run this yourself — "Open School Agent.command"
# calls it when
# something is missing, and never again after that.
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  ══════════════════════════════════════════════════════"
echo "    School Agent — first-time setup"
echo "  ══════════════════════════════════════════════════════"
echo

# --- 1. A Python we can actually use ---------------------------------------
find_python() {
    # Homebrew's python is often not first on PATH, and macOS's own
    # /usr/bin/python3 is a stub that prompts for Xcode tools. Check the
    # versioned names first, then the generic one.
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
                echo "$candidate"; return 0
            fi
        fi
    done
    return 1
}

if ! PY="$(find_python)"; then
    echo "  [1/4] Python 3.10 or newer isn't installed. Installing it now."
    echo
    if command -v brew >/dev/null 2>&1; then
        echo "        Using Homebrew — this takes a few minutes the first time."
        brew install python@3.12
    else
        echo "  Homebrew isn't installed, and it's the sane way to get Python on a Mac."
        echo
        echo "  Install Homebrew by pasting this into Terminal, then run setup again:"
        echo
        echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        echo
        echo "  Or install Python directly from https://www.python.org/downloads/macos/"
        echo
        exit 1
    fi
    PY="$(find_python)" || { echo "  Python still not found after installing. Open a new Terminal window and try again."; exit 1; }
fi
echo "  [1/4] Python found: $($PY --version)"
echo

# --- 2. A private environment, so this app never touches your system Python -
if [ -x ".venv/bin/python" ]; then
    echo "  [2/4] App environment already exists — reusing it."
else
    echo "  [2/4] Creating a private environment for this app..."
    "$PY" -m venv .venv
fi
echo

# --- 3. Libraries -----------------------------------------------------------
echo "  [3/4] Installing the libraries the app needs..."
echo "        (calendar sync, PDF reading, flashcard scheduling, web UI)"
".venv/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check
".venv/bin/python" -m pip install -r requirements.txt --quiet --disable-pip-version-check
# Stamp what was installed, so start.sh can tell when requirements.txt
# changes under an existing venv and re-sync instead of starting broken.
".venv/bin/python" -c "import hashlib,pathlib; pathlib.Path('.venv/.reqs-stamp').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())" >/dev/null 2>&1 || true
echo "        Done."
echo

# --- 4. Config --------------------------------------------------------------
echo "  [4/4] Setting up your config..."
mkdir -p config data
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp ".env.example" ".env"
    echo "        Created .env — that's where an AI key goes, if you use one."
else
    echo "        .env already exists — left it alone."
fi
echo
echo "  ══════════════════════════════════════════════════════"
echo "    Setup complete."
echo "  ══════════════════════════════════════════════════════"
echo
echo "  Starting School Agent. Your browser will open by itself."
echo "  From now on just double-click \"Open School Agent.command\" — setup"
echo "  won't run again."
echo
sleep 2
exec ".venv/bin/python" ui/server.py
