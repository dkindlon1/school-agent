"""Shared JSON storage helpers: atomic writes and self-healing loads.

Added after an adversarial review (2026-08-25) found that every JSON-backed
store in this package wrote directly to its final path (a crash mid-write
leaves a truncated file) and loaded with a bare json.loads (a truncated or
otherwise corrupt file then hard-crashes every future run, with no recovery
). Both problems get fixed
once, here, instead of three times.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .notify import notify


def atomic_write_json(path: Path | str, data: Any) -> None:
    """Write to a temp file in the same directory, then os.replace() onto the
    real path — the rename is atomic on both POSIX and Windows (NTFS), so a
    crash mid-write leaves either the old file intact or the new one, never
    a truncated hybrid."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, default=str)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path | str, text: str) -> None:
    """Same atomicity guarantee as atomic_write_json, for plain-text files
    (used by materials.save_pasted_text so a paste-in-progress can never
    leave a half-written .txt file behind)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_json_self_healing(path: Path | str, default: Any) -> Any:
    """Load JSON, but never let one corrupt file take down an entire class's
    data. On a parse failure: quarantine the bad file (renamed, not deleted
    — nothing is silently destroyed) and return `default` so the rest of the
    app keeps working. The owner gets a visible notification either way."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = path.with_name(f"{path.name}.corrupt-{timestamp}")
        try:
            os.replace(path, quarantine)
        except OSError:
            quarantine = None
        notify(
            f"STORAGE CORRUPTION: {path} failed to parse ({exc}) — "
            + (f"quarantined to {quarantine}, " if quarantine else "could not quarantine it, ")
            + "starting from an empty store. Nothing else was touched."
        )
        return default


def safe_map(items: list, fn: Callable[[Any], Any], on_item_name: Callable[[Any], str]) -> list:
    """Apply fn to each item; a single bad item is skipped and notified
    instead of aborting the whole batch (finding: one card with a malformed
    fsrs_state used to raise and hide every OTHER due card in the same
    class)."""
    out = []
    for item in items:
        try:
            out.append(fn(item))
        except Exception as exc:  # noqa: BLE001 - deliberate: isolate one bad record from the rest
            notify(f"skipping unreadable record {on_item_name(item)!r}: {exc}")
    return out
