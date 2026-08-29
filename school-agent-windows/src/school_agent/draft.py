"""Drafting assistant — the one capability where the owner explicitly chose
"full drafting help" over more conservative options (see the DRAFT tag
below, which exists precisely because of that choice) (
school-agent.md §4).

The line that does NOT move regardless of that choice: this module has no
submit path, structurally — there is no function here that talks to
MyCourses/Brightspace, no matter how the draft is generated. It writes a
local file, tagged, and stops. The owner reviews and submits by hand, always.

Academic integrity is real and institution/assignment-specific (some
explicitly allow AI-assisted drafting, some forbid it, most don't say) — the
DRAFT_HEADER exists to make that decision point visible rather than silent,
without blocking anything or being preachy about it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

DRAFT_HEADER = "## DRAFT — verify this specific assignment's AI-use policy before submitting\n\n"


class LLMFn(Protocol):
    def __call__(self, prompt: str, context: str) -> str: ...


def wrap_draft(body: str, header: str = DRAFT_HEADER) -> str:
    """Idempotent: won't double-tag a body that's already tagged."""
    if body.startswith(header):
        return body
    return f"{header}{body}"


def generate_draft(assignment_prompt: str, materials_context: str, llm_fn: LLMFn) -> str:
    """llm_fn is injected deliberately — which model answers this call is a
    per-owner runtime decision (see iris-model-architecture-decided in
    project memory), not something this module should hardcode a provider
    for. Drafting is exactly the kind of judgment-heavy, substantive-answer
    work that architecture scopes to the cloud reasoning tier, not the local
    floor."""
    raw = llm_fn(assignment_prompt, materials_context)
    return wrap_draft(raw)


def save_draft(drafts_dir: Path, assignment_slug: str, content: str) -> Path:
    """Never overwrites. v2 wrote `<slug>.md` with a plain write_text and the
    slug defaulted to "draft", so every unnamed draft silently destroyed the
    previous one — real work lost while iterating on an essay, with no warning.
    Now each save gets its own file, and the name is slugified so it can't
    escape the drafts directory."""
    from .config import slugify
    from .storage import atomic_write_text

    drafts_dir = Path(drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    base = slugify(assignment_slug) if assignment_slug and assignment_slug.strip() else "draft"
    out_path = drafts_dir / f"{base}.md"
    n = 2
    while out_path.exists():
        out_path = drafts_dir / f"{base}-{n}.md"
        n += 1
    atomic_write_text(out_path, wrap_draft(content))
    return out_path


def list_drafts(drafts_dir: Path) -> list[dict]:
    """Saved drafts were previously write-only — nothing in the UI could list
    or reopen one, so a draft scrolled off screen was effectively gone."""
    drafts_dir = Path(drafts_dir)
    if not drafts_dir.exists():
        return []
    out = []
    for p in drafts_dir.glob("*.md"):
        try:
            stat = p.stat()
            out.append({"name": p.stem, "filename": p.name, "modified": stat.st_mtime, "size": stat.st_size})
        except OSError:
            continue
    out.sort(key=lambda d: d["modified"], reverse=True)  # newest first, not alphabetical
    return out
