from school_agent.materials import MaterialEntry
from school_agent.quiz import _parse_qa_pairs, generate_cards_from_materials


def test_parse_qa_pairs_extracts_well_formed_pairs():
    raw = "Q: What is Big-O of binary search?\nA: O(log n)\nQ: What is a stack?\nA: LIFO data structure"
    pairs = _parse_qa_pairs(raw, max_cards=10)
    assert pairs == [
        ("What is Big-O of binary search?", "O(log n)"),
        ("What is a stack?", "LIFO data structure"),
    ]


def test_parse_qa_pairs_respects_max_cards():
    raw = "\n".join(f"Q: q{i}\nA: a{i}" for i in range(5))
    assert len(_parse_qa_pairs(raw, max_cards=2)) == 2


def test_parse_qa_pairs_ignores_malformed_lines():
    raw = "Some preamble the model added.\nQ: real question\nA: real answer\nRandom trailing text"
    assert _parse_qa_pairs(raw, max_cards=10) == [("real question", "real answer")]


def test_generate_cards_from_materials_returns_empty_with_no_materials():
    assert generate_cards_from_materials("cs401", [], llm_fn=lambda p, c: "Q: x\nA: y") == []


def test_generate_cards_from_materials_skips_entries_with_no_usable_text():
    materials = [MaterialEntry(filename="scan.pdf", relpath="scan.pdf", text_excerpt="", extracted=False)]
    assert generate_cards_from_materials("cs401", materials, llm_fn=lambda p, c: "Q: x\nA: y") == []


def test_generate_cards_from_materials_calls_llm_with_real_context():
    materials = [MaterialEntry(filename="notes.txt", relpath="notes.txt", text_excerpt="recursion basics", extracted=True)]
    captured = {}

    def fake_llm(prompt, context):
        captured["prompt"] = prompt
        captured["context"] = context
        return "Q: What is recursion?\nA: A function calling itself"

    pairs = generate_cards_from_materials("cs401", materials, fake_llm)
    assert pairs == [("What is recursion?", "A function calling itself")]
    assert "recursion basics" in captured["context"]
    assert "notes.txt" in captured["context"]
