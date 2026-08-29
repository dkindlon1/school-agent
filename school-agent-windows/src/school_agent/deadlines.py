"""Deadline sync via MyCourses' (Brightspace/D2L) self-service ICS calendar feed.

Confirmed real and self-service: no institutional admin approval is needed
to subscribe to your own calendar feed. The full
Brightspace Valence REST API is a separate, admin-gated path and is NOT what
this module talks to.

This module only reads. It never writes anything back to MyCourses — there
is no execute path here, structurally — this module cannot change anything
in your course shell even if something asked it to.

**Recurring events (2026-08-25 fix):** Brightspace commonly encodes weekly
quizzes, labs, and recurring office hours as a single VEVENT with an RRULE,
not one VEVENT per occurrence. Reading VEVENTs directly (the v1 approach)
returned only the first occurrence and silently went quiet on every
occurrence after that — the exact failure mode a "never miss a deadline"
tool cannot have. This module now expands recurring events with
`recurring-ical-events` within a bounded date window, and filters out
STATUS:CANCELLED events (an instructor cancelling an assignment sets this
rather than deleting the VEVENT — unfiltered, a cancelled assignment kept
showing up as a live deadline).
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import recurring_ical_events
from icalendar import Calendar

from . import localtime
from .notify import notify
from .storage import atomic_write_json, load_json_self_healing, safe_map

Fetcher = Callable[[str], bytes]

# How far back/forward to expand recurring events from "now". Past events
# still matter (a missed-but-not-yet-graded deadline); far-future events on
# an open-ended weekly RRULE aren't useful to show yet.
DEFAULT_PAST_WINDOW_DAYS = 30
DEFAULT_FUTURE_WINDOW_DAYS = 365
# Generous for a real course (a daily event for a year is 365) and far below
# what a malformed or hostile rule produces.
MAX_OCCURRENCES = 5_000
_SUBDAILY = ("SECONDLY", "MINUTELY", "HOURLY")


def _drop_subdaily_rules(cal) -> None:
    for component in cal.walk("VEVENT"):
        rule = component.get("RRULE")
        if rule is None:
            continue
        freqs = [str(f).upper() for f in (rule.get("FREQ") or [])]
        if any(f in _SUBDAILY for f in freqs):
            del component["RRULE"]


@dataclass
class Deadline:
    uid: str  # unique PER OCCURRENCE — see _occurrence_uid; do not assume this equals the ICS UID
    class_slug: str
    title: str
    due: str  # ISO 8601
    description: str = ""
    link: str = ""  # deep link into MyCourses for this specific item, when one is available

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Deadline":
        return Deadline(**d)


def _normalize_feed_url(url: str) -> str:
    """webcal:// is just https:// with a different scheme name for calendar
    clients — normalize so plain HTTP fetchers can use it directly."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://") :]
    return url


ALLOWED_FEED_SCHEMES = ("http", "https")


