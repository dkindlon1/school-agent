"""Regressions for the 2026-08-26 adversarial-review findings.

Every test here is a bug that shipped and was caught by review rather than by
the suite. The comments say what it looked like from the student's side.
"""

import json
from datetime import date, datetime, timezone

import pytest

from school_agent import briefing, deadlines, ladder, localtime, study


# --- the timezone fix was only half-applied ------------------------------

def test_the_deadline_board_computes_days_in_local_time(monkeypatch, tmp_path):
    """briefing.py was fixed; ui/server.py's /api/deadlines was not — so the
    8pm "everything is overdue" bug stayed live on the main screen."""
    import sys
    sys.path.insert(0, "ui")
    import server as ui_server

    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    monkeypatch.setattr(ui_server, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ui_server, "CONFIG_PATH", tmp_path / "config" / "classes.yaml")
    ui_server.app.config["TESTING"] = True
    with ui_server.app.test_client() as c:
        c.post("/api/classes", json={"name": "Thermo"})
        from school_agent import paths
        deadlines.save_deadlines(
            paths.deadlines_path(tmp_path, "thermo"),
            [deadlines.Deadline(uid="tonight", class_slug="thermo", title="PS 11",
                                due="2026-08-26T23:59:00-04:00")],
        )
        # 9pm Eastern == 01:00 UTC on the 27th.
        monkeypatch.setattr(localtime, "now_local",
                            lambda now=None: datetime(2026, 8, 26, 21, 0,
                                                      tzinfo=localtime.local_tz()))
        row = c.get("/api/deadlines").get_json()[0]
    assert row["days_until"] == 0, "work due at 11:59 tonight must not read as overdue at 9pm"


def test_local_zone_is_resolved_by_name_not_by_todays_offset(monkeypatch):
    """A frozen offset is wrong across a DST change, and for a 23:59 deadline
    an hour of error is a whole DAY of error."""
    monkeypatch.delenv(localtime.TIMEZONE_ENV, raising=False)
    monkeypatch.setenv("TZ", "America/New_York")
    noon_est_dec14 = datetime(2026, 12, 14, 17, 0, tzinfo=timezone.utc)
    assert localtime.days_until("2026-12-14T23:59:00-05:00", noon_est_dec14) == 0
    assert localtime.days_until("2026-12-13T23:59:00-05:00", noon_est_dec14) == -1


# --- the uid migration ---------------------------------------------------

_CAL = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    b"BEGIN:VEVENT\r\nUID:weekly@d2l\r\nDTSTART:20260901T235900Z\r\n"
    b"RRULE:FREQ=WEEKLY;COUNT=6\r\nSUMMARY:Weekly Quiz\r\nEND:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


def test_a_mark_survives_even_when_deadlines_json_was_lost():
    """The migration is one-shot. If deadlines.json was quarantined as corrupt,
    `old` is empty — but done.json still holds bare uids that would otherwise
    never match anything again."""
    new = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1), past_window_days=1, future_window_days=3)
    assert len(new) == 1  # exactly one occurrence in this window
    _, done, _ = deadlines.merge_preserving_marks([], new, {"weekly@d2l"}, set())
    assert done == {new[0].uid}


def test_a_moved_occurrence_still_migrates():
    """rekey_map used to match on the due date, but the new uid's suffix is the
    RECURRENCE-ID — so an occurrence the instructor moved never migrated, and
    left a permanent duplicate plus a stranded checkmark."""
    old = [deadlines.Deadline(uid="weekly@d2l", class_slug="c", title="Weekly Quiz",
                              due="2026-09-01T23:59:00+00:00")]
    moved = _CAL.replace(
        b"BEGIN:VEVENT\r\nUID:weekly@d2l\r\nDTSTART:20260901T235900Z\r\nRRULE:FREQ=WEEKLY;COUNT=6\r\nSUMMARY:Weekly Quiz\r\nEND:VEVENT\r\n",
        b"BEGIN:VEVENT\r\nUID:weekly@d2l\r\nDTSTART:20260901T235900Z\r\nRRULE:FREQ=WEEKLY;COUNT=6\r\nSUMMARY:Weekly Quiz\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:weekly@d2l\r\nRECURRENCE-ID:20260901T235900Z\r\nDTSTART:20260903T235900Z\r\nSUMMARY:Weekly Quiz\r\nEND:VEVENT\r\n",
    )
    new = deadlines.parse_ics(moved, "c", today=date(2026, 9, 1))
    merged, done, _ = deadlines.merge_preserving_marks(old, new, {"weekly@d2l"}, set())
    assert all("::" in d.uid for d in merged), "no bare-uid duplicate should survive"
    assert next(iter(done)).startswith("weekly@d2l::")


