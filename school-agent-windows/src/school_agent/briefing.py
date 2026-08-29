"""Scheduled study briefings — the "scan everything and tell me where I
stand" loop (owner request, 2026-08-25).

Two-layer design, deliberately:

1. `build_facts` is a DETERMINISTIC scan of everything the app knows —
   synced deadlines, syllabus topics, quiz-card review history, ingested
   documents — producing a structured facts dict. No model involved, so
   this layer can never hallucinate an assignment that doesn't exist.
2. `generate_briefing` renders those facts. If a model is available it
   writes the narrative version (grounded in, and only in, the facts JSON
   it's handed); if not — or if the provider errors — the deterministic
   plain-text digest IS the briefing. The loop never silently produces
   nothing just because a model was down.

The background scheduler (scheduler.py) re-generates a stale briefing
roughly daily and fires a desktop notification; the dashboard shows the
latest one on Overview with a manual refresh button.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import deadlines, getahead, grades, localtime, materials, paths, sessions
from .config import ClassConfig
from .notify import notify
from .quiz import CardStore
from .storage import atomic_write_json, load_json_self_healing

BRIEFING_INTERVAL_HOURS = 24
UPCOMING_WINDOW_DAYS = 14
RECENT_REVIEW_DAYS = 7

# How far back an unfinished deadline is still worth putting in front of you.
# Without a floor the overdue section only ever grows: by week 15 of a real
# semester a five-class load produced 285 overdue rows, the model got 65k
# characters of facts every morning, and the "work on this next" line
# recommended a February exam. Anything older than this is counted, not
# listed — you have not forgotten about a three-week-old assignment, and if
# it genuinely is dead you can clear it from the board.
OVERDUE_FLOOR_DAYS = 21
# Even inside the floor, a long list is a list nobody reads.
MAX_OVERDUE_LISTED = 8

_MODEL_PROMPT = (
    "You are writing a short study briefing for a student, from the JSON facts that follow. "
    "The facts are their REAL synced deadlines, syllabus topics, quiz review history, and "
    "uploaded documents — never invent an assignment, topic, or date that is not in the facts. "
    "Write these sections, concretely and briefly (under 350 words total):\n"
    "1. What needs your attention — overdue items and the next 7 days. Never tell them to "
    "start something more than a week past due; if `stale_overdue` or a larger `overdue_total` "
    "is present, mention the count in one clause and move on. Order by GRADE IMPACT "
    "(worth_pct) first and due date only as a tiebreak; a 20% exam in four days matters more than "
    "a 1% post due tomorrow. Say what each item is worth when worth_pct is present.\n"
    "2. What you've been learning — from recently reviewed quiz cards and documents.\n"
    "3. Coming up — assignments and syllabus topics beyond this week.\n"
    "4. Where you stand — current grade per class and what's needed on the remaining weight, "
    "when grade facts are present. Never state a grade that is not in the facts.\n"
    "5. One specific suggestion for the next study session, tied to real upcoming material and "
    "to whichever class has the most grade at stake.\n"
    "Plain text with simple section headings. No preamble."
)


# ---- check-offs (2026-08-26) -------------------------------------------
# A briefing you can only read is a wall of text you re-read every morning.
# Ticking a line strikes it through and, more usefully, takes it out of the
# "still to do" count. Keyed by a hash of the normalized line so that (a) a
# page reload keeps your ticks and (b) a regenerated briefing that repeats a
# line you already handled shows it already handled. Entries expire so last
# week's ticks can't silently suppress this week's identical-looking work.

CHECK_EXPIRY_DAYS = 14


def line_key(text: str) -> str:
    """Stable id for one briefing line: the normalized text itself, not a hash.

    Deliberate — the browser has to derive the identical key to know which
    lines to strike through, and it can only do SHA-1 asynchronously via
    crypto.subtle, which is awkward inside a synchronous render. These lines
    are short and few, so plain normalized text is a better key: both sides
    compute it with the same three string operations, and the stored file
    stays human-readable."""
    return " ".join(str(text).replace("\u2014", "-").split()).strip("-•* ").lower()


def load_checks(path: Path | str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    raw = load_json_self_healing(path, default={})
    if not isinstance(raw, dict):
        return {}
    cutoff = now - timedelta(days=CHECK_EXPIRY_DAYS)
    fresh = {}
    for key, stamp in raw.items():
        try:
            when = datetime.fromisoformat(str(stamp))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if when >= cutoff:
            fresh[key] = stamp
    return fresh


def set_check(path: Path | str, key: str, checked: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    checks = load_checks(path, now=now)
    if checked:
        checks[key] = now.isoformat()
    else:
        checks.pop(key, None)
    atomic_write_json(path, checks)
    return checks


def _briefings_dir(repo_root: Path) -> Path:
    return Path(repo_root) / "data" / "briefings"


def _parse_days_until(due_iso: str, now: datetime) -> int | None:
    """Whole days from today to the due date, both read in LOCAL time.

    This used to compare a UTC date against the due date, which meant that
    from 8pm Eastern onward — every single evening — work due at 11:59
    tonight rendered as OVERDUE, because UTC had already rolled over. See
    localtime.py."""
    return localtime.days_until(due_iso, now)


def _grade_facts(scheme, scores) -> dict | None:
    if not scheme.components:
        return None
    s = grades.summarize(scheme, scores)
    row = grades.needed_for_target(scheme, scores, 90.0)
    return {
        "current_pct": s.current_pct,
        "current_letter": s.current_letter,
        "graded_weight": s.graded_weight,
        "remaining_weight": s.remaining_weight,
        "needed_for_a_minus": row.get("needed_pct"),
        "a_minus_possible": row.get("possible"),
        "confirmed": s.scheme_confirmed,
    }


def build_facts(repo_root: Path, classes: list[ClassConfig], now: datetime | None = None) -> dict:
    """Deterministic scan of everything on file. Pure read, no model."""
    now = localtime.now_local(now)
    today = now.date()
    facts: dict = {
        "generated_at": now.isoformat(),
        "classes": [],
        "study_last_7_days": sessions.summary(paths.sessions_path(repo_root), now=now),
    }
    for c in classes:
        dls = deadlines.load_deadlines(paths.deadlines_path(repo_root, c.slug))
        dismissed = deadlines.load_dismissed(paths.dismissed_path(repo_root, c.slug))
        done = deadlines.load_done(paths.done_path(repo_root, c.slug))
        scheme = grades.load_scheme(paths.grading_path(repo_root, c.slug))
        overdue, this_week, upcoming = [], [], []
        stale_overdue = 0  # older than the floor: counted, not listed
        completed_recently = 0
        for d in dls:
            if d.uid in dismissed:  # cleared from the board — never nag about it again
                continue
            if d.uid in done:
                days_done = _parse_days_until(d.due, now)
                if days_done is not None and -14 <= days_done <= 0:
                    completed_recently += 1
                continue
            days = _parse_days_until(d.due, now)
            impact = grades.deadline_impact(scheme, d.title)
            item = {
                "title": d.title,
                "due": d.due,
                "days_until": days,
                # What it's worth, so prioritization can be by grade impact
                # rather than by whichever date happens to come first.
                "worth_pct": (impact or {}).get("item_weight"),
                # Ordering is separate from display: an item whose per-item
                # share is unknown still belongs above a small known one.
                "order_pct": (impact or {}).get("ordering_weight"),
                "component_pct": (impact or {}).get("component_weight"),
                "component": (impact or {}).get("component"),
            }
            if days is None:
                continue
            if days < 0:
                # Past the floor it stops being "attention needed" and
                # becomes semester sediment — counted so it is not hidden,
                # but never again the thing you are told to work on.
                if days < -OVERDUE_FLOOR_DAYS:
                    stale_overdue += 1
                else:
                    overdue.append(item)
            elif days <= 7:
                this_week.append(item)
            elif days <= UPCOMING_WINDOW_DAYS:
                upcoming.append(item)

        store = CardStore(paths.cards_path(repo_root, c.slug))
        cards = store.all_cards(c.slug)
        recently_reviewed, struggling = [], []
        cutoff = now - timedelta(days=RECENT_REVIEW_DAYS)
        for card in cards:
            last = card.fsrs_state.get("last_review")
            if not last:
                continue
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if last_dt >= cutoff:
                recently_reviewed.append(card.question)
                stability = card.fsrs_state.get("stability")
                if isinstance(stability, (int, float)) and stability < 2:
                    struggling.append(card.question)

        topics = getahead.upcoming_topics(c, today=today, lookahead_days=UPCOMING_WINDOW_DAYS)
        entries = materials.load_index(paths.materials_index_path(repo_root, c.slug))

        facts["classes"].append(
            {
                "name": c.name,
                "slug": c.slug,
                "overdue": sorted(
                    overdue, key=lambda i: (-(i.get("worth_pct") or 0), i["days_until"])
                )[:MAX_OVERDUE_LISTED],
                "overdue_total": len(overdue),
                "stale_overdue": stale_overdue,
                "due_this_week": this_week,
                "upcoming": upcoming,
                "recently_reviewed_questions": recently_reviewed[:8],
                "struggling_with": struggling[:5],
                "due_card_count": len(store.due_cards(now=now, class_slug=c.slug)),
                "completed_recently": completed_recently,
                "grade": _grade_facts(scheme, grades.load_scores(paths.scores_path(repo_root, c.slug))),
                "upcoming_topics": [{"date": d.isoformat(), "topic": t} for d, t in topics],
                "documents": [m.filename for m in entries],
            }
        )
    return facts


def render_deterministic(facts: dict) -> str:
    """The no-model briefing — plain, honest, entirely from the facts."""
    lines: list[str] = []
    any_overdue = [(c["name"], d) for c in facts["classes"] for d in c["overdue"]]
    any_week = [(c["name"], d) for c in facts["classes"] for d in c["due_this_week"]]
    any_upcoming = [(c["name"], d) for c in facts["classes"] for d in c["upcoming"]]

    def worth(pair):
        # order_pct falls back to the component's weight when the per-item
        # share is unknown, so "Exams: 70%, count unstated" no longer sorts
        # as zero and lands beneath a 3% problem set.
        d = pair[1]
        return d.get("order_pct") or d.get("worth_pct") or 0

    def label(d):
        w = d.get("worth_pct")
        if w:
            return f" [{w:g}% of grade]"
        # Say what IS known rather than nothing at all — silence here reads
        # as "this is worth nothing", which for an exam is the opposite of
        # the truth.
        cw, comp = d.get("component_pct"), d.get("component")
        if cw and comp:
            return f" [{comp}, {cw:g}% of grade overall]"
        return ""

    lines.append("# What needs your attention")
    if any_overdue:
        for name, d in sorted(any_overdue, key=lambda x: (-worth(x), x[1]["due"])):
            lines.append(f"- OVERDUE: {d['title']} ({name}){label(d)}")
    hidden = sum(c.get("overdue_total", 0) for c in facts["classes"]) - len(any_overdue)
    stale = sum(c.get("stale_overdue", 0) for c in facts["classes"])
    if hidden > 0 or stale:
        bits = []
        if hidden > 0:
            bits.append(f"{hidden} more recent")
        if stale:
            bits.append(f"{stale} from earlier in the semester")
        lines.append(
            f"- ({' and '.join(bits)} still unmarked — open a class to clear anything already handed in.)"
        )
    if any_week:
        # Grade impact first, due date only as the tiebreak — a 20% midterm in
        # four days outranks a 1% discussion post due tomorrow.
        for name, d in sorted(any_week, key=lambda x: (-worth(x), x[1]["due"])):
            when = "today" if d["days_until"] == 0 else f"in {d['days_until']}d"
            lines.append(f"- {d['title']} ({name}) — {when}{label(d)}")
    if not any_overdue and not any_week:
        lines.append("- Nothing overdue and nothing due this week. Clear runway.")

    standings = [(c["name"], c["grade"]) for c in facts["classes"] if c.get("grade") and c["grade"].get("current_pct") is not None]
    if standings:
        lines.append("")
        lines.append("# Where you stand")
        for name, g in sorted(standings, key=lambda x: x[1]["current_pct"]):
            note = ""
            if g.get("needed_for_a_minus") is not None and g.get("a_minus_possible"):
                note = f" — needs {g['needed_for_a_minus']}% on the remaining {g['remaining_weight']}% for an A-"
            unconfirmed = "" if g.get("confirmed") else " (unconfirmed weights)"
            lines.append(f"- {name}: {g['current_pct']}% ({g['current_letter']}){note}{unconfirmed}")

    lines.append("")
    lines.append("# What you've been learning")
    learned_any = False
    for c in facts["classes"]:
        if c["recently_reviewed_questions"]:
            learned_any = True
            lines.append(f"- {c['name']}: reviewed {len(c['recently_reviewed_questions'])} card(s) this week")
            if c["struggling_with"]:
                lines.append(f"  still shaky on: {'; '.join(c['struggling_with'][:3])}")
    study = facts.get("study_last_7_days") or {}
    if study.get("minutes"):
        learned_any = True
        lines.append(f"- {int(study['minutes'])} minutes of focused study across {study['sessions']} session(s) this week.")
    finished = sum(c.get("completed_recently", 0) for c in facts["classes"])
    if finished:
        learned_any = True
        lines.append(f"- Finished and checked off {finished} item(s) in the last two weeks.")
    if not learned_any:
        lines.append("- No quiz reviews this week. Open a class and run through what's due — small sessions beat cramming.")

    lines.append("")
    lines.append("# Coming up")
    for name, d in sorted(any_upcoming, key=lambda x: x[1]["due"])[:10]:
        lines.append(f"- {d['title']} ({name}) — in {d['days_until']}d")
    for c in facts["classes"]:
        for t in c["upcoming_topics"][:3]:
            lines.append(f"- Topic: {t['topic']} ({c['name']}) — {t['date']}")
    if len(lines) > 0 and lines[-1] == "# Coming up":
        lines.append("- Nothing on the radar in the next two weeks.")

    review_total = sum(c["due_card_count"] for c in facts["classes"])
    lines.append("")
    lines.append("# Suggested next step")
    # Must follow the SAME grade-impact ordering as the section above — a
    # suggestion that contradicts the priority list teaches the owner to
    # ignore both. And no unsupported superlatives: the app cannot know that
    # reviewing is "the highest-value ten minutes available".
    # Only work that is still actually actionable can be "the next thing to
    # do". Without this the week-15 briefing recommended starting a midterm
    # that was taken in February.
    actionable = [p for p in (any_overdue + any_week) if (p[1].get("days_until") or 0) >= -7]
    heavy = [p for p in actionable if worth(p) >= 10]
    if heavy:
        name, d = max(heavy, key=worth)
        when = "overdue" if d["days_until"] < 0 else ("due today" if d["days_until"] == 0 else f"due in {d['days_until']}d")
        share = (f"{d['worth_pct']:g}% of that grade" if d.get("worth_pct")
                 else f"part of {d.get('component', 'a')} — {d.get('component_pct', 0):g}% of that grade")
        lines.append(f"- Work on “{d['title']}” ({name}) — {share} and {when}. It outweighs everything else on the list.")
    elif review_total:
        lines.append(f"- {review_total} quiz card(s) are due — a short review session is the cheapest thing you can do today.")
    elif actionable:
        name, d = max(actionable, key=lambda x: (worth(x), -(x[1]["days_until"])))
        lines.append(f"- Start on “{d['title']}” ({name}).")
    else:
        lines.append("- Upload recent lecture notes to a class and generate quiz questions from them.")
    return "\n".join(lines)


def generate_briefing(
    repo_root: Path,
    classes: list[ClassConfig],
    llm_fn=None,
    now: datetime | None = None,
    announce: bool = False,
) -> dict:
    """announce=False by default because the v2 sequence was absurd: open the
    app → the browser lands on Overview → a desktop toast appears telling you
    to open Overview. Only an unattended background regeneration announces."""
    facts = build_facts(repo_root, classes, now=now)
    content = render_deterministic(facts)
    used_model = False
    if llm_fn is not None:
        try:
            content = llm_fn(_MODEL_PROMPT, json.dumps(facts, indent=2))
            used_model = True
        except Exception as exc:  # noqa: BLE001 - a down model must never kill the briefing loop
            notify(f"briefing: model unavailable, using the plain digest ({exc})", channel="console")
    briefing = {"generated_at": facts["generated_at"], "content": content, "used_model": used_model}

    bdir = _briefings_dir(repo_root)
    atomic_write_json(bdir / "latest.json", briefing)
    day = facts["generated_at"][:10]
    atomic_write_json(bdir / f"briefing-{day}.json", briefing)
    if announce:
        notify("Your study briefing is ready — open the dashboard's Overview to read it.", title="School Agent briefing")
    else:
        notify("Study briefing regenerated.", channel="console")
    return briefing


def load_latest(repo_root: Path) -> dict | None:
    data = load_json_self_healing(_briefings_dir(repo_root) / "latest.json", default=None)
    return data if isinstance(data, dict) and data.get("content") else None


def is_stale(repo_root: Path, interval_hours: int = BRIEFING_INTERVAL_HOURS, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    latest = load_latest(repo_root)
    if latest is None:
        return True
    try:
        generated = datetime.fromisoformat(latest["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return True
    return now - generated >= timedelta(hours=interval_hours)


def maybe_generate(
    repo_root: Path,
    classes: list[ClassConfig],
    llm_fn=None,
    interval_hours: int = BRIEFING_INTERVAL_HOURS,
    now: datetime | None = None,
) -> bool:
    """The scheduled entry point: regenerate only when the latest briefing
    is older than the interval (or missing), so restarting the app several
    times a day doesn't spam model calls or notifications."""
    if not classes or not is_stale(repo_root, interval_hours=interval_hours, now=now):
        return False
    generate_briefing(repo_root, classes, llm_fn=llm_fn, now=now, announce=True)
    return True
