"""Struggle ladders: the advancement rules, generation, and the escape hatches.

The rules are the product here — a ladder that advances you when you peeked,
or that punishes you for a broken generated problem, teaches the wrong thing.
So they are tested directly rather than only through the API.
"""

import json

import pytest

from school_agent import ladder, materials, paths

_GEN = json.dumps({
    "problem": "A 3 m beam is pinned at A with a 400 N load at B. Find the reaction at A.",
    "given": ["L = 3 m", "F = 400 N"],
    "shown": [{"action": "Draw the free body diagram", "why": "Fixes which forces act"}],
    "blanks": ["Take moments about A and solve for the reaction"],
    "solution": [{"action": "Sum M_A = 0", "why": "Removes the pin reaction from the equation"}],
    "answer": "R_A = 400 N up",
    "principle": "Sum of moments about any point is zero in equilibrium",
    "watch_out": "Counterclockwise positive — the sign convention is where this usually goes wrong",
})


def gen(prompt, context):
    return _GEN


def check(verdict):
    return lambda p, c: json.dumps(
        {"verdict": verdict, "summary": "s", "went_wrong": "w", "right_move": "r"}
    )


@pytest.fixture()
def cls(tmp_path):
    slug = "mece-103"
    paths.ensure_class_dirs(tmp_path, slug)
    mdir = paths.materials_dir(tmp_path, slug)
    body = "\n".join(
        "Method of joints. Moments about a point. Counterclockwise positive sign convention. " * 6
        for _ in range(30)
    )
    entry = materials.ingest_file(mdir, materials.save_pasted_text(mdir, "Chapter 5", body))
    materials.save_index(paths.materials_index_path(tmp_path, slug), [entry])
    return slug


def _advance(tmp_path, slug, lid, verdict, used=False, answer="R_A = 400 N"):
    l = ladder.get_ladder(tmp_path, slug, lid)
    if not l.current:
        ladder.next_problem(tmp_path, slug, lid, gen)
    return ladder.attempt(tmp_path, slug, lid, answer, check(verdict), used_solution=used)


# --- starting -----------------------------------------------------------

def test_a_ladder_starts_at_the_bottom_with_a_problem_ready(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "I keep dropping the sign on moment arms", gen)
    assert l.rung == 0
    assert ladder.progress(l)["rung_label"] == "Watch it done"
    assert l.current["problem"]
    assert l.sources  # built from real material, never from nothing


def test_the_struggle_is_kept_verbatim(tmp_path, cls):
    words = "I keep dropping the sign on moment arms"
    l = ladder.start(tmp_path, cls, words, gen)
    assert l.struggle == words


def test_an_empty_struggle_asks_for_something_specific(tmp_path, cls):
    with pytest.raises(ValueError, match="in your own words"):
        ladder.start(tmp_path, cls, "   ", gen)


def test_a_class_with_no_material_still_builds_problems(tmp_path):
    """Uploads tune the problems to the course; they are not permission to
    know the subject. Refusing here made the feature useless on day one, when
    nothing is uploaded yet and the student most needs it."""
    paths.ensure_class_dirs(tmp_path, "empty")
    seen = {}

    def spy(prompt, context):
        seen["prompt"], seen["context"] = prompt, context
        return _GEN

    l = ladder.start(tmp_path, "empty", "I don't get vectors vs scalars", spy)
    assert l.current["problem"]
    assert l.sources == []
    assert "no uploaded material" in seen["context"]
    assert "never refuse to build a problem" in seen["prompt"]


def test_uploaded_material_is_context_not_a_limit(tmp_path, cls):
    seen = {}

    def spy(prompt, context):
        seen["prompt"], seen["context"] = prompt, context
        return _GEN

    ladder.start(tmp_path, cls, "signs on moment arms", spy)
    assert "Counterclockwise positive" in seen["context"]     # their convention reaches it
    assert "match its notation" in seen["prompt"]
    assert "own knowledge of the subject freely" in seen["prompt"]


def test_the_checker_backs_the_student_over_a_wrong_reference():
    """The reference solution is generated too. If it disagrees with the
    subject, the student should not lose a rung for being right."""
    assert "the student is right" in ladder._CHECK_PROMPT


def test_generation_is_told_which_problems_were_already_used(tmp_path, cls):
    seen = {}

    def spy(prompt, context):
        seen["prompt"] = prompt
        return _GEN

    l = ladder.start(tmp_path, cls, "signs", gen)
    ladder.next_problem(tmp_path, cls, l.ladder_id, spy)
    assert "do not repeat" in seen["prompt"]
    assert "3 m beam" in seen["prompt"]


def test_the_fade_instruction_changes_with_the_rung(tmp_path, cls):
    prompts = []

    def spy(prompt, context):
        prompts.append(prompt)
        return _GEN

    l = ladder.start(tmp_path, cls, "signs", spy)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")   # reading rung
    ladder.next_problem(tmp_path, cls, l.ladder_id, spy)
    assert "COMPLETE solution" in prompts[0]
    assert "STOP before the final step" in prompts[-1]


