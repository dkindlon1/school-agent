"""Notifications — console always, plus a real macOS Notification Center
banner when one can be posted.

Console-only was the first version, and it is invisible unless you happen to
be staring at the Terminal window, which defeats the point of a tool whose
whole job is telling you about a deadline you forgot.

macOS is served by `osascript` FIRST and `plyer` only as a fallback. plyer's
macOS backend shells out to osascript anyway, and it fails silently on
several macOS versions; calling osascript ourselves is fewer moving parts and
gives a real error we can fall back from. Console is always written either
way, so a notification is never lost even when running headless or over SSH.

Note on macOS permissions: the first banner asks the user to allow
notifications for Terminal (or whichever app launched this). If they decline,
osascript still succeeds silently and nothing appears — which is why the
console line is unconditional rather than a fallback.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

_last_messages: list[str] = []  # in-process log the dashboard reads for its "recent activity" feed
_MAX_LAST_MESSAGES = 200


def _console(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def _applescript_quote(value: str) -> str:
    """AppleScript string literals escape with backslashes, and an unescaped
    quote in a deadline title would turn the notification into a syntax
    error — silently, since we swallow the failure."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _macos_banner(title: str, message: str) -> bool:
    """Post to Notification Center via osascript. Returns False, never raises."""
    try:
        import subprocess

        script = (
            f'display notification "{_applescript_quote(message)}" '
            f'with title "{_applescript_quote(title)}"'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 - a notification must never break the caller
        return False


def _desktop(title: str, message: str) -> bool:
    """Best-effort desktop notification. Returns False (never raises) if none
    of the backends are available — headless, no permission, or osascript
    missing entirely."""
    if _macos_banner(title, message):
        return True
    try:
        from plyer import notification as plyer_notification

        plyer_notification.notify(title=title, message=message, timeout=10)
        return True
    except Exception:  # noqa: BLE001 - a notification backend failing must never break the caller
        return False


def notify(message: str, channel: str = "both", title: str = "School Agent") -> None:
    """channel: "console" | "desktop" | "both" (default). "both" always logs
    to console regardless of whether the desktop notification succeeds —
    console is the guaranteed-delivery floor."""
    if channel not in ("console", "desktop", "both"):
        raise ValueError(f"channel must be one of 'console'/'desktop'/'both', got {channel!r}")

    _last_messages.append(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}")
    del _last_messages[:-_MAX_LAST_MESSAGES]

    if channel in ("console", "both"):
        _console(message)
    if channel in ("desktop", "both"):
        _desktop(title, message)


def recent_messages(limit: int = 20) -> list[str]:
    """Powers the dashboard's activity feed — the in-process record of what
    notify() has said recently, newest last."""
    return _last_messages[-limit:]
