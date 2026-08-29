import io
import sys
from datetime import timedelta

from school_agent import localtime


def _today():
    """The clock the APP reads, not the stdlib's — so these tests stay correct
    under any timezone setting and can't disagree with the server about what
    day it is."""
    return localtime.today_local()


def _soon_iso(days: int = 3) -> str:
    """A due date a few days out, computed from today.

    Pinned dates rot: the briefing has a 21-day overdue floor, so a deadline
    hardcoded in September silently vanishes from it in October and the test
    fails for a reason that has nothing to do with the code."""
    return (_today() + timedelta(days=days)).isoformat() + "T23:59:00+00:00"
from pathlib import Path

import pytest

import server as ui_server
from school_agent import deadlines as deadlines_mod
from school_agent.deadlines import Deadline
from school_agent.llm import LLMNotConfiguredError


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(ui_server, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ui_server, "CONFIG_PATH", tmp_path / "config" / "classes.yaml")
    # Swap in a copy of the environment so settings endpoints (which mutate
    # os.environ) can't leak between tests or read this machine's real env.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    from school_agent.env_settings import MANAGED_KEYS

    for k in MANAGED_KEYS:
        os.environ.pop(k, None)
    ui_server.app.config["TESTING"] = True
    with ui_server.app.test_client() as c:
        yield c


def test_index_serves_dashboard_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"School Agent" in resp.data


