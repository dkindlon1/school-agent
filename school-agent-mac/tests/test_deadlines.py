from datetime import date

import pytest

from school_agent import deadlines
from school_agent.deadlines import (
    diff_deadlines,
    load_deadlines,
    parse_ics,
    save_deadlines,
)

SAMPLE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//D2L//Brightspace//EN
BEGIN:VEVENT
UID:evt-1@mycourses.example.edu
SUMMARY:Problem Set 1 Due
DTSTART:20260910T235900Z
DESCRIPTION:Submit via MyCourses dropbox
END:VEVENT
BEGIN:VEVENT
UID:evt-2@mycourses.example.edu
SUMMARY:Midterm Exam
DTSTART:20261015T140000Z
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_extracts_events_sorted_by_due():
    events = parse_ics(SAMPLE_ICS, class_slug="cs401", today=date(2026, 8, 25))
    assert [e.title for e in events] == ["Problem Set 1 Due", "Midterm Exam"]
    assert events[0].class_slug == "cs401"
    assert events[0].description == "Submit via MyCourses dropbox"
    assert events[0].due.startswith("2026-09-10")


# --- 2026-08-25: deep links straight to the assignment/quiz, not just the calendar ---

LINKED_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//D2L//Brightspace//EN
BEGIN:VEVENT
UID:evt-linked@mycourses.example.edu
SUMMARY:Academic Honesty Form - Due
DTSTART:20260829T035959Z
DESCRIPTION:Assignments:\\nAcademic Honesty Form - https://mycourses.example.edu/d2l/lms/dropbox/user/folder_submit_files.d2l?ou=100200&db=300400\\n\\n\\nView event - https://mycourses.example.edu/d2l/le/calendar/100200/event/500600/detailsview?ou=100200#500600
END:VEVENT
BEGIN:VEVENT
UID:evt-nolink@mycourses.example.edu
SUMMARY:Plain reminder, no link
DTSTART:20260901T000000Z
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_extracts_direct_assignment_link_over_view_event_link():
    events = parse_ics(LINKED_ICS, class_slug="mece103", today=date(2026, 8, 25))
    honesty = next(e for e in events if "Academic Honesty" in e.title)
    assert honesty.link == "https://mycourses.example.edu/d2l/lms/dropbox/user/folder_submit_files.d2l?ou=100200&db=300400"


def test_parse_ics_link_defaults_to_empty_when_none_present():
    events = parse_ics(LINKED_ICS, class_slug="mece103", today=date(2026, 8, 25))
    reminder = next(e for e in events if "Plain reminder" in e.title)
    assert reminder.link == ""


# --- 2026-08-25: course_filter, for sharing one feed URL across classes ---

MULTI_COURSE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//D2L//Brightspace//EN
BEGIN:VEVENT
UID:course-hw3@mycourses.example.edu
SUMMARY:[COURSE.102.01] Homework 3 Due
DTSTART:20260910T235900Z
CATEGORIES:COURSE.102.01
END:VEVENT
BEGIN:VEVENT
UID:math-quiz2@mycourses.example.edu
SUMMARY:Quiz 2
DTSTART:20260912T140000Z
DESCRIPTION:Covers chapter 4 - MATH 219 Multivariable Calculus
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_with_no_filter_returns_every_course():
    events = parse_ics(MULTI_COURSE_ICS, class_slug="shared", today=date(2026, 8, 25))
    assert len(events) == 2


def test_course_filter_matches_categories_field():
    events = parse_ics(MULTI_COURSE_ICS, class_slug="course", course_filter="COURSE.102", today=date(2026, 8, 25))
    assert [e.title for e in events] == ["[COURSE.102.01] Homework 3 Due"]


def test_course_filter_matches_description_field_case_insensitively():
    events = parse_ics(MULTI_COURSE_ICS, class_slug="math", course_filter="multivariable calculus", today=date(2026, 8, 25))
    assert [e.title for e in events] == ["Quiz 2"]


def test_course_filter_with_no_match_returns_empty_not_an_error():
    events = parse_ics(MULTI_COURSE_ICS, class_slug="nope", course_filter="Nonexistent Course", today=date(2026, 8, 25))
    assert events == []


