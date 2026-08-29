"""Grade ledger — weights, scores, and what you'd need on what's left.

Added 2026-08-26. Until now the app stored calendar events, text excerpts,
flashcards and drafts, and had no concept of `points`, `weight`, `score` or
`grade` anywhere in it. That had one concrete consequence: every prioritization
the app did was due-date order, which ranks a 1%-of-grade discussion post due
tomorrow above a 20% midterm four days out. For an engineering course load
where most points live in a handful of exam sittings, that ordering is
actively wrong.

This module is the missing spine. It holds:

- a **grading scheme** per class (components, weights, item counts, drop-lowest
  rules, exam dates) — extracted from the syllabus the owner already uploads,
  but never trusted until they confirm it, because a wrong weight produces
  confident wrong advice, which is worse than no advice;
- **scores** as they come back;
- the arithmetic that turns those into a current grade, the weight still in
  play, and the average needed on everything remaining to land each letter.

Deliberately pure: no I/O beyond its own JSON files, no model calls except the
one clearly-marked extraction helper, so the math is trivially testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .storage import atomic_write_json, load_json_self_healing

# Standard US letter thresholds. Individual syllabi vary, and when the
# extraction finds an explicit scale we store it on the scheme and use that.
DEFAULT_LETTER_SCALE = [
    ("A", 93.0), ("A-", 90.0), ("B+", 87.0), ("B", 83.0), ("B-", 80.0),
    ("C+", 77.0), ("C", 73.0), ("C-", 70.0), ("D", 60.0), ("F", 0.0),
]


@dataclass
class Component:
    """One row of the syllabus grading table, e.g. Homework 20%, 10 sets,
    lowest dropped."""
    name: str
    weight_pct: float
    count: int | None = None  # items in the category over the term, if stated
    drop_lowest: int = 0

    @staticmethod
    def from_dict(d: dict) -> "Component":
        return Component(
            name=str(d.get("name", "")).strip() or "Unnamed",
            weight_pct=float(d.get("weight_pct", 0) or 0),
            count=int(d["count"]) if d.get("count") else None,
            drop_lowest=int(d.get("drop_lowest", 0) or 0),
        )


@dataclass
class Score:
    component: str
    name: str
    earned: float
    possible: float
    date: str = ""

    @property
    def fraction(self) -> float:
        return (self.earned / self.possible) if self.possible else 0.0

    @staticmethod
    def from_dict(d: dict) -> "Score":
        return Score(
            component=str(d.get("component", "")).strip(),
            name=str(d.get("name", "")).strip(),
            earned=float(d.get("earned", 0) or 0),
            possible=float(d.get("possible", 0) or 0),
            date=str(d.get("date", "")),
        )


@dataclass
class GradingScheme:
    components: list[Component] = field(default_factory=list)
    exams: list[dict] = field(default_factory=list)  # {name, date, scope}
    letter_scale: list[list] = field(default_factory=list)  # [[letter, min_pct], ...]
    notes: str = ""
    confirmed: bool = False  # extraction is a PROPOSAL until the owner says otherwise

    def to_dict(self) -> dict:
        return {
            "components": [asdict(c) for c in self.components],
            "exams": self.exams,
            "letter_scale": self.letter_scale,
            "notes": self.notes,
            "confirmed": self.confirmed,
        }

    @staticmethod
    def from_dict(d: dict) -> "GradingScheme":
        return GradingScheme(
            components=[Component.from_dict(c) for c in d.get("components", [])],
            exams=list(d.get("exams", [])),
            letter_scale=list(d.get("letter_scale", [])),
            notes=str(d.get("notes", "")),
            confirmed=bool(d.get("confirmed", False)),
        )

    @property
    def total_weight(self) -> float:
        return sum(c.weight_pct for c in self.components)

    def scale(self) -> list[tuple[str, float]]:
        if self.letter_scale:
            try:
                return [(str(l), float(p)) for l, p in self.letter_scale]
            except (TypeError, ValueError):
                pass
        return DEFAULT_LETTER_SCALE


# ------------------------------------------------------------ persistence --

def load_scheme(path: Path | str) -> GradingScheme:
    raw = load_json_self_healing(path, default=None)
    return GradingScheme.from_dict(raw) if isinstance(raw, dict) else GradingScheme()


def save_scheme(path: Path | str, scheme: GradingScheme) -> None:
    atomic_write_json(path, scheme.to_dict())


def load_scores(path: Path | str) -> list[Score]:
    raw = load_json_self_healing(path, default=[])
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            out.append(Score.from_dict(d))
        except (TypeError, ValueError):
            continue  # one malformed row must not lose the rest
    return out


def save_scores(path: Path | str, scores: list[Score]) -> None:
    atomic_write_json(path, [asdict(s) for s in scores])


# -------------------------------------------------------------- the math --

@dataclass
class ComponentProgress:
    name: str
    weight_pct: float
    graded_items: int
    total_items: int | None
    fraction: float  # 0-1 average on graded items, after drop-lowest
    graded_weight: float  # share of the final grade already decided here
    earned_weight: float  # of that share, how much was earned


def component_progress(scheme: GradingScheme, scores: list[Score]) -> list[ComponentProgress]:
    by_component: dict[str, list[Score]] = {}
    for s in scores:
        by_component.setdefault(_norm(s.component), []).append(s)

    out = []
    for c in scheme.components:
        mine = by_component.get(_norm(c.name), [])
        fractions = sorted(s.fraction for s in mine if s.possible)
        # Drop-lowest is applied as soon as there's more than one score. It's
        # optimistic mid-semester (the dropped one might not stay the lowest)
        # but it matches how the syllabus will actually compute the final
        # grade, which is the number the owner is steering toward.
        if c.drop_lowest and len(fractions) > c.drop_lowest:
            fractions = fractions[c.drop_lowest:]
        fraction = (sum(fractions) / len(fractions)) if fractions else 0.0

        # How much of this component's weight is already decided. With a known
        # item count that's proportional; without one, any score means we treat
        # the component as fully represented by what's entered.
        if not mine:
            graded_weight = 0.0
        elif c.count:
            # Uses the count of items actually returned — NOT the post-drop
            # count. Dropping the lowest changes your average, not how much of
            # the semester has been graded; conflating the two made a component
            # look less decided than it is.
            effective_total = max(1, c.count - c.drop_lowest)
            graded_weight = c.weight_pct * min(1.0, len(mine) / effective_total)
        else:
            graded_weight = c.weight_pct

        out.append(
            ComponentProgress(
                name=c.name,
                weight_pct=c.weight_pct,
                graded_items=len(mine),
                total_items=c.count,
                fraction=fraction,
                graded_weight=graded_weight,
                earned_weight=graded_weight * fraction,
            )
        )
    return out


@dataclass
class GradeSummary:
    has_data: bool
    current_pct: float | None  # grade on work graded so far
    current_letter: str | None
    graded_weight: float  # % of the final grade already determined
    remaining_weight: float
    earned_weight: float
    components: list[ComponentProgress]
    scheme_total_weight: float
    scheme_confirmed: bool
    # Scores whose component name matches nothing in the current scheme —
    # see orphaned_scores(). Never silently dropped again.
    orphaned: list[dict] = field(default_factory=list)


def orphaned_scores(scheme: GradingScheme, scores: list[Score]) -> list[dict]:
    """Scores filed under a component the scheme no longer has.

    These count for nothing in the average, which is correct — the app cannot
    invent a weight for a bucket that does not exist. What was NOT correct was
    doing it silently (2026-08-26): renaming "Homework" to "Problem Sets", or
    re-running syllabus extraction and getting slightly different component
    names back, moved a measured 85.5% B to 82.8% B- with no indication that
    six graded assignments had just stopped counting. A grade tracker that
    quietly changes your grade is worse than no grade tracker.

    Grouped by the orphaned name so the UI can offer "move these to <x>"
    rather than making you re-enter them."""
    known = {_norm(c.name) for c in scheme.components}
    groups: dict[str, dict] = {}
    for s in scores:
        key = _norm(s.component)
        if key in known:
            continue
        g = groups.setdefault(key, {"component": s.component, "count": 0, "titles": []})
        g["count"] += 1
        if len(g["titles"]) < 5 and s.name:
            g["titles"].append(s.name)
    return sorted(groups.values(), key=lambda g: -g["count"])


def reassign_component(scores: list[Score], from_component: str, to_component: str) -> list[Score]:
    """Move every score filed under one component name to another, so a
    rename costs one click instead of re-entering a semester of grades."""
    src = _norm(from_component)
    out = []
    for s in scores:
        if _norm(s.component) == src:
            s = replace(s, component=to_component)
        out.append(s)
    return out


def summarize(scheme: GradingScheme, scores: list[Score]) -> GradeSummary:
    comps = component_progress(scheme, scores)
    graded_weight = sum(c.graded_weight for c in comps)
    earned_weight = sum(c.earned_weight for c in comps)
    current = (earned_weight / graded_weight * 100.0) if graded_weight > 0 else None
    return GradeSummary(
        has_data=bool(scheme.components) and graded_weight > 0,
        current_pct=round(current, 1) if current is not None else None,
        current_letter=letter_for(current, scheme) if current is not None else None,
        graded_weight=round(graded_weight, 1),
        remaining_weight=round(max(0.0, scheme.total_weight - graded_weight), 1),
        earned_weight=round(earned_weight, 2),
        components=comps,
        scheme_total_weight=round(scheme.total_weight, 1),
        scheme_confirmed=scheme.confirmed,
        orphaned=orphaned_scores(scheme, scores),
    )


def letter_for(pct: float | None, scheme: GradingScheme | None = None) -> str | None:
    if pct is None:
        return None
    scale = scheme.scale() if scheme else DEFAULT_LETTER_SCALE
    for letter, minimum in sorted(scale, key=lambda x: x[1], reverse=True):
        if pct >= minimum:
            return letter
    return scale[-1][0] if scale else None


def needed_for_target(scheme: GradingScheme, scores: list[Score], target_pct: float) -> dict:
    """The single most decision-changing number a student can have: what
    average the remaining work has to hit to land a given overall grade."""
    summary = summarize(scheme, scores)
    remaining = summary.remaining_weight
    if remaining <= 0:
        return {
            "target_pct": target_pct,
            "possible": summary.current_pct is not None and summary.current_pct >= target_pct,
            "needed_pct": None,
            "reason": "Everything is graded — this is your final standing.",
        }
    needed = (target_pct - summary.earned_weight) / remaining * 100.0
    return {
        "target_pct": target_pct,
        "needed_pct": round(needed, 1),
        "remaining_weight": remaining,
        # >100 means unreachable even with perfect scores; <=0 means already locked in.
        "possible": needed <= 100.0,
        "already_secured": needed <= 0.0,
    }


def targets_table(scheme: GradingScheme, scores: list[Score]) -> list[dict]:
    out = []
    for letter, minimum in scheme.scale():
        if letter in ("D", "F"):
            continue
        row = needed_for_target(scheme, scores, minimum)
        row["letter"] = letter
        out.append(row)
    return out


# ------------------------------------------------- deadline grade impact --

_ITEM_NUM = re.compile(r"\b\d+\b")


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(s).lower()))


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", str(s).lower()) if len(w) > 2}


# Words that mean the same category under different syllabus vocabularies.
_SYNONYMS = {
    "homework": {"homework", "hw", "problem", "problems", "set", "sets", "assignment", "assignments"},
    "quiz": {"quiz", "quizzes"},
    "exam": {"exam", "exams", "midterm", "midterms", "test", "tests", "final"},
    "lab": {"lab", "labs", "laboratory"},
    "project": {"project", "projects"},
    "discussion": {"discussion", "discussions", "post", "posts", "forum"},
    "participation": {"participation", "attendance"},
}


def _category_of(text: str) -> str | None:
    words = set(re.findall(r"[a-z]+", str(text).lower()))
    for category, vocabulary in _SYNONYMS.items():
        if words & vocabulary:
            return category
    return None


def match_component(scheme: GradingScheme, title: str) -> Component | None:
    """Map a Brightspace deadline title onto a grading component, so the app
    can say what an assignment is actually worth. Category synonyms first
    (a syllabus says "Problem Sets", the calendar says "HW 4"), then direct
    token overlap as a fallback."""
    if not scheme.components:
        return None
    title_category = _category_of(title)
    if title_category:
        for c in scheme.components:
            if _category_of(c.name) == title_category:
                return c
    title_tokens = _tokens(title)
    best, best_score = None, 0
    for c in scheme.components:
        overlap = len(title_tokens & _tokens(c.name))
        if overlap > best_score:
            best, best_score = c, overlap
    return best


def _effective_count(scheme: GradingScheme, component: Component) -> int | None:
    """How many items this component actually holds.

    Falls back to the number of exams the syllabus listed. Extraction is told
    to use null for `count` when the syllabus does not state one — and
    syllabi very often say "Exams: 70%" while listing the exam dates
    separately. Those dates are already on the scheme; counting them beats
    treating the component as unquantifiable.
    """
    if component.count:
        return component.count
    if scheme.exams and _norm(component.name) in _SYNONYMS["exam"]:
        return len(scheme.exams) or None
    return None


def item_weight(scheme: GradingScheme, component: Component) -> float | None:
    """What one item in this component is worth, as a % of the final grade.
    None when the component's item count is genuinely unknown."""
    count = _effective_count(scheme, component)
    if not count:
        return None
    effective = max(1, count - component.drop_lowest)
    return round(component.weight_pct / effective, 2)