def default_fetcher(url: str) -> bytes:
    """Fetch a calendar feed. http(s) only.

    urllib happily opens file:// and ftp://, and the URL here is typed into a
    box by someone pasting whatever their school gave them. A mistyped or
    pasted `file:///C:/Users/.../.env` was read off disk and its contents came
    back in the validation error message shown on screen — and then got
    re-read every 30 minutes by the background sync.
    """
    normalized = _normalize_feed_url(url)
    scheme = urllib.parse.urlparse(normalized).scheme.lower()
    if scheme not in ALLOWED_FEED_SCHEMES:
        raise ValueError(
            f"A calendar feed has to be an http:// or https:// address — got {scheme or 'no'} scheme. "
            "In Brightspace it's Calendar → Subscribe."
        )
    req = urllib.request.Request(normalized, headers={"User-Agent": "school-agent/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - scheme checked above
        return resp.read()


_URL_RE = re.compile(r"https?://\S+")


def _extract_link(component, description: str) -> str:
    """Brightspace's DESCRIPTION field puts the direct link to the actual
    assignment/quiz/dropbox BEFORE its generic "View event" calendar link
    (verified against a real RIT feed, 2026-08-25) — taking the first URL
    found gets you straight to the item, not just its calendar entry. Falls
    back to a bare URL property if a feed sets one instead, then to nothing
    (never fabricated) if no link is present at all."""
    url_prop = component.get("URL")
    if url_prop:
        return str(url_prop).strip()
    match = _URL_RE.search(description)
    return match.group(0).rstrip(".,)>\"'") if match else ""


def _matches_course_filter(summary: str, description: str, categories: str, keyword: str) -> bool:
    """Case-insensitive substring match across every field D2L might use to
    identify which course an event belongs to. Lets several classes share
    ONE calendar feed URL (e.g. Brightspace's "All Calendars and Tasks"
    subscription link) instead of each needing its own per-course export —
    2026-08-25, after the owner pointed out hunting down a separate feed
    link per class was the actual tedious part, not a real requirement."""
    kw = keyword.strip().lower()
    if not kw:
        return True
    haystack = f"{summary}\n{description}\n{categories}".lower()
    return kw in haystack


def parse_ics(
    ics_bytes: bytes,
    class_slug: str,
    course_filter: str | None = None,
    today: date | None = None,
    past_window_days: int = DEFAULT_PAST_WINDOW_DAYS,
    future_window_days: int = DEFAULT_FUTURE_WINDOW_DAYS,
) -> list[Deadline]:
    cal = Calendar.from_ical(ics_bytes)
    today = today or localtime.today_local()
    window_start = today - timedelta(days=past_window_days)
    window_end = today + timedelta(days=future_window_days)

    # A single FREQ=SECONDLY rule expands to ~34 million occurrences across the
    # window and wedges the sync thread — which then runs again every 30
    # minutes. Nothing legitimate in a course calendar recurs sub-daily, so
    # drop those rules before expanding rather than trying to survive them.
    _drop_subdaily_rules(cal)
    occurrences = recurring_ical_events.of(cal).between(window_start, window_end)
    if len(occurrences) > MAX_OCCURRENCES:
        notify(
            f"{class_slug}: calendar feed expanded to {len(occurrences)} events — keeping the first "
            f"{MAX_OCCURRENCES}. That usually means a runaway repeating event in the feed.",
            channel="console",
        )
        occurrences = occurrences[:MAX_OCCURRENCES]

    # First pass: pull out the fields we need, plus RECURRENCE-ID — the
    # iCal-standard "which slot in the series is this" identity, which
    # recurring_ical_events sets on every occurrence it returns (recurring
    # or not). Second pass below decides the diff key.
    parsed = []
    for component in occurrences:
        status = str(component.get("STATUS", "")).upper()
        if status == "CANCELLED":
            continue
        raw_uid = str(component.get("UID", ""))
        summary = str(component.get("SUMMARY", "")).strip()
        dt = component.get("DTSTART")
        if dt is None or not summary:
            continue
        dt_value = dt.dt
        if isinstance(dt_value, (datetime, date)):
            due = dt_value.isoformat()
        else:
            continue
        recurrence_id_prop = component.get("RECURRENCE-ID")
        recurrence_id = recurrence_id_prop.dt.isoformat() if recurrence_id_prop is not None else due
        description = str(component.get("DESCRIPTION", "")).strip()
        categories = str(component.get("CATEGORIES", "")).strip()
        if course_filter and not _matches_course_filter(summary, description, categories, course_filter):
            continue
        parsed.append(
            {
                "raw_uid": raw_uid,
                "summary": summary,
                "due": due,
                "recurrence_id": recurrence_id,
                "description": description,
                "link": _extract_link(component, description),
            }
        )

    # Second pass — what makes an occurrence's identity (2026-08-26 fix).
    #
    # A recurring series needs RECURRENCE-ID in its key, so week 4's quiz is
    # a different row from week 5's. A one-off does NOT, so that an
    # instructor moving its due date reads as one deadline changing rather
    # than one vanishing and an unrelated one appearing.
    #
    # The old rule guessed which was which by counting how many occurrences
    # of a UID landed inside today's expansion window. That count CHANGES as
    # the window slides: a series spaced more than 30 days apart, or one
    # thinned by STATUS:CANCELLED, drops to a single visible occurrence and
    # silently switches key scheme mid-semester. From the outside that looks
    # like an assignment you marked done coming back undone, a row that
    # duplicates permanently, and a "new deadline" toast for an exam you have
    # already sat. Reproduced at roughly week 13 of a real term.
    #
    # The window-independent answer is in the SOURCE calendar, not in the
    # expansion: an event recurs if its own VEVENT carries an RRULE or RDATE.
    # (recurring_ical_events strips those from the occurrences it returns and
    # stamps a RECURRENCE-ID on every occurrence including one-offs, so
    # neither of those can be read off the occurrence.)
    recurring_uids: set[str] = set()
    for component in cal.walk("VEVENT"):
        if component.get("RRULE") is not None or component.get("RDATE") is not None:
            uid = str(component.get("UID", ""))
            if uid:
                recurring_uids.add(uid)

    out: list[Deadline] = []
    for p in parsed:
        raw_uid = p["raw_uid"]
        if not raw_uid:
            key = f"{class_slug}:{p['summary']}:{p['due']}"
        elif raw_uid in recurring_uids:
            key = f"{raw_uid}::{p['recurrence_id']}"
        else:
            key = raw_uid
        out.append(
            Deadline(
                uid=key,
                class_slug=class_slug,
                title=p["summary"],
                due=p["due"],
                description=p["description"],
                link=p["link"],
            )
        )

    # De-dupe by the final key (defensive — a malformed feed could in
    # principle repeat an identical occurrence).
    deduped: dict[str, Deadline] = {}
    for d in out:
        deduped[d.uid] = d
    result = list(deduped.values())
    result.sort(key=lambda d: d.due)
    return result


def fetch_deadlines(
    url: str,
    class_slug: str,
    fetcher: Fetcher = default_fetcher,
    course_filter: str | None = None,
) -> list[Deadline]:
    return parse_ics(fetcher(url), class_slug, course_filter=course_filter)


def load_deadlines(path: Path | str) -> list[Deadline]:
    raw = load_json_self_healing(path, default=[])
    return safe_map(raw, Deadline.from_dict, on_item_name=lambda d: d.get("title", "?"))


def save_deadlines(path: Path | str, deadlines: list[Deadline]) -> None:
    atomic_write_json(path, [d.to_dict() for d in deadlines])


def load_dismissed(path: Path | str) -> set[str]:
    """Uids the owner has cleared from the board. Brightspace never marks
    some calendar items done (forms, 'Available' events, one-off notices),
    so they'd sit as 'overdue' forever — dismissal is presentation-level:
    the event stays in deadlines.json (sync keeps overwriting that file),
    this set just hides it everywhere it's shown, restorably."""
    raw = load_json_self_healing(path, default=[])
    return {u for u in raw if isinstance(u, str)}


def save_dismissed(path: Path | str, uids: set[str]) -> None:
    atomic_write_json(path, sorted(uids))


def dismiss_deadline(path: Path | str, uid: str) -> set[str]:
    uids = load_dismissed(path)
    uids.add(uid)
    save_dismissed(path, uids)
    return uids


def restore_deadline(path: Path | str, uid: str) -> set[str]:
    uids = load_dismissed(path)
    uids.discard(uid)
    save_dismissed(path, uids)
    return uids


# ---- completion (2026-08-26) -------------------------------------------
# Brightspace's calendar has no notion of "you submitted this", so without a
# local done state every assignment the owner actually handed in sat in red
# "Overdue" for the full 30-day past window. Across 5 classes with weekly work
# that's 30-60 permanently-overdue rows by week 4 — the board stops meaning
# anything, and the daily briefing leads with the same dead list every morning.


def load_done(path: Path | str) -> set[str]:
    raw = load_json_self_healing(path, default=[])
    return {u for u in raw if isinstance(u, str)}


def save_done(path: Path | str, uids: set[str]) -> None:
    atomic_write_json(path, sorted(uids))


def set_done(path: Path | str, uid: str, done: bool) -> set[str]:
    uids = load_done(path)
    if done:
        uids.add(uid)
    else:
        uids.discard(uid)
    save_done(path, uids)
    return uids


@dataclass
class DeadlineDiff:
    added: list[Deadline]
    changed: list[Deadline]
    removed: list[Deadline]

    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def diff_deadlines(old: list[Deadline], new: list[Deadline]) -> DeadlineDiff:
    old_by_uid = {d.uid: d for d in old}
    new_by_uid = {d.uid: d for d in new}
    added = [d for uid, d in new_by_uid.items() if uid not in old_by_uid]
    removed = [d for uid, d in old_by_uid.items() if uid not in new_by_uid]
    changed = [
        d
        for uid, d in new_by_uid.items()
        if uid in old_by_uid and old_by_uid[uid].to_dict() != d.to_dict()
    ]
    return DeadlineDiff(added=added, changed=changed, removed=removed)


# ---------------------------------------------------------- uid migration --

def rekey_map(
    old: list[Deadline],
    new: list[Deadline],
    marks: set[str] | None = None,
) -> dict[str, str]:
    """Old uid -> new uid, for rows whose identity scheme changed underneath
    them (2026-08-26).

    Recurring occurrences used to be keyed by a bare ICS UID whenever only one
    of them happened to fall inside the expansion window; they are now always
    keyed `uid::recurrence_id`. Without a migration the first sync after the
    upgrade sees the new key as an unknown deadline and keeps the old row
    forever beside it: every recurring assignment silently doubles, and any
    "done" or "cleared" mark you had made goes with the row nobody looks at.

    Three things this has to get right, each learned from a reproduction:

    * Match on the RECURRENCE-ID as well as the due date. The new uid's suffix
      is the occurrence's originally-scheduled slot, which is NOT the due date
      once an instructor moves one instance (or shifts the series). Matching on
      due alone left those unmigrated — a permanent duplicate plus a stranded
      mark.
    * Take `marks` into account, not just `old`. If deadlines.json is missing
      or was quarantined as corrupt, `old` is empty and there is nothing to
      migrate from — but done.json still holds bare uids that will never match
      anything again. The migration is one-shot; it has to work from whatever
      survived.
    * Never guess. A prefix match is only trusted when the new uid's prefix is
      a uid that genuinely recurs in this fetch, and when exactly one candidate
      claims it. Ambiguity is left alone rather than resolved arbitrarily: a
      mark landing on the wrong deadline is worse than a mark not moving.
    """
    recurring_prefixes: dict[str, list[Deadline]] = {}
    for d in new:
        if "::" in d.uid:
            recurring_prefixes.setdefault(d.uid.split("::", 1)[0], []).append(d)

    def _candidate(bare_uid: str, due: str | None) -> str | None:
        rows = recurring_prefixes.get(bare_uid)
        if not rows:
            return None
        if due is not None:
            # The occurrence's own slot first (survives a moved instance),
            # then its due date.
            hits = [d for d in rows if d.uid.split("::", 1)[1] == due]
            if len(hits) == 1:
                return hits[0].uid
            hits = [d for d in rows if d.due == due]
            if len(hits) == 1:
                return hits[0].uid
            if hits:
                return None  # ambiguous — leave it alone rather than guess
        # No due to disambiguate (a mark with no surviving row): only safe
        # when the series has exactly one occurrence in this fetch.
        return rows[0].uid if len(rows) == 1 else None

    mapping: dict[str, str] = {}
    for d in old:
        if "::" in d.uid:
            continue
        replacement = _candidate(d.uid, d.due)
        if replacement and replacement != d.uid:
            mapping[d.uid] = replacement
    for uid in marks or ():
        if "::" in uid or uid in mapping:
            continue
        replacement = _candidate(uid, None)
        if replacement and replacement != uid:
            mapping[uid] = replacement
    return mapping


def merge_preserving_marks(
    old: list[Deadline],
    new: list[Deadline],
    done: set[str],
    dismissed: set[str],
    window_start: str | None = None,
) -> tuple[list[Deadline], set[str], set[str]]:
    """Merge a fresh fetch into what is already on file, carrying done and
    dismissed marks across any uid that was re-keyed by the migration above.

    Merge rather than replace, because the fetch window slides forward daily
    and overwriting wholesale silently deleted every deadline older than 30
    days — you could no longer answer "when was Exam 1?".

    But merging alone never forgets anything, and that is its own bug: an
    assignment the instructor cancels (STATUS:CANCELLED) or deletes stays on
    the board permanently, and the sync announces "removed — Homework 3" on
    every single tick, forever. So rows INSIDE the window that the feed no
    longer lists are dropped; rows older than `window_start` are kept as
    history. Pass window_start=None to keep everything (the old behaviour).
    """
    mapping = rekey_map(old, new, marks=done | dismissed)
    fresh = {d.uid for d in new}
    merged: dict[str, Deadline] = {}
    for d in old:
        uid = mapping.get(d.uid, d.uid)
        if window_start is not None and uid not in fresh and d.due >= window_start:
            # In the window the feed just returned, and absent from it —
            # cancelled or deleted upstream, not merely aged out.
            continue
        merged[uid] = d
    merged.update({d.uid: d for d in new})
    done = {mapping.get(u, u) for u in done}
    dismissed = {mapping.get(u, u) for u in dismissed}
    return sorted(merged.values(), key=lambda d: d.due), done, dismissed