# --- the advancement rules ----------------------------------------------

def test_a_clean_correct_moves_you_up(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    r = _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    assert r["outcome"]["moved"] == "up"
    assert r["progress"]["rung_index"] == 1


def test_looking_at_the_solution_never_advances_you(tmp_path, cls):
    # Peeking is allowed and unpunished, but the ladder measures what you can
    # do unaided, so it cannot count toward moving up.
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    for _ in range(3):
        r = _advance(tmp_path, cls, l.ladder_id, "correct", used=True)
        assert r["outcome"]["moved"] == "stay"
        assert r["progress"]["rung_index"] == 1


def test_a_wrong_answer_drops_you_back_a_rung(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    _advance(tmp_path, cls, l.ladder_id, "correct")
    assert ladder.get_ladder(tmp_path, cls, l.ladder_id).rung == 2
    r = _advance(tmp_path, cls, l.ladder_id, "wrong")
    assert r["outcome"]["moved"] == "down"
    assert r["progress"]["rung_index"] == 1


def test_you_never_drop_below_the_bottom(tmp_path, cls):
    # Reached by re-marking, not by attempting: the bottom rung is reading,
    # so there is nothing there that CAN be wrong.
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    out = ladder.override_verdict(tmp_path, cls, l.ladder_id, "wrong")
    assert out["progress"]["rung_index"] == 0
    l2 = ladder.get_ladder(tmp_path, cls, l.ladder_id)
    outcome = ladder._apply_outcome(l2, "wrong", used_solution=False)
    assert l2.rung == 0
    assert outcome["moved"] == "stay"
    assert "no penalty" in outcome["note"]


def test_the_reading_rung_cannot_be_failed(tmp_path, cls):
    # Nothing is submitted there, so a "check" of it would be theatre.
    l = ladder.start(tmp_path, cls, "signs", gen)
    r = ladder.attempt(tmp_path, cls, l.ladder_id, "", check("wrong"))
    assert r["check"]["verdict"] == "correct"
    assert r["progress"]["rung_index"] == 1


def test_partial_holds_you_at_the_same_rung(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    r = _advance(tmp_path, cls, l.ladder_id, "partial")
    assert r["outcome"]["moved"] == "stay"
    assert r["progress"]["rung_index"] == 1


def test_the_top_two_rungs_need_two_clean_solves_each(tmp_path, cls):
    # One lucky solo problem is not evidence that a method has landed.
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")   # -> rung 1
    _advance(tmp_path, cls, l.ladder_id, "correct")              # -> rung 2
    _advance(tmp_path, cls, l.ladder_id, "correct")              # -> rung 3
    r = _advance(tmp_path, cls, l.ladder_id, "correct")          # 1 of 2 at rung 3
    assert r["progress"]["rung_index"] == 3
    assert r["progress"]["clean_streak"] == 1
    r = _advance(tmp_path, cls, l.ladder_id, "correct")          # -> rung 4
    assert r["progress"]["rung_index"] == 4


def test_a_wrong_answer_resets_the_streak_not_just_the_rung(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    _advance(tmp_path, cls, l.ladder_id, "correct")
    _advance(tmp_path, cls, l.ladder_id, "correct")              # rung 3
    _advance(tmp_path, cls, l.ladder_id, "correct")              # streak 1
    r = _advance(tmp_path, cls, l.ladder_id, "wrong")
    assert r["progress"]["clean_streak"] == 0
    assert r["progress"]["rung_index"] == 2


def test_graduating_takes_two_clean_solo_solves(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    for _ in range(3):
        _advance(tmp_path, cls, l.ladder_id, "correct", answer="x")
    for _ in range(4):
        r = _advance(tmp_path, cls, l.ladder_id, "correct")
    assert r["outcome"]["moved"] == "graduated"
    done = ladder.get_ladder(tmp_path, cls, l.ladder_id)
    assert done.graduated
    assert done.current is None  # nothing left in front of you


def test_the_reading_rung_needs_no_answer(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    # Nothing is submitted at rung 0, so no model call is made to check it.
    def explode(p, c):
        raise AssertionError("the reading rung must not call the model to grade nothing")
    r = ladder.attempt(tmp_path, cls, l.ladder_id, "", explode)
    assert r["progress"]["rung_index"] == 1


def test_every_other_rung_requires_an_attempt(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    ladder.next_problem(tmp_path, cls, l.ladder_id, gen)
    with pytest.raises(ValueError, match="Write your working"):
        ladder.attempt(tmp_path, cls, l.ladder_id, "   ", check("correct"))


def test_an_answered_problem_is_cleared_so_a_stale_one_is_never_re_served(tmp_path, cls):
    # The bug this prevents: you move up to "you do the hard part" and are
    # shown the previous rung's problem with its FULL worked solution still
    # attached, which defeats the entire fade.
    l = ladder.start(tmp_path, cls, "signs", gen)
    ladder.attempt(tmp_path, cls, l.ladder_id, "", check("correct"))
    after = ladder.get_ladder(tmp_path, cls, l.ladder_id)
    assert after.rung == 1
    assert after.current is None
    with pytest.raises(ValueError, match="no problem in front of you"):
        ladder.attempt(tmp_path, cls, l.ladder_id, "x", check("correct"))


# --- escape hatches ------------------------------------------------------

def test_discarding_a_broken_problem_costs_nothing(tmp_path, cls):
    # Models get physics wrong. A ladder that punished you for noticing
    # would be worse than useless.
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    before = ladder.get_ladder(tmp_path, cls, l.ladder_id)
    ladder.discard_problem(tmp_path, cls, l.ladder_id, gen)
    after = ladder.get_ladder(tmp_path, cls, l.ladder_id)
    assert after.rung == before.rung
    assert len(after.attempts) == len(before.attempts)


def test_you_can_overrule_a_check_that_marked_you_wrong(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    _advance(tmp_path, cls, l.ladder_id, "correct")               # rung 2
    _advance(tmp_path, cls, l.ladder_id, "wrong")                 # knocked to rung 1
    assert ladder.get_ladder(tmp_path, cls, l.ladder_id).rung == 1
    out = ladder.override_verdict(tmp_path, cls, l.ladder_id, "correct")
    # Rewinds exactly one move and re-applies, rather than stacking a second
    # attempt on top of the first.
    assert out["progress"]["rung_index"] == 3
    assert ladder.get_ladder(tmp_path, cls, l.ladder_id).attempts[-1]["verdict"] == "correct"


def test_overruling_can_also_take_a_correct_back(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    assert ladder.get_ladder(tmp_path, cls, l.ladder_id).rung == 1
    out = ladder.override_verdict(tmp_path, cls, l.ladder_id, "wrong")
    assert out["progress"]["rung_index"] == 0


def test_an_unreadable_check_is_a_clear_error(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    with pytest.raises(ValueError):
        ladder.attempt(tmp_path, cls, l.ladder_id, "x", lambda p, c: '{"verdict":"maybe"}')


def test_a_nonsense_override_is_rejected(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    with pytest.raises(ValueError, match="verdict must be"):
        ladder.override_verdict(tmp_path, cls, l.ladder_id, "brilliant")


# --- storage -------------------------------------------------------------

def test_ladders_survive_a_reload_with_their_position(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    _advance(tmp_path, cls, l.ladder_id, "correct", answer="")
    again = ladder.get_ladder(tmp_path, cls, l.ladder_id)
    assert again.rung == 1
    assert again.attempts


def test_attempt_history_is_bounded(tmp_path, cls):
    l = ladder.start(tmp_path, cls, "signs", gen)
    for _ in range(ladder.MAX_ATTEMPTS_KEPT + 8):
        _advance(tmp_path, cls, l.ladder_id, "partial", answer="x")
    assert len(ladder.get_ladder(tmp_path, cls, l.ladder_id).attempts) == ladder.MAX_ATTEMPTS_KEPT


def test_deleting_one_ladder_leaves_the_others(tmp_path, cls):
    a = ladder.start(tmp_path, cls, "signs", gen)
    b = ladder.start(tmp_path, cls, "method of joints", gen)
    ladder.delete_ladder(tmp_path, cls, a.ladder_id)
    assert [x.ladder_id for x in ladder.load_ladders(tmp_path, cls)] == [b.ladder_id]


def test_a_malformed_row_does_not_lose_the_rest(tmp_path, cls):
    from school_agent.storage import atomic_write_json
    good = ladder.start(tmp_path, cls, "signs", gen)
    path = paths.class_dir(tmp_path, cls) / "ladders.json"
    rows = [{"nonsense": True}, good.to_dict()]
    atomic_write_json(path, rows)
    assert [l.ladder_id for l in ladder.load_ladders(tmp_path, cls)] == [good.ladder_id]


# --- it steers the study recommendation ----------------------------------

def test_an_open_ladder_becomes_the_recommendation(tmp_path, cls):
    from school_agent import study
    ladder.start(tmp_path, cls, "I keep dropping the sign on moment arms", gen)
    rec = study.recommend_for_class(tmp_path, cls)
    assert rec["mode"] == "ladder"
    assert "moment arms" in rec["reason"]


def test_a_graduated_ladder_stops_steering_it(tmp_path, cls):
    from school_agent import study
    l = ladder.start(tmp_path, cls, "signs", gen)
    for _ in range(3):
        _advance(tmp_path, cls, l.ladder_id, "correct", answer="x")
    for _ in range(4):
        _advance(tmp_path, cls, l.ladder_id, "correct")
    assert ladder.get_ladder(tmp_path, cls, l.ladder_id).graduated
    assert study.recommend_for_class(tmp_path, cls)["mode"] != "ladder"