def deadline_impact(scheme: GradingScheme, title: str) -> dict | None:
    c = match_component(scheme, title)
    if c is None:
        return None
    per_item = item_weight(scheme, c)
    return {
        "component": c.name,
        "component_weight": c.weight_pct,
        "item_weight": per_item,
        # What to SORT by. An unknown per-item weight used to sort as zero,
        # which put a midterm in an unquantified 70% bucket below a 3.33%
        # problem set — the exact inversion the grade ledger exists to
        # prevent, and it happens whenever a syllabus says "Exams: 70%"
        # without saying how many there are. When the per-item share is
        # unknown, the component's own weight is the honest lower bound on
        # how much this item could matter.
        "ordering_weight": per_item if per_item is not None else c.weight_pct,
        "item_weight_known": per_item is not None,
    }


# --------------------------------------------------- syllabus extraction --

EXTRACTION_PROMPT = (
    "From the course syllabus text below, extract the grading scheme as STRICT JSON only — "
    "no prose, no markdown fences. Use exactly this shape:\n"
    '{"components":[{"name":"Homework","weight_pct":20,"count":10,"drop_lowest":1}],'
    '"exams":[{"name":"Midterm 1","date":"2026-10-14","scope":"Ch 1-4"}],'
    '"letter_scale":[["A",93],["B",83]],"notes":"late policy: 10%/day, 3 days max"}\n'
    "Rules: weight_pct are numbers that should sum to about 100. Use null for count when the "
    "syllabus does not say how many items there are. Use ISO dates (YYYY-MM-DD) and omit any "
    "exam whose date is not stated. Include the late/drop policy in notes if present. "
    "Include ONLY what the syllabus actually states — never invent a weight, a count, or a date."
)


def parse_scheme_json(raw: str) -> GradingScheme:
    """Tolerant of the usual model output noise — fenced blocks, a sentence of
    preamble — because a parse failure here means the owner has to type the
    whole grading table by hand."""
    text = str(raw).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in the model's reply")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    scheme = GradingScheme.from_dict(data)
    scheme.confirmed = False  # always a proposal
    return scheme


def extract_scheme(materials_dir, entries, llm_fn) -> GradingScheme:
    """Pull the grading table out of whatever syllabus-ish material is on file.
    Returns an UNCONFIRMED scheme for the owner to check — the whole point of
    this module is that downstream advice is only as good as these numbers."""
    from .materials import build_context, relevant_chunks

    chunks = relevant_chunks(
        materials_dir, entries,
        "grading grade weight percent exam midterm final homework quiz participation policy late drop",
        k=8,
    )
    if not chunks:
        raise ValueError(
            "No syllabus-like material found for this class. Upload the syllabus (or paste its "
            "grading section) into Documents, then try again."
        )
    return parse_scheme_json(llm_fn(EXTRACTION_PROMPT, build_context(chunks, max_chars=18_000)))
