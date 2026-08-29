"""Regressions for the six semester-scale bugs found on 2026-08-26.

Each of these was reproduced against a simulated late-semester state before
being fixed; the comments say what the wrong behaviour looked like from the
student's side, because that is the thing that must never come back.
"""

from datetime import date, datetime, timezone

import pytest

from school_agent import briefing, config, deadlines, grades, localtime, materials


# --- 1. tonight's work read as overdue every evening after 8pm ------------

def test_work_due_tonight_is_not_overdue_at_9pm_local(monkeypatch):
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    # 9pm Eastern == 01:00 UTC the NEXT day. The old code compared the due
    # date against now.date() in UTC, so from 8pm onward every single
    # evening, work due at 11:59 tonight rendered OVERDUE.
    nine_pm_eastern = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
    assert localtime.days_until("2026-08-26T23:59:00-04:00", nine_pm_eastern) == 0
    assert localtime.days_until("2026-08-27T23:59:00-04:00", nine_pm_eastern) == 1
    assert localtime.days_until("2026-08-25T23:59:00-04:00", nine_pm_eastern) == -1


def test_all_day_event_today_is_due_today_not_yesterday(monkeypatch):
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    now = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)  # still the 26th locally
    assert localtime.days_until("2026-08-26", now) == 0


def test_floating_ics_time_is_read_as_local_not_utc(monkeypatch):
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    now = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
    # A naive datetime in an ICS feed is floating local time per the spec.
    assert localtime.days_until("2026-08-26T23:59:00", now) == 0


# --- 2. the semester turning into a permanent wall of overdue -------------

def _class(tmp_path, slug="mece-110"):
    return config.ClassConfig(slug=slug, name="Thermodynamics")


def test_ancient_overdue_is_counted_not_listed(tmp_path, monkeypatch):
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    now = datetime(2026, 12, 8, 15, 0, tzinfo=timezone.utc)  # week 15
    c = _class(tmp_path)
    from school_agent import paths
    paths.ensure_class_dirs(tmp_path, c.slug)
    # A whole semester of never-marked-off work, plus one genuinely recent item.
    rows = [
        deadlines.Deadline(uid=f"old-{i}", class_slug=c.slug, title=f"Reading {i}", due="2026-09-10T23:59:00+00:00")
        for i in range(60)
    ]
    rows.append(deadlines.Deadline(uid="recent", class_slug=c.slug, title="Problem Set 11", due="2026-12-02T23:59:00+00:00"))
    deadlines.save_deadlines(paths.deadlines_path(tmp_path, c.slug), rows)

    facts = briefing.build_facts(tmp_path, [c], now=now)
    listed = facts["classes"][0]["overdue"]
    assert len(listed) <= briefing.MAX_OVERDUE_LISTED
    # September work is sediment, not "attention needed".
    assert all(row["title"] != "Reading 0" for row in listed)
    assert facts["classes"][0]["stale_overdue"] == 60
    assert any(row["title"] == "Problem Set 11" for row in listed)


def test_next_step_never_recommends_starting_a_long_past_exam(tmp_path, monkeypatch):
    monkeypatch.setenv(localtime.TIMEZONE_ENV, "America/New_York")
    # The measured week-15 failure: "Work on Exam 1 - overdue" for a February exam.
    facts = {
        "classes": [
            {
                "name": "Thermodynamics",
                "overdue": [{"title": "Exam 1", "due": "2026-09-20T23:59:00+00:00", "days_until": -79, "worth_pct": 25, "component": "Exams"}],
                "overdue_total": 1,
                "stale_overdue": 0,
                "due_this_week": [{"title": "Problem Set 11", "due": "2026-12-10T23:59:00+00:00", "days_until": 2, "worth_pct": 3, "component": "Problem Sets"}],
                "upcoming": [],
                "recently_reviewed_questions": [],
                "struggling_with": [],
                "due_card_count": 0,
                "completed_recently": 0,
                "grade": None,
                "upcoming_topics": [],
                "documents": [],
            }
        ],
        "study_last_7_days": {},
    }
    text = briefing.render_deterministic(facts)
    suggestion = text.split("# Suggested next step")[1]
    assert "Exam 1" not in suggestion
    assert "Problem Set 11" in suggestion


