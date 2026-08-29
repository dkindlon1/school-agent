"""One definition of "what day is it" for the whole app.

Why this exists (2026-08-26): the briefing computed days-until as
`(due.date() - now.date()).days` with `now` in UTC and due dates in the
feed's own timezone. In Eastern time UTC rolls over at 8pm, so from 8pm
onward every single evening, work due at 11:59 tonight rendered as
OVERDUE and sorted into the overdue pile. A "never miss a deadline" tool
that cries wolf every night after dinner trains you to ignore it.

Calendar arithmetic is a LOCAL-CALENDAR question — "is this due today"
means today where the student is sitting, not today in UTC. So every
day-count in the app goes through here.

The zone comes from SCHOOL_AGENT_TIMEZONE (an IANA name like
"America/New_York") when set, otherwise from the machine's own clock,
which is right for a dashboard you run on your own laptop.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone, tzinfo

try:  # stdlib since 3.9; the fallback keeps this importable on odd builds
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

TIMEZONE_ENV = "SCHOOL_AGENT_TIMEZONE"


def _zone(name: str) -> tzinfo | None:
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a typo'd zone must not break the app
        return None


def local_tz() -> tzinfo:
    """The zone to read due dates in, resolved BY NAME wherever possible.

    The name matters more than it looks. A fixed offset snapshotted today is
    wrong on the other side of a DST change, and for a deadline at 23:59 an
    hour of error is a whole DAY of error: in August, a 23:59 EST deadline in
    December evaluated against a frozen EDT offset lands on the following
    calendar day, so "due this week" quietly becomes "due in 8 days" in the
    week it is actually due. Hence: env var first, then the OS zone name, and
    only then a fixed offset as a last resort.
    """
    for name in (os.environ.get(TIMEZONE_ENV) or "").strip(), os.environ.get("TZ", "").strip():
        zone = _zone(name)
        if zone is not None:
            return zone
    # The platform's own zone name (Linux/macOS expose it here; on Windows
    # tzlocal-style resolution isn't available without a dependency).
    try:
        import time as _time

        for name in getattr(_time, "tzname", ()) or ():
            zone = _zone(name)
            if zone is not None:
                return zone
    except Exception:  # noqa: BLE001
        pass
    try:
        from tzlocal import get_localzone  # type: ignore

        return get_localzone()
    except Exception:  # noqa: BLE001 - optional; absent on a default install
        pass
    # Last resort: today's offset. Off by an hour across a DST boundary, which
    # for a 23:59 deadline reads as a day — better than crashing, and the env
    # var above exists precisely so this never has to be relied on.
    return datetime.now().astimezone().tzinfo or timezone.utc


def now_local(now: datetime | None = None) -> datetime:
    """Aware datetime in the local zone. Pass `now` to convert it rather
    than to ignore it — callers hand in a fixed clock during tests."""
    if now is None:
        return datetime.now(local_tz())
    if now.tzinfo is None:
        now = now.replace(tzinfo=local_tz())
    return now.astimezone(local_tz())


def today_local(now: datetime | None = None) -> date:
    return now_local(now).date()


def to_local(value: datetime | date) -> datetime:
    """Normalize anything the ICS feed can produce into local-zone time.

    An all-day VEVENT gives a bare `date`; treat it as local midnight.
    A floating (naive) datetime is local by the iCalendar spec, NOT UTC —
    reading it as UTC was half of the original bug.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=local_tz())
        return value.astimezone(local_tz())
    return datetime(value.year, value.month, value.day, tzinfo=local_tz())


def parse_local(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        try:
            parsed = datetime.combine(date.fromisoformat(str(value)), datetime.min.time())
        except (TypeError, ValueError):
            return None
    return to_local(parsed)


def days_until(due_iso: str, now: datetime | None = None) -> int | None:
    """Whole calendar days from today to the due date, both read in local
    time. 0 means "due today", negative means past."""
    due = parse_local(due_iso)
    if due is None:
        return None
    return (due.date() - today_local(now)).days


def start_of_day(when: datetime | None = None) -> datetime:
    d = now_local(when)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def days_ago(days: int, now: datetime | None = None) -> datetime:
    return now_local(now) - timedelta(days=days)
