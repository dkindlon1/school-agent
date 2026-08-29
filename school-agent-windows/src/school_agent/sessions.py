"""Focus-session log — what was actually worked on, for how long.

Added 2026-08-26 with Focus mode. Two jobs:

1. Give the briefing a real answer to "what have you been doing" that isn't
   inferred from FSRS timestamps alone (which go quiet exactly when the owner
   stops studying, making the briefing quieter precisely when it should get
   louder).
2. Start collecting time-on-task, which is the missing input for any honest
   workload planning later — the app currently has no idea how long anything
   takes, so it can't warn that four deliverables land on one Thursday.

Deliberately append-only and bounded. A session is recorded when it ENDS, so
an abandoned session simply leaves no trace rather than polluting the log.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import atomic_write_json, load_json_self_healing

MAX_SESSIONS = 500  # a couple of semesters' worth; trimmed oldest-first


@dataclass
class Session:
    started_at: str
    ended_at: str
    class_slug: str
    class_name: str
    kind: str  # "review" | "practice" | "assignment"
    label: str  # what was worked on, in the owner's own terms
    items: int = 0  # cards reviewed, where applicable
    minutes: float = 0.0

    @staticmethod
    def from_dict(d: dict) -> "Session":
        return Session(
            started_at=str(d.get("started_at", "")),
            ended_at=str(d.get("ended_at", "")),
            class_slug=str(d.get("class_slug", "")),
            class_name=str(d.get("class_name", "")),
            kind=str(d.get("kind", "")),
            label=str(d.get("label", "")),
            items=int(d.get("items", 0) or 0),
            minutes=float(d.get("minutes", 0) or 0),
        )


def load_sessions(path: Path | str) -> list[Session]:
    raw = load_json_self_healing(path, default=[])
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            out.append(Session.from_dict(d))
        except (TypeError, ValueError):
            continue
    return out


def record(path: Path | str, session: Session) -> list[Session]:
    sessions = load_sessions(path)
    sessions.append(session)
    sessions = sessions[-MAX_SESSIONS:]
    atomic_write_json(path, [asdict(s) for s in sessions])
    return sessions


def recent(path: Path | str, days: int = 7, now: datetime | None = None) -> list[Session]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    out = []
    for s in load_sessions(path):
        try:
            ended = datetime.fromisoformat(s.ended_at)
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ended >= cutoff:
            out.append(s)
    return out


def summary(path: Path | str, days: int = 7, now: datetime | None = None) -> dict:
    rows = recent(path, days=days, now=now)
    per_class: dict[str, float] = {}
    for s in rows:
        per_class[s.class_name or s.class_slug] = per_class.get(s.class_name or s.class_slug, 0.0) + s.minutes
    return {
        "sessions": len(rows),
        "minutes": round(sum(s.minutes for s in rows), 1),
        "items": sum(s.items for s in rows),
        "per_class": {k: round(v, 1) for k, v in sorted(per_class.items(), key=lambda kv: -kv[1])},
    }