# --- 3. retrieval regressing to cover pages at 8+ documents ---------------

def test_sample_chunks_does_not_collapse_to_cover_pages(tmp_path):
    mdir = tmp_path / "materials"
    mdir.mkdir()
    entries = []
    for n in range(1, 13):
        body = f"COVERPAGE{n} syllabus title author\n" + "\n".join(
            f"doc{n} section {k} entropy moments equilibrium content " * 8 for k in range(40)
        )
        fp = materials.save_pasted_text(mdir, f"Lecture {n}", body)
        entries.append(materials.ingest_file(mdir, fp))

    chunks = materials.sample_chunks(mdir, entries, max_chunks=8)
    assert len(chunks) == 8
    # The bug: every chunk was position 0 of a different document, i.e. all
    # title pages, covering ~1% of the library.
    assert not any("COVERPAGE" in c.text for c in chunks)
    assert len({c.filename for c in chunks}) == 8


def test_sample_chunks_still_goes_deep_on_a_small_library(tmp_path):
    mdir = tmp_path / "materials"
    mdir.mkdir()
    body = "\n".join(f"section {k} thermodynamics content here " * 10 for k in range(40))
    entries = [materials.ingest_file(mdir, materials.save_pasted_text(mdir, "Only Doc", body))]
    chunks = materials.sample_chunks(mdir, entries, max_chunks=6)
    assert len(chunks) == 6
    assert len({c.text for c in chunks}) == 6  # six DIFFERENT parts of it


# --- 4. recurring-series identity flipping mid-semester -------------------

_CAL = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    b"BEGIN:VEVENT\r\nUID:weekly@d2l\r\nDTSTART:20260901T235900Z\r\n"
    b"RRULE:FREQ=WEEKLY;COUNT=6\r\nSUMMARY:Weekly Quiz\r\nEND:VEVENT\r\n"
    b"BEGIN:VEVENT\r\nUID:oneoff@d2l\r\nDTSTART:20260915T235900Z\r\n"
    b"SUMMARY:Project Proposal\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _uid_for(rows, title, due_prefix):
    return next(d.uid for d in rows if d.title == title and d.due.startswith(due_prefix))


def test_recurring_uid_is_stable_as_the_expansion_window_moves():
    # The bug: identity depended on how many siblings landed inside the
    # window, so a series thinned by time or by cancellation silently
    # changed key -- done-state lost, row duplicated, false "new deadline".
    wide = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1), past_window_days=30, future_window_days=365)
    narrow = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1), past_window_days=1, future_window_days=5)
    assert _uid_for(wide, "Weekly Quiz", "2026-09-01") == _uid_for(narrow, "Weekly Quiz", "2026-09-01")


def test_one_off_deadline_keeps_a_date_independent_uid():
    # ...and the other half: moving a one-off's due date must read as one
    # deadline changing, not one vanishing and a new one appearing.
    rows = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    assert _uid_for(rows, "Project Proposal", "2026-09-15") == "oneoff@d2l"


def test_cancelling_occurrences_does_not_rekey_the_survivors():
    thinned = _CAL.replace(b"RRULE:FREQ=WEEKLY;COUNT=6", b"RRULE:FREQ=WEEKLY;COUNT=1")
    full = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    one = deadlines.parse_ics(thinned, "c", today=date(2026, 9, 1))
    assert _uid_for(full, "Weekly Quiz", "2026-09-01") == _uid_for(one, "Weekly Quiz", "2026-09-01")


# --- 5. renaming a grading component silently orphaning scores ------------

def test_renaming_a_component_surfaces_orphans_instead_of_moving_the_grade():
    scheme = grades.GradingScheme(components=[grades.Component(name="Problem Sets", weight_pct=20, count=10)])
    scores = [grades.Score(component="Homework", name=f"HW {i}", earned=9, possible=10) for i in range(1, 7)]
    summary = grades.summarize(scheme, scores)
    assert summary.orphaned and summary.orphaned[0]["component"] == "Homework"
    assert summary.orphaned[0]["count"] == 6


