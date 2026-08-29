"""Ingests uploaded course material (syllabi, readings, slides, notes) into a
per-class index, and retrieves the parts of it that are actually relevant.

**The 2026-08-26 rewrite — why this file changed shape.** v2 stored only the
first 2,000 characters of every document (`text_excerpt`) and that excerpt was
the ONLY text any feature ever saw: quiz generation, get-ahead summaries, and
drafting all read it. Measured against a real 40-page PDF that was 1.3% of the
document — the cover page. So "generate questions from my thermo chapter"
produced flashcards about the attendance policy, and topic search matched
against a title page. There was never a context-window problem; the app was
starving the model.

Now: the full extracted text is persisted per file under `_extracted/`,
`text_excerpt` is demoted to a UI preview, and callers ask for *chunks* —
either the ones matching a topic (`relevant_chunks`) or a spread across the
whole library (`sample_chunks`). Retrieval is token-overlap scored rather than
whole-phrase substring, because real syllabus topics ("Ch. 6 — Entropy and the
Second Law") essentially never appear verbatim inside a document.

Text extraction supports .txt/.md directly and .pdf via pypdf. Anything else
(scanned images, .pptx, .docx) is indexed by filename only for now — OCR/doc
extraction is a natural future addition.

**Scanned-PDF fix (2026-08-25):** a scanned/image-only PDF has no embedded
text layer, so pypdf's extract_text() returns "" per page — not None. v1
treated "" as a successful extraction, so a scanned syllabus got indexed as
"extracted=True" with an empty excerpt and could masquerade as relevant
material. `extracted` now means "we got usable, non-empty text."
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import slugify
from .storage import atomic_write_json, atomic_write_text, load_json_self_healing, safe_map

TEXT_EXTENSIONS = {".txt", ".md"}
PDF_EXTENSIONS = {".pdf"}

# Cap how much of a huge file we ever read into memory. Raised from 20k on
# 2026-08-26: 20k chars is ~5 pages, which silently cut most textbook chapters
# in half. 400k is roughly a 200-page PDF and still only ~100k tokens, and we
# never send the whole thing to a model anyway — only selected chunks.
MAX_EXTRACT_CHARS = 400_000
MAX_FILE_BYTES = 60 * 1024 * 1024

EXCERPT_CHARS = 2_000  # UI preview only — NOT what gets sent to a model
CHUNK_CHARS = 1_600
CHUNK_OVERLAP = 200

# Cache directory for extracted text, kept inside the class's materials dir so
# it travels with the data. Skipped by scan_materials so it never self-indexes.
EXTRACTED_DIRNAME = "_extracted"

_STOPWORDS = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "at", "by",
    "from", "is", "are", "was", "were", "be", "been", "it", "its", "as", "or",
    "this", "that", "these", "those", "ch", "chapter", "week", "part", "intro",
    "introduction", "section", "lecture", "unit", "topic", "notes", "review",
}


@dataclass
class MaterialEntry:
    filename: str
    relpath: str
    text_excerpt: str  # first EXCERPT_CHARS — for display/preview ONLY
    extracted: bool  # whether we pulled USABLE (non-empty) text out
    char_count: int = 0  # length of the full extracted text, 0 when none
    mtime: float = 0.0  # source-file mtime, for incremental reindexing
    size: int = 0  # source-file size, same purpose

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "MaterialEntry":
        # Tolerate indexes written by older versions, which lack the newer
        # fields — they simply reindex on the next upload.
        return MaterialEntry(
            filename=d["filename"],
            relpath=d["relpath"],
            text_excerpt=d.get("text_excerpt", ""),
            extracted=d.get("extracted", False),
            char_count=d.get("char_count", 0),
            mtime=d.get("mtime", 0.0),
            size=d.get("size", 0),
        )


# ------------------------------------------------------------- extraction --

def extract_text(path: Path, max_chars: int = MAX_EXTRACT_CHARS) -> str | None:
    """Returns None when extraction isn't supported or the file is too large;
    returns "" (not None) when extraction ran but found no text — e.g. a
    scanned PDF. Callers must not treat "" as success."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    if suffix in PDF_EXTENSIONS:
        try:
            from pypdf import PdfReader
        except ImportError:
            return None
        try:
            reader = PdfReader(str(path))
        except Exception:  # noqa: BLE001 - a malformed/encrypted PDF shouldn't crash indexing
            return None
        chunks = []
        total = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one bad page shouldn't lose the whole document
                text = ""
            chunks.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "".join(chunks)[:max_chars]
    return None


def extracted_dir(materials_dir: Path) -> Path:
    return Path(materials_dir) / EXTRACTED_DIRNAME


def text_cache_path(materials_dir: Path, relpath: str) -> Path:
    """One .txt per source file, name-flattened so nested paths can't collide
    or escape the cache directory."""
    flat = re.sub(r"[^A-Za-z0-9._-]+", "_", str(relpath))
    return extracted_dir(materials_dir) / f"{flat}.txt"


