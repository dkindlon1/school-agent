from school_agent.draft import DRAFT_HEADER, generate_draft, save_draft, wrap_draft


def test_wrap_draft_adds_header():
    out = wrap_draft("My essay body.")
    assert out.startswith(DRAFT_HEADER)
    assert "My essay body." in out


def test_wrap_draft_is_idempotent():
    once = wrap_draft("body")
    twice = wrap_draft(once)
    assert once == twice
    assert twice.count("DRAFT —") == 1


def test_generate_draft_always_tags_llm_output():
    def fake_llm(prompt: str, context: str) -> str:
        return "Untagged model output that never mentions being a draft."

    out = generate_draft("Write my essay", "some context", fake_llm)
    assert out.startswith(DRAFT_HEADER)


def test_save_draft_writes_tagged_file(tmp_path):
    out_path = save_draft(tmp_path / "drafts", "essay-1", "content here")
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith(DRAFT_HEADER)
