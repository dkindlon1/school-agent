"""Active-recall quizzing, scheduled with py-fsrs (open-spaced-repetition/py-fsrs).

Reused, not built, for one reason: the
forgetting-curve math is the one genuinely hard part of a spaced-repetition
system, and py-fsrs is a real, maintained implementation of it.

**Question generation (2026-08-25 fix):** v1 shipped `add_card(question,
answer)` with nothing anywhere that actually produced a question or answer
— an adversarial review confirmed the only calls to `add_card` were in
tests with hand-written strings. `generate_cards_from_materials` below
closes that gap: it's a real, callable path from ingested course material
to new quiz cards, via the same injectable LLM function every generation
capability in this package uses (see llm.py) — never a hardcoded provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fsrs import Card, Rating, Scheduler

from .materials import MaterialEntry, build_context, sample_chunks
from .storage import atomic_write_json, load_json_self_healing
from .notify import notify

_RATING_BY_NAME = {"again": Rating.Again, "hard": Rating.Hard, "good": Rating.Good, "easy": Rating.Easy}


def normalize_question(q: str) -> str:
    """Dedupe key. Generation used to run against the same fixed excerpts every
    time with no comparison to existing cards, so pressing 'Generate' twice
    produced a second copy of the same ten questions — and every duplicate then
    consumed review time forever."""
    return " ".join(q.lower().split()).strip(" ?.!")


@dataclass
class QuizCard:
    card_id: str
    class_slug: str
    question: str
    answer: str
    fsrs_state: dict


class CardStore:
    """One JSON file per class holding every quiz card and its FSRS state."""

    def __init__(self, path: Path | str, scheduler: Scheduler | None = None) -> None:
        self.path = Path(path)
        self.scheduler = scheduler or Scheduler()
        self._cards: dict[str, QuizCard] = {}
        self._load()

    def _load(self) -> None:
        raw = load_json_self_healing(self.path, default=[])
        for entry in raw:
            try:
                c = QuizCard(**entry)
            except TypeError as exc:
                notify(f"skipping malformed card entry in {self.path}: {exc}")
                continue
            self._cards[c.card_id] = c

    def save(self) -> None:
        atomic_write_json(self.path, [vars(c) for c in self._cards.values()])

    def add_card(self, class_slug: str, question: str, answer: str) -> QuizCard:
        card = Card()
        card_id = str(card.card_id)
        qc = QuizCard(card_id=card_id, class_slug=class_slug, question=question, answer=answer, fsrs_state=card.to_dict())
        self._cards[card_id] = qc
        return qc

    def has_question(self, question: str, class_slug: str | None = None) -> bool:
        key = normalize_question(question)
        return any(
            normalize_question(c.question) == key
            for c in self.all_cards(class_slug)
        )

    def delete_card(self, card_id: str) -> None:
        """A wrong LLM-generated card used to be permanent — and FSRS makes
        that actively harmful, because rating it 'Again' (since the answer
        looks wrong) schedules it MORE often. A wrong fact would get the most
        drill time in the system. Removal is the fix."""
        if card_id not in self._cards:
            raise KeyError(f"no card with id {card_id!r}")
        del self._cards[card_id]

    def edit_card(self, card_id: str, question: str | None = None, answer: str | None = None) -> QuizCard:
        """Correct a card's text while keeping its FSRS schedule — a small
        wording fix shouldn't reset everything you've already learned."""
        if card_id not in self._cards:
            raise KeyError(f"no card with id {card_id!r}")
        qc = self._cards[card_id]
        if question is not None and question.strip():
            qc.question = question.strip()
        if answer is not None and answer.strip():
            qc.answer = answer.strip()
        return qc

    def all_cards(self, class_slug: str | None = None) -> list[QuizCard]:
        cards = list(self._cards.values())
        if class_slug is not None:
            cards = [c for c in cards if c.class_slug == class_slug]
        return cards

    def due_cards(self, now: datetime | None = None, class_slug: str | None = None) -> list[QuizCard]:
        """A card with a malformed/missing fsrs_state is skipped and
        reported, not allowed to abort the whole batch — a review found the
        v1 version raised KeyError on one bad card and hid every OTHER due
        card in the same class as a side effect."""
        now = now or datetime.now(timezone.utc)
        due = []
        for c in self.all_cards(class_slug):
            try:
                due_at = datetime.fromisoformat(c.fsrs_state["due"])
            except (KeyError, ValueError, TypeError) as exc:
                notify(f"skipping card {c.card_id!r} ({c.question[:40]!r}) with unreadable schedule: {exc}")
                continue
            if due_at <= now:
                due.append(c)
        due.sort(key=lambda c: c.fsrs_state["due"])
        return due

    def review(self, card_id: str, rating: str, now: datetime | None = None) -> QuizCard:
        if card_id not in self._cards:
            raise KeyError(f"no card with id {card_id!r}")
        if rating not in _RATING_BY_NAME:
            raise ValueError(f"rating must be one of {sorted(_RATING_BY_NAME)}, got {rating!r}")
        qc = self._cards[card_id]
        card = Card.from_dict(qc.fsrs_state)
        updated_card, _log = self.scheduler.review_card(card, _RATING_BY_NAME[rating], review_datetime=now)
        qc.fsrs_state = updated_card.to_dict()
        return qc


class LLMFn(Protocol):
    def __call__(self, prompt: str, context: str) -> str: ...


def generate_cards_from_materials(
    class_slug: str,
    materials: list[MaterialEntry],
    llm_fn: LLMFn,
    max_cards: int = 10,
    materials_dir=None,
    topic: str | None = None,
) -> list[tuple[str, str]]:
    """Ask the LLM for question/answer pairs grounded in real ingested
    material — never generic trivia (the
    best-in-class bar requires questions test what the owner is actually
    being taught). Returns (question, answer) pairs; the caller adds them to
    a CardStore, so this stays a pure generator that's easy to test.

    2026-08-26: context now comes from chunks sampled across the WHOLE of
    each document (materials.sample_chunks) instead of every document's first
    2,000 characters. Before this, a 40-page chapter contributed its title
    page, so the generated questions were about cover-page boilerplate.
    `materials_dir` is optional only so older callers keep working — without
    it there is no full text to sample and this falls back to excerpts.
    """
    if not materials:
        return []
    if materials_dir is not None:
        if topic:
            from .materials import relevant_chunks

            chunks = relevant_chunks(materials_dir, materials, topic, k=8)
        else:
            chunks = sample_chunks(materials_dir, materials, max_chunks=8)
        context = build_context(chunks)
    else:
        context = "\n\n---\n\n".join(
            f"[{m.filename}]\n{m.text_excerpt}" for m in materials if m.text_excerpt.strip()
        )
    if not context.strip():
        return []
    scope = f" Focus specifically on: {topic}." if topic else ""
    prompt = (
        f"Generate up to {max_cards} active-recall quiz questions (question and concise answer, "
        "one per line, formatted exactly as 'Q: ...\\nA: ...') from the course material below."
        f"{scope} Test understanding of specific facts, definitions, and relationships actually "
        "present in the material — never generic trivia, and never questions about administrative "
        "boilerplate such as office hours, grading policy, or attendance rules."
    )
    raw = llm_fn(prompt, context)
    return _parse_qa_pairs(raw, max_cards)


def _parse_qa_pairs(raw: str, max_cards: int) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_q: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("Q:"):
            pending_q = line[2:].strip()
        elif line.startswith("A:") and pending_q is not None:
            pairs.append((pending_q, line[2:].strip()))
            pending_q = None
        if len(pairs) >= max_cards:
            break
    return pairs
