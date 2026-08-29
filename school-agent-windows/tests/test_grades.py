import pytest

from school_agent.grades import (
    Component,
    GradingScheme,
    Score,
    deadline_impact,
    extract_scheme,
    item_weight,
    letter_for,
    load_scheme,
    load_scores,
    match_component,
    needed_for_target,
    parse_scheme_json,
    save_scheme,
    save_scores,
    summarize,
    targets_table,
)


def _scheme():
    return GradingScheme(
        components=[
            Component(name="Homework", weight_pct=20, count=10, drop_lowest=1),
            Component(name="Quizzes", weight_pct=20, count=5),
            Component(name="Midterm", weight_pct=25, count=1),
            Component(name="Final Exam", weight_pct=35, count=1),
        ],
        confirmed=True,
    )


# ------------------------------------------------------------ the math --

def test_current_grade_uses_only_graded_work(tmp_path):
    scheme = _scheme()
    # 2 of 9 counted homeworks graded (10 minus 1 dropped), both perfect.
    scores = [
        Score("Homework", "HW1", 10, 10),
        Score("Homework", "HW2", 10, 10),
    ]
    s = summarize(scheme, scores)
    assert s.current_pct == 100.0
    # Only the graded slice of homework's 20% counts as decided so far.
    assert s.graded_weight == pytest.approx(20 * 2 / 9, abs=0.1)
    assert s.remaining_weight == pytest.approx(100 - 20 * 2 / 9, abs=0.1)


def test_current_grade_blends_components_by_weight():
    scheme = GradingScheme(components=[
        Component(name="Homework", weight_pct=50, count=1),
        Component(name="Exam", weight_pct=50, count=1),
    ])
    scores = [Score("Homework", "HW1", 100, 100), Score("Exam", "Midterm", 50, 100)]
    s = summarize(scheme, scores)
    assert s.current_pct == 75.0
    assert s.graded_weight == 100.0
    assert s.remaining_weight == 0.0


def test_drop_lowest_is_applied():
    scheme = GradingScheme(components=[Component(name="Quiz", weight_pct=100, count=3, drop_lowest=1)])
    scores = [
        Score("Quiz", "Q1", 100, 100),
        Score("Quiz", "Q2", 100, 100),
        Score("Quiz", "Q3", 0, 100),  # the zero should fall away
    ]
    assert summarize(scheme, scores).current_pct == 100.0


def test_no_scores_yet_reports_no_data():
    s = summarize(_scheme(), [])
    assert s.has_data is False
    assert s.current_pct is None
    assert s.remaining_weight == 100.0


def test_component_without_a_count_counts_as_fully_graded_once_scored():
    scheme = GradingScheme(components=[Component(name="Participation", weight_pct=10)])
    s = summarize(scheme, [Score("Participation", "term", 9, 10)])
    assert s.graded_weight == 10.0
    assert s.current_pct == 90.0


def test_needed_for_target_is_the_average_required_on_remaining_work():
    scheme = GradingScheme(components=[
        Component(name="Midterm", weight_pct=40, count=1),
        Component(name="Final", weight_pct=60, count=1),
    ])
    scores = [Score("Midterm", "Midterm", 80, 100)]  # 32 of 40 points earned
    got = needed_for_target(scheme, scores, 90.0)
    # Needs (90 - 32) / 60 = 96.67% on the final.
    assert got["needed_pct"] == pytest.approx(96.7, abs=0.1)
    assert got["possible"] is True


def test_needed_for_target_flags_an_unreachable_grade():
    scheme = GradingScheme(components=[
        Component(name="Midterm", weight_pct=50, count=1),
        Component(name="Final", weight_pct=50, count=1),
    ])
    got = needed_for_target(scheme, [Score("Midterm", "Midterm", 20, 100)], 93.0)
    assert got["needed_pct"] > 100
    assert got["possible"] is False


def test_needed_for_target_flags_an_already_secured_grade():
    scheme = GradingScheme(components=[
        Component(name="Midterm", weight_pct=90, count=1),
        Component(name="Final", weight_pct=10, count=1),
    ])
    got = needed_for_target(scheme, [Score("Midterm", "Midterm", 100, 100)], 83.0)
    assert got["already_secured"] is True


def test_targets_table_covers_the_letters_that_matter():
    rows = targets_table(_scheme(), [Score("Homework", "HW1", 9, 10)])
    letters = [r["letter"] for r in rows]
    assert "A" in letters and "B" in letters
    assert "F" not in letters  # not a target anyone steers toward


