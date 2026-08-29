"""Get-ahead study: given the syllabus's topic order and today's date, work
out what's coming up next and produce a pre-read summary from the owner's own
material — honestly flagged when nothing relevant is on file, never a generic
filler summary.

**2026-08-26 rewrite.** This feature was structurally dead in v2 for two
compounding reasons, both now fixed here and in materials.py:

1. Retrieval was `topic_string in first_2000_chars_of_file` — a whole-phrase
   substring test against a cover page. A real syllabus label like "Ch. 6 —
   Entropy and the Second Law" essentially never appears verbatim in a
   document body, so the honest "no material ingested yet" message fired even
   when the owner had uploaded the entire chapter. The honesty was masking a
   matching bug, not an empty library.
2. Even on a hit it summarized the file's *cover page*, because that excerpt
   was the only text stored.

Now it retrieves the top-scoring chunks from the full text of every document
and summarizes those.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from .config import ClassConfig
from .materials import MaterialEntry, build_context, relevant_chunks
from .notify import notify


def _parse_topics(class_config: ClassConfig) -> list[tuple[date, str]]:
    """One malformed row is skipped and reported rather than raising — a
    mistyped date in hand-edited classes.yaml used to raise ValueError, which
    500'd the topics endpoint, silently emptied the panel, AND stopped the
    briefing from regenerating."""
    out = []
    for entry in class_config.topics:
        try:
            d_str, topic = entry[0], entry[1]
            out.append((date.fromisoformat(str(d_str)), str(topic)))
        except (IndexError, TypeError, ValueError) as exc:
            notify(f"skipping malformed topic entry {entry!r} in {class_config.slug}: {exc}", channel="console")
    out.sort(key=lambda t: t[0])
    return out


def upcoming_topics(class_config: ClassConfig, today: date, lookahead_days: int = 7) -> list[tuple[date, str]]:
    topics = _parse_topics(class_config)
    horizon = today + timedelta(days=lookahead_days)
    return [(d, t) for d, t in topics if today <= d <= horizon]


class LLMFn(Protocol):
    def __call__(self, prompt: str, context: str) -> str: ...


def summarize_topic(
    topic: str,
    materials: list[MaterialEntry],
    llm_fn: LLMFn | None,
    materials_dir=None,
) -> str:
    if materials_dir is not None:
        chunks = relevant_chunks(materials_dir, materials, topic, k=6)
        context = build_context(chunks)
        source_count = len({c.filename for c in chunks})
    else:  # legacy path: no full text available, fall back to excerpts
        from .materials import search

        relevant = search(materials, topic)
        context = "\n\n".join(m.text_excerpt for m in relevant)
        source_count = len(relevant)

    if not context.strip():
        return (
            f"No material on file covers '{topic}' yet. Upload the reading, slides, or your notes "
            "for this topic to the Documents section and try again — this only ever summarizes "
            "your own material, so it won't guess."
        )
    if llm_fn is None:
        return f"Relevant material found for '{topic}' ({source_count} file(s)) but no model configured yet:\n\n{context[:1500]}"
    return llm_fn(
        f"Summarize the upcoming topic '{topic}' for a student who hasn't covered it in class yet. "
        "Lead with the core idea in two sentences, then the key definitions, equations, and where "
        "students typically go wrong. Use only the material provided.",
        context,
    )
