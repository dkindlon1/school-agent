"""Chat with @-mentions of real classes and documents."""

import pytest

from school_agent import chat, materials, paths
from school_agent.config import ClassConfig


def _seed(tmp_path, slug="mece-110"):
    c = ClassConfig(slug=slug, name="COURSE.101 — Thermodynamics I")
    paths.ensure_class_dirs(tmp_path, slug)
    mdir = paths.materials_dir(tmp_path, slug)
    materials.save_pasted_text(mdir, "chapter 4", "Front matter. " * 300 +
                               "Entropy generation is zero only for a reversible process. ")
    materials.save_pasted_text(mdir, "syllabus", "Office hours Tuesday. Grading: homework 20%.")
    idx = paths.materials_index_path(tmp_path, slug)
    materials.save_index(idx, materials.reindex(mdir, materials.load_index(idx)))
    return c


def test_mentionable_lists_classes_and_readable_documents(tmp_path):
    c = _seed(tmp_path)
    items = chat.mentionable(tmp_path, [c])
    assert {"type": "class", "slug": c.slug, "name": c.name, "detail": "class"} in items
    assert {i["name"] for i in items if i["type"] == "doc"} == {"chapter-4.txt", "syllabus.txt"}


def test_class_mention_retrieves_material_relevant_to_the_question(tmp_path):
    c = _seed(tmp_path)
    ctx = chat.build_context(tmp_path, [c], [{"type": "class", "slug": c.slug, "name": c.name}],
                             "explain entropy generation")
    assert "Entropy generation" in ctx  # deep content, not the front matter
    assert c.name in ctx


def test_doc_mention_scopes_context_to_that_one_file(tmp_path):
    c = _seed(tmp_path)
    ctx = chat.build_context(tmp_path, [c],
                             [{"type": "doc", "slug": c.slug, "relpath": "syllabus.txt", "name": "syllabus.txt"}],
                             "what is the grading")
    assert "homework 20%" in ctx
    assert "Entropy generation" not in ctx  # the other document stayed out


def test_mention_with_no_readable_material_still_asks_for_a_real_answer(tmp_path):
    """The point of naming the gap is so the model doesn't imply it read notes
    that aren't there — NOT so it refuses to answer. Asking "what's a vector"
    with an empty class should still get a real explanation."""
    c = ClassConfig(slug="empty", name="Empty Class")
    paths.ensure_class_dirs(tmp_path, "empty")
    ctx = chat.build_context(tmp_path, [c], [{"type": "class", "slug": "empty", "name": c.name}], "anything")
    assert "Nothing readable is on file" in ctx
    assert "answer from your own knowledge" in ctx
    assert "don't imply you read their material" in ctx


def test_the_preamble_never_tells_the_model_to_withhold_an_explanation():
    """The regression this guards: "say plainly when it does not cover
    something rather than filling the gap with general knowledge" turned a
    textbook question into a refusal."""
    assert "Never refuse or hedge" in chat.SYSTEM_PREAMBLE
    assert "rather than filling the gap with general knowledge" not in chat.SYSTEM_PREAMBLE
    # ...while the honesty requirement that motivated it survives intact.
    assert "not present your own knowledge as something their course said" in chat.SYSTEM_PREAMBLE


def test_unknown_mention_is_ignored_not_fatal(tmp_path):
    assert chat.build_context(tmp_path, [], [{"type": "class", "slug": "ghost", "name": "Ghost"}], "hi") == ""


def test_send_records_both_turns_and_titles_the_conversation(tmp_path):
    c = _seed(tmp_path)
    convo, convos = chat.send(
        tmp_path, [c], [], None, "Explain entropy generation to me",
        [{"type": "class", "slug": c.slug, "name": c.name}],
        lambda prompt, context: "Entropy generation measures irreversibility.",
    )
    assert [m.role for m in convo.messages] == ["user", "assistant"]
    assert convo.title == "Explain entropy generation to me"
    assert convo.messages[0].mentions[0]["slug"] == c.slug
    assert len(convos) == 1


def test_the_model_sees_the_conversation_history(tmp_path):
    c = _seed(tmp_path)
    seen = {}

    def fake_llm(prompt, context):
        seen["prompt"] = prompt
        return "reply"

    convo, convos = chat.send(tmp_path, [c], [], None, "First question", [], fake_llm)
    chat.send(tmp_path, [c], convos, convo.id, "And a follow-up", [], fake_llm)
    assert "First question" in seen["prompt"]
    assert "And a follow-up" in seen["prompt"]


def test_a_failed_reply_still_keeps_the_question(tmp_path):
    c = _seed(tmp_path)

    def broken(prompt, context):
        raise RuntimeError("provider down")

    convos = []
    with pytest.raises(RuntimeError):
        chat.send(tmp_path, [c], convos, None, "my question", [], broken)
    # The conversation exists with the user's turn recorded.
    assert convos[0].messages[0].content == "my question"


