"""Struggle ladders — "I struggle with this" turned into a progression that
starts by explaining and ends with you working alone.

Owner request, 2026-08-26: *"have the option for ai to start generating
practice problems based off what the user struggles with... build problems
to explain it and eventually graduate them to on their own problems."*

This is the guidance-fading effect, and it is one of the better-replicated
findings in instructional research. Two facts sit behind it:

  * the worked-example effect — when a procedure is new, studying a full
    solution beats attempting a problem, because attempting one you cannot
    do yet spends all your working memory on being stuck rather than on the
    method (Sweller, Cooper);
  * expertise reversal — the SAME worked example becomes redundant and then
    actively harmful once the method is familiar; at that point you need to
    be generating solutions yourself (Kalyuga).

Which means neither "here are ten worked examples" nor "here are ten
problems, good luck" is right. The thing that works is the ladder: full
support first, then remove it one piece at a time, with each step earned
(Renkl & Atkinson's fading). That is exactly what a student means when they
say "explain it and eventually graduate me to my own problems."

The design consequences that matter here:

  * **Support fades, difficulty does not.** Every rung is the same kind of
    problem at the same level. What changes is how much of the solution you
    are handed. Making problems harder as you go would confound the two and
    you would learn nothing about whether the method has landed.
  * **Every rung generates a NEW problem.** Re-showing one you have seen
    measures recall of an answer, not command of a method.
  * **Advancement is earned and reversible.** Getting one wrong drops you
    back a rung. Looking at the solution is allowed and never punished, but
    it does not advance you — the whole point is that the ladder tracks what
    you can do unaided.
  * **A generated problem can be wrong.** There is an explicit "this problem
    is broken" path that discards it without counting it against you, and
    the check of your answer is presented as a check, not a verdict, with a
    way to disagree. A tutor that is confidently wrong and unarguable is
    worse than no tutor.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import localtime, materials, paths
from .storage import atomic_write_json, load_json_self_healing
from .study import parse_session_json

CONTEXT_CHARS = 16_000
MAX_LADDERS_PER_CLASS = 24
MAX_ATTEMPTS_KEPT = 40
# Per-field caps on stored attempt text. Only `problem` was clipped, so a
# student pasting their working built a 3.3 MB ladders.json for one class —
# re-parsed, re-serialised and shipped to the browser on EVERY ladder render.
MAX_ATTEMPT_FIELD_CHARS = 2_000

NO_MATERIAL_NOTE = (
    "(This class has no uploaded material yet, so there are no excerpts to match. Build the "
    "problem the standard way, at the level a first-year engineering course would. Do not "
    "mention the absence of material.)"
)


# ------------------------------------------------------------------ rungs --

@dataclass(frozen=True)
class Rung:
    key: str
    label: str
    # What the student sees at this rung, in their words.
    support: str
    # Appended to the generation prompt — the ONLY thing that varies.
    fade: str
    # Clean, unaided successes needed to move up from here. Higher near the
    # top because one lucky solo problem is not evidence of anything.
    clean_needed: int
    # Whether the student submits an answer at all (rung 0 is reading).
    submits: bool = True


RUNGS: tuple[Rung, ...] = (
    Rung(
        key="worked",
        label="Watch it done",
        support="The whole solution, with the reason for every step.",
        fade=(
            "Give the COMPLETE solution: every step, and for each step the reason it is the "
            "right move. Leave nothing for the student to fill in — this rung is for reading, "
            "not attempting. blanks must be an empty list."
        ),
        clean_needed=1,
        submits=False,
    ),
    Rung(
        key="last-step",
        label="You finish it",
        support="Worked through to the last move — you take it home.",
        fade=(
            "Work the solution through but STOP before the final step. The last step goes in "
            "blanks, described by what the student must do (not the answer). The student "
            "supplies it."
        ),
        clean_needed=1,
    ),
    Rung(
        key="middle-out",
        label="You do the hard part",
        support="Setup and finish given. The step that actually matters is yours.",
        fade=(
            "Give the setup (diagram, control volume, what is known) and the final arithmetic, "
            "but remove the one or two steps in the middle where the actual method lives — the "
            "step a student who has not understood this would get wrong. Those go in blanks."
        ),
        clean_needed=1,
    ),
    Rung(
        key="principle",
        label="Just the principle",
        support="You are told which idea applies. Everything else is yours.",
        fade=(
            "Give ONLY the name of the governing principle or the right starting move, in one "
            "sentence, and nothing else — no setup, no equations, no steps. Everything else is "
            "the student's work. Put the whole solution in solution for checking, not for "
            "showing."
        ),
        clean_needed=2,
    ),
    Rung(
        key="solo",
        label="On your own",
        support="The problem, and nothing else.",
        fade=(
            "Give the problem and nothing else — no principle, no hint, no setup. Put the full "
            "solution in solution for checking, not for showing."
        ),
        clean_needed=2,
    ),
)

RUNG_BY_KEY = {r.key: i for i, r in enumerate(RUNGS)}
TOP_RUNG = len(RUNGS) - 1


def rungs_for_ui() -> list[dict]:
    return [
        {"key": r.key, "label": r.label, "support": r.support, "clean_needed": r.clean_needed,
         "submits": r.submits}
        for r in RUNGS
    ]


# ---------------------------------------------------------------- prompts --

# On what the model may draw on — see the long note in study.py. Short
# version: your knowledge of the subject is the engine; their material is the
# tuning. It was originally "use only what the material supports", which meant
# the app couldn't build a problem about vectors until a PDF defining vectors
# had been uploaded. That is backwards.
_GEN_PROMPT = (
    "You are building one practice problem for a student who has told you exactly what they "
    "struggle with. Use your own knowledge of the subject freely — you are expected to know "
    "the standard material, and you must never refuse to build a problem because the excerpts "
    "do not cover it. "
    "Their own course material follows as CONTEXT: match its notation, sign conventions, units "
    "and level, and prefer its framing over generic phrasing, because their exam follows their "
    "course. Where it is silent, use the standard treatment. "
    "The problem must target THEIR stated difficulty specifically — not the general topic it "
    "sits in. Keep the difficulty the same every time you are asked; only the amount of the "
    "solution you reveal changes. Never reuse a problem you are told they have already seen. "
    "Never state a fact specific to their course (a due date, a weight, an exam scope) that is "
    "not in the material. Reply with JSON only, no prose outside it, no code fence."
)

_GEN_SHAPE = (
    '{"problem":str,"given":[str],"shown":[{"action":str,"why":str}],'
    '"blanks":[str],"solution":[{"action":str,"why":str}],"answer":str,'
    '"principle":str,"watch_out":str}'
)

_CHECK_PROMPT = (
    "A student attempted the practice problem below and their answer follows, marked STUDENT "
    "ANSWER. Judge it against the subject as you know it, not merely against the reference "
    "solution — if the student is right and the reference is wrong, the student is right. "
    "Judge ONLY whether their reasoning and result are right — never whether it is "
    "phrased the way you would phrase it, and never penalise a different but valid route. "
    "Round numbers generously; a rounding difference is correct. verdict must be exactly one "
    "of correct, partial, or wrong. If it is not correct, name the single specific place their "
    "reasoning went wrong and what the right move there is — one place, the earliest one, not a "
    "list of everything. If they are right, say what they did well in one line and stop. Be "
    "honest: marking a wrong answer correct here costs them the exam. Reply with JSON only."
)

_CHECK_SHAPE = '{"verdict":str,"summary":str,"went_wrong":str,"right_move":str}'

_VERDICTS = ("correct", "partial", "wrong")


# ----------------------------------------------------------------- model --

@dataclass
class Attempt:
    at: str
    rung: str
    verdict: str
    student_answer: str = ""
    summary: str = ""
    went_wrong: str = ""
    right_move: str = ""
    used_solution: bool = False
    problem: str = ""
    # The ladder's position BEFORE this attempt moved it, so override_verdict
    # can rewind exactly one move rather than guessing.
    rung_before: int = 0
    streak_before: int = 0


@dataclass
class Ladder:
    ladder_id: str
    class_slug: str
    # Verbatim what they typed. Never paraphrased — "I keep dropping the sign
    # on moment arms" is a better generation prompt than anything a model
    # would rewrite it into, and seeing their own words back is the point.
    struggle: str
    rung: int = 0
    clean_streak: int = 0
    created_at: str = ""
    updated_at: str = ""
    graduated_at: str = ""
    current: dict | None = None
    attempts: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    # Problem statements already served, so generation does not repeat one.
    seen: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Ladder":
        known = {f for f in Ladder.__dataclass_fields__}
        return Ladder(**{k: v for k, v in d.items() if k in known})

    @property
    def graduated(self) -> bool:
        return bool(self.graduated_at)


def progress(ladder: Ladder) -> dict:
    """Everything the UI needs to draw the ladder without recomputing rules."""
    rung = RUNGS[min(ladder.rung, TOP_RUNG)]
    return {
        "rung_index": ladder.rung,
        "rung_key": rung.key,
        "rung_label": rung.label,
        "support": rung.support,
        "submits": rung.submits,
        "clean_streak": ladder.clean_streak,
        "clean_needed": rung.clean_needed,
        "total_rungs": len(RUNGS),
        "graduated": ladder.graduated,
        "attempts": len(ladder.attempts),
    }


# ----------------------------------------------------------- persistence --

def _path(repo_root: Path, class_slug: str) -> Path:
    return paths.class_dir(repo_root, class_slug) / "ladders.json"


def load_ladders(repo_root: Path, class_slug: str) -> list[Ladder]:
    raw = load_json_self_healing(_path(repo_root, class_slug), default=[])
    out = []
    for row in raw if isinstance(raw, list) else []:
        try:
            out.append(Ladder.from_dict(row))
        except TypeError:
            continue  # one malformed ladder must not lose the rest
    return sorted(out, key=lambda l: (bool(l.graduated_at), l.updated_at), reverse=False)


def save_ladders(repo_root: Path, class_slug: str, ladders: list[Ladder]) -> None:
    """Cap by dropping the LEAST useful rows, not the last ones in the list.

    The naive `ladders[:MAX]` dropped the tail — and _upsert appends the row it
    just modified to the tail, so hitting the cap silently threw away the
    ladder you had just created, complete with the problem you had just paid a
    model to generate. start() returned it, the API returned 200, the UI opened
    it, and it was gone on the next refresh with no error anywhere.

    Finished ladders go first, oldest first; live ones are only ever dropped if
    the cap is somehow full of them.
    """
    rows = list(ladders)
    if len(rows) <= MAX_LADDERS_PER_CLASS:
        atomic_write_json(_path(repo_root, class_slug), [l.to_dict() for l in rows])
        return
    # ladder_id is in the key as a deterministic tie-break. Sorting on a
    # timestamp alone means that when two rows share one, which row survives
    # the cap depends on dict/list order — surfaced by freezing the clock,
    # where the just-created ladder was the one dropped.
    ranked = sorted(rows, key=lambda l: (not l.graduated_at, l.updated_at, l.ladder_id), reverse=True)
    keep = {id(l) for l in ranked[:MAX_LADDERS_PER_CLASS]}
    atomic_write_json(_path(repo_root, class_slug), [l.to_dict() for l in rows if id(l) in keep])


def get_ladder(repo_root: Path, class_slug: str, ladder_id: str) -> Ladder:
    for l in load_ladders(repo_root, class_slug):
        if l.ladder_id == ladder_id:
            return l
    raise KeyError(f"no ladder {ladder_id!r}")


def _upsert(repo_root: Path, ladder: Ladder) -> Ladder:
    rows = [l for l in load_ladders(repo_root, ladder.class_slug) if l.ladder_id != ladder.ladder_id]
    rows.append(ladder)
    save_ladders(repo_root, ladder.class_slug, rows)
    return ladder


def delete_ladder(repo_root: Path, class_slug: str, ladder_id: str) -> None:
    rows = [l for l in load_ladders(repo_root, class_slug) if l.ladder_id != ladder_id]
    save_ladders(repo_root, class_slug, rows)


# ---------------------------------------------------------- generation --

def _context(repo_root: Path, class_slug: str, struggle: str) -> tuple[str, list[str]]:
    mdir = paths.materials_dir(repo_root, class_slug)
    entries = materials.load_index(paths.materials_index_path(repo_root, class_slug))
    chunks = materials.relevant_chunks(mdir, entries, struggle, k=8)
    if not chunks:
        # A struggle phrased in the student's own words often shares no
        # vocabulary with the slides ("I keep messing up the signs"). That is
        # a lookup miss, not an empty class.
        chunks = materials.sample_chunks(mdir, entries, max_chunks=8)
    if not chunks:
        # Not an error. Without material the model still knows the subject —
        # it just cannot match this professor's conventions, which is a
        # smaller loss than refusing to build a problem at all.
        return NO_MATERIAL_NOTE, []
    return materials.build_context(chunks, max_chars=CONTEXT_CHARS), sorted({c.filename for c in chunks})


def _generate(repo_root: Path, ladder: Ladder, llm_fn) -> dict:
    rung = RUNGS[min(ladder.rung, TOP_RUNG)]
    context, sources = _context(repo_root, ladder.class_slug, ladder.struggle)
    avoid = ""
    if ladder.seen:
        recent = "; ".join(s[:160] for s in ladder.seen[-4:])
        avoid = f"\nAlready used, do not repeat or lightly reword: {recent}"
    prompt = (
        f"{_GEN_PROMPT}\n\nThe student says they struggle with: {ladder.struggle}\n"
        f"{rung.fade}{avoid}\nReturn exactly this JSON shape: {_GEN_SHAPE}"
    )
    data = _coerce(parse_session_json(llm_fn(prompt, context)))
    _require_rung_shape(rung, data)
    data["rung"] = rung.key
    ladder.sources = sources
    return data


def _require_rung_shape(rung: Rung, data: dict) -> None:
    """Each rung promises the student something specific. Check it arrived.

    _coerce turns malformed shapes into empty lists rather than crashing,
    which is right — but only `problem` was ever checked, so an empty
    `shown`/`blanks`/`solution` sailed through and the rung silently became a
    different, worse rung. Measured: at "Watch it done" the page rendered no
    worked solution at all and still let the student advance; at "You finish
    it" the reference solution was empty, so a correct answer was graded
    against nothing, marked wrong, and cost them a rung.
    """
    if not str(data.get("problem", "")).strip():
        raise ValueError("the model returned no problem — try again, or switch provider in Settings")
    missing = []
    if rung.key == "worked" and not data.get("shown"):
        missing.append("the worked solution")
    if rung.key in ("last-step", "middle-out") and not data.get("blanks"):
        missing.append("the part left for you")
    if rung.key != "worked" and not data.get("solution"):
        missing.append("a solution to check your answer against")
    if missing:
        raise ValueError(
            f"The model's problem came back without {' and '.join(missing)} — that rung can't work "
            "without it. Press the button again, or switch provider in Settings if it keeps happening."
        )


def _coerce(data: dict) -> dict:
    """Force the model's reply into the shapes the checker and the renderer
    assume, instead of storing whatever came back and crashing later.

    Models routinely flatten `[{"action":..,"why":..}]` to `["do this","then
    that"]`, or send null for a list. The old code validated only that a
    problem string existed, so a flattened `solution` was accepted, saved, and
    then blew up inside attempt() with `'str' object has no attribute 'get'`
    — as a 500, on every press of "Check my work", with the student's typed
    working discarded each time and no way out but guessing that "this problem
    looks wrong" would help.
    """
    def steps(value):
        out = []
        for item in value if isinstance(value, list) else ([value] if value else []):
            if isinstance(item, dict):
                out.append({"action": str(item.get("action", "")).strip(),
                            "why": str(item.get("why", "")).strip()})
            elif str(item).strip():
                out.append({"action": str(item).strip(), "why": ""})
        return [s for s in out if s["action"]]

    def strings(value):
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value).strip()] if value and str(value).strip() else []

    for key in ("shown", "solution"):
        data[key] = steps(data.get(key))
    for key in ("given", "blanks"):
        data[key] = strings(data.get(key))
    for key in ("problem", "answer", "principle", "watch_out"):
        data[key] = str(data.get(key) or "").strip()
    return data


def start(repo_root: Path, class_slug: str, struggle: str, llm_fn, now: datetime | None = None) -> Ladder:
    struggle = (struggle or "").strip()
    if not struggle:
        raise ValueError(
            "Say what you're stuck on, in your own words — 'I keep dropping the sign on moment "
            "arms' works better than 'statics'."
        )
    stamp = localtime.now_local(now).isoformat()
    ladder = Ladder(
        ladder_id=uuid.uuid4().hex[:12],
        class_slug=class_slug,
        struggle=struggle,
        created_at=stamp,
        updated_at=stamp,
    )
    ladder.current = _generate(repo_root, ladder, llm_fn)
    ladder.seen.append(str(ladder.current.get("problem", ""))[:400])
    return _upsert(repo_root, ladder)


def next_problem(repo_root: Path, class_slug: str, ladder_id: str, llm_fn,
                 now: datetime | None = None) -> Ladder:
    """A fresh problem at the CURRENT rung. Used after an attempt, and after
    discarding one that was broken."""
    ladder = get_ladder(repo_root, class_slug, ladder_id)
    ladder.current = _generate(repo_root, ladder, llm_fn)
    ladder.seen.append(str(ladder.current.get("problem", ""))[:400])
    ladder.seen = ladder.seen[-12:]
    ladder.updated_at = localtime.now_local(now).isoformat()
    return _upsert(repo_root, ladder)


def discard_problem(repo_root: Path, class_slug: str, ladder_id: str, llm_fn,
                    now: datetime | None = None) -> Ladder:
    """The escape hatch for a generated problem that is wrong or unanswerable.

    It replaces the problem WITHOUT recording an attempt, so a bad generation
    never costs you a rung. Models get physics wrong; a ladder that punished
    you for noticing would be worse than useless."""
    return next_problem(repo_root, class_slug, ladder_id, llm_fn, now=now)


# ------------------------------------------------------------ advancement --

def _apply_outcome(ladder: Ladder, verdict: str, used_solution: bool) -> dict:
    """The rules, in one place and with no model involved, so they are
    testable and so the same input always moves you the same way.

    correct and unaided  -> streak +1; advance when the rung's bar is met
    correct but you looked -> stays put, streak resets. Looking is allowed
                              and is never punished, but the ladder measures
                              what you can do unaided, so it cannot count.
    partial              -> stays put, streak resets
    wrong                -> drop back a rung (never below the bottom)
    """
    before = ladder.rung
    rung = RUNGS[min(ladder.rung, TOP_RUNG)]
    moved, note = "stay", ""

    if verdict == "correct" and not used_solution:
        ladder.clean_streak += 1
        if ladder.clean_streak >= rung.clean_needed:
            ladder.clean_streak = 0
            if ladder.rung >= TOP_RUNG:
                moved, note = "graduated", "You solved it cold, twice. That's the whole ladder."
            else:
                ladder.rung += 1
                moved = "up"
                note = f"Up a rung — {RUNGS[ladder.rung].support}"
        else:
            left = rung.clean_needed - ladder.clean_streak
            note = f"Right. {left} more like that and the next rung opens."
    elif verdict == "correct" and used_solution:
        # Streak deliberately UNCHANGED. Both the module docstring and the
        # button in the UI promise that looking costs you nothing except this
        # rung's tick; resetting the streak also took away a clean solve you
        # had already earned, which is a punishment, and a silent one.
        note = "Right, but you had the solution open — this one doesn't count toward the next rung."
    elif verdict == "partial":
        ladder.clean_streak = 0
        note = "Close. Same rung again — the gap is worth one more go at this level."
    else:
        ladder.clean_streak = 0
        if ladder.rung > 0:
            ladder.rung -= 1
            moved = "down"
            note = f"Back a rung, on purpose — {RUNGS[ladder.rung].support}"
        else:
            note = "Still the bottom rung. Read the worked solution again; there's no penalty here."

    return {"moved": moved, "note": note, "from_rung": before, "to_rung": ladder.rung}


def attempt(repo_root: Path, class_slug: str, ladder_id: str, student_answer: str, llm_fn,
            used_solution: bool = False, now: datetime | None = None) -> dict:
    """Check one attempt, move the ladder, and record it."""
    ladder = get_ladder(repo_root, class_slug, ladder_id)
    if not ladder.current:
        raise ValueError("no problem in front of you — generate one first")
    rung = RUNGS[min(ladder.rung, TOP_RUNG)]
    answer = (student_answer or "").strip()

    if rung.submits and not answer:
        raise ValueError("Write your working or your answer first — even a wrong attempt is what moves this.")
    if not rung.submits:
        # The bottom rung IS the full solution; "you looked at it" is not a
        # meaningful thing to record there, and letting it through pinned the
        # ladder at rung 0 forever.
        used_solution = False

    if not rung.submits:
        # The reading rung: there is nothing to check, and pretending to
        # grade "I follow this" would be theatre.
        check = {"verdict": "correct", "summary": "Read through. Now try one with the last step left to you.",
                 "went_wrong": "", "right_move": ""}
    else:
        prompt = f"{_CHECK_PROMPT}\n\nReturn exactly this JSON shape: {_CHECK_SHAPE}"
        problem = ladder.current
        context = (
            f"PROBLEM\n{problem.get('problem', '')}\n\n"
            f"GIVEN\n" + "\n".join(str(g) for g in problem.get("given", [])) + "\n\n"
            f"REFERENCE SOLUTION\n"
            + "\n".join(f"- {s.get('action', '')}" for s in problem.get("solution", []))
            + f"\nREFERENCE ANSWER: {problem.get('answer', '')}\n\n"
            f"--- STUDENT ANSWER ---\n{answer}"
        )
        check = parse_session_json(llm_fn(prompt, context))
        if str(check.get("verdict", "")).lower() not in _VERDICTS:
            raise ValueError("the check came back in a shape this can't read — try again")
        check["verdict"] = str(check["verdict"]).lower()

    rung_before, streak_before = ladder.rung, ladder.clean_streak
    outcome = _apply_outcome(ladder, check["verdict"], used_solution)
    ladder.attempts.append(
        asdict(Attempt(
            at=localtime.now_local(now).isoformat(),
            rung=rung.key,
            rung_before=rung_before,
            streak_before=streak_before,
            verdict=check["verdict"],
            student_answer=answer[:MAX_ATTEMPT_FIELD_CHARS],
            summary=str(check.get("summary", ""))[:MAX_ATTEMPT_FIELD_CHARS],
            went_wrong=str(check.get("went_wrong", ""))[:MAX_ATTEMPT_FIELD_CHARS],
            right_move=str(check.get("right_move", ""))[:MAX_ATTEMPT_FIELD_CHARS],
            used_solution=used_solution,
            problem=str((ladder.current or {}).get("problem", ""))[:400],
        ))
    )
    ladder.attempts = ladder.attempts[-MAX_ATTEMPTS_KEPT:]
    ladder.updated_at = localtime.now_local(now).isoformat()
    # The problem you just answered is spent, whichever way it went — you have
    # seen its solution, and if the rung moved it is the wrong amount of
    # support now anyway. Clearing it is what stops a rung-3 problem being
    # served with a rung-0 worked solution still attached to it.
    ladder.current = None
    if outcome["moved"] == "graduated":
        ladder.graduated_at = ladder.updated_at
    elif ladder.graduated_at and outcome["moved"] == "down":
        # ONLY on the way down. This used to clear on any outcome that wasn't
        # a fresh graduation — including a correct unaided solve, because
        # graduating resets the streak and the top rung needs two, so the very
        # next right answer scored "stay" and silently revoked the badge.
        # Pressing "Next problem" after finishing is the most likely thing
        # anyone does, so it landed within a session of every graduation.
        ladder.graduated_at = ""
    _upsert(repo_root, ladder)
    return {"check": check, "outcome": outcome, "ladder": ladder.to_dict(), "progress": progress(ladder)}


def override_verdict(repo_root: Path, class_slug: str, ladder_id: str, verdict: str,
                     now: datetime | None = None) -> dict:
    """Disagree with the check.

    The model marking a right answer wrong is not hypothetical, and a tutor
    you cannot argue with is one you stop using. This re-applies the ladder
    rules with the student's verdict and rewrites the last attempt, rather
    than stacking a second attempt on top of the first."""
    verdict = str(verdict).lower()
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(_VERDICTS)}")
    ladder = get_ladder(repo_root, class_slug, ladder_id)
    if not ladder.attempts:
        raise ValueError("nothing to re-mark yet")
    last = ladder.attempts[-1]
    if last.get("verdict") == verdict:
        return {"outcome": {"moved": "stay", "note": "Already marked that way."},
                "ladder": ladder.to_dict(), "progress": progress(ladder)}

    # Undo the last move, then re-apply with the corrected verdict.
    rung_on_screen = ladder.rung  # where the student is standing right now
    ladder.rung = int(last.get("rung_before", ladder.rung))
    ladder.clean_streak = int(last.get("streak_before", 0))
    outcome = _apply_outcome(ladder, verdict, last.get("used_solution", False))
    last["verdict"] = verdict
    base = (last.get("summary") or "").split("  [you re-marked this]")[0]
    last["summary"] = base + "  [you re-marked this]"
    ladder.updated_at = localtime.now_local(now).isoformat()
    if ladder.rung != rung_on_screen:
        # Compared against where the ladder actually WAS, not against the
        # rewound position — the rewind is an implementation detail and
        # outcome["from_rung"] reports it. Same reason attempt() clears it: a
        # problem generated for one rung carries the wrong amount of the
        # solution for any other, and the renderer keys off the ladder's rung
        # rather than the problem's.
        ladder.current = None
    if outcome["moved"] == "graduated":
        ladder.graduated_at = ladder.updated_at
        ladder.current = None
    elif ladder.graduated_at:
        # Any re-mark that is not itself a graduation takes the badge back.
        # The old test (to_rung < TOP_RUNG) missed "partial" at the top rung,
        # which left the ladder graduated off an attempt now recorded as
        # partial, streak zeroed — and the dashboard renders no body for a
        # finished ladder, so it could not be continued, only deleted.
        ladder.graduated_at = ""
    _upsert(repo_root, ladder)
    return {"outcome": outcome, "ladder": ladder.to_dict(), "progress": progress(ladder)}
