"""Notifications — v1 shipped console-only (a stderr print), which an
adversarial review correctly called out as invisible unless you're staring
at that terminal. This version adds a real desktop-notification channel
(via `plyer`, which picks the right OS backend automatically — win10toast-
style on Windows) and keeps console as the always-on fallback, so a
notification is never silently lost even if the desktop backend can't
initialize (e.g. running headless).

"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

_last_messages: list[str] = []  # in-process log the dashboard reads for its "recent activity" feed
_MAX_LAST_MESSAGES = 200


def _console(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def _desktop(title: str, message: str) -> bool:
    """Best-effort OS desktop notification. Returns False (never raises) if
    the backend isn't available — e.g. no display, unsupported OS, or the
    optional plyer dependency isn't usable in this environment."""
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
