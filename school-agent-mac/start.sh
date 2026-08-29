#!/usr/bin/env bash
# The boring "just run it" entry point. Hands off to setup.sh whenever
# anything is missing, so there is exactly one place that knows how to install.
set -euo pipefail
cd "$(dirname "$0")"

needs_setup=0
[ -x ".venv/bin/python" ] || needs_setup=1
if [ "$needs_setup" -eq 0 ]; then
    ".venv/bin/python" -c "import flask, yaml, icalendar" >/dev/null 2>&1 || needs_setup=1
fi

if [ "$needs_setup" -eq 1 ]; then
    echo "Something needed for School Agent is missing — running setup first."
    exec ./setup.sh
fi

# Re-sync dependencies when requirements.txt has moved since the last install.
# The import check above only proves the original three are there, so without
# this an existing environment silently misses anything newly added.
stamp_ok=1
".venv/bin/python" -c "import hashlib,pathlib,sys; h=hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest(); s=pathlib.Path('.venv/.reqs-stamp'); sys.exit(0 if s.exists() and s.read_text().strip()==h else 1)" >/dev/null 2>&1 || stamp_ok=0
if [ "$stamp_ok" -eq 0 ]; then
    echo "Updating dependencies..."
    ".venv/bin/python" -m pip install -q -r requirements.txt
    ".venv/bin/python" -c "import hashlib,pathlib; pathlib.Path('.venv/.reqs-stamp').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())"
fi

echo "Starting School Agent — your browser will open automatically..."
exec ".venv/bin/python" ui/server.py