def test_reassign_component_recovers_the_grade_without_re_entry():
    scheme = grades.GradingScheme(components=[grades.Component(name="Problem Sets", weight_pct=20, count=10)])
    scores = [grades.Score(component="Homework", name=f"HW {i}", earned=9, possible=10) for i in range(1, 7)]
    fixed = grades.reassign_component(scores, "Homework", "Problem Sets")
    summary = grades.summarize(scheme, fixed)
    assert summary.orphaned == []
    assert summary.current_pct == 90.0


# --- 6. classes.yaml was the last non-atomic write -----------------------

def test_save_classes_leaves_no_partial_file_when_the_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "classes.yaml"
    config.save_classes(path, [config.ClassConfig(slug="a", name="Statics")])
    good = path.read_text()

    # The real guarantee: a crash mid-write leaves the OLD file, never a
    # truncated hybrid. classes.yaml cannot be regenerated from anything.
    import school_agent.config as cfg

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cfg, "atomic_write_text", boom)
    with pytest.raises(OSError):
        config.save_classes(path, [config.ClassConfig(slug="b", name="Other")])
    assert path.read_text() == good
    assert not list(tmp_path.glob(".classes.yaml.*"))


# --- 4b. migrating uids that the fix itself changed ----------------------

def test_upgrading_rekeys_recurring_rows_instead_of_duplicating_them():
    """The fix in #4 changes what a recurring occurrence's uid IS. Without a
    migration, the first sync after upgrading sees every recurring assignment
    as brand new and keeps the old row beside it forever — the deadline list
    silently doubles."""
    old = [deadlines.Deadline(uid="weekly@d2l", class_slug="c", title="Weekly Quiz",
                              due="2026-09-01T23:59:00+00:00")]
    new = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    merged, _, _ = deadlines.merge_preserving_marks(old, new, set(), set())
    week1 = [d for d in merged if d.due.startswith("2026-09-01") and d.title == "Weekly Quiz"]
    assert len(week1) == 1
    assert week1[0].uid == "weekly@d2l::2026-09-01T23:59:00+00:00"


def test_done_and_cleared_marks_survive_the_rekey():
    old = [deadlines.Deadline(uid="weekly@d2l", class_slug="c", title="Weekly Quiz",
                              due="2026-09-01T23:59:00+00:00")]
    new = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    _, done, dismissed = deadlines.merge_preserving_marks(old, new, {"weekly@d2l"}, {"weekly@d2l"})
    assert done == {"weekly@d2l::2026-09-01T23:59:00+00:00"}
    assert dismissed == {"weekly@d2l::2026-09-01T23:59:00+00:00"}


def test_the_rekey_never_collapses_a_series_onto_one_member():
    # Matching on uid alone would map an old row onto whichever occurrence
    # sorted first, quietly marking the wrong week done.
    old = [deadlines.Deadline(uid="weekly@d2l", class_slug="c", title="Weekly Quiz",
                              due="2026-10-06T23:59:00+00:00")]
    new = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    mapping = deadlines.rekey_map(old, new)
    assert mapping == {"weekly@d2l": "weekly@d2l::2026-10-06T23:59:00+00:00"}


def test_a_one_off_uid_is_left_alone_by_the_migration():
    old = [deadlines.Deadline(uid="oneoff@d2l", class_slug="c", title="Project Proposal",
                              due="2026-09-15T23:59:00+00:00")]
    new = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    assert deadlines.rekey_map(old, new) == {}


def test_merging_still_keeps_history_outside_the_fetch_window():
    old = [deadlines.Deadline(uid="ancient", class_slug="c", title="Exam 1",
                              due="2026-02-10T23:59:00+00:00")]
    new = deadlines.parse_ics(_CAL, "c", today=date(2026, 9, 1))
    merged, _, _ = deadlines.merge_preserving_marks(old, new, set(), set())
    assert any(d.uid == "ancient" for d in merged)
