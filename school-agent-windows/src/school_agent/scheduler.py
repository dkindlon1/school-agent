"""Background scheduler — the other missing piece an adversarial review
flagged: v1 required the owner to manually run pull_deadlines.py (via their
own cron/Task Scheduler entry) for anything to stay current. This module
runs that pull automatically, in-process, on an interval, for as long as
the dashboard (ui/server.py) is running — "start the dashboard" IS "set up
the automation," no separate scheduling step for the owner to configure.

Deliberately in-process (APScheduler's BackgroundScheduler) rather than a
separate daemon/service — this is a personal tool, not infrastructure; the
dashboard being open is a fine proxy for "I want this actively watching."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from . import briefing, deadlines, localtime, paths
from .config import ClassConfig
from .notify import notify

DEFAULT_INTERVAL_MINUTES = 30
BATCH_NOTIFY_THRESHOLD = 3  # more new items than this in one sync → one summary toast
PAST_WINDOW_GRACE_DAYS = 21  # ignore "removals" that are just the fetch window sliding
BRIEFING_CHECK_MINUTES = 60  # staleness CHECK cadence; regeneration itself is ~daily (briefing.py)


def pull_all_deadlines(repo_root: Path, classes: list[ClassConfig]) -> None:
    """One sync pass across every configured class. Safe to call repeatedly
    (e.g. from the scheduler AND a manual "sync now" button) — each class's
    load/diff/save is independent, so one class's feed failing doesn't stop
    the others."""
    for c in classes:
        if not c.ics_feed_url:
            continue
        paths.ensure_class_dirs(repo_root, c.slug)
        dpath = paths.deadlines_path(repo_root, c.slug)
        old = deadlines.load_deadlines(dpath)
        try:
            new = deadlines.fetch_deadlines(c.ics_feed_url, c.slug, course_filter=c.course_filter)
        except Exception as exc:  # noqa: BLE001 - one class's dead feed must not stop the others
            notify(f"{c.name}: couldn't reach the calendar feed ({exc})", channel="console")
            continue
        diff = deadlines.diff_deadlines(old, new)
        if not diff.is_empty():
            dismissed = deadlines.load_dismissed(paths.dismissed_path(repo_root, c.slug))
            visible = lambda items: [d for d in items if d.uid not in dismissed]  # noqa: E731
            added, changed, removed = visible(diff.added), visible(diff.changed), visible(diff.removed)

            first_sync = not old
            if first_sync:
                # Adding a class used to fire ONE desktop toast per deadline —
                # a weekly recurring quiz expands to ~50 occurrences over the
                # 365-day window, so setting up five classes meant a burst of
                # a hundred-plus notifications as a first impression.
                if added:
                    notify(f"{c.name}: {len(added)} deadline(s) synced", channel="console")
            elif len(added) > BATCH_NOTIFY_THRESHOLD:
                notify(f"{c.name}: {len(added)} new deadlines synced", title="Deadlines synced")
            else:
                for d in added:
                    notify(f"{c.name}: new — {d.title} due {d.due[:16].replace('T', ' ')}", title="New deadline")

            # A date change on something already known is the genuinely urgent
            # case, so it stays per-item regardless of volume.
            for d in changed:
                notify(f"{c.name}: date changed — {d.title} now due {d.due[:16].replace('T', ' ')}", title="Deadline changed")

            # Removals are mostly an artifact of the 30-day past window sliding
            # forward, which would otherwise cry wolf about every deadline
            # roughly a month after it was due. Only report ones still recent
            # enough that the instructor plausibly deleted them.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=PAST_WINDOW_GRACE_DAYS)).isoformat()
            for d in removed:
                if d.due >= cutoff:
                    notify(f"{c.name}: removed — {d.title}", title="Deadline removed")

        # Merge rather than replace (the fetch window slides forward daily, so
        # overwriting wholesale silently deleted every deadline older than 30
        # days), carrying done/dismissed marks across any occurrence whose uid
        # scheme changed underneath it — see deadlines.rekey_map.
        done_path = paths.done_path(repo_root, c.slug)
        dis_path = paths.dismissed_path(repo_root, c.slug)
        done = deadlines.load_done(done_path)
        dismissed = deadlines.load_dismissed(dis_path)
        window_start = (
            localtime.today_local() - timedelta(days=deadlines.DEFAULT_PAST_WINDOW_DAYS)
        ).isoformat()
        merged, done_after, dismissed_after = deadlines.merge_preserving_marks(
            old, new, done, dismissed, window_start=window_start
        )
        # Marks are written FIRST. The uid migration is one-shot: once
        # deadlines.json holds new-scheme uids there is nothing left to
        # migrate from, so a crash between these writes must not be the one
        # that leaves the marks behind.
        if done_after != done:
            deadlines.save_done(done_path, done_after)
        if dismissed_after != dismissed:
            deadlines.save_dismissed(dis_path, dismissed_after)
        deadlines.save_deadlines(dpath, merged)


def start_background_sync(
    repo_root: Path,
    load_classes_fn,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    briefing_llm_fn=None,
) -> BackgroundScheduler:
    """load_classes_fn is called fresh on every tick (not once at startup)
    so a class added through the dashboard mid-session gets picked up on
    the next tick without a restart."""
    scheduler = BackgroundScheduler(daemon=True)

    def _tick():
        try:
            classes = load_classes_fn()
        except Exception as exc:  # noqa: BLE001 - a bad config must not kill the scheduler thread
            notify(f"background sync couldn't read classes.yaml: {exc}")
            return
        pull_all_deadlines(repo_root, classes)

    def _briefing_tick():
        """The scan-everything loop: regenerates the study briefing when the
        latest one is older than ~a day (briefing.maybe_generate holds the
        staleness rule, so app restarts don't spam model calls). Runs in the
        scheduler's worker thread — a slow model call never blocks a sync."""
        try:
            classes = load_classes_fn()
        except Exception:  # noqa: BLE001 - same guard as _tick
            return
        try:
            briefing.maybe_generate(repo_root, classes, llm_fn=briefing_llm_fn)
        except Exception as exc:  # noqa: BLE001 - the loop must survive any one bad generation
            notify(f"briefing generation failed ({exc}) — will retry on the next check", channel="console")

    # Deadline sync runs once immediately (synchronously, so opening the
    # dashboard shows fresh data right away), then on a repeating interval.
    _tick()
    scheduler.add_job(_tick, "interval", minutes=interval_minutes)
    # The briefing check runs shortly AFTER startup in the background (a
    # model call can take a minute — it must not delay the dashboard), then
    # hourly; actual regeneration is ~daily via the staleness rule.
    scheduler.add_job(_briefing_tick, "date")  # date-trigger with no run_date == once, now, off-thread
    scheduler.add_job(_briefing_tick, "interval", minutes=BRIEFING_CHECK_MINUTES)
    scheduler.start()
    return scheduler
