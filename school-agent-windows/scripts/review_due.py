#!/usr/bin/env python3
"""Interactive terminal review of due flashcards, across all classes or one.

Usage: python scripts/review_due.py [class-slug]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from school_agent import paths  # noqa: E402
from school_agent.config import load_classes  # noqa: E402
from school_agent.quiz import CardStore  # noqa: E402


def review_one_class(slug: str) -> None:
    store = CardStore(paths.cards_path(REPO_ROOT, slug))
    due = store.due_cards()
    if not due:
        print(f"{slug}: nothing due")
        return
    print(f"{slug}: {len(due)} card(s) due")
    for card in due:
        print(f"\nQ: {card.question}")
        input("  (press enter to reveal answer) ")
        print(f"A: {card.answer}")
        rating = input("  rate [again/hard/good/easy]: ").strip().lower()
        store.review(card.card_id, rating)
    store.save()


def main() -> int:
    classes = load_classes(REPO_ROOT / "config" / "classes.yaml")
    slugs = [sys.argv[1]] if len(sys.argv) > 1 else [c.slug for c in classes]
    for slug in slugs:
        review_one_class(slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
