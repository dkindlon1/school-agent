"""Conversational interface over the owner's own course material.

Added 2026-08-26. Everything else in this app is a one-shot: generate cards,
summarize a topic, write a draft. This is the open-ended half — ask a question,
get an answer, follow up — which is what the Claude Projects workflow this
venture replaces was actually used for day to day.

The thing that makes it more than a generic chat box is **mentions**. Typing
`@` offers the owner's real classes and documents; whatever they pick is
resolved server-side into actual excerpts from those files and attached to the
turn. So "explain @COURSE.101 entropy like I'm five" retrieves the chunks of the
owner's own thermodynamics material that are about entropy, rather than
answering from the model's general knowledge.

Design notes worth keeping:

- **History is flattened into one prompt** rather than each provider growing a
  multi-turn code path. llm.py deliberately exposes one `(prompt, context)`
  call across four providers; giving chat its own per-provider message-array
  format would quadruple that surface for a quality difference that doesn't
  matter at this length. The transcript is labelled clearly so the model reads
  it as a conversation.
- **Context is rebuilt per turn from the current mentions**, never accumulated,
  so a long conversation can't silently grow past the context window.
- Conversations are capped and trimmed; this is a study tool, not an archive.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import materials, paths
from .storage import atomic_write_json, load_json_self_healing

MAX_CONVERSATIONS = 50
# Per-conversation cap. The count of conversations was capped from the start;
# their CONTENTS were not, so one thread used all semester grew to ~10 MB and
# was parsed and rewritten on every single send. Well above a real thread, far
# below the point where the file hurts.
MAX_MESSAGES_PER_CONVERSATION = 200
MAX_TURNS_IN_PROMPT = 12  # older turns drop out of the prompt, not the file
MAX_CONTEXT_CHARS = 20_000

# The line this used to carry — "say plainly when it does not cover something
# rather than filling the gap with general knowledge" — was written to stop the
# model implying it had read notes it hadn't. It did that, and it also did
# something much worse: it turned "what's the difference between a vector and a
# scalar" into a refusal, because no uploaded excerpt happened to define one.
#
# The real requirement was never "don't use general knowledge". It was "don't
# pass general knowledge off as something their professor said". Those are
# separable, and separating them is the whole point of this rewrite.
SYSTEM_PREAMBLE = (
    "You are the student's study assistant. Answer from your own knowledge of the subject "
    "fully and confidently — explain, derive, work through examples. Never refuse or hedge "
    "because the excerpts below don't happen to cover something; a question like 'what is a "
    "vector' deserves a real answer whether or not anything is uploaded. "
    "Excerpts from their own course material follow when any are relevant. Use them as "
    "context: match their notation and conventions, and prefer their framing, since the "
    "student's exam follows their course. Where the excerpts and the general treatment "
    "differ, follow the excerpts and say you're going by their course. "
    "Do not present your own knowledge as something their course said. If you are asked "
    "something only their course can answer — a due date, a weight, what's on the exam, what "
    "their professor covered — and it is not in front of you, say so instead of guessing. "
    "Explain like a good tutor: concrete, worked through, and honest about what is uncertain."
)


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str
    at: str = ""
    mentions: list = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "Message":
        return Message(
            role=str(d.get("role", "user")),
            content=str(d.get("content", "")),
            at=str(d.get("at", "")),
            mentions=list(d.get("mentions", [])),
        )


@dataclass
class Conversation:
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["messages"] = [asdict(m) if isinstance(m, Message) else m for m in self.messages]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Conversation":
        return Conversation(
            id=str(d.get("id", "")),
            title=str(d.get("title", "New conversation")),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
        )


# ----------------------------------------------------------- persistence --

def load_all(path: Path | str) -> list[Conversation]:
    raw = load_json_self_healing(path, default=[])
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            out.append(Conversation.from_dict(d))
        except (TypeError, ValueError):
            continue  # one bad record must not lose the rest
    return out


def save_one(path: Path | str, conversation: Conversation) -> list[Conversation]:
    """Write ONE conversation back, re-reading the store inside the write.

    This exists because save_all rewrites the whole list from whatever the
    caller was holding — and the caller holds it across the model call, which
    can take a minute. In that window: start a chat in a second tab and it is
    destroyed when the first request returns; delete a conversation and it
    comes back from the dead. Both reproduced.

    ladder._upsert and study.save_session already worked this way; chat was the
    one store that clobbered records it had never touched.
    """
    rows = [c for c in load_all(path) if c.id != conversation.id]
    rows.append(conversation)
    return _write(path, rows)


def delete_one(path: Path | str, conv_id: str) -> list[Conversation]:
    return _write(path, [c for c in load_all(path) if c.id != conv_id])


def _write(path: Path | str, conversations: list[Conversation]) -> list[Conversation]:
    trimmed = sorted(conversations, key=lambda c: c.updated_at, reverse=True)[:MAX_CONVERSATIONS]
    atomic_write_json(path, [c.to_dict() for c in trimmed])
    return trimmed


def save_all(path: Path | str, conversations: list[Conversation]) -> None:
    """Whole-store write. Safe only when the caller read the store moments ago
    and nothing slow happened in between — otherwise use save_one/delete_one."""
    _write(path, conversations)


def find(conversations: list[Conversation], conv_id: str) -> Conversation | None:
    return next((c for c in conversations if c.id == conv_id), None)


def new_conversation(now: datetime | None = None) -> Conversation:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    return Conversation(id=stamp.replace(":", "").replace(".", ""), title="New conversation",
                        created_at=stamp, updated_at=stamp)


def title_from(text: str) -> str:
    """First line, trimmed — a conversation list of "New conversation" ×20 is
    useless for finding the one you want."""
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    return (cleaned[:58] + "…") if len(cleaned) > 58 else (cleaned or "New conversation")


# -------------------------------------------------------------- mentions --

def uploads(repo_root: Path) -> list:
    """Index of files shared straight into chat."""
    return materials.load_index(paths.chat_uploads_index(repo_root))


def save_upload(repo_root: Path, filename: str, data: bytes) -> dict:
    """Store a shared file and extract its text. Reuses the materials
    pipeline wholesale — a chat upload is just a document in a different
    directory, so it gets the same PDF extraction, chunking and retrieval."""
    updir = paths.chat_uploads_dir(repo_root)
    updir.mkdir(parents=True, exist_ok=True)

    safe = Path(filename).name  # strip any path components — no traversal
    dest = updir / safe
    stem, suffix, n = dest.stem, dest.suffix, 2
    while dest.exists():  # never silently overwrite a file shared earlier
        dest = updir / f"{stem}-{n}{suffix}"
        n += 1
    dest.write_bytes(data)

    index_path = paths.chat_uploads_index(repo_root)
    entries = materials.reindex(updir, materials.load_index(index_path))
    materials.save_index(index_path, entries)

    entry = next((e for e in entries if e.relpath == dest.name), None)
    return {
        "type": "upload",
        "relpath": dest.name,
        "name": dest.name,
        "detail": "shared file",
        "extracted": bool(entry and entry.extracted),
        "char_count": entry.char_count if entry else 0,
    }


def delete_upload(repo_root: Path, relpath: str) -> bool:
    updir = paths.chat_uploads_dir(repo_root)
    if not materials.delete_material(updir, relpath):
        return False
    index_path = paths.chat_uploads_index(repo_root)
    materials.save_index(index_path, materials.reindex(updir, materials.load_index(index_path)))
    return True


def mentionable(repo_root: Path, classes: list) -> list[dict]:
    """Everything `@` can offer: shared files first (most recently relevant),
    then each class, then each readable document inside it."""
    out = []
    for u in uploads(repo_root):
        if u.extracted:
            out.append({"type": "upload", "relpath": u.relpath, "name": u.filename, "detail": "shared file"})
    for c in classes:
        out.append({"type": "class", "slug": c.slug, "name": c.name, "detail": "class"})
        for m in materials.load_index(paths.materials_index_path(repo_root, c.slug)):
            if m.extracted:
                out.append({
                    "type": "doc", "slug": c.slug, "relpath": m.relpath,
                    "name": m.filename, "detail": c.name,
                })
    return out


def build_context(repo_root: Path, classes: list, mentions: list, query: str) -> str:
    """Resolve mentions into real excerpts.

    For a class mention, the chunks are chosen by relevance to what was
    actually asked — mentioning a class and asking about entropy should pull
    the entropy pages, not the first pages. Falls back to a spread across the
    class when nothing matches, so a mention is never silently worthless.
    """
    by_slug = {c.slug: c for c in classes}
    blocks: list[str] = []
    budget = MAX_CONTEXT_CHARS // max(1, len(mentions or []))

    for mention in mentions or []:
        if mention.get("type") == "upload":
            updir = paths.chat_uploads_dir(repo_root)
            entries = [e for e in uploads(repo_root) if e.relpath == mention.get("relpath")]
            if not entries:
                continue
            chunks = materials.relevant_chunks(updir, entries, query, k=5) or \
                     materials.sample_chunks(updir, entries, max_chunks=5)
            body = materials.build_context(chunks, max_chars=budget)
            label = f"shared file: {entries[0].filename}"
            blocks.append(f"=== {label} ===\n{body}" if body.strip()
                          else f"=== {label} ===\n(No readable text could be extracted from this file.)")
            continue

        slug = mention.get("slug")
        cls = by_slug.get(slug)
        if cls is None:
            continue
        mdir = paths.materials_dir(repo_root, slug)
        entries = materials.load_index(paths.materials_index_path(repo_root, slug))

        if mention.get("type") == "doc":
            entries = [e for e in entries if e.relpath == mention.get("relpath")]
            if not entries:
                continue
            chunks = materials.relevant_chunks(mdir, entries, query, k=4) or \
                     materials.sample_chunks(mdir, entries, max_chunks=4)
            label = f"{cls.name} — {entries[0].filename}"
        else:
            chunks = materials.relevant_chunks(mdir, entries, query, k=6) or \
                     materials.sample_chunks(mdir, entries, max_chunks=4)
            label = cls.name

        body = materials.build_context(chunks, max_chars=budget)
        if body.strip():
            blocks.append(f"=== Material from {label} ===\n{body}")
        else:
            # Still say so — not to stop the model answering (it should), but
            # so it doesn't imply it read notes that aren't there.
            blocks.append(
                f"=== {label} ===\n(Nothing readable is on file for this yet — answer from your "
                "own knowledge of the subject, and don't imply you read their material.)"
            )

    return "\n\n".join(blocks)[:MAX_CONTEXT_CHARS]


# ---------------------------------------------------------------- prompt --

def build_prompt(conversation: Conversation, new_text: str) -> str:
    history = [m for m in conversation.messages if m.content.strip()][-MAX_TURNS_IN_PROMPT:]
    lines = [SYSTEM_PREAMBLE, ""]
    if history:
        lines.append("Conversation so far:")
        for m in history:
            who = "Student" if m.role == "user" else "You"
            lines.append(f"{who}: {m.content.strip()}")
        lines.append("")
    lines.append(f"Student: {new_text.strip()}")
    lines.append("You:")
    return "\n".join(lines)


def send(
    repo_root: Path,
    classes: list,
    conversations: list[Conversation],
    conv_id: str | None,
    text: str,
    mentions: list,
    llm_fn,
    now: datetime | None = None,
) -> tuple[Conversation, list[Conversation]]:
    """Append the owner's turn, call the model, append the reply. The user's
    message is stored BEFORE the call, so a provider failure loses the answer
    but never the question."""
    now = now or datetime.now(timezone.utc)
    conversation = find(conversations, conv_id or "")
    if conversation is None:
        conversation = new_conversation(now)
        conversations.append(conversation)

    if conversation.title == "New conversation":
        conversation.title = title_from(text)

    # A file shared earlier in this conversation stays in context for every
    # later turn — asking "and what about section 3?" must not silently lose
    # the document. Class mentions are NOT sticky: switching classes mid-chat
    # is a deliberate act, and carrying the old one would poison the answer.
    effective = list(mentions or [])
    seen = {(m.get("type"), m.get("relpath"), m.get("slug")) for m in effective}
    for older in conversation.messages:
        for m in (older.mentions or []):
            key = (m.get("type"), m.get("relpath"), m.get("slug"))
            if m.get("type") == "upload" and key not in seen:
                effective.append(m)
                seen.add(key)

    prompt = build_prompt(conversation, text)
    conversation.messages.append(Message(role="user", content=text, at=now.isoformat(), mentions=mentions or []))
    conversation.updated_at = now.isoformat()

    context = build_context(repo_root, classes, effective, text)
    reply = llm_fn(prompt, context)

    conversation.messages.append(Message(role="assistant", content=reply, at=datetime.now(timezone.utc).isoformat()))
    # Keep the tail: the prompt only ever uses the last MAX_TURNS_IN_PROMPT
    # anyway, so older turns are already invisible to the model — they were
    # just being carried, parsed and rewritten forever.
    if len(conversation.messages) > MAX_MESSAGES_PER_CONVERSATION:
        conversation.messages = conversation.messages[-MAX_MESSAGES_PER_CONVERSATION:]
    conversation.updated_at = datetime.now(timezone.utc).isoformat()
    return conversation, conversations