def test_the_migration_refuses_to_guess_on_an_ambiguous_prefix():
    """A mark landing on the WRONG deadline is worse than a mark not moving."""
    old = [deadlines.Deadline(uid="hw", class_slug="c", title="Homework 1",
                              due="2026-09-01T23:59:00+00:00")]
    new = [
        deadlines.Deadline(uid="hw::a", class_slug="c", title="Quiz A", due="2026-09-01T23:59:00+00:00"),
        deadlines.Deadline(uid="hw::b", class_slug="c", title="Quiz B", due="2026-09-01T23:59:00+00:00"),
    ]
    assert deadlines.rekey_map(old, new) == {}


def test_a_cancelled_assignment_stops_coming_back_forever():
    """Merging never forgot anything, so a cancelled assignment stayed red on
    the board and fired a "removed" toast on every sync — ~48 a day."""
    live = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    cancelled = deadlines.parse_ics(
        _CAL.replace(b"SUMMARY:Weekly Quiz", b"STATUS:CANCELLED\r\nSUMMARY:Weekly Quiz"),
        "c", today=date(2026, 9, 1),
    )
    assert cancelled == []
    merged, _, _ = deadlines.merge_preserving_marks(
        live, cancelled, set(), set(), window_start="2026-08-02"
    )
    assert merged == []


def test_history_outside_the_window_is_still_kept():
    """...but pruning must not delete the answer to "when was Exam 1?"."""
    old = [deadlines.Deadline(uid="feb", class_slug="c", title="Exam 1",
                              due="2026-02-10T23:59:00+00:00")]
    merged, _, _ = deadlines.merge_preserving_marks(old, [], set(), set(), window_start="2026-08-02")
    assert [d.uid for d in merged] == ["feb"]


def test_the_standalone_script_uses_the_same_sync_as_the_app():
    """It carried its own copy that overwrote wholesale — which deleted all
    history AND destroyed the one-shot uid migration."""
    src = (__import__("pathlib").Path(__file__).parent.parent / "scripts" / "pull_deadlines.py").read_text()
    assert "pull_all_deadlines" in src
    assert "save_deadlines" not in src


# --- ladder ---------------------------------------------------------------

def _lad_class(tmp_path, slug="s"):
    from school_agent import materials, paths
    paths.ensure_class_dirs(tmp_path, slug)
    mdir = paths.materials_dir(tmp_path, slug)
    e = materials.ingest_file(mdir, materials.save_pasted_text(
        mdir, "ch", "\n".join("moments signs joints equilibrium content " * 8 for _ in range(30))))
    materials.save_index(paths.materials_index_path(tmp_path, slug), [e])
    return slug


# A well-formed generation, used wherever a test just needs the ladder to move.
_FLAT = json.dumps({
    "problem": "Find the reaction at A.",
    "given": "L = 3 m",                                # a string where a list is declared
    "shown": [{"action": "Draw the FBD", "why": "fixes the forces"}],
    "blanks": ["Take moments about A"],
    "solution": ["Sum moments about A", "Solve"],      # flattened, not objects
    "answer": "400 N", "principle": "equilibrium", "watch_out": "signs",
})


def test_a_flattened_model_reply_does_not_brick_the_problem(tmp_path):
    """The old code stored whatever came back, then threw
    "'str' object has no attribute 'get'" as a 500 on EVERY press of
    "Check my work", discarding the student's typed working each time."""
    slug = _lad_class(tmp_path)
    l = ladder.start(tmp_path, slug, "signs", lambda p, c: _FLAT)
    assert l.current["solution"] == [{"action": "Sum moments about A", "why": ""},
                                     {"action": "Solve", "why": ""}]
    assert l.current["given"] == ["L = 3 m"]           # string coerced to a list
    ladder.attempt(tmp_path, slug, l.ladder_id, "", lambda p, c: '{"verdict":"correct","summary":"s"}')


