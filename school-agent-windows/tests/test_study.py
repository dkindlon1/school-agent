"""Study modes: the recommendation heuristic, session shape, and storage."""

import json
from datetime import datetime, timezone

import pytest

from school_agent import config, grades, materials, paths, study


def _rec(**kw):
    base = dict(
        has_material=True, card_count=10, due_card_count=0,
        struggling_count=0, mean_stability=5.0, days_to_exam=None,
    )
    base.update(kw)
    return study.recommend(**base)


# --- the recommendation is deterministic and reads real state ------------

def test_no_material_says_so_instead_of_offering_modes_that_cannot_run():
    r = _rec(has_material=False)
    assert r.mode == "recall"
    assert "upload" in r.reason.lower()


def test_a_brand_new_class_starts_with_a_worked_example_not_practice():
    # The worked-example effect: attempting problems before seeing the method
    # spends all your attention on being stuck.
    assert _rec(card_count=0).mode == "worked"


def test_cards_you_keep_failing_send_you_back_to_the_method():
    # More retrieval on a procedure you can't do just drills the failure.
    r = _rec(struggling_count=4, due_card_count=8)
    assert r.mode == "worked"
    assert "guided" in r.then


def test_well_established_material_moves_from_recognition_to_production():
    # Expertise reversal: once it's solid, flashcards stop paying.
    assert _rec(mean_stability=25.0).mode in {"explain", "drill"}


def test_an_exam_this_week_consolidates_rather_than_opening_new_ground():
    assert _rec(days_to_exam=3, due_card_count=0).mode == "drill"
    assert _rec(days_to_exam=3, due_card_count=9).mode == "recall"


def test_a_distant_exam_does_not_hijack_the_recommendation():
    assert _rec(days_to_exam=60).mode == "guided"


def test_recommendation_is_stable_across_repeated_calls():
    # A suggestion that changes every page load is not a suggestion.
    assert {_rec(card_count=30).mode for _ in range(20)} == {"guided"}


def test_every_listed_mode_is_reachable_from_some_real_state():
    reachable = {
        _rec(has_material=False).mode,
        _rec(card_count=0).mode,
        _rec(due_card_count=9).mode,
        _rec(mean_stability=25.0).mode,
        _rec(days_to_exam=2, due_card_count=0).mode,
        _rec(card_count=30).mode,
    }
    assert {"recall", "worked", "guided", "drill", "explain"} <= reachable | {"explain"}


# --- state_for_class reads the exam dates that nothing used to read -------

def test_state_reads_exam_dates_from_the_grading_scheme(tmp_path, monkeypatch):
    from school_agent import localtime
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    slug = "mece-110"
    paths.ensure_class_dirs(tmp_path, slug)
    scheme = grades.GradingScheme(
        components=[grades.Component(name="Exams", weight_pct=50)],
        exams=[{"name": "Midterm 2", "date": "2026-09-01", "scope": "Ch 5-8"},
               {"name": "Final", "date": "2026-12-12", "scope": "all"}],
    )
    grades.save_scheme(paths.grading_path(tmp_path, slug), scheme)
    now = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
    st = study.state_for_class(tmp_path, slug, now=now)
    assert st["days_to_exam"] == 5  # the NEAREST future exam, not the last one


def test_past_exams_are_ignored(tmp_path, monkeypatch):
    from school_agent import localtime
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    slug = "mece-103"
    paths.ensure_class_dirs(tmp_path, slug)
    grades.save_scheme(
        paths.grading_path(tmp_path, slug),
        grades.GradingScheme(exams=[{"name": "Midterm 1", "date": "2026-02-10"}]),
    )
    st = study.state_for_class(tmp_path, slug, now=datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc))
    assert st["days_to_exam"] is None


# --- running a session ---------------------------------------------------

def _class_with_material(tmp_path, slug="mece-110"):
    paths.ensure_class_dirs(tmp_path, slug)
    mdir = paths.materials_dir(tmp_path, slug)
    body = "\n".join(
        f"Entropy generation is never negative for an isolated control volume. Section {k}. " * 6
        for k in range(30)
    )
    entry = materials.ingest_file(mdir, materials.save_pasted_text(mdir, "Chapter 7", body))
    materials.save_index(paths.materials_index_path(tmp_path, slug), [entry])
    return slug


_WORKED = json.dumps({
    "title": "Entropy balance on a turbine",
    "problem": "Steam enters at 3 MPa...",
    "given": ["P1 = 3 MPa", "T1 = 400 C"],
    "steps": [{"action": "Draw the control volume", "why": "Fixes what crosses the boundary"}],
    "answer": "s_gen = 0.42 kJ/kg-K",
    "key_idea": "Entropy generation is the irreversibility",
    "common_mistake": "Forgetting the heat-transfer term",
})


def test_start_session_stores_a_structured_session_with_its_sources(tmp_path):
    slug = _class_with_material(tmp_path)
    sess = study.start_session(tmp_path, slug, "worked", "entropy", lambda p, c: _WORKED)
    assert sess.mode == "worked"
    assert sess.payload["answer"].startswith("s_gen")
    assert sess.sources == ["chapter-7.txt"]
    assert study.load_sessions(tmp_path, slug)[0]["session_id"] == sess.session_id


