"""Dashboard-managed model settings, persisted to the venture's .env file.

Added 2026-08-25 so the owner can paste an API key / pick a model inside the
app instead of hand-editing .env in Notepad. Design constraints that matter:

- The .env file stays the single source of truth (the same one start.bat
  copies from .env.example and load_dotenv reads at boot) — this module
  edits it in place rather than inventing a second settings store.
- Writes are atomic (storage.atomic_write_text), and unknown lines/comments
  in the file are preserved — only the managed keys are touched.
- Changes are ALSO applied to os.environ in the running process, because
  llm.py reads the environment at call time — so a key pasted in Settings
  works on the very next generation call, no restart.
- The full key value is never sent back to the browser; ui/server.py only
  ever returns whether one is set plus its last 4 characters.
"""

from __future__ import annotations

import os
from pathlib import Path

from .storage import atomic_write_text

MANAGED_KEYS = (
    "SCHOOL_AGENT_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "SCHOOL_AGENT_OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "SCHOOL_AGENT_ANTHROPIC_MODEL",
    "GEMINI_API_KEY",
    "SCHOOL_AGENT_GEMINI_MODEL",
    "OLLAMA_BASE_URL",
    "SCHOOL_AGENT_OLLAMA_MODEL",
)


def update_env_file(path: Path | str, changes: dict[str, str]) -> None:
    """Set (or clear, with an empty value) managed KEY=VALUE lines in .env,
    preserving every other line — comments included — byte for byte."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# Model provider config — managed by the dashboard's Settings page;",
        "# hand-editing still works, this is the same file either way.",
    ]
    remaining = dict(changes)
    out = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        for key in list(remaining):
            if stripped.startswith(f"{key}="):
                out.append(f"{key}={remaining.pop(key)}")
                replaced = True
                break
        if not replaced:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    atomic_write_text(path, "\n".join(out) + "\n")


def apply_to_process(changes: dict[str, str]) -> None:
    """Mirror the .env changes into the live process environment so llm.py's
    call-time reads pick them up immediately. An empty value clears the
    variable entirely (so `os.environ.get(...)` falls back to defaults)."""
    for key, value in changes.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