def test_a_rung_whose_promise_did_not_arrive_is_an_error_not_a_silent_downgrade(tmp_path):
    """_coerce turns malformed shapes into empty lists rather than crashing,
    which is right — but only `problem` was checked, so an empty worked
    solution sailed through. Measured: "Watch it done" rendered nothing and
    still let the student advance; at "You finish it" a correct answer was
    graded against an empty reference, marked wrong, and cost a rung."""
    slug = _lad_class(tmp_path)
    no_worked = json.dumps({"problem": "p", "given": [], "shown": None,
                            "solution": [{"action": "a", "why": "w"}], "answer": "x"})
    with pytest.raises(ValueError, match="worked solution"):
        ladder.start(tmp_path, slug, "signs", lambda p, c: no_worked)

    l = ladder.start(tmp_path, slug, "signs", lambda p, c: _FLAT)
    ladder.attempt(tmp_path, slug, l.ladder_id, "", lambda p, c: '{"verdict":"correct","summary":"s"}')
    no_blanks = json.dumps({"problem": "p", "given": [], "shown": [{"action": "a", "why": "w"}],
                            "blanks": [], "solution": [{"action": "a", "why": "w"}], "answer": "x"})
    with pytest.raises(ValueError, match="part left for you"):
        ladder.next_problem(tmp_path, slug, l.ladder_id, lambda p, c: no_blanks)


def test_a_graduated_ladder_survives_getting_the_next_one_right(tmp_path):
    """Graduating resets the streak and the top rung needs two clean solves,
    so the next correct answer scored "stay" — and the badge was revoked for
    being right. Pressing "Next problem" after finishing is the most likely
    thing anyone does."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    ok = lambda p, c: '{"verdict":"correct","summary":"s"}'  # noqa: E731
    l = ladder.start(tmp_path, slug, "signs", gen)
    while not ladder.get_ladder(tmp_path, slug, l.ladder_id).graduated:
        if not ladder.get_ladder(tmp_path, slug, l.ladder_id).current:
            ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
        ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok)
    ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
    r = ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok)
    assert r["progress"]["graduated"] is True


def test_remarking_a_graduation_as_partial_takes_the_badge_back(tmp_path):
    """It left the ladder graduated off an attempt recorded as partial, with
    the streak zeroed — and the dashboard renders no body for a finished
    ladder, so it could not be continued, only deleted."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    ok = lambda p, c: '{"verdict":"correct","summary":"s"}'  # noqa: E731
    l = ladder.start(tmp_path, slug, "signs", gen)
    while not ladder.get_ladder(tmp_path, slug, l.ladder_id).graduated:
        if not ladder.get_ladder(tmp_path, slug, l.ladder_id).current:
            ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
        ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok)
    out = ladder.override_verdict(tmp_path, slug, l.ladder_id, "partial")
    assert out["progress"]["graduated"] is False


def test_stored_attempt_text_is_capped(tmp_path):
    """Uncapped, a student pasting their working built a 3.3 MB ladders.json
    for one class — re-parsed and shipped to the browser on every render."""
    slug = _lad_class(tmp_path)
    l = ladder.start(tmp_path, slug, "signs", lambda p, c: _FLAT)
    ladder.attempt(tmp_path, slug, l.ladder_id, "", lambda p, c: '{"verdict":"correct","summary":"s"}')
    ladder.next_problem(tmp_path, slug, l.ladder_id, lambda p, c: _FLAT)
    huge = "x" * 50_000
    ladder.attempt(tmp_path, slug, l.ladder_id, huge,
                   lambda p, c: json.dumps({"verdict": "correct", "summary": huge}))
    a = ladder.get_ladder(tmp_path, slug, l.ladder_id).attempts[-1]
    assert len(a["student_answer"]) == ladder.MAX_ATTEMPT_FIELD_CHARS
    assert len(a["summary"]) == ladder.MAX_ATTEMPT_FIELD_CHARS


