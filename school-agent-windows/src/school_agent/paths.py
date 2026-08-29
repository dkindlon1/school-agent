"""Per-class data directory layout — filesystem + JSON, no database server.

data/<class-slug>/
    materials/       ingested source files (syllabi, readings, slides, notes)
    deadlines.json   parsed + deduped events from the ICS feed
    cards.json        FSRS card state (quiz/flashcard review schedule)
    drafts/            drafted written work, always DRAFT-tagged (see draft.py)
"""

from __future__ import annotations

from pathlib import Path


def data_root(repo_root: Path | str) -> Path:
    return Path(repo_root) / "data"


def class_dir(repo_root: Path | str, class_slug: str) -> Path:
    return data_root(repo_root) / class_slug


def materials_dir(repo_root: Path | str, class_slug: str) -> Path:
    return class_dir(repo_root, class_slug) / "materials"


def drafts_dir(repo_root: Path | str, class_slug: str) -> Path:
    return class_dir(repo_root, class_slug) / "drafts"


def deadlines_path(repo_root: Path | str, class_slug: str) -> Path:
    return class_dir(repo_root, class_slug) / "deadlines.json"


def cards_path(repo_root: Path | str, class_slug: str) -> Path:
    return class_dir(repo_root, class_slug) / "cards.json"


def grading_path(repo_root: Path | str, class_slug: str) -> Path:
    """The syllabus grading table: components, weights, exam dates. Without
    this the app can only sort by due date, which ranks a 1% discussion post
    above a 20% midterm."""
    return class_dir(repo_root, class_slug) / "grading.json"


def scores_path(repo_root: Path | str, class_slug: str) -> Path:
    return class_dir(repo_root, class_slug) / "scores.json"


def briefing_checks_path(repo_root: Path | str) -> Path:
    """Which briefing lines the owner has ticked off. Keyed by a hash of the
    line text rather than by position, so a regenerated briefing that still
    lists something already done shows it already done."""
    return data_root(repo_root) / "briefing_checks.json"


def chat_uploads_dir(repo_root: Path | str) -> Path:
    """Files shared directly into a chat rather than filed under a class.

    Kept separate from any class's materials/ on purpose: a lot of what you
    want to hand the assistant mid-conversation isn't course material at all —
    a problem you're stuck on, a job posting, a paper someone sent you — and
    forcing a class choice before you can ask a question is friction for no
    benefit. Structurally it's still just a materials directory, so all the
    existing extraction and chunk retrieval works on it unchanged."""
    return data_root(repo_root) / "chat_uploads"


def chat_uploads_index(repo_root: Path | str) -> Path:
    """Deliberately OUTSIDE chat_uploads/ — an index stored inside the very
    directory it indexes ends up indexing itself, which showed up as a phantom
    "index.json" file offered as something you could share with the model."""
    return data_root(repo_root) / "chat_uploads_index.json"


def chats_path(repo_root: Path | str) -> Path:
    """Saved chat conversations, shared across classes."""
    return data_root(repo_root) / "chats.json"


def sessions_path(repo_root: Path | str) -> Path:
    """Focus-session history, shared across classes."""
    return data_root(repo_root) / "sessions.json"


def done_path(repo_root: Path | str, class_slug: str) -> Path:
    """Deadline uids the owner has marked completed. Separate from
    dismissed.json on purpose: "I turned this in" and "this was never a real
    deadline" are different facts, and only one of them is worth counting."""
    return class_dir(repo_root, class_slug) / "done.json"


def dismissed_path(repo_root: Path | str, class_slug: str) -> Path:
    """Deadline uids the owner cleared from the board — kept SEPARATE from
    deadlines.json, which the 30-minute sync overwrites wholesale; a
    dismissal must survive every future re-sync of the same feed."""
    return class_dir(repo_root, class_slug) / "dismissed.json"


def materials_index_path(repo_root: Path | str, class_slug: str) -> Path:
    return class_dir(repo_root, class_slug) / "materials_index.json"


def ensure_class_dirs(repo_root: Path | str, class_slug: str) -> None:
    materials_dir(repo_root, class_slug).mkdir(parents=True, exist_ok=True)
    drafts_dir(repo_root, class_slug).mkdir(parents=True, exist_ok=True)