def test_history_sent_to_the_model_is_bounded(tmp_path):
    convo = chat.new_conversation()
    for i in range(40):
        convo.messages.append(chat.Message(role="user", content=f"question {i}"))
    prompt = chat.build_prompt(convo, "latest")
    assert "question 39" in prompt
    assert "question 0" not in prompt  # older turns fall out of the window


def test_conversations_roundtrip_and_are_capped(tmp_path):
    p = tmp_path / "chats.json"
    convos = []
    for i in range(chat.MAX_CONVERSATIONS + 10):
        c = chat.new_conversation()
        c.id, c.title, c.updated_at = f"c{i:03d}", f"conversation {i}", f"2026-08-{(i % 28) + 1:02d}T00:00:00+00:00"
        convos.append(c)
    chat.save_all(p, convos)
    assert len(chat.load_all(p)) == chat.MAX_CONVERSATIONS


def test_corrupt_chat_file_does_not_crash(tmp_path):
    p = tmp_path / "chats.json"
    p.write_text("not json", encoding="utf-8")
    assert chat.load_all(p) == []


# --- 2026-08-26: files shared directly into a conversation ---

def test_upload_is_stored_extracted_and_offered_as_a_mention(tmp_path):
    entry = chat.save_upload(tmp_path, "problem-set.txt", b"Determine the reaction forces at pin A.")
    assert entry["type"] == "upload"
    assert entry["extracted"] is True
    assert entry["char_count"] > 0

    names = [m["name"] for m in chat.mentionable(tmp_path, []) if m["type"] == "upload"]
    assert "problem-set.txt" in names


def test_upload_context_is_retrieved_for_the_question(tmp_path):
    chat.save_upload(tmp_path, "notes.txt", b"Front matter. " * 300 + b"Shear force diagrams start at the left support.")
    ctx = chat.build_context(tmp_path, [], [{"type": "upload", "relpath": "notes.txt", "name": "notes.txt"}],
                             "how do shear force diagrams work")
    assert "Shear force diagrams" in ctx  # found deep in the file, not just the head
    assert "shared file" in ctx


def test_upload_never_overwrites_a_file_shared_earlier(tmp_path):
    first = chat.save_upload(tmp_path, "hw.txt", b"first version")
    second = chat.save_upload(tmp_path, "hw.txt", b"second version")
    assert first["relpath"] == "hw.txt"
    assert second["relpath"] == "hw-2.txt"
    assert len([u for u in chat.uploads(tmp_path)]) == 2


def test_upload_filename_cannot_escape_the_uploads_directory(tmp_path):
    entry = chat.save_upload(tmp_path, "../../escaped.txt", b"content")
    assert "/" not in entry["relpath"] and "\\" not in entry["relpath"]
    assert not (tmp_path / "escaped.txt").exists()
    assert (paths.chat_uploads_dir(tmp_path) / entry["relpath"]).is_file()


def test_unreadable_upload_is_reported_not_silently_useless(tmp_path):
    entry = chat.save_upload(tmp_path, "scan.png", b"\x89PNG\r\n\x1a\n binary")
    assert entry["extracted"] is False  # caller warns the owner


def test_a_shared_file_stays_in_context_for_later_turns(tmp_path):
    chat.save_upload(tmp_path, "paper.txt", b"The measured yield strength was 250 MPa.")
    attachment = {"type": "upload", "relpath": "paper.txt", "name": "paper.txt"}
    seen = []

    def fake_llm(prompt, context):
        seen.append(context)
        return "ok"

    convo, convos = chat.send(tmp_path, [], [], None, "summarize this", [attachment], fake_llm)
    # Follow-up sends NO mentions — the file must still be there.
    chat.send(tmp_path, [], convos, convo.id, "what was the yield strength?", [], fake_llm)
    assert "250 MPa" in seen[1]


def test_a_class_mention_does_not_stick_to_later_turns(tmp_path):
    c = _seed(tmp_path)
    seen = []

    def fake_llm(prompt, context):
        seen.append(context)
        return "ok"

    convo, convos = chat.send(tmp_path, [c], [], None, "entropy?",
                              [{"type": "class", "slug": c.slug, "name": c.name}], fake_llm)
    chat.send(tmp_path, [c], convos, convo.id, "unrelated follow-up", [], fake_llm)
    assert seen[0] != ""      # first turn had the class material
    assert seen[1] == ""      # switching topics does not drag it along


def test_deleting_an_upload_removes_it_from_mentions(tmp_path):
    chat.save_upload(tmp_path, "temp.txt", b"some content here")
    assert chat.delete_upload(tmp_path, "temp.txt") is True
    assert [m for m in chat.mentionable(tmp_path, []) if m["type"] == "upload"] == []
    assert chat.delete_upload(tmp_path, "temp.txt") is False