def test_hitting_the_ladder_cap_never_drops_the_one_just_created(tmp_path):
    """It dropped the tail, and _upsert appends the new row to the tail — so
    start() returned a ladder that vanished on the next refresh."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    for i in range(ladder.MAX_LADDERS_PER_CLASS):
        l = ladder.start(tmp_path, slug, f"struggle {i}", gen)
        ladder.attempt(tmp_path, slug, l.ladder_id, "", lambda p, c: '{"verdict":"correct","summary":"s"}')
    newest = ladder.start(tmp_path, slug, "THE ONE THAT MATTERS", gen)
    assert ladder.get_ladder(tmp_path, slug, newest.ladder_id).struggle == "THE ONE THAT MATTERS"


def test_peeking_does_not_erase_a_clean_solve_you_already_earned(tmp_path):
    """Both the docstring and the button promise looking "costs you nothing
    except this rung's tick". It was also silently resetting the streak."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    ok = lambda p, c: '{"verdict":"correct","summary":"s"}'  # noqa: E731
    l = ladder.start(tmp_path, slug, "signs", gen)
    for _ in range(3):  # climb to rung 3, which needs two clean solves
        ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok)
        ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
    r = ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok)
    assert r["progress"]["rung_index"] == 3 and r["progress"]["clean_streak"] == 1
    ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
    r = ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok, used_solution=True)
    assert r["progress"]["clean_streak"] == 1, "the earned solve must survive a peek"


def test_a_graduated_ladder_that_gets_one_WRONG_stops_reporting_graduated(tmp_path):
    """It kept graduated_at set while sitting at a lower rung — the dashboard
    files those under "Finished" and renders no body, stranding you."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    ok = lambda p, c: '{"verdict":"correct","summary":"s"}'  # noqa: E731
    l = ladder.start(tmp_path, slug, "signs", gen)
    while not ladder.get_ladder(tmp_path, slug, l.ladder_id).graduated:
        cur = ladder.get_ladder(tmp_path, slug, l.ladder_id)
        if not cur.current:
            ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
        ladder.attempt(tmp_path, slug, l.ladder_id, "x", ok)
    ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
    r = ladder.attempt(tmp_path, slug, l.ladder_id, "x", lambda p, c: '{"verdict":"wrong","summary":"s"}')
    assert r["progress"]["graduated"] is False
    assert r["progress"]["rung_index"] < ladder.TOP_RUNG


def test_remarking_across_a_rung_change_clears_the_stale_problem(tmp_path):
    """Otherwise the ladder says "just the principle" while the payload on
    screen is a fully-worked last-step problem."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    l = ladder.start(tmp_path, slug, "signs", gen)
    ladder.attempt(tmp_path, slug, l.ladder_id, "", lambda p, c: '{"verdict":"correct","summary":"s"}')
    ladder.next_problem(tmp_path, slug, l.ladder_id, gen)
    assert ladder.get_ladder(tmp_path, slug, l.ladder_id).current is not None
    ladder.override_verdict(tmp_path, slug, l.ladder_id, "wrong")
    assert ladder.get_ladder(tmp_path, slug, l.ladder_id).current is None


def test_repeated_remarks_do_not_stack_the_annotation(tmp_path):
    slug = _lad_class(tmp_path)
    l = ladder.start(tmp_path, slug, "signs", lambda p, c: _FLAT)
    ladder.attempt(tmp_path, slug, l.ladder_id, "", lambda p, c: '{"verdict":"correct","summary":"s"}')
    for v in ("wrong", "partial", "correct"):
        ladder.override_verdict(tmp_path, slug, l.ladder_id, v)
    summary = ladder.get_ladder(tmp_path, slug, l.ladder_id).attempts[-1]["summary"]
    assert summary.count("[you re-marked this]") == 1


# --- study recommendation -------------------------------------------------

def _rec(**kw):
    base = dict(has_material=True, card_count=10, due_card_count=0,
                struggling_count=0, mean_stability=5.0, days_to_exam=None)
    base.update(kw)
    return study.recommend(**base)


def test_an_exam_this_week_outranks_an_open_ladder():
    """One abandoned ladder used to pin the recommendation for the rest of the
    semester — still saying "finish your ladder" with an exam tomorrow and 48
    cards due."""
    r = _rec(days_to_exam=1, due_card_count=48, struggling_count=9, open_ladder="signs")
    assert r.mode != "ladder"


def test_failing_cards_outrank_an_open_ladder():
    assert _rec(struggling_count=4, open_ladder="signs").mode == "worked"


def test_an_open_ladder_still_wins_when_nothing_urgent_is_happening():
    assert _rec(open_ladder="moment arms").mode == "ladder"


