from datetime import date

from school_agent.config import ClassConfig
from school_agent.getahead import summarize_topic, upcoming_topics
from school_agent.materials import MaterialEntry


def make_class(topics):
    return ClassConfig(slug="cs401", name="CS 401", topics=topics)


def test_upcoming_topics_within_lookahead_window():
    cls = make_class(
        [
            ["2026-09-01", "Past topic"],
            ["2026-09-05", "This week"],
            ["2026-10-01", "Far future"],
        ]
    )
    result = upcoming_topics(cls, today=date(2026, 9, 3), lookahead_days=7)
    assert result == [(date(2026, 9, 5), "This week")]


def test_upcoming_topics_empty_when_nothing_in_window():
    cls = make_class([["2026-12-01", "Way later"]])
    assert upcoming_topics(cls, today=date(2026, 9, 3), lookahead_days=7) == []


def test_summarize_topic_flags_missing_material_honestly():
    out = summarize_topic("quantum computing", materials=[], llm_fn=None)
    assert "No material on file covers" in out


def test_summarize_topic_uses_llm_fn_when_material_and_fn_present():
    materials = [MaterialEntry(filename="notes.txt", relpath="notes.txt", text_excerpt="recursion basics", extracted=True)]

    def fake_llm(prompt: str, context: str) -> str:
        assert "recursion basics" in context
        return "Recursion is a function calling itself."

    out = summarize_topic("recursion", materials, fake_llm)
    assert out == "Recursion is a function calling itself."


def test_summarize_topic_returns_raw_excerpts_when_no_llm_configured():
    materials = [MaterialEntry(filename="notes.txt", relpath="notes.txt", text_excerpt="recursion basics", extracted=True)]
    out = summarize_topic("recursion", materials, llm_fn=None)
    assert "no model configured yet" in out
    assert "recursion basics" in out