def load_full_text(materials_dir: Path, entry: MaterialEntry) -> str:
    """The full extracted text for one entry, or "" when there is none.
    Falls back to the stored excerpt for indexes written before this cache
    existed, so an old index degrades to v2 behavior instead of breaking."""
    p = text_cache_path(materials_dir, entry.relpath)
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        pass
    return entry.text_excerpt


def ingest_file(materials_dir: Path, file_path: Path) -> MaterialEntry:
    """file_path must already live under materials_dir. Writes the full
    extracted text to the cache and returns the index entry."""
    materials_dir = Path(materials_dir)
    relpath = str(file_path.relative_to(materials_dir))
    text = extract_text(file_path)
    has_usable_text = bool(text and text.strip())

    if has_usable_text:
        atomic_write_text(text_cache_path(materials_dir, relpath), text)

    try:
        stat = file_path.stat()
        mtime, size = stat.st_mtime, stat.st_size
    except OSError:
        mtime, size = 0.0, 0

    return MaterialEntry(
        filename=file_path.name,
        relpath=relpath,
        text_excerpt=(text or "")[:EXCERPT_CHARS],
        extracted=has_usable_text,
        char_count=len(text) if text else 0,
        mtime=mtime,
        size=size,
    )


def save_pasted_text(materials_dir: Path, title: str, text: str) -> Path:
    """Lets the owner paste content directly — a syllabus blurb, lecture
    notes, a reading — the same way they'd paste it into a chat, instead of
    first saving it as a file. Written as a plain .txt so it flows through the
    exact same ingest path as anything dragged onto the dropzone."""
    materials_dir = Path(materials_dir)
    materials_dir.mkdir(parents=True, exist_ok=True)
    base = slugify(title) if title and title.strip() else "note"
    existing = {p.stem for p in materials_dir.glob("*.txt")}
    name, n = base, 2
    while name in existing:
        name = f"{base}-{n}"
        n += 1
    dest = materials_dir / f"{name}.txt"
    atomic_write_text(dest, text)
    return dest


def _source_files(materials_dir: Path) -> list[Path]:
    cache = extracted_dir(materials_dir)
    out = []
    for p in sorted(materials_dir.rglob("*")):
        if not p.is_file():
            continue
        if cache in p.parents:  # never index our own text cache
            continue
        out.append(p)
    return out


def scan_materials(materials_dir: Path) -> list[MaterialEntry]:
    """Full reindex — re-extracts every file. Prefer reindex() on the hot
    path; this stays for a deliberate 'rebuild everything' action."""
    materials_dir = Path(materials_dir)
    if not materials_dir.exists():
        return []
    return [ingest_file(materials_dir, p) for p in _source_files(materials_dir)]


def reindex(materials_dir: Path, existing: list[MaterialEntry]) -> list[MaterialEntry]:
    """Incremental: re-extract only files that are new or changed (mtime+size),
    reuse the existing entry otherwise, and drop entries whose file is gone.

    Fixes a real scaling problem — every upload used to re-parse the entire
    class library synchronously inside the request, so filing one lecture PDF
    in week 10 meant re-parsing all 40, getting slower every week and training
    the owner out of filing material at all."""
    materials_dir = Path(materials_dir)
    if not materials_dir.exists():
        return []
    by_relpath = {e.relpath: e for e in existing}
    out = []
    for p in _source_files(materials_dir):
        relpath = str(p.relative_to(materials_dir))
        prior = by_relpath.get(relpath)
        try:
            stat = p.stat()
            unchanged = prior is not None and prior.mtime == stat.st_mtime and prior.size == stat.st_size
        except OSError:
            unchanged = False
        if unchanged and text_cache_path(materials_dir, relpath).is_file():
            out.append(prior)
        else:
            out.append(ingest_file(materials_dir, p))
    return out


def delete_material(materials_dir: Path, relpath: str) -> bool:
    """Delete one ingested file (and its cached text) by index relpath.
    Returns False rather than raising when the file is missing or the path
    escapes the materials dir — a stale delete click must not crash, and a
    crafted path must never reach outside this class's own folder."""
    materials_dir = Path(materials_dir)
    try:
        target = (materials_dir / relpath).resolve()
        mdir = materials_dir.resolve()
        if not target.is_relative_to(mdir):
            return False
        if not target.is_file():
            return False
        target.unlink()
    except OSError:
        return False
    try:
        cache = text_cache_path(materials_dir, relpath)
        if cache.is_file():
            cache.unlink()
    except OSError:
        pass  # orphaned cache file is harmless; the index no longer references it
    return True


def load_index(path: Path | str) -> list[MaterialEntry]:
    raw = load_json_self_healing(path, default=[])
    return safe_map(raw, MaterialEntry.from_dict, on_item_name=lambda d: d.get("filename", "?"))


def save_index(path: Path | str, entries: list[MaterialEntry]) -> None:
    atomic_write_json(path, [e.to_dict() for e in entries])


# -------------------------------------------------------------- retrieval --

