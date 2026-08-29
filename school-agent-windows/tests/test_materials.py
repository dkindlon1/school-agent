from school_agent.materials import (
    extract_text,
    load_index,
    save_index,
    save_pasted_text,
    scan_materials,
    search,
)


def test_extract_text_reads_txt_files(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("binary search runs in O(log n) time", encoding="utf-8")
    assert extract_text(p) == "binary search runs in O(log n) time"


def test_extract_text_returns_none_for_unsupported_extension(tmp_path):
    p = tmp_path / "slides.pptx"
    p.write_bytes(b"not real pptx bytes")
    assert extract_text(p) is None


def test_scan_materials_indexes_files_recursively(tmp_path):
    mdir = tmp_path / "materials"
    (mdir / "week1").mkdir(parents=True)
    (mdir / "syllabus.md").write_text("# Syllabus\ncovers big-o notation", encoding="utf-8")
    (mdir / "week1" / "notes.txt").write_text("recursion and big-o", encoding="utf-8")

    entries = scan_materials(mdir)
    assert {e.filename for e in entries} == {"syllabus.md", "notes.txt"}
    assert all(e.extracted for e in entries)


def test_search_matches_filename_or_excerpt(tmp_path):
    mdir = tmp_path / "materials"
    mdir.mkdir()
    (mdir / "recursion_notes.txt").write_text("irrelevant body text", encoding="utf-8")
    (mdir / "unrelated.txt").write_text("this one mentions entropy in the body", encoding="utf-8")
    (mdir / "nothing.txt").write_text("completely different subject matter", encoding="utf-8")
    entries = scan_materials(mdir)

    by_filename = search(entries, "recursion")
    assert {e.filename for e in by_filename} == {"recursion_notes.txt"}

    by_body = search(entries, "entropy")
    assert {e.filename for e in by_body} == {"unrelated.txt"}


def test_search_matches_multiword_topic_labels_not_just_exact_phrases(tmp_path):
    # The v2 bug: search was `topic_string in text`, so a real syllabus label
    # never matched a document body and get-ahead claimed "no material" on a
    # class where the whole chapter was uploaded.
    mdir = tmp_path / "materials"
    mdir.mkdir()
    (mdir / "week5.txt").write_text(
        "Entropy increases in any spontaneous process; the second law bounds efficiency.",
        encoding="utf-8",
    )
    entries = scan_materials(mdir)
    assert search(entries, "Ch. 6 - Entropy and the Second Law")
    assert search(entries, "The First Law: Closed Systems") == []


def test_index_roundtrip_through_json(tmp_path):
    mdir = tmp_path / "materials"
    mdir.mkdir()
    (mdir / "a.txt").write_text("hello", encoding="utf-8")
    entries = scan_materials(mdir)
    idx_path = tmp_path / "materials_index.json"
    save_index(idx_path, entries)
    assert load_index(idx_path) == entries


def test_load_index_missing_file_returns_empty(tmp_path):
    assert load_index(tmp_path / "missing.json") == []


# --- 2026-08-25 fixes: empty-text extraction no longer counts as success ---

def test_empty_text_file_is_not_marked_extracted(tmp_path):
    mdir = tmp_path / "materials"
    mdir.mkdir()
    (mdir / "blank.txt").write_text("   \n  \n", encoding="utf-8")  # whitespace only
    entries = scan_materials(mdir)
    assert entries[0].extracted is False


def test_extract_text_returns_empty_string_not_none_for_pdf_with_no_text_layer(tmp_path, monkeypatch):
    # Simulate a scanned/image-only PDF: pypdf's extract_text() returns "" per page.
    import pypdf

    class FakePage:
        def extract_text(self):
            return ""

    class FakeReader:
        def __init__(self, path):
            self.pages = [FakePage(), FakePage()]

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    p = tmp_path / "scanned.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    assert extract_text(p) == ""  # extraction "succeeded" but found nothing


def test_scanned_pdf_with_no_text_is_indexed_but_not_marked_extracted(tmp_path, monkeypatch):
    import pypdf

    class FakePage:
        def extract_text(self):
            return ""

    class FakeReader:
        def __init__(self, path):
            self.pages = [FakePage()]

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    mdir = tmp_path / "materials"
    mdir.mkdir()
    (mdir / "scanned.pdf").write_bytes(b"%PDF-1.4 fake")
    entries = scan_materials(mdir)
    assert entries[0].extracted is False
    assert entries[0].text_excerpt == ""


def test_search_excludes_entries_with_no_usable_text(tmp_path, monkeypatch):
    # A scanned PDF whose filename happens to match must not be treated as
    # "relevant material found" downstream (the getahead.py bug this closes).
    import pypdf

    class FakePage:
        def extract_text(self):
            return ""

    class FakeReader:
        def __init__(self, path):
            self.pages = [FakePage()]

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    mdir = tmp_path / "materials"
    mdir.mkdir()
    (mdir / "recursion_scan.pdf").write_bytes(b"%PDF-1.4 fake")
    entries = scan_materials(mdir)
    assert search(entries, "recursion") == []


def test_extract_text_skips_files_over_size_cap(tmp_path, monkeypatch):
    import pathlib

    import school_agent.materials as materials_mod

    p = tmp_path / "huge.txt"
    p.write_text("x", encoding="utf-8")

    class FakeStatResult:
        st_size = materials_mod.MAX_FILE_BYTES + 1

    monkeypatch.setattr(pathlib.Path, "stat", lambda self: FakeStatResult())
    assert extract_text(p) is None


def test_load_index_survives_corrupt_json(tmp_path):
    p = tmp_path / "materials_index.json"
    p.write_text("not json", encoding="utf-8")
    assert load_index(p) == []
    assert not p.exists()


# --- 2026-08-25: paste-text-directly, added alongside file upload ---

def test_save_pasted_text_writes_txt_file_named_from_title(tmp_path):
    mdir = tmp_path / "materials"
    dest = save_pasted_text(mdir, "Week 3 lecture notes", "recursion and big-o")
    assert dest.name == "week-3-lecture-notes.txt"
    assert dest.read_text(encoding="utf-8") == "recursion and big-o"


def test_save_pasted_text_defaults_filename_when_no_title(tmp_path):
    mdir = tmp_path / "materials"
    dest = save_pasted_text(mdir, "", "some content")
    assert dest.name == "note.txt"


def test_save_pasted_text_disambiguates_on_collision(tmp_path):
    mdir = tmp_path / "materials"
    first = save_pasted_text(mdir, "notes", "first paste")
    second = save_pasted_text(mdir, "notes", "second paste")
    assert first.name == "notes.txt"
    assert second.name == "notes-2.txt"
    assert first.read_text(encoding="utf-8") == "first paste"
    assert second.read_text(encoding="utf-8") == "second paste"


def test_pasted_text_is_indexed_and_searchable_like_an_uploaded_file(tmp_path):
    mdir = tmp_path / "materials"
    save_pasted_text(mdir, "Syllabus highlights", "the midterm covers dynamic programming")
    entries = scan_materials(mdir)
    assert len(entries) == 1
    assert entries[0].extracted is True
    assert search(entries, "dynamic programming")