def test_the_just_did_it_guard_reads_the_newest_sessions():
    """recent_modes is newest-first; the guard read the tail, so it suggested
    explain-it-back exactly when you had just done one."""
    r = _rec(mean_stability=25.0, recent_modes=["explain", "worked", "guided", "map"])
    assert r.mode != "explain"
    r = _rec(mean_stability=25.0, recent_modes=["drill", "why", "explain", "worked"])
    assert r.mode == "explain"


def test_a_ladder_abandoned_weeks_ago_stops_steering(tmp_path):
    slug = _lad_class(tmp_path)
    l = ladder.start(tmp_path, slug, "old struggle", lambda p, c: _FLAT)
    stale = ladder.get_ladder(tmp_path, slug, l.ladder_id)
    stale.updated_at = "2026-01-01T00:00:00-05:00"
    ladder.save_ladders(tmp_path, slug, [stale])
    assert study.recommend_for_class(tmp_path, slug)["mode"] != "ladder"


def test_the_ladder_being_worked_is_the_one_reported(tmp_path):
    """load_ladders sorts ascending, so "the ladder you have going" was the
    LEAST recently touched one — it nagged about the wrong ladder."""
    slug = _lad_class(tmp_path)
    gen = lambda p, c: _FLAT  # noqa: E731
    a = ladder.start(tmp_path, slug, "AAA first", gen)
    ladder.start(tmp_path, slug, "BBB second", gen)
    ladder.next_problem(tmp_path, slug, a.ladder_id, gen)  # A is what you're working
    assert study.recommend_for_class(tmp_path, slug)["state"]["open_ladder"] == "AAA first"


def test_one_malformed_session_row_does_not_break_the_study_panel(tmp_path):
    from school_agent.storage import atomic_write_json
    slug = _lad_class(tmp_path)
    atomic_write_json(tmp_path / "data" / slug / "study_sessions.json",
                      [{"session_id": "abc", "created_at": "2026-08-01T00:00:00-04:00"}])
    assert study.recommend_for_class(tmp_path, slug)["mode"]


# --- briefing -------------------------------------------------------------

def test_due_today_still_counts_as_actionable():
    facts = {
        "classes": [{
            "name": "Thermo",
            "overdue": [], "overdue_total": 0, "stale_overdue": 0,
            "due_this_week": [{"title": "PS 11", "due": "2026-08-26T23:59:00-04:00",
                               "days_until": 0, "worth_pct": 15, "component": "Problem Sets"}],
            "upcoming": [], "recently_reviewed_questions": [], "struggling_with": [],
            "due_card_count": 0, "completed_recently": 0, "grade": None,
            "upcoming_topics": [], "documents": [],
        }],
        "study_last_7_days": {},
    }
    assert "PS 11" in briefing.render_deterministic(facts).split("# Suggested next step")[1]


# --- model provider diagnostics ------------------------------------------

def test_a_read_timeout_is_explained_as_a_network_problem():
    """"HTTPSConnectionPool(...): Read timed out" reads like a broken app or a
    bad key. It is neither."""
    import requests

    from school_agent import llm
    msg = llm._network_error_detail("gemini", requests.exceptions.ReadTimeout("read timeout=60"))
    assert "generativelanguage.googleapis.com" in msg
    assert "hotspot" in msg.lower()
    assert "key and model are probably fine" in msg.lower()