def _tokenize(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _min_overlap(query_tokens: set[str]) -> int:
    """A multi-word topic needs more than one shared word to count as a match.
    Otherwise "The First Law: Closed Systems" matches any document containing
    the word "law" — including one that's entirely about the second law. A
    single-word query has nothing else to go on, so one hit stands."""
    return 1 if len(query_tokens) <= 1 else 2


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fixed-size overlapping windows. Overlap matters: a definition split
    across a boundary would otherwise be unfindable from either side."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text), step) if text[i : i + size].strip()]


@dataclass
class Chunk:
    filename: str
    text: str
    score: int = 0


def _all_chunks(materials_dir: Path, entries: list[MaterialEntry]) -> list[Chunk]:
    out: list[Chunk] = []
    for e in entries:
        if not e.extracted:
            continue
        for piece in chunk_text(load_full_text(materials_dir, e)):
            out.append(Chunk(filename=e.filename, text=piece))
    return out


def relevant_chunks(
    materials_dir: Path,
    entries: list[MaterialEntry],
    query: str,
    k: int = 6,
) -> list[Chunk]:
    """Top-k chunks by shared content-word count with the query. Replaces
    whole-phrase substring matching, which could not match a real syllabus
    topic label against a document body."""
    q = _tokenize(query)
    if not q:
        return []
    threshold = _min_overlap(q)
    scored = []
    for c in _all_chunks(materials_dir, entries):
        overlap = len(q & _tokenize(c.text))
        if overlap >= threshold:
            c.score = overlap
            scored.append(c)
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:k]


def _bisecting_order(n: int) -> list[int]:
    """The indices 0..n-1, reordered so the FIRST one is the middle of the
    document, then the quarter points, then the eighths.

    This is the whole fix for the 2026-08-26 cover-page regression. The old
    sampler round-robined position 0 of every document, then position 1, and
    so on — which is fine with three documents and useless with eight,
    because once you have as many documents as chunks you can afford, the
    loop never advances past position 0 and every chunk you send the model is
    a title page. Measured on a real library: eight documents in, 100% of
    sampled chunks were page one, covering 1.1% of the material.

    Ordering each document's chunks middle-outward instead makes the first
    pick from every document a substantive one, and still degrades sensibly
    when there is budget for several chunks per document."""
    order: list[int] = []
    queue: list[tuple[int, int]] = [(0, n)]
    while queue:
        lo, hi = queue.pop(0)
        if lo >= hi:
            continue
        mid = (lo + hi) // 2
        order.append(mid)
        queue.append((lo, mid))
        queue.append((mid + 1, hi))
    return order


def _spread_indices(n: int, take: int) -> list[int]:
    """`take` evenly spread positions across n items (used to pick WHICH
    documents to sample when there are more documents than chunk budget —
    so a 30-document class contributes early, middle and late material
    rather than whichever thirty percent happens to sort first)."""
    if take >= n:
        return list(range(n))
    if take <= 0:
        return []
    return sorted({min(n - 1, int((i + 0.5) * n / take)) for i in range(take)})


def sample_chunks(materials_dir: Path, entries: list[MaterialEntry], max_chunks: int = 8) -> list[Chunk]:
    """A spread across the whole library rather than the first N chars of it —
    round-robins across documents, taking the middle of each before taking
    any document's opening page. See _bisecting_order."""
    per_doc: list[list[Chunk]] = []
    for e in entries:
        if not e.extracted:
            continue
        pieces = chunk_text(load_full_text(materials_dir, e))
        if pieces:
            per_doc.append([Chunk(filename=e.filename, text=p) for p in pieces])
    if not per_doc:
        return []
    if len(per_doc) > max_chunks:
        per_doc = [per_doc[i] for i in _spread_indices(len(per_doc), max_chunks)]

    ordered = [[d[i] for i in _bisecting_order(len(d))] for d in per_doc]
    out: list[Chunk] = []
    i = 0
    while len(out) < max_chunks and any(len(d) > i for d in ordered):
        for d in ordered:
            if len(d) > i:
                out.append(d[i])
                if len(out) >= max_chunks:
                    break
        i += 1
    return out


def build_context(chunks: list[Chunk], max_chars: int = 24_000) -> str:
    """Render chunks for a prompt, labelled by source file and bounded so a
    huge library can't blow past a local model's context window."""
    parts, total = [], 0
    for c in chunks:
        piece = f"[{c.filename}]\n{c.text.strip()}"
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n---\n\n".join(parts)


def search(entries: list[MaterialEntry], keyword: str) -> list[MaterialEntry]:
    """Token-overlap search over filename + excerpt, kept for callers that
    want whole entries rather than chunks. Entries with no usable text are
    excluded so an unreadable file can never masquerade as relevant."""
    q = _tokenize(keyword)
    if not q:
        return []
    threshold = _min_overlap(q)
    out = []
    for e in entries:
        if not e.extracted:
            continue
        haystack = _tokenize(f"{e.filename} {e.text_excerpt}")
        if len(q & haystack) >= threshold:
            out.append(e)
    return out