def test_letter_for_uses_the_syllabus_scale_when_present():
    lenient = GradingScheme(letter_scale=[["A", 85], ["B", 70]])
    assert letter_for(86, lenient) == "A"
    assert letter_for(86) == "B"  # default scale is stricter


# ------------------------------------------------ deadline grade impact --

def test_match_component_maps_calendar_titles_to_syllabus_categories():
    scheme = GradingScheme(components=[
        Component(name="Problem Sets", weight_pct=20, count=10),
        Component(name="Midterm Exams", weight_pct=40, count=2),
    ])
    # The calendar says "HW 4"; the syllabus says "Problem Sets".
    assert match_component(scheme, "HW 4 - Due").name == "Problem Sets"
    assert match_component(scheme, "Midterm 1 - Due").name == "Midterm Exams"


def test_item_weight_accounts_for_dropped_items():
    scheme = _scheme()
    hw = scheme.components[0]  # 20% over 10 items, lowest dropped
    assert item_weight(scheme, hw) == pytest.approx(20 / 9, abs=0.01)


def test_deadline_impact_gives_a_percentage_for_a_real_title():
    impact = deadline_impact(_scheme(), "Quiz 3 - Due")
    assert impact["component"] == "Quizzes"
    assert impact["item_weight"] == pytest.approx(4.0, abs=0.01)


def test_deadline_impact_is_none_without_a_scheme():
    assert deadline_impact(GradingScheme(), "HW 1") is None


# --------------------------------------------------------- persistence --

def test_scheme_and_scores_roundtrip(tmp_path):
    scheme_path, scores_path = tmp_path / "grading.json", tmp_path / "scores.json"
    save_scheme(scheme_path, _scheme())
    save_scores(scores_path, [Score("Homework", "HW1", 9, 10, "2026-09-01")])

    loaded = load_scheme(scheme_path)
    assert [c.name for c in loaded.components] == ["Homework", "Quizzes", "Midterm", "Final Exam"]
    assert loaded.confirmed is True
    assert load_scores(scores_path)[0].earned == 9


def test_missing_files_give_empty_defaults(tmp_path):
    assert load_scheme(tmp_path / "nope.json").components == []
    assert load_scores(tmp_path / "nope.json") == []


def test_corrupt_files_do_not_crash(tmp_path):
    p = tmp_path / "grading.json"
    p.write_text("not json", encoding="utf-8")
    assert load_scheme(p).components == []


# ---------------------------------------------------------- extraction --

def test_parse_scheme_json_tolerates_fences_and_preamble():
    raw = 'Here you go:\n```json\n{"components":[{"name":"Homework","weight_pct":30,"count":8}]}\n```'
    scheme = parse_scheme_json(raw)
    assert scheme.components[0].name == "Homework"
    assert scheme.components[0].weight_pct == 30
    assert scheme.confirmed is False  # always a proposal, never auto-trusted


def test_parse_scheme_json_rejects_garbage():
    with pytest.raises(ValueError):
        parse_scheme_json("I could not find a grading table.")


def test_extract_scheme_reads_real_syllabus_text(tmp_path):
    from school_agent.materials import save_pasted_text, scan_materials

    mdir = tmp_path / "materials"
    save_pasted_text(
        mdir,
        "syllabus",
        "GRADING: Homework 20% (10 sets, lowest dropped). Quizzes 20%. "
        "Midterm exam 25% on 2026-10-14 covering chapters 1-4. Final exam 35%. "
        "Late policy: 10% per day up to three days.",
    )
    entries = scan_materials(mdir)

    seen = {}

    def fake_llm(prompt, context):
        seen["context"] = context
        return '{"components":[{"name":"Homework","weight_pct":20,"count":10,"drop_lowest":1}],"exams":[{"name":"Midterm","date":"2026-10-14","scope":"Ch 1-4"}]}'

    scheme = extract_scheme(mdir, entries, fake_llm)
    assert "Homework 20%" in seen["context"]  # the real syllabus text reached the model
    assert scheme.components[0].count == 10
    assert scheme.exams[0]["date"] == "2026-10-14"
    assert scheme.confirmed is False


def test_extract_scheme_refuses_when_no_syllabus_is_on_file(tmp_path):
    from school_agent.materials import save_pasted_text, scan_materials

    mdir = tmp_path / "materials"
    save_pasted_text(mdir, "notes", "Entropy never decreases in an isolated system.")
    entries = scan_materials(mdir)
    with pytest.raises(ValueError, match="No syllabus-like material"):
        extract_scheme(mdir, entries, lambda p, c: "{}")