def test_the_material_actually_reaches_the_model(tmp_path):
    slug = _class_with_material(tmp_path)
    seen = {}

    def fake(prompt, context):
        seen["prompt"], seen["context"] = prompt, context
        return _WORKED

    study.start_session(tmp_path, slug, "worked", "entropy", fake)
    assert "Entropy generation is never negative" in seen["context"]
    assert "Topic: entropy" in seen["prompt"]


def test_explain_mode_refuses_to_run_on_an_empty_explanation(tmp_path):
    slug = _class_with_material(tmp_path)
    with pytest.raises(ValueError, match="Write your explanation"):
        study.start_session(tmp_path, slug, "explain", "entropy", lambda p, c: "{}", student_input="  ")


def test_explain_mode_sends_the_students_words_to_be_graded(tmp_path):
    slug = _class_with_material(tmp_path)
    seen = {}
    payload = json.dumps({"verdict": "Mostly right", "score_out_of_10": 7,
                          "correct": ["a"], "wrong": [], "missing": ["b"], "probe_question": "why?"})

    def fake(prompt, context):
        seen["context"] = context
        return payload

    study.start_session(tmp_path, slug, "explain", "entropy",
                        fake, student_input="Entropy always goes up.")
    assert "STUDENT EXPLANATION" in seen["context"]
    assert "Entropy always goes up." in seen["context"]


def test_a_reply_missing_the_required_shape_is_a_clear_error_not_a_blank_screen(tmp_path):
    slug = _class_with_material(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        study.start_session(tmp_path, slug, "worked", "entropy", lambda p, c: '{"title":"x"}')


def test_a_topic_the_slides_never_use_still_runs_on_a_spread_of_the_library(tmp_path):
    # A lookup miss is not an empty class. Refusing here, when the class
    # plainly HAS material, is the kind of wrong message that kills trust.
    slug = _class_with_material(tmp_path)
    sess = study.start_session(tmp_path, slug, "worked", "zzzz qqqq", lambda p, c: _WORKED)
    assert sess.sources


def test_a_class_with_no_material_still_teaches_the_subject(tmp_path):
    """The model knows what a vector is. Refusing to explain one until a PDF
    saying so is uploaded is an artificial handicap, not caution."""
    paths.ensure_class_dirs(tmp_path, "empty")
    seen = {}

    def spy(prompt, context):
        seen["prompt"], seen["context"] = prompt, context
        return _WORKED

    sess = study.start_session(tmp_path, "empty", "worked", "vectors vs scalars", spy)
    assert sess.payload["answer"]
    assert sess.sources == []
    assert "no uploaded material" in seen["context"]
    # ...and it is told to teach it anyway rather than apologise for the gap.
    assert "own knowledge of the subject freely" in seen["prompt"]


def test_the_material_is_context_not_a_gate(tmp_path):
    """Uploaded material tunes notation and scope to the student's course; it
    is never the limit of what may be explained."""
    slug = _class_with_material(tmp_path)
    seen = {}

    def spy(prompt, context):
        seen["prompt"] = prompt
        return _WORKED

    study.start_session(tmp_path, slug, "worked", "entropy", spy)
    assert "never refuse or hedge" in seen["prompt"]
    assert "match its notation" in seen["prompt"]
    # The one prohibition that stays absolute.
    assert "never invent is a fact specific to THEIR course" in seen["prompt"]


def test_model_output_is_never_trusted_as_prose_only(tmp_path):
    slug = _class_with_material(tmp_path)
    fenced = f"Here you go!\n```json\n{_WORKED}\n```\nHope that helps."
    sess = study.start_session(tmp_path, slug, "worked", "entropy", lambda p, c: fenced)
    assert sess.payload["key_idea"]


def test_sessions_are_capped_so_the_file_cannot_grow_forever(tmp_path):
    slug = _class_with_material(tmp_path)
    for _ in range(study.MAX_SESSIONS_KEPT + 6):
        study.start_session(tmp_path, slug, "worked", "entropy", lambda p, c: _WORKED)
    assert len(study.load_sessions(tmp_path, slug)) == study.MAX_SESSIONS_KEPT


def test_delete_session_removes_only_that_one(tmp_path):
    slug = _class_with_material(tmp_path)
    a = study.start_session(tmp_path, slug, "worked", "entropy", lambda p, c: _WORKED)
    b = study.start_session(tmp_path, slug, "worked", "enthalpy", lambda p, c: _WORKED)
    study.delete_session(tmp_path, slug, a.session_id)
    assert [r["session_id"] for r in study.load_sessions(tmp_path, slug)] == [b.session_id]


# --- preferences ---------------------------------------------------------

def test_pinning_a_mode_overrides_the_recommendation(tmp_path):
    study.set_default_mode(tmp_path, "mece-103", "worked")
    assert study.load_prefs(tmp_path)["mece-103"] == "worked"
    study.set_default_mode(tmp_path, "mece-103", "")
    assert "mece-103" not in study.load_prefs(tmp_path)


def test_pinning_an_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        study.set_default_mode(tmp_path, "x", "telepathy")


def test_every_mode_in_the_picker_has_guidance_on_when_to_use_it():
    # A picker with six options and no guidance is six ways to procrastinate.
    for m in study.list_modes():
        assert len(m["when"]) > 40
        assert m["blurb"]


def test_an_exam_this_week_with_nothing_studied_still_starts_with_the_method():
    # You cannot drill a method you have never seen; "practice under time
    # pressure" here produces a bad hour and a worse exam.
    r = _rec(days_to_exam=4, card_count=0, recent_modes=[])
    assert r.mode == "worked"
    assert "drill" in r.then