def test_parse_ics_ignores_events_without_summary_or_dtstart():
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:no-summary@example.edu
DTSTART:20260910T235900Z
END:VEVENT
END:VCALENDAR
"""
    assert parse_ics(ics, class_slug="cs401", today=date(2026, 8, 25)) == []


def test_deadlines_roundtrip_through_json(tmp_path):
    events = parse_ics(SAMPLE_ICS, class_slug="cs401", today=date(2026, 8, 25))
    p = tmp_path / "deadlines.json"
    save_deadlines(p, events)
    reloaded = load_deadlines(p)
    assert reloaded == events


def test_load_deadlines_missing_file_returns_empty(tmp_path):
    assert load_deadlines(tmp_path / "missing.json") == []


def test_diff_deadlines_detects_added_changed_removed():
    old = parse_ics(SAMPLE_ICS, class_slug="cs401", today=date(2026, 8, 25))
    # simulate: problem set date pushed back a day, midterm removed, final added
    new_ics = SAMPLE_ICS.replace(b"20260910T235900Z", b"20260911T235900Z").replace(
        b"""BEGIN:VEVENT
UID:evt-2@mycourses.example.edu
SUMMARY:Midterm Exam
DTSTART:20261015T140000Z
END:VEVENT
""",
        b"""BEGIN:VEVENT
UID:evt-3@mycourses.example.edu
SUMMARY:Final Exam
DTSTART:20261210T140000Z
END:VEVENT
""",
    )
    new = parse_ics(new_ics, class_slug="cs401", today=date(2026, 8, 25))
    diff = diff_deadlines(old, new)
    assert not diff.is_empty()
    assert [d.title for d in diff.added] == ["Final Exam"]
    assert [d.title for d in diff.removed] == ["Midterm Exam"]
    assert [d.title for d in diff.changed] == ["Problem Set 1 Due"]


def test_diff_deadlines_empty_when_nothing_changed():
    events = parse_ics(SAMPLE_ICS, class_slug="cs401", today=date(2026, 8, 25))
    diff = diff_deadlines(events, list(events))
    assert diff.is_empty()


# --- 2026-08-25 fixes: recurring events, cancelled events ---------------

RECURRING_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//D2L//Brightspace//EN
BEGIN:VEVENT
UID:evt-recur@mycourses.example.edu
SUMMARY:Weekly Quiz
DTSTART:20260901T140000Z
RRULE:FREQ=WEEKLY;COUNT=4
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_expands_recurring_events_into_every_occurrence():
    events = parse_ics(RECURRING_ICS, class_slug="cs401", today=date(2026, 8, 25))
    assert len(events) == 4
    assert all(e.title == "Weekly Quiz" for e in events)
    due_dates = sorted(e.due[:10] for e in events)
    assert due_dates == ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22"]


def test_recurring_occurrences_get_distinct_uids():
    events = parse_ics(RECURRING_ICS, class_slug="cs401", today=date(2026, 8, 25))
    assert len({e.uid for e in events}) == 4  # would have been 1 before the fix


def test_recurring_occurrences_survive_diff_as_separate_deadlines():
    events = parse_ics(RECURRING_ICS, class_slug="cs401", today=date(2026, 8, 25))
    diff = diff_deadlines([], events)
    assert len(diff.added) == 4


CANCELLED_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:evt-live@mycourses.example.edu
SUMMARY:Quiz 2
DTSTART:20260905T140000Z
END:VEVENT
BEGIN:VEVENT
UID:evt-cancelled@mycourses.example.edu
SUMMARY:Quiz 3 (cancelled)
DTSTART:20260912T140000Z
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_filters_out_cancelled_events():
    events = parse_ics(CANCELLED_ICS, class_slug="cs401", today=date(2026, 8, 25))
    assert [e.title for e in events] == ["Quiz 2"]


def test_load_deadlines_survives_corrupt_json(tmp_path):
    p = tmp_path / "deadlines.json"
    p.write_text("not valid json at all", encoding="utf-8")
    assert load_deadlines(p) == []
    assert not p.exists()  # quarantined, not left in a broken state


def test_a_runaway_repeating_event_cannot_wedge_the_sync():
    """FREQ=SECONDLY expands to ~34 million occurrences across the window and
    hangs the sync thread — which then runs again 30 minutes later. Nothing in
    a course calendar recurs sub-daily."""
    import time
    cal = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        b"BEGIN:VEVENT\r\nUID:runaway@x\r\nDTSTART:20260901T000000Z\r\n"
        b"RRULE:FREQ=SECONDLY\r\nSUMMARY:Runaway\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:real@x\r\nDTSTART:20260910T235900Z\r\n"
        b"SUMMARY:Problem Set 2\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    t0 = time.monotonic()
    rows = deadlines.parse_ics(cal, "c", today=date(2026, 9, 1))
    assert time.monotonic() - t0 < 10, "sub-daily rule was expanded"
    assert any(d.title == "Problem Set 2" for d in rows), "the real event must survive"
    assert len(rows) <= deadlines.MAX_OCCURRENCES


def test_a_non_http_feed_url_is_refused():
    for bad in ("file:///etc/passwd", "ftp://x/y.ics", "gopher://x"):
        with pytest.raises(ValueError, match="http"):
            deadlines.default_fetcher(bad)
