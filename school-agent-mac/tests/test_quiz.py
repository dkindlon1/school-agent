from datetime import datetime, timedelta, timezone

import pytest
from school_agent.quiz import CardStore


def test_add_and_persist_card_roundtrip(tmp_path):
    store_path = tmp_path / "cards.json"
    store = CardStore(store_path)
    card = store.add_card("cs401", "What is Big-O of binary search?", "O(log n)")
    store.save()

    reloaded = CardStore(store_path)
    cards = reloaded.all_cards("cs401")
    assert len(cards) == 1
    assert cards[0].card_id == card.card_id
    assert cards[0].question == "What is Big-O of binary search?"


def test_delete_card_removes_it_from_the_deck(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    keep = store.add_card("cs401", "What is entropy?", "A measure of disorder")
    bad = store.add_card("cs401", "Hallucinated nonsense?", "Wrong answer")
    store.delete_card(bad.card_id)
    assert [c.card_id for c in store.all_cards("cs401")] == [keep.card_id]

    store.save()
    assert [c.question for c in CardStore(tmp_path / "cards.json").all_cards()] == ["What is entropy?"]


def test_delete_unknown_card_raises(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    with pytest.raises(KeyError):
        store.delete_card("no-such-card")


def test_edit_card_keeps_its_fsrs_schedule(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    qc = store.add_card("cs401", "Whats entropy", "disorder")
    store.review(qc.card_id, "good")
    scheduled = qc.fsrs_state["due"]

    edited = store.edit_card(qc.card_id, question="What is entropy?", answer="A measure of disorder")
    assert edited.question == "What is entropy?"
    assert edited.answer == "A measure of disorder"
    assert edited.fsrs_state["due"] == scheduled  # a wording fix must not reset learning


def test_edit_card_ignores_blank_fields(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    qc = store.add_card("cs401", "Original question", "Original answer")
    store.edit_card(qc.card_id, question="   ", answer=None)
    assert qc.question == "Original question"
    assert qc.answer == "Original answer"


def test_has_question_normalizes_whitespace_case_and_punctuation(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    store.add_card("cs401", "What is the Second Law?", "Entropy never decreases")
    assert store.has_question("what is the   second law") is True
    assert store.has_question("What is the first law?") is False


def test_has_question_is_scoped_per_class(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    store.add_card("cs401", "What is entropy?", "Disorder")
    assert store.has_question("What is entropy?", class_slug="cs401") is True
    assert store.has_question("What is entropy?", class_slug="math219") is False


def test_new_card_is_immediately_due(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    card = store.add_card("cs401", "Q", "A")
    due = store.due_cards(now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert card.card_id in {c.card_id for c in due}


def test_review_pushes_due_date_into_the_future(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    card = store.add_card("cs401", "Q", "A")
    now = datetime.now(timezone.utc)
    updated = store.review(card.card_id, "good", now=now)
    new_due = datetime.fromisoformat(updated.fsrs_state["due"])
    assert new_due > now


def test_review_again_schedules_sooner_than_review_easy(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    c_again = store.add_card("cs401", "Q1", "A1")
    c_easy = store.add_card("cs401", "Q2", "A2")
    now = datetime.now(timezone.utc)
    again = store.review(c_again.card_id, "again", now=now)
    easy = store.review(c_easy.card_id, "easy", now=now)
    due_again = datetime.fromisoformat(again.fsrs_state["due"])
    due_easy = datetime.fromisoformat(easy.fsrs_state["due"])
    assert due_again <= due_easy


def test_review_rejects_unknown_rating(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    card = store.add_card("cs401", "Q", "A")
    with pytest.raises(ValueError):
        store.review(card.card_id, "amazing")


def test_review_rejects_unknown_card_id(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    with pytest.raises(KeyError):
        store.review("nonexistent", "good")


def test_due_cards_filters_by_class(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    store.add_card("cs401", "Q1", "A1")
    store.add_card("math201", "Q2", "A2")
    future = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert len(store.due_cards(now=future, class_slug="cs401")) == 1
    assert len(store.due_cards(now=future, class_slug="math201")) == 1
    assert len(store.due_cards(now=future)) == 2


# --- 2026-08-25 fixes: crash-safe storage, one bad card can't hide the rest ---

def test_due_cards_skips_card_with_missing_due_key_keeps_others(tmp_path):
    store = CardStore(tmp_path / "cards.json")
    good = store.add_card("cs401", "Good Q", "Good A")
    bad = store.add_card("cs401", "Bad Q", "Bad A")
    del bad.fsrs_state["due"]  # simulate a corrupted/partial record

    future = datetime.now(timezone.utc) + timedelta(seconds=1)
    due = store.due_cards(now=future)

    assert good.card_id in {c.card_id for c in due}
    assert bad.card_id not in {c.card_id for c in due}


def test_store_survives_corrupt_cards_json(tmp_path):
    p = tmp_path / "cards.json"
    p.write_text("{totally broken", encoding="utf-8")

    store = CardStore(p)  # must not raise
    assert store.all_cards() == []
    assert not p.exists()  # quarantined


def test_store_skips_malformed_card_entry_keeps_the_rest(tmp_path):
    p = tmp_path / "cards.json"
    p.write_text(
        '[{"card_id": "1", "class_slug": "cs401", "question": "Q", "answer": "A", '
        '"fsrs_state": {"due": "2020-01-01T00:00:00+00:00"}}, {"card_id": "2", "not_a_real_field": true}]',
        encoding="utf-8",
    )
    store = CardStore(p)
    assert [c.card_id for c in store.all_cards()] == ["1"]


def test_card_store_save_is_atomic(tmp_path):
    p = tmp_path / "cards.json"
    store = CardStore(p)
    store.add_card("cs401", "Q", "A")
    store.save()
    leftovers = [f for f in tmp_path.iterdir() if f.name != "cards.json"]
    assert leftovers == []