def test_status_with_no_classes(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.get_json()["class_count"] == 0


def test_add_class_without_feed_url(client):
    resp = client.post("/api/classes", json={"name": "CS 401", "term": "Fall 2026"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["slug"] == "cs-401"
    assert body["events_found"] is None

    listed = client.get("/api/classes").get_json()
    assert [c["slug"] for c in listed] == ["cs-401"]


def test_add_class_requires_name(client):
    resp = client.post("/api/classes", json={"term": "Fall 2026"})
    assert resp.status_code == 400


def test_add_class_validates_feed_url_before_saving(client, monkeypatch):
    def fake_fetch(url, class_slug, course_filter=None):
        raise ValueError("connection refused")

    monkeypatch.setattr(ui_server.deadlines, "fetch_deadlines", fake_fetch)

    resp = client.post("/api/classes", json={"name": "CS 401", "ics_feed_url": "https://bad.example/feed.ics"})
    assert resp.status_code == 400
    assert "connection refused" in resp.get_json()["error"]
    assert client.get("/api/classes").get_json() == []  # never saved


def test_add_class_with_valid_feed_url_pulls_deadlines_immediately(client, monkeypatch):
    def fake_fetch(url, class_slug, course_filter=None):
        return [Deadline(uid="1", class_slug=class_slug, title="Quiz 1", due=_soon_iso())]

    monkeypatch.setattr(ui_server.deadlines, "fetch_deadlines", fake_fetch)

    resp = client.post("/api/classes", json={"name": "CS 401", "ics_feed_url": "https://good.example/feed.ics"})
    assert resp.status_code == 200
    assert resp.get_json()["events_found"] == 1

    listed = client.get("/api/deadlines").get_json()
    assert len(listed) == 1
    assert listed[0]["title"] == "Quiz 1"


def test_add_class_with_course_filter_scopes_shared_feed(client, monkeypatch):
    all_events = [
        Deadline(uid="1", class_slug="x", title="[COURSE.102] HW3", due=_soon_iso()),
        Deadline(uid="2", class_slug="x", title="Quiz 2", due="2026-09-12T14:00:00+00:00", description="MATH 219"),
    ]

    def fake_fetch(url, class_slug, course_filter=None):
        if course_filter:
            return [d for d in all_events if course_filter.lower() in (d.title + d.description).lower()]
        return all_events

    monkeypatch.setattr(ui_server.deadlines, "fetch_deadlines", fake_fetch)

    resp = client.post(
        "/api/classes",
        json={"name": "Statics", "ics_feed_url": "https://shared.example/all.ics", "course_filter": "COURSE.102"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["events_found"] == 1

    listed = client.get("/api/deadlines").get_json()
    assert len(listed) == 1
    assert listed[0]["title"] == "[COURSE.102] HW3"


def test_materials_upload_and_list(client):
    client.post("/api/classes", json={"name": "CS 401"})
    data = {"file": (io.BytesIO(b"recursion basics and big-o notation"), "notes.txt")}
    resp = client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)
    assert resp.status_code == 200
    entries = resp.get_json()
    assert entries[0]["filename"] == "notes.txt"
    assert entries[0]["extracted"] is True

    listed = client.get("/api/materials/cs-401").get_json()
    assert len(listed) == 1


def test_materials_upload_unknown_class_404s(client):
    data = {"file": (io.BytesIO(b"x"), "notes.txt")}
    resp = client.post("/api/materials/nope", content_type="multipart/form-data", data=data)
    assert resp.status_code == 404


def test_materials_paste_text_directly(client):
    client.post("/api/classes", json={"name": "CS 401"})
    resp = client.post(
        "/api/materials/cs-401/paste",
        json={"title": "Week 3 notes", "text": "recursion basics and big-o notation"},
    )
    assert resp.status_code == 200
    entries = resp.get_json()
    assert entries[0]["filename"] == "week-3-notes.txt"
    assert entries[0]["extracted"] is True

    listed = client.get("/api/materials/cs-401").get_json()
    assert len(listed) == 1


def test_materials_paste_requires_text(client):
    client.post("/api/classes", json={"name": "CS 401"})
    resp = client.post("/api/materials/cs-401/paste", json={"title": "empty"})
    assert resp.status_code == 400


def test_materials_paste_unknown_class_404s(client):
    resp = client.post("/api/materials/nope/paste", json={"text": "something"})
    assert resp.status_code == 404


# --- 2026-08-25: document deletion, added with the tabbed-classes redesign ---

def test_materials_delete_removes_file_and_reindexes(client):
    client.post("/api/classes", json={"name": "CS 401"})
    for name in ("keep.txt", "remove.txt"):
        data = {"file": (io.BytesIO(b"some real content"), name)}
        client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)
    assert len(client.get("/api/materials/cs-401").get_json()) == 2

    resp = client.post("/api/materials/cs-401/delete", json={"relpath": "remove.txt"})
    assert resp.status_code == 200
    remaining = resp.get_json()
    assert [e["filename"] for e in remaining] == ["keep.txt"]


def test_materials_delete_missing_file_404s_without_side_effects(client):
    client.post("/api/classes", json={"name": "CS 401"})
    resp = client.post("/api/materials/cs-401/delete", json={"relpath": "never-existed.txt"})
    assert resp.status_code == 404


def test_materials_delete_rejects_path_traversal(client, tmp_path):
    client.post("/api/classes", json={"name": "CS 401"})
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must survive", encoding="utf-8")
    resp = client.post("/api/materials/cs-401/delete", json={"relpath": "../../outside-secret.txt"})
    assert resp.status_code == 404
    assert outside.exists()


def test_materials_delete_unknown_class_404s(client):
    resp = client.post("/api/materials/nope/delete", json={"relpath": "x.txt"})
    assert resp.status_code == 404


def test_quiz_generate_requires_llm_configured(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    data = {"file": (io.BytesIO(b"recursion is a function calling itself"), "notes.txt")}
    client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)

    def raise_not_configured(prompt, context):
        raise LLMNotConfiguredError("no provider configured")

    monkeypatch.setattr(ui_server, "default_llm_fn", raise_not_configured)
    resp = client.post("/api/quiz/generate", json={"class_slug": "cs-401"})
    assert resp.status_code == 400
    assert "no provider configured" in resp.get_json()["error"]


def test_quiz_generate_and_review_roundtrip(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    data = {"file": (io.BytesIO(b"recursion is a function calling itself"), "notes.txt")}
    client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)

    def fake_llm(prompt, context):
        return "Q: What is recursion?\nA: A function calling itself"

    monkeypatch.setattr(ui_server, "default_llm_fn", fake_llm)
    gen_resp = client.post("/api/quiz/generate", json={"class_slug": "cs-401"})
    assert gen_resp.status_code == 200
    assert gen_resp.get_json()["added"] == 1

    due = client.get("/api/quiz/due").get_json()
    assert len(due) == 1
    card_id = due[0]["card_id"]

    review_resp = client.post("/api/quiz/review", json={"class_slug": "cs-401", "card_id": card_id, "rating": "good"})
    assert review_resp.status_code == 200

    due_again = client.get("/api/quiz/due", query_string={"class": "cs-401"}).get_json()
    assert due_again == []  # just reviewed, no longer due


def test_quiz_review_unknown_class_404s(client):
    resp = client.post("/api/quiz/review", json={"class_slug": "nope", "card_id": "1", "rating": "good"})
    assert resp.status_code == 404


def test_draft_requires_llm_and_returns_tagged_content(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})

    def fake_llm(prompt, context):
        return "Here is my essay about recursion."

    monkeypatch.setattr(ui_server, "default_llm_fn", fake_llm)
    resp = client.post("/api/draft", json={"class_slug": "cs-401", "assignment_slug": "essay-1", "prompt": "Write about recursion"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["content"].startswith("## DRAFT")
    assert Path(body["path"]).exists()


def test_getahead_summary_is_honest_when_no_material_ingested(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    from school_agent.config import ClassConfig, save_classes

    # Relative to today, not a pinned date: "upcoming topics" is defined
    # against the real clock, so a hardcoded date turns this test into a
    # time bomb that fails for anyone cloning the repo weeks later.
    soon = (_today() + timedelta(days=2)).isoformat()
    save_classes(
        ui_server.CONFIG_PATH,
        [ClassConfig(slug="cs-401", name="CS 401", topics=[[soon, "Recursion"]])],
    )
    topics_resp = client.get("/api/getahead/cs-401")
    assert topics_resp.status_code == 200
    assert topics_resp.get_json() == [{"date": soon, "topic": "Recursion"}]

    def fake_llm(prompt, context):
        return "should not be called — no material ingested"

    monkeypatch.setattr(ui_server, "default_llm_fn", fake_llm)
    summary_resp = client.post("/api/getahead/cs-401/summarize", json={"topic": "Recursion"})
    assert summary_resp.status_code == 200
    assert "No material on file covers" in summary_resp.get_json()["summary"]


def test_getahead_summary_uses_ingested_material(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    from school_agent.config import ClassConfig, save_classes

    save_classes(
        ui_server.CONFIG_PATH,
        [ClassConfig(slug="cs-401", name="CS 401", topics=[["2026-08-26", "Recursion"]])],
    )
    data = {"file": (io.BytesIO(b"Recursion is when a function calls itself with a base case."), "notes.txt")}
    client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)

    def fake_llm(prompt, context):
        assert "base case" in context
        return "Recursion is when a function calls itself."

    monkeypatch.setattr(ui_server, "default_llm_fn", fake_llm)
    summary_resp = client.post("/api/getahead/cs-401/summarize", json={"topic": "Recursion"})
    assert summary_resp.status_code == 200
    assert "calls itself" in summary_resp.get_json()["summary"]


# --- 2026-08-26: clear-from-board (Brightspace items that never resolve) ---

def _add_class_with_deadline(client, monkeypatch):
    # localtime.now_local, not datetime.now(utc): the briefing computes
    # "overdue" and "due this week" from the app's clock, so a helper reading
    # a different one puts these deadlines in whichever bucket the offset
    # between the two happens to produce.
    now = localtime.now_local()
    overdue_iso = (now - timedelta(days=5)).isoformat()
    upcoming_iso = (now + timedelta(days=4)).isoformat()

    def fake_fetch(url, class_slug, course_filter=None):
        return [
            Deadline(uid="stuck-1", class_slug=class_slug, title="Academic Honesty Form", due=overdue_iso),
            Deadline(uid="real-1", class_slug=class_slug, title="HW 1", due=upcoming_iso),
        ]

    monkeypatch.setattr(ui_server.deadlines, "fetch_deadlines", fake_fetch)
    client.post("/api/classes", json={"name": "CS 401", "ics_feed_url": "https://good.example/feed.ics"})


def test_dismiss_marks_deadline_and_restore_undoes_it(client, monkeypatch):
    _add_class_with_deadline(client, monkeypatch)

    resp = client.post("/api/deadlines/dismiss", json={"class_slug": "cs-401", "uid": "stuck-1"})
    assert resp.status_code == 200
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["dismissed"] is True
    assert by_uid["real-1"]["dismissed"] is False

    client.post("/api/deadlines/restore", json={"class_slug": "cs-401", "uid": "stuck-1"})
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["dismissed"] is False


def test_dismissal_survives_a_resync(client, monkeypatch):
    _add_class_with_deadline(client, monkeypatch)
    client.post("/api/deadlines/dismiss", json={"class_slug": "cs-401", "uid": "stuck-1"})
    client.post("/api/sync")  # sync overwrites deadlines.json wholesale
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["dismissed"] is True  # dismissal lives in its own file


def test_dismissed_deadline_excluded_from_briefing(client, monkeypatch):
    _add_class_with_deadline(client, monkeypatch)
    client.post("/api/deadlines/dismiss", json={"class_slug": "cs-401", "uid": "stuck-1"})
    monkeypatch.setattr(ui_server, "default_llm_fn", None)
    gen = client.post("/api/briefing/generate").get_json()
    assert "Academic Honesty Form" not in gen["content"]
    assert "HW 1" in gen["content"]


def test_dismiss_unknown_class_404s(client):
    resp = client.post("/api/deadlines/dismiss", json={"class_slug": "nope", "uid": "x"})
    assert resp.status_code == 404


# --- 2026-08-26: completion, card lifecycle, error surfacing, draft safety ---

def test_marking_done_clears_it_from_overdue_and_the_briefing(client, monkeypatch):
    _add_class_with_deadline(client, monkeypatch)
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["done"] is False

    assert client.post("/api/deadlines/done", json={"class_slug": "cs-401", "uid": "stuck-1"}).status_code == 200
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["done"] is True

    monkeypatch.setattr(ui_server, "default_llm_fn", None)
    content = client.post("/api/briefing/generate").get_json()["content"]
    assert "Academic Honesty Form" not in content  # no longer nags about finished work
    assert "Finished and checked off" in content  # counted as progress instead


def test_done_can_be_unchecked(client, monkeypatch):
    _add_class_with_deadline(client, monkeypatch)
    client.post("/api/deadlines/done", json={"class_slug": "cs-401", "uid": "stuck-1"})
    client.post("/api/deadlines/done", json={"class_slug": "cs-401", "uid": "stuck-1", "done": False})
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["done"] is False


def test_done_survives_a_resync(client, monkeypatch):
    _add_class_with_deadline(client, monkeypatch)
    client.post("/api/deadlines/done", json={"class_slug": "cs-401", "uid": "stuck-1"})
    client.post("/api/sync")
    by_uid = {d["uid"]: d for d in client.get("/api/deadlines").get_json()}
    assert by_uid["stuck-1"]["done"] is True


def test_quiz_generation_does_not_duplicate_existing_cards(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    data = {"file": (io.BytesIO(b"Entropy never decreases in an isolated system."), "notes.txt")}
    client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "Q: What is entropy?\nA: A measure of disorder")

    first = client.post("/api/quiz/generate", json={"class_slug": "cs-401"}).get_json()
    assert first["added"] == 1

    second = client.post("/api/quiz/generate", json={"class_slug": "cs-401"}).get_json()
    assert second["added"] == 0
    assert second["skipped"] == 1
    assert len(client.get("/api/quiz/deck/cs-401").get_json()) == 1


def test_quiz_card_can_be_deleted_and_edited(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    data = {"file": (io.BytesIO(b"Entropy never decreases in an isolated system."), "notes.txt")}
    client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "Q: Bad question?\nA: Wrong answer")
    client.post("/api/quiz/generate", json={"class_slug": "cs-401"})

    deck = client.get("/api/quiz/deck/cs-401").get_json()
    card_id = deck[0]["card_id"]

    patched = client.patch("/api/quiz/card", json={"class_slug": "cs-401", "card_id": card_id, "answer": "Corrected answer"})
    assert patched.status_code == 200
    assert client.get("/api/quiz/deck/cs-401").get_json()[0]["answer"] == "Corrected answer"

    assert client.delete("/api/quiz/card", json={"class_slug": "cs-401", "card_id": card_id}).status_code == 200
    assert client.get("/api/quiz/deck/cs-401").get_json() == []


def test_quiz_card_delete_unknown_id_404s(client):
    client.post("/api/classes", json={"name": "CS 401"})
    resp = client.delete("/api/quiz/card", json={"class_slug": "cs-401", "card_id": "nope"})
    assert resp.status_code == 404


def test_provider_failure_surfaces_a_real_message_not_request_failed_500(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    data = {"file": (io.BytesIO(b"real course content about entropy"), "notes.txt")}
    client.post("/api/materials/cs-401", content_type="multipart/form-data", data=data)

    def bad_key(prompt, context):
        raise RuntimeError("HTTP 401: incorrect api key provided")

    monkeypatch.setattr(ui_server, "default_llm_fn", bad_key)
    resp = client.post("/api/quiz/generate", json={"class_slug": "cs-401"})
    assert resp.status_code >= 400
    assert resp.is_json
    assert "incorrect api key" in resp.get_json()["error"]


def test_drafts_do_not_overwrite_each_other(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "First version")
    first = client.post("/api/draft", json={"class_slug": "cs-401", "prompt": "write something"}).get_json()
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "Second version")
    second = client.post("/api/draft", json={"class_slug": "cs-401", "prompt": "write something else"}).get_json()

    assert first["path"] != second["path"]
    listed = client.get("/api/drafts/cs-401").get_json()
    assert len(listed) == 2  # the first draft was not clobbered

    bodies = {d["name"]: client.get(f"/api/drafts/cs-401/{d['name']}").get_json()["content"] for d in listed}
    assert any("First version" in b for b in bodies.values())
    assert any("Second version" in b for b in bodies.values())


def test_draft_read_rejects_path_traversal(client):
    client.post("/api/classes", json={"name": "CS 401"})
    assert client.get("/api/drafts/cs-401/..%2f..%2fsecret").status_code == 404


def test_malformed_classes_yaml_gives_a_banner_not_a_blank_page(client):
    (ui_server.REPO_ROOT / "config").mkdir(parents=True, exist_ok=True)
    ui_server.CONFIG_PATH.write_text("classes:\n\t- bad: tab\n", encoding="utf-8")
    resp = client.get("/api/status")
    assert resp.status_code == 200  # the dashboard still renders
    body = resp.get_json()
    assert body["class_count"] == 0
    assert body["config_error"]
    assert "classes.yaml" in body["config_error"]


def test_cross_origin_state_change_is_refused(client):
    resp = client.post(
        "/api/settings",
        json={"openai_base_url": "http://evil.example/v1"},
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403
    assert "OPENAI_BASE_URL" not in (ui_server.REPO_ROOT / ".env").read_text(encoding="utf-8") if (ui_server.REPO_ROOT / ".env").exists() else True


def test_same_origin_state_change_still_works(client):
    resp = client.post(
        "/api/classes",
        json={"name": "CS 401"},
        headers={"Origin": "http://127.0.0.1:5057", "Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200


# --- 2026-08-26: grade ledger and focus sessions ---

def _scheme_body():
    return {
        "components": [
            {"name": "Homework", "weight_pct": 20, "count": 10, "drop_lowest": 1},
            {"name": "Midterm", "weight_pct": 30, "count": 1},
            {"name": "Final", "weight_pct": 50, "count": 1},
        ],
        "confirmed": True,
    }


def test_grades_empty_before_a_scheme_exists(client):
    client.post("/api/classes", json={"name": "CS 401"})
    body = client.get("/api/grades/cs-401").get_json()
    assert body["summary"]["has_data"] is False
    assert body["scheme"]["components"] == []
    assert body["targets"] == []


def test_save_scheme_then_add_scores_computes_a_current_grade(client):
    client.post("/api/classes", json={"name": "CS 401"})
    client.post("/api/grades/cs-401/scheme", json=_scheme_body())
    body = client.post("/api/grades/cs-401/scores", json={
        "component": "Midterm", "name": "Midterm 1", "earned": 84, "possible": 100,
    }).get_json()

    assert body["summary"]["current_pct"] == 84.0
    assert body["summary"]["graded_weight"] == 30.0
    assert body["summary"]["remaining_weight"] == 70.0


def test_targets_tell_you_what_you_need_on_whats_left(client):
    client.post("/api/classes", json={"name": "CS 401"})
    client.post("/api/grades/cs-401/scheme", json=_scheme_body())
    client.post("/api/grades/cs-401/scores", json={
        "component": "Midterm", "name": "Midterm 1", "earned": 84, "possible": 100,
    })
    targets = {t["letter"]: t for t in client.get("/api/grades/cs-401").get_json()["targets"]}
    # earned 25.2 of 100; an A- (90) needs (90-25.2)/70 = 92.6% on the rest.
    assert targets["A-"]["needed_pct"] == pytest.approx(92.6, abs=0.2)
    assert targets["A-"]["possible"] is True


def test_score_can_be_deleted_by_index(client):
    client.post("/api/classes", json={"name": "CS 401"})
    client.post("/api/grades/cs-401/scheme", json=_scheme_body())
    client.post("/api/grades/cs-401/scores", json={"component": "Midterm", "name": "M1", "earned": 50, "possible": 100})
    body = client.post("/api/grades/cs-401/scores", json={"delete_index": 0}).get_json()
    assert body["scores"] == []


def test_score_rejects_zero_points_possible(client):
    client.post("/api/classes", json={"name": "CS 401"})
    resp = client.post("/api/grades/cs-401/scores", json={"component": "Midterm", "earned": 5, "possible": 0})
    assert resp.status_code == 400


def test_deadlines_carry_grade_impact_once_a_scheme_exists(client, monkeypatch):
    def fake_fetch(url, class_slug, course_filter=None):
        return [Deadline(uid="hw3", class_slug=class_slug, title="HW 3 - Due", due=_soon_iso())]

    monkeypatch.setattr(ui_server.deadlines, "fetch_deadlines", fake_fetch)
    client.post("/api/classes", json={"name": "CS 401", "ics_feed_url": "https://good.example/f.ics"})

    assert client.get("/api/deadlines").get_json()[0]["impact"] is None  # nothing to say yet

    client.post("/api/grades/cs-401/scheme", json=_scheme_body())
    impact = client.get("/api/deadlines").get_json()[0]["impact"]
    assert impact["component"] == "Homework"  # "HW 3" matched to the syllabus category
    assert impact["item_weight"] == pytest.approx(20 / 9, abs=0.01)


def test_extract_scheme_from_the_uploaded_syllabus(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    client.post("/api/materials/cs-401/paste", json={
        "title": "syllabus",
        "text": "GRADING: Homework 20% over 10 sets, lowest dropped. Midterm exam 30%. Final exam 50%.",
    })
    monkeypatch.setattr(
        ui_server, "default_llm_fn",
        lambda p, c: '{"components":[{"name":"Homework","weight_pct":20,"count":10,"drop_lowest":1},{"name":"Midterm","weight_pct":30,"count":1},{"name":"Final","weight_pct":50,"count":1}]}',
    )
    body = client.post("/api/grades/cs-401/extract").get_json()
    assert [c["name"] for c in body["scheme"]["components"]] == ["Homework", "Midterm", "Final"]
    assert body["scheme"]["confirmed"] is False  # a proposal, never auto-trusted
    assert body["summary"]["scheme_total_weight"] == 100.0


def test_extract_scheme_without_a_syllabus_explains_itself(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "{}")
    resp = client.post("/api/grades/cs-401/extract")
    assert resp.status_code == 400
    assert "syllabus" in resp.get_json()["error"].lower()


def test_focus_session_is_recorded_and_summarized(client):
    client.post("/api/classes", json={"name": "CS 401"})
    body = client.post("/api/sessions", json={
        "class_slug": "cs-401", "kind": "review", "label": "Review cards", "items": 12, "minutes": 25,
    }).get_json()
    assert body["sessions"] == 1
    assert body["minutes"] == 25.0
    assert body["items"] == 12
    assert body["per_class"]["CS 401"] == 25.0


# --- 2026-08-25: scheduled study briefings ---

def test_briefing_get_before_any_generation(client):
    resp = client.get("/api/briefing")
    assert resp.status_code == 200
    assert resp.get_json()["exists"] is False


def test_briefing_generate_requires_a_class(client):
    resp = client.post("/api/briefing/generate")
    assert resp.status_code == 400


def test_briefing_generate_and_read_back(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "Briefing: all clear.")
    gen = client.post("/api/briefing/generate")
    assert gen.status_code == 200
    assert gen.get_json()["content"] == "Briefing: all clear."

    got = client.get("/api/briefing").get_json()
    assert got["exists"] is True
    assert got["content"] == "Briefing: all clear."
    assert got["used_model"] is True


# --- 2026-08-25: in-app model settings (key + model choice from the UI) ---

def test_settings_get_reports_unconfigured_state(client, monkeypatch):
    monkeypatch.setattr(ui_server, "_ollama_quickcheck", lambda: False)
    monkeypatch.setattr(ui_server.llm, "_ollama_reachable", lambda url: False)
    s = client.get("/api/settings").get_json()
    assert s["active_provider"] == "none"
    assert s["preferred"] == "auto"
    assert s["configured"] == []
    for name in ("openai", "anthropic", "gemini", "ollama"):
        assert name in s["providers"]
    assert s["providers"]["openai"]["key_set"] is False


@pytest.mark.parametrize(
    "provider,env_var",
    [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"), ("gemini", "GEMINI_API_KEY")],
)
def test_each_provider_key_saves_and_is_never_echoed_back(client, monkeypatch, provider, env_var):
    monkeypatch.setattr(ui_server.llm, "_ollama_reachable", lambda url: False)
    secret = f"sk-{provider}-secret-12345678"
    resp = client.post("/api/settings", json={f"{provider}_api_key": secret})
    assert resp.status_code == 200

    s = resp.get_json()
    assert s["providers"][provider]["key_set"] is True
    assert s["providers"][provider]["key_hint"] == "…5678"
    assert s["active_provider"] == provider
    assert secret not in resp.get_data(as_text=True)

    assert f"{env_var}={secret}" in (ui_server.REPO_ROOT / ".env").read_text(encoding="utf-8")
    import os

    assert os.environ[env_var] == secret


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_each_provider_key_can_be_cleared(client, monkeypatch, provider):
    monkeypatch.setattr(ui_server.llm, "_ollama_reachable", lambda url: False)
    client.post("/api/settings", json={f"{provider}_api_key": "sk-something-12345678"})
    s = client.post("/api/settings", json={f"clear_{provider}_key": True}).get_json()
    assert s["providers"][provider]["key_set"] is False
    assert s["active_provider"] == "none"


def test_auto_selection_prefers_openai_then_anthropic_then_gemini(client, monkeypatch):
    monkeypatch.setattr(ui_server.llm, "_ollama_reachable", lambda url: False)
    client.post("/api/settings", json={"gemini_api_key": "g-key-12345678"})
    assert client.get("/api/settings").get_json()["active_provider"] == "gemini"

    client.post("/api/settings", json={"anthropic_api_key": "a-key-12345678"})
    assert client.get("/api/settings").get_json()["active_provider"] == "anthropic"

    client.post("/api/settings", json={"openai_api_key": "o-key-12345678"})
    s = client.get("/api/settings").get_json()
    assert s["active_provider"] == "openai"
    assert set(s["configured"]) == {"openai", "anthropic", "gemini"}


def test_explicit_provider_choice_overrides_auto_order(client, monkeypatch):
    monkeypatch.setattr(ui_server.llm, "_ollama_reachable", lambda url: False)
    client.post("/api/settings", json={"openai_api_key": "o-key-12345678"})
    client.post("/api/settings", json={"gemini_api_key": "g-key-12345678"})

    s = client.post("/api/settings", json={"preferred": "gemini"}).get_json()
    assert s["preferred"] == "gemini"
    assert s["active_provider"] == "gemini"  # not openai, despite auto order


def test_explicit_provider_without_its_key_errors_instead_of_falling_back(client, monkeypatch):
    monkeypatch.setattr(ui_server.llm, "_ollama_reachable", lambda url: False)
    client.post("/api/settings", json={"openai_api_key": "o-key-12345678"})
    client.post("/api/settings", json={"preferred": "anthropic"})

    assert client.get("/api/settings").get_json()["active_provider"] == "none"
    with pytest.raises(LLMNotConfiguredError, match="Claude"):
        ui_server.llm.default_llm_fn("hi", "")


def test_provider_models_are_settable_and_apply_immediately(client):
    client.post("/api/settings", json={
        "anthropic_model": "claude-opus-5",
        "gemini_model": "gemini-3.7-flash",
    })
    assert ui_server.llm.model_for("anthropic") == "claude-opus-5"
    assert ui_server.llm.model_for("gemini") == "gemini-3.7-flash"
    env = (ui_server.REPO_ROOT / ".env").read_text(encoding="utf-8")
    assert "SCHOOL_AGENT_ANTHROPIC_MODEL=claude-opus-5" in env
    assert "SCHOOL_AGENT_GEMINI_MODEL=gemini-3.7-flash" in env


def test_settings_preserves_unmanaged_env_lines(client):
    env_path = ui_server.REPO_ROOT / ".env"
    env_path.write_text("# my comment\nSOME_OTHER_TOOL_FLAG=1\nOPENAI_API_KEY=old-key-value\n", encoding="utf-8")
    client.post("/api/settings", json={"openai_api_key": "sk-new-key-87654321"})
    env_text = env_path.read_text(encoding="utf-8")
    assert "# my comment" in env_text
    assert "SOME_OTHER_TOOL_FLAG=1" in env_text
    assert "OPENAI_API_KEY=sk-new-key-87654321" in env_text
    assert "old-key-value" not in env_text


def test_settings_test_endpoint_reports_success_and_failure(client, monkeypatch):
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "OK")
    ok = client.post("/api/settings/test")
    assert ok.status_code == 200
    assert ok.get_json()["ok"] is True

    def boom(p, c):
        raise LLMNotConfiguredError("nothing configured")

    monkeypatch.setattr(ui_server, "default_llm_fn", boom)
    bad = client.post("/api/settings/test")
    assert bad.status_code == 400
    assert bad.get_json()["ok"] is False


def test_activity_feed_reflects_recent_actions(client):
    client.post("/api/classes", json={"name": "CS 401"})
    activity = client.get("/api/activity").get_json()
    assert any("CS 401" in line for line in activity)


# --- 2026-08-26: briefing check-offs ---

def test_briefing_line_can_be_checked_and_unchecked(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "# Today\n- Work on HW 4\n- Review 12 cards")
    client.post("/api/briefing/generate")

    r = client.post("/api/briefing/check", json={"line": "- Work on HW 4", "checked": True})
    assert r.status_code == 200
    assert "work on hw 4" in r.get_json()["checked"]

    got = client.get("/api/briefing").get_json()
    assert "work on hw 4" in got["checked"]
    assert "review 12 cards" not in got["checked"]

    r = client.post("/api/briefing/check", json={"line": "- Work on HW 4", "checked": False})
    assert "work on hw 4" not in r.get_json()["checked"]


def test_briefing_check_requires_a_line(client):
    assert client.post("/api/briefing/check", json={"checked": True}).status_code == 400


def test_briefing_checks_persist_across_regeneration(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "# Today\n- Work on HW 4")
    client.post("/api/briefing/generate")
    client.post("/api/briefing/check", json={"line": "- Work on HW 4", "checked": True})

    regenerated = client.post("/api/briefing/generate").get_json()
    assert "work on hw 4" in regenerated["checked"]


# --- 2026-08-26: chat interface ---

def test_chat_send_creates_a_conversation_and_persists_it(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "Here is an explanation.")

    convo = client.post("/api/chat", json={"text": "explain recursion"}).get_json()
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["title"] == "explain recursion"

    listed = client.get("/api/chat").get_json()
    assert len(listed) == 1
    assert client.get("/api/chat/" + convo["id"]).get_json()["id"] == convo["id"]


def test_chat_mention_puts_real_course_material_in_front_of_the_model(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    client.post("/api/materials/cs-401/paste", json={
        "title": "notes", "text": "Tail recursion reuses the same stack frame, avoiding overflow.",
    })
    seen = {}

    def fake_llm(prompt, context):
        seen["context"] = context
        return "answer"

    monkeypatch.setattr(ui_server, "default_llm_fn", fake_llm)
    client.post("/api/chat", json={
        "text": "explain tail recursion",
        "mentions": [{"type": "class", "slug": "cs-401", "name": "CS 401"}],
    })
    assert "same stack frame" in seen["context"]


def test_chat_mentionable_offers_classes_and_documents(client):
    client.post("/api/classes", json={"name": "CS 401"})
    client.post("/api/materials/cs-401/paste", json={"title": "notes", "text": "real readable content here"})
    items = client.get("/api/chat/mentionable").get_json()
    assert {"type": "class", "slug": "cs-401", "name": "CS 401", "detail": "class"} in items
    assert any(i["type"] == "doc" and i["name"] == "notes.txt" for i in items)


def test_chat_follow_up_continues_the_same_conversation(client, monkeypatch):
    client.post("/api/classes", json={"name": "CS 401"})
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "reply")
    first = client.post("/api/chat", json={"text": "first"}).get_json()
    second = client.post("/api/chat", json={"conversation_id": first["id"], "text": "second"}).get_json()
    assert second["id"] == first["id"]
    assert len(second["messages"]) == 4
    assert len(client.get("/api/chat").get_json()) == 1


def test_chat_requires_text_and_a_configured_model(client, monkeypatch):
    assert client.post("/api/chat", json={"text": "   "}).status_code == 400

    def not_configured(p, c):
        raise LLMNotConfiguredError("nothing configured")

    monkeypatch.setattr(ui_server, "default_llm_fn", not_configured)
    resp = client.post("/api/chat", json={"text": "hello"})
    assert resp.status_code == 400
    assert "nothing configured" in resp.get_json()["error"]


def test_chat_conversation_can_be_deleted(client, monkeypatch):
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "reply")
    convo = client.post("/api/chat", json={"text": "hello"}).get_json()
    assert client.delete("/api/chat/" + convo["id"]).status_code == 200
    assert client.get("/api/chat").get_json() == []
    assert client.delete("/api/chat/" + convo["id"]).status_code == 404


def test_chat_file_upload_and_use_in_a_conversation(client, monkeypatch):
    data = {"file": (io.BytesIO(b"The beam deflection formula is PL^3 over 3EI."), "beam.txt")}
    resp = client.post("/api/chat/upload", content_type="multipart/form-data", data=data)
    assert resp.status_code == 200
    entry = resp.get_json()
    assert entry["type"] == "upload" and entry["extracted"] is True

    seen = {}

    def fake_llm(prompt, context):
        seen["context"] = context
        return "answer"

    monkeypatch.setattr(ui_server, "default_llm_fn", fake_llm)
    client.post("/api/chat", json={"text": "what is the deflection formula?", "mentions": [entry]})
    assert "PL^3" in seen["context"]

    assert any(u["name"] == "beam.txt" for u in client.get("/api/chat/uploads").get_json())
    assert any(m["type"] == "upload" for m in client.get("/api/chat/mentionable").get_json())


def test_chat_upload_requires_a_file(client):
    assert client.post("/api/chat/upload", content_type="multipart/form-data", data={}).status_code == 400


def test_chat_upload_can_be_deleted(client):
    data = {"file": (io.BytesIO(b"disposable content"), "temp.txt")}
    client.post("/api/chat/upload", content_type="multipart/form-data", data=data)
    assert client.delete("/api/chat/upload/temp.txt").status_code == 200
    assert client.get("/api/chat/uploads").get_json() == []
    assert client.delete("/api/chat/upload/temp.txt").status_code == 404


# --- study modes (2026-08-26) --------------------------------------------

def _class_with_docs(client, name="Thermo", slug="thermo"):
    client.post("/api/classes", json={"name": name})
    body = "\n".join(f"Entropy generation section {k} control volume analysis. " * 8 for k in range(30))
    client.post(f"/api/materials/{slug}/paste", json={"title": "Chapter 7", "text": body})
    return slug


def test_study_modes_are_listed_with_guidance(client):
    rows = client.get("/api/study/modes").get_json()
    assert [r["key"] for r in rows][0] == "recall"
    assert all(r["when"] and r["blurb"] for r in rows)


def test_study_state_recommends_and_explains(client):
    slug = _class_with_docs(client)
    d = client.get(f"/api/study/{slug}").get_json()
    assert d["recommended"] in {m["key"] for m in client.get("/api/study/modes").get_json()}
    assert d["reason"]
    assert d["active_mode"] == d["recommended"]
    assert d["state"]["has_material"] is True


def test_study_state_404s_on_an_unknown_class(client):
    assert client.get("/api/study/nope").status_code == 404


def test_pinning_a_mode_overrides_the_recommendation(client):
    slug = _class_with_docs(client)
    assert client.post(f"/api/study/{slug}/pin", json={"mode": "guided"}).get_json()["pinned_mode"] == "guided"
    d = client.get(f"/api/study/{slug}").get_json()
    assert d["active_mode"] == "guided"
    client.post(f"/api/study/{slug}/pin", json={"mode": ""})
    d2 = client.get(f"/api/study/{slug}").get_json()
    assert d2["active_mode"] == d2["recommended"]


def test_pinning_nonsense_is_a_400(client):
    slug = _class_with_docs(client)
    assert client.post(f"/api/study/{slug}/pin", json={"mode": "telepathy"}).status_code == 400


def test_starting_a_session_without_a_provider_is_a_clear_400(client, monkeypatch):
    slug = _class_with_docs(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", _boom_no_provider)
    resp = client.post(f"/api/study/{slug}/start", json={"mode": "worked", "topic": "entropy"})
    assert resp.status_code == 400
    assert "provider" in resp.get_json()["error"].lower()


def _boom_no_provider(prompt, context):
    raise LLMNotConfiguredError("No model provider is configured.")


_WORKED_JSON = (
    '{"title":"T","problem":"p","given":["g"],'
    '"steps":[{"action":"a","why":"w"}],"answer":"x","key_idea":"k","common_mistake":"m"}'
)


def test_a_started_session_is_stored_and_reopenable(client, monkeypatch):
    slug = _class_with_docs(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _WORKED_JSON)
    sess = client.post(f"/api/study/{slug}/start", json={"mode": "worked", "topic": "entropy"}).get_json()
    assert sess["payload"]["key_idea"] == "k"

    listed = client.get(f"/api/study/{slug}").get_json()["sessions"]
    assert listed[0]["session_id"] == sess["session_id"]
    # The list is deliberately payload-free — it is a picker, not the content.
    assert "payload" not in listed[0]

    reopened = client.get(f"/api/study/{slug}/session/{sess['session_id']}").get_json()
    assert reopened["payload"]["problem"] == "p"

    assert client.delete(f"/api/study/{slug}/session/{sess['session_id']}").status_code == 200
    assert client.get(f"/api/study/{slug}").get_json()["sessions"] == []


def test_reopening_a_session_that_does_not_exist_is_a_404(client):
    slug = _class_with_docs(client)
    assert client.get(f"/api/study/{slug}/session/deadbeef").status_code == 404


def test_a_model_reply_of_the_wrong_shape_is_a_400_not_a_500(client, monkeypatch):
    slug = _class_with_docs(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: "here you go!")
    resp = client.post(f"/api/study/{slug}/start", json={"mode": "worked", "topic": "entropy"})
    assert resp.status_code == 400


# --- orphaned scores -----------------------------------------------------

def test_orphaned_scores_are_surfaced_and_remappable(client):
    from school_agent import grades, paths

    client.post("/api/classes", json={"name": "Statics"})
    slug = "statics"
    grades.save_scheme(
        paths.grading_path(ui_server.REPO_ROOT, slug),
        grades.GradingScheme(components=[grades.Component(name="Problem Sets", weight_pct=100, count=10)]),
    )
    grades.save_scores(
        paths.scores_path(ui_server.REPO_ROOT, slug),
        [grades.Score(component="Homework", name=f"HW {i}", earned=9, possible=10) for i in range(1, 7)],
    )
    payload = client.get(f"/api/grades/{slug}").get_json()
    assert payload["summary"]["orphaned"][0]["component"] == "Homework"
    assert payload["summary"]["current_pct"] is None  # they really are not counting

    assert client.post(f"/api/grades/{slug}/reassign",
                       json={"from": "Homework", "to": "Problem Sets"}).status_code == 200
    fixed = client.get(f"/api/grades/{slug}").get_json()
    assert fixed["summary"]["orphaned"] == []
    assert fixed["summary"]["current_pct"] == 90.0


def test_reassign_needs_both_ends(client):
    client.post("/api/classes", json={"name": "Statics"})
    assert client.post("/api/grades/statics/reassign", json={"from": "Homework"}).status_code == 400


# --- struggle ladder (2026-08-26) ----------------------------------------

_LADDER_GEN = (
    '{"problem":"A 3 m beam pinned at A carries 400 N at B. Find the reaction at A.",'
    '"given":["L = 3 m"],"shown":[{"action":"Draw the FBD","why":"fixes the forces"}],'
    '"blanks":["Take moments about A"],"solution":[{"action":"Sum M_A = 0","why":"drops the pin"}],'
    '"answer":"400 N up","principle":"Equilibrium of moments","watch_out":"sign convention"}'
)


def _ladder_class(client):
    client.post("/api/classes", json={"name": "Statics"})
    body = "\n".join("Moments about a point. Counterclockwise positive. Method of joints. " * 8 for _ in range(30))
    client.post("/api/materials/statics/paste", json={"title": "Ch 5", "text": body})
    return "statics"


def test_ladder_rungs_are_listed(client):
    rows = client.get("/api/ladder/rungs").get_json()
    assert [r["key"] for r in rows][0] == "worked"
    assert rows[-1]["key"] == "solo"
    assert rows[0]["submits"] is False  # the bottom rung is reading


def test_starting_a_ladder_returns_a_problem_and_a_position(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    l = client.post(f"/api/ladder/{slug}", json={"struggle": "I keep dropping the sign on moment arms"}).get_json()
    assert l["progress"]["rung_index"] == 0
    assert l["current"]["problem"]
    assert l["struggle"] == "I keep dropping the sign on moment arms"
    assert client.get(f"/api/ladder/{slug}").get_json()[0]["ladder_id"] == l["ladder_id"]


def test_a_vague_struggle_is_a_400_with_an_example(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    resp = client.post(f"/api/ladder/{slug}", json={"struggle": ""})
    assert resp.status_code == 400
    assert "own words" in resp.get_json()["error"]


def test_starting_a_ladder_without_a_provider_is_a_clear_400(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", _boom_no_provider)
    resp = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"})
    assert resp.status_code == 400
    assert "provider" in resp.get_json()["error"].lower()


def test_working_a_ladder_up_a_rung_over_http(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    l = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"}).get_json()
    lid = l["ladder_id"]

    # rung 0 is reading — no answer, no grading call
    r = client.post(f"/api/ladder/{slug}/{lid}/attempt", json={}).get_json()
    assert r["progress"]["rung_index"] == 1

    client.post(f"/api/ladder/{slug}/{lid}/next")
    monkeypatch.setattr(
        ui_server, "default_llm_fn",
        lambda p, c: '{"verdict":"correct","summary":"good","went_wrong":"","right_move":""}',
    )
    r = client.post(f"/api/ladder/{slug}/{lid}/attempt", json={"answer": "400 N"}).get_json()
    assert r["outcome"]["moved"] == "up"
    assert r["progress"]["rung_index"] == 2


def test_peeking_is_reported_and_does_not_advance(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    lid = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"}).get_json()["ladder_id"]
    client.post(f"/api/ladder/{slug}/{lid}/attempt", json={})
    client.post(f"/api/ladder/{slug}/{lid}/next")
    monkeypatch.setattr(
        ui_server, "default_llm_fn",
        lambda p, c: '{"verdict":"correct","summary":"good","went_wrong":"","right_move":""}',
    )
    r = client.post(f"/api/ladder/{slug}/{lid}/attempt",
                    json={"answer": "400 N", "used_solution": True}).get_json()
    assert r["outcome"]["moved"] == "stay"
    assert r["progress"]["rung_index"] == 1


def test_remarking_an_attempt_over_http(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    lid = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"}).get_json()["ladder_id"]
    client.post(f"/api/ladder/{slug}/{lid}/attempt", json={})
    assert client.post(f"/api/ladder/{slug}/{lid}/remark", json={"verdict": "wrong"}
                       ).get_json()["progress"]["rung_index"] == 0
    assert client.post(f"/api/ladder/{slug}/{lid}/remark", json={"verdict": "sideways"}).status_code == 400


def test_ladder_routes_404_on_unknown_ids(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    assert client.post(f"/api/ladder/{slug}/nope/next").status_code == 404
    assert client.post(f"/api/ladder/{slug}/nope/attempt", json={"answer": "x"}).status_code == 404
    assert client.post("/api/ladder/nosuchclass", json={"struggle": "x"}).status_code == 404


def test_a_finished_ladder_can_become_a_flashcard(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    lid = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"}).get_json()["ladder_id"]
    body = {"question": "Sign convention for moments?", "answer": "Counterclockwise positive."}
    assert client.post(f"/api/ladder/{slug}/{lid}/card", json=body).get_json()["added"] == 1
    # ...and it does not duplicate on a second click
    assert client.post(f"/api/ladder/{slug}/{lid}/card", json=body).get_json()["added"] == 0
    assert any(c["question"].startswith("Sign convention")
               for c in client.get(f"/api/quiz/deck/{slug}").get_json())


def test_a_half_filled_card_is_rejected(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    lid = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"}).get_json()["ladder_id"]
    assert client.post(f"/api/ladder/{slug}/{lid}/card", json={"question": "q"}).status_code == 400


def test_deleting_a_ladder(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    lid = client.post(f"/api/ladder/{slug}", json={"struggle": "signs"}).get_json()["ladder_id"]
    assert client.delete(f"/api/ladder/{slug}/{lid}").status_code == 200
    assert client.get(f"/api/ladder/{slug}").get_json() == []


def test_an_open_ladder_steers_the_study_recommendation(client, monkeypatch):
    slug = _ladder_class(client)
    monkeypatch.setattr(ui_server, "default_llm_fn", lambda p, c: _LADDER_GEN)
    client.post(f"/api/ladder/{slug}", json={"struggle": "sign on moment arms"})
    d = client.get(f"/api/study/{slug}").get_json()
    assert d["recommended"] == "ladder"
    assert "moment arms" in d["reason"]
    assert d["state"]["open_ladder"] == "sign on moment arms"


# --- security hardening, 2026-08-26 review --------------------------------

def test_a_rebound_hostname_cannot_read_this_server(client):
    """DNS rebinding: a page at evil.com repoints that name to 127.0.0.1, so
    the browser thinks it is same-origin with evil.com and the Origin checks
    never fire. GET was fully exempt, which leaked chats, drafts, deadlines
    and the API key's last four characters."""
    client.post("/api/classes", json={"name": "Thermo"})
    assert client.get("/api/settings", headers={"Host": "evil.com"}).status_code == 403
    assert client.get("/api/chat", headers={"Host": "evil.com:5057"}).status_code == 403
    # ...and localhost still works, including with a port.
    assert client.get("/api/settings", headers={"Host": "127.0.0.1:5057"}).status_code == 200
    assert client.get("/api/settings", headers={"Host": "localhost"}).status_code == 200


def test_a_calendar_feed_must_be_http(client, monkeypatch):
    """urllib opens file:// happily, the URL is typed into a box, and the
    contents came back in the on-screen validation error — then got re-read
    every 30 minutes by the background sync."""
    resp = client.post("/api/classes", json={"name": "X", "ics_feed_url": "file:///etc/passwd"})
    assert resp.status_code >= 400
    assert "http" in resp.get_json()["error"].lower()