def test_a_wrong_gemini_model_is_named_as_such(monkeypatch):
    from school_agent import llm
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("SCHOOL_AGENT_GEMINI_MODEL", "gemini-9.9-imaginary")
    monkeypatch.setenv("SCHOOL_AGENT_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "list_remote_models", lambda p: ["gemini-3.7-flash", "gemini-3.5-flash"])
    out = llm.diagnose()
    assert out["ok"] is False and out["stage"] == "model"
    assert "gemini-9.9-imaginary" in out["detail"]


def test_a_rejected_key_is_distinguished_from_an_unreachable_host(monkeypatch):
    import requests

    from school_agent import llm
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("SCHOOL_AGENT_PROVIDER", "gemini")

    def boom(_):
        raise requests.exceptions.ReadTimeout("read timeout=60")

    monkeypatch.setattr(llm, "list_remote_models", boom)
    assert llm.diagnose()["stage"] == "network"

    monkeypatch.setattr(llm, "list_remote_models", lambda p: (_ for _ in ()).throw(RuntimeError("HTTP 400: API key not valid")))
    out = llm.diagnose()
    assert out["stage"] == "key" and "API key not valid" in out["detail"]


# --- second review pass, 2026-08-26 ---------------------------------------

def test_one_press_of_send_is_bounded(monkeypatch):
    """Three 60-second read timeouts plus backoff plus a 60-second fallback
    measured at 244 seconds of spinner, with no fetch timeout in the browser
    to cut it short.

    The local-fallback probe is pinned off on purpose: left live, this test
    measured a different code path on a machine that happens to be running
    Ollama than on one that isn't, and went red on the developer's own box
    while staying green everywhere else. The budget is what's under test, not
    whether a local model is up."""
    import time as _t

    from school_agent import llm
    monkeypatch.setattr(llm, "_ollama_reachable", lambda url: False)
    monkey = []

    def slow(provider, prompt, context):
        monkey.append(provider)
        _t.sleep(0.4)
        raise llm.ProviderBusyError("busy")

    import types
    orig = llm._attempt
    llm._attempt = slow
    llm_backoff = llm.RETRY_BACKOFF_S
    llm_budget = llm.MAX_RECOVERY_SECONDS
    llm.RETRY_BACKOFF_S = (0.3, 0.3)
    llm.MAX_RECOVERY_SECONDS = 1.0
    try:
        t0 = _t.monotonic()
        try:
            llm._call_with_recovery("gemini", "p", "c")
        except RuntimeError:
            pass
        elapsed = _t.monotonic() - t0
    finally:
        llm._attempt = orig
        llm.RETRY_BACKOFF_S = llm_backoff
        llm.MAX_RECOVERY_SECONDS = llm_budget
    # Generous against a 1.0s budget on purpose - the bug being pinned down
    # was 244 seconds, so anything in this range proves boundedness without
    # turning a loaded CI box or laptop into a false failure.
    assert elapsed < 3.0, f"recovery ran {elapsed:.1f}s past its budget"
    assert len(monkey) < 3, "it kept attempting after the budget was spent"


def test_when_the_local_fallback_also_fails_you_see_ITS_error(monkeypatch):
    """The original says "the cloud is busy, try later". The fallback's says
    "that model isn't pulled" — the only one you can act on, and it was being
    discarded while the activity log claimed the local model had answered."""
    from school_agent import llm

    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.delenv("SCHOOL_AGENT_PROVIDER", raising=False)
    monkeypatch.setattr(llm, "_ollama_reachable", lambda url: True)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def attempt(provider, prompt, context):
        if provider == "ollama":
            raise RuntimeError("model 'gemma4:12b' not found, try pulling it first")
        raise llm.ProviderBusyError("Gemini is overloaded right now (HTTP 503).")

    monkeypatch.setattr(llm, "_attempt", attempt)
    with pytest.raises(RuntimeError) as e:
        llm._call_with_recovery("gemini", "p", "c")
    assert "not found, try pulling it first" in str(e.value)
    assert "overloaded" in str(e.value)


def test_a_captive_portal_is_named_and_not_retried(monkeypatch):
    import requests

    from school_agent import llm
    calls = {"n": 0}

    def attempt(url, **kw):
        calls["n"] += 1
        raise requests.exceptions.JSONDecodeError("Expecting value", "<html>sign in</html>", 0)

    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(llm.requests, "post", attempt)
    with pytest.raises(RuntimeError, match="wifi sign-in page"):
        llm.default_llm_fn("p", "c")
    assert calls["n"] == 1


def test_a_stale_session_no_longer_defeats_the_never_seen_it_guard():
    from school_agent import study
    base = dict(has_material=True, card_count=0, due_card_count=0, struggling_count=0,
                mean_stability=None, days_to_exam=1)
    assert study.recommend(**base, recent_modes=[]).mode == "worked"
    assert study.recommend(**base, recent_modes=["map"]).mode == "worked"


def test_diagnose_does_not_claim_reachable_without_reaching_anything(monkeypatch):
    from school_agent import llm
    monkeypatch.setenv("SCHOOL_AGENT_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(llm, "list_remote_models", lambda p: [])
    out = llm.diagnose()
    assert out["ok"] is False and out["stage"] == "key"


def test_a_parse_failure_is_explained_in_words():
    from school_agent import study
    for junk in ("{not json at all}", '{"a":1} {"b":2}', "{'single': 'quotes'}"):
        with pytest.raises(ValueError) as e:
            study.parse_session_json(junk)
        assert "Expecting" not in str(e.value), f"raw json message leaked for {junk!r}"
        assert "switch provider" in str(e.value) or "format this needs" in str(e.value)


# --- grade-impact ordering, final sign-off pass ---------------------------

def test_an_exam_outranks_a_small_assignment_even_with_no_stated_count():
    """The app's headline claim is 'ordered by grade impact, not by whichever
    date comes first'. It inverted whenever a syllabus said 'Exams: 70%'
    without saying how many there are — extremely common, and exactly what
    the extraction prompt tells the model to record as count: null. The
    briefing then said: work on the 3.33% problem set, not the midterm."""
    from school_agent import grades
    scheme = grades.GradingScheme(components=[
        grades.Component(name="Problem Sets", weight_pct=30, count=10, drop_lowest=1),
        grades.Component(name="Exams", weight_pct=70),
    ])
    ps = grades.deadline_impact(scheme, "Problem Set 11")
    exam = grades.deadline_impact(scheme, "Midterm 2")
    assert ps["item_weight"] == 3.33
    assert exam["item_weight"] is None            # honestly unknown...
    assert exam["ordering_weight"] == 70          # ...but it still outranks
    assert exam["ordering_weight"] > ps["ordering_weight"]


def test_the_exam_count_is_inferred_from_the_dates_the_syllabus_listed():
    """Those dates are already extracted onto the scheme. Counting them beats
    treating the component as unquantifiable."""
    from school_agent import grades
    scheme = grades.GradingScheme(
        components=[grades.Component(name="Exams", weight_pct=60)],
        exams=[{"name": "Midterm 1", "date": "2026-10-14"},
               {"name": "Midterm 2", "date": "2026-11-18"},
               {"name": "Final", "date": "2026-12-15"}],
    )
    assert grades.deadline_impact(scheme, "Midterm 2")["item_weight"] == 20.0


def test_the_briefing_recommends_the_exam_over_the_problem_set():
    facts = {"classes": [{
        "name": "Thermo", "overdue": [], "overdue_total": 0, "stale_overdue": 0,
        "due_this_week": [
            {"title": "Problem Set 11", "due": "2026-09-01T23:59:00-04:00", "days_until": 2,
             "worth_pct": 3.33, "order_pct": 3.33, "component_pct": 30, "component": "Problem Sets"},
            {"title": "Midterm 2", "due": "2026-09-04T23:59:00-04:00", "days_until": 5,
             "worth_pct": None, "order_pct": 70, "component_pct": 70, "component": "Exams"}],
        "upcoming": [], "recently_reviewed_questions": [], "struggling_with": [],
        "due_card_count": 0, "completed_recently": 0, "grade": None,
        "upcoming_topics": [], "documents": []}],
        "study_last_7_days": {}}
    out = briefing.render_deterministic(facts)
    attention = out.split("# Where you stand")[0]
    assert attention.index("Midterm 2") < attention.index("Problem Set 11"), "exam must be listed first"
    assert "Midterm 2" in out.split("# Suggested next step")[1]
    # ...and it must not claim a per-item number it does not have.
    assert "None%" not in out
    assert "70% of that grade" in out


def test_an_unknown_share_is_described_not_left_blank():
    """Showing nothing next to an exam reads as 'this is worth nothing'."""
    facts = {"classes": [{
        "name": "Thermo", "overdue": [], "overdue_total": 0, "stale_overdue": 0,
        "due_this_week": [{"title": "Midterm 2", "due": "2026-09-04T23:59:00-04:00", "days_until": 5,
                           "worth_pct": None, "order_pct": 70, "component_pct": 70, "component": "Exams"}],
        "upcoming": [], "recently_reviewed_questions": [], "struggling_with": [],
        "due_card_count": 0, "completed_recently": 0, "grade": None,
        "upcoming_topics": [], "documents": []}],
        "study_last_7_days": {}}
    assert "[Exams, 70% of grade overall]" in briefing.render_deterministic(facts)
