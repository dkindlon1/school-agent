"""Regression tests for the 2026-08-26 retrieval rewrite.

The bug these exist to prevent from ever coming back: every generation feature
read `text_excerpt`, which was the first 2,000 characters of a document. On a
real 40-page PDF that was 1.3% of the file — the cover page — so quiz
generation produced flashcards about the attendance policy and topic search
matched against a title page.
"""

from school_agent.materials import (
    build_context,
    chunk_text,
    ingest_file,
    load_full_text,
    reindex,
    relevant_chunks,
    sample_chunks,
    scan_materials,
    text_cache_path,
)

# A document whose interesting content is deliberately far past the 2,000-char
# excerpt boundary — exactly the shape that used to be invisible.
FRONT_MATTER = "COURSE.101 Thermodynamics I. Office hours Tuesday. Late policy: 10% per day. " * 40
DEEP_CONTENT = (
    "The second law states that entropy of an isolated system never decreases. "
    "Carnot efficiency is one minus the ratio of cold to hot reservoir temperature. "
)


def _seed(tmp_path, body=None):
    mdir = tmp_path / "materials"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "chapter.txt").write_text(body or (FRONT_MATTER + DEEP_CONTENT), encoding="utf-8")
    return mdir


def test_full_text_is_persisted_not_truncated_to_the_excerpt(tmp_path):
    mdir = _seed(tmp_path)
    entries = scan_materials(mdir)
    e = entries[0]

    assert len(e.text_excerpt) == 2000  # excerpt still capped, for display
    assert e.char_count > 2000  # but the real length is recorded
    full = load_full_text(mdir, e)
    assert len(full) == e.char_count
    assert "Carnot efficiency" in full  # content past the old cutoff survives
    assert text_cache_path(mdir, e.relpath).is_file()


def test_content_past_the_excerpt_boundary_is_retrievable(tmp_path):
    mdir = _seed(tmp_path)
    entries = scan_materials(mdir)

    assert "Carnot efficiency" not in entries[0].text_excerpt  # invisible to v2
    hits = relevant_chunks(mdir, entries, "Carnot efficiency and reservoir temperature")
    assert hits
    assert any("Carnot" in c.text for c in hits)


def test_sample_chunks_spreads_across_the_document_not_just_the_head(tmp_path):
    mdir = _seed(tmp_path)
    entries = scan_materials(mdir)
    context = build_context(sample_chunks(mdir, entries, max_chunks=8))
    assert "entropy of an isolated system" in context  # the tail made it in


def test_sample_chunks_round_robins_across_multiple_documents(tmp_path):
    mdir = tmp_path / "materials"
    mdir.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (mdir / name).write_text(f"{name} content. " * 400, encoding="utf-8")
    entries = scan_materials(mdir)
    chunks = sample_chunks(mdir, entries, max_chunks=3)
    # One from each document before taking a second from any — otherwise a
    # single long file would crowd out every other document.
    assert {c.filename for c in chunks} == {"a.txt", "b.txt", "c.txt"}


def test_relevant_chunks_requires_more_than_one_shared_word_for_multiword_topics(tmp_path):
    mdir = _seed(tmp_path, body="Entropy increases in any spontaneous process; the second law bounds efficiency.")
    entries = scan_materials(mdir)
    assert relevant_chunks(mdir, entries, "Entropy and the Second Law")
    # Shares only "law" — must not count as coverage of a different topic.
    assert relevant_chunks(mdir, entries, "The First Law: Closed Systems") == []


def test_relevant_chunks_returns_nothing_when_topic_is_genuinely_absent(tmp_path):
    mdir = _seed(tmp_path)
    entries = scan_materials(mdir)
    assert relevant_chunks(mdir, entries, "Renaissance portraiture techniques") == []


def test_build_context_respects_its_char_budget(tmp_path):
    mdir = _seed(tmp_path, body="x" * 60_000)
    entries = scan_materials(mdir)
    context = build_context(sample_chunks(mdir, entries, max_chunks=50), max_chars=5_000)
    assert len(context) <= 5_000


def test_chunk_text_overlaps_so_boundary_content_stays_findable(tmp_path):
    chunks = chunk_text("abcdefghij" * 100, size=400, overlap=100)
    assert len(chunks) > 1
    assert chunks[0][-50:] in chunks[1]  # tail of one appears in the next


def test_extracted_cache_dir_is_never_itself_indexed(tmp_path):
    mdir = _seed(tmp_path)
    scan_materials(mdir)  # writes the cache
    entries = scan_materials(mdir)  # second pass must not pick the cache up
    assert [e.filename for e in entries] == ["chapter.txt"]


def test_reindex_reuses_unchanged_entries_and_reextracts_changed_ones(tmp_path):
    mdir = _seed(tmp_path)
    first = scan_materials(mdir)

    calls = {"n": 0}
    import school_agent.materials as m

    real_extract = m.extract_text

    def counting_extract(path, *a, **k):
        calls["n"] += 1
        return real_extract(path, *a, **k)

    m.extract_text = counting_extract
    try:
        again = reindex(mdir, first)
        assert calls["n"] == 0  # nothing changed → nothing re-extracted
        assert [e.filename for e in again] == ["chapter.txt"]

        (mdir / "new.txt").write_text("a brand new document about enthalpy", encoding="utf-8")
        after = reindex(mdir, again)
        assert calls["n"] == 1  # only the new file
        assert {e.filename for e in after} == {"chapter.txt", "new.txt"}
    finally:
        m.extract_text = real_extract


def test_reindex_drops_entries_whose_file_was_removed(tmp_path):
    mdir = _seed(tmp_path)
    (mdir / "temp.txt").write_text("scratch", encoding="utf-8")
    entries = scan_materials(mdir)
    assert len(entries) == 2

    (mdir / "temp.txt").unlink()
    assert [e.filename for e in reindex(mdir, entries)] == ["chapter.txt"]


def test_ingest_records_source_mtime_and_size(tmp_path):
    mdir = _seed(tmp_path)
    entry = ingest_file(mdir, mdir / "chapter.txt")
    assert entry.mtime > 0
    assert entry.size > 0
