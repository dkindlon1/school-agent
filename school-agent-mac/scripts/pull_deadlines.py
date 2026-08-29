#!/usr/bin/env python3
"""Pull every configured class's ICS feed once, then exit.

Thin wrapper on purpose. This used to carry its OWN copy of the sync logic,
which had drifted into a genuinely destructive difference: it overwrote
deadlines.json wholesale instead of merging. That deleted every deadline
older than the fetch window (you could no longer answer "when was Exam 1?"),
and — after the 2026-08-26 uid change — it wrote new-scheme uids to disk
WITHOUT migrating done/dismissed marks across, which permanently orphaned
every checkmark, because the migration only ever gets one chance to run.

So there is now exactly one implementation of "sync the deadlines", in
scheduler.pull_all_deadlines, and both the dashboard and this script call it.

You do not normally need this: opening the dashboard starts the same sync on
a 30-minute loop. It exists for a cron/Task Scheduler entry if you want the
sync to run without the app open.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from school_agent.config import load_classes  # noqa: E402
from school_agent.notify import notify  # noqa: E402
from school_agent.scheduler import pull_all_deadlines  # noqa: E402


def main() -> int:
    classes = load_classes(REPO_ROOT / "config" / "classes.yaml")
    if not classes:
        notify("no classes configured yet — add one in the dashboard first")
        return 0
    pull_all_deadlines(REPO_ROOT, classes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
