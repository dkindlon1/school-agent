#!/usr/bin/env python3
"""The dashboard — the primary interface, replacing raw CLI scripts as the
thing the owner actually touches. Added 2026-08-25 after a UX review found
that "run three scripts by hand, catch notifications by watching a
terminal" was a strictly worse experience than the Claude Projects workflow
this venture was built to replace. Same pattern as the sibling ventures
: a local Flask server + one
single-file HTML page, nothing to deploy, nothing but this repo and a
browser pointed at localhost.

Starting this process IS the setup step for automation — see scheduler.py:
opening the dashboard starts the background deadline-sync loop for as long
as it stays running. There is no separate cron/Task Scheduler entry to
configure.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, request, send_from_directory

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")  # picks up OPENAI_API_KEY/OLLAMA_BASE_URL/etc. — v1 documented
# .env but nothing ever loaded it into the process; a review caught this.

from school_agent import (  # noqa: E402
    briefing, chat, deadlines, env_settings, getahead, grades, llm, materials, paths, quiz, scheduler,
    ladder, localtime, sessions, study,
)
from school_agent.config import (  # noqa: E402
    add_class,
    get_class,
    load_classes_or_empty,
)
from school_agent.draft import generate_draft, list_drafts, save_draft  # noqa: E402
from school_agent.llm import LLMNotConfiguredError, default_llm_fn  # noqa: E402
from school_agent.notify import notify, recent_messages  # noqa: E402
from school_agent.quiz import CardStore, generate_cards_from_materials  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "classes.yaml"
HOST = "127.0.0.1"
PORT = 5057

app = Flask(__name__, static_folder=None)


@app.errorhandler(Exception)
def _json_errors(exc):
    """Every generation route used to catch only LLMNotConfiguredError, so a
    bad API key or an unpulled Ollama model reached the browser as an HTML 500
    page and the dashboard rendered the literal string "request failed (500)".
    The real reason was already in the exception — it just never got out."""
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return jsonify({"error": exc.description}), exc.code
    return jsonify({"error": str(exc) or exc.__class__.__name__}), 500


@app.before_request
def _same_origin_only():
    """This server holds an API key and course material on localhost with no
    auth. Without this, any page the owner happens to be browsing could POST
    to /api/settings (Flask's force=True parses regardless of content type)
    and repoint the model endpoint at an attacker, exfiltrating the key and
    every document on the next generation call. Cheap to close, so close it."""
    # The Host check runs on EVERY method, GET included. Without it a page at
    # evil.com can DNS-rebind that name to 127.0.0.1 and then read this server
    # cross-origin — the browser believes it is talking to evil.com, so the
    # Origin checks below never fire. Reads alone leak the chat history, every
    # draft, the deadline list and the API key's last four characters, which
    # is why exempting GET was not safe.
    host = (request.headers.get("Host") or "").split(":")[0].strip("[]").lower()
    if host and host not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"error": "this server only answers to localhost"}), 403

    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    site = request.headers.get("Sec-Fetch-Site")
    if site and site != "same-origin":
        return jsonify({"error": "cross-origin requests are not allowed"}), 403
    origin = request.headers.get("Origin")
    if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
        return jsonify({"error": "cross-origin requests are not allowed"}), 403
    return None


_config_error: str | None = None


def _classes():
    """A malformed classes.yaml used to raise straight through /api/status,
    which the dashboard never catches — so one stray tab in the file the app
    itself tells you to hand-edit produced a completely blank page with no
    explanation. Now it degrades to "no classes" plus a banner."""
    global _config_error
    try:
        classes = load_classes_or_empty(CONFIG_PATH)
        _config_error = None
        return classes
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner, never fatal
        _config_error = f"{CONFIG_PATH.name}: {exc}"
        return []


def _class_or_404(slug: str):
    try:
        return get_class(_classes(), slug)
    except KeyError:
        return None


# ---------------------------------------------------------------- static --

@app.route("/")
def index():
    return send_from_directory(Path(__file__).parent, "dashboard.html")


# ------------------------------------------------------------ status/sync --

@app.route("/api/status")
def api_status():
    # Was hardcoded to OPENAI_API_KEY, which would have reported "no model"
    # for someone running purely on a Claude or Gemini key.
    llm_configured = llm.active_provider() is not None
    classes = _classes()
    return jsonify(
        {
            "classes": [c.slug for c in classes],
            "class_count": len(classes),
            "llm_configured": llm_configured,
            "activity": recent_messages(10),
            "config_error": _config_error,
        }
    )


def _ollama_quickcheck() -> bool:
    return llm._ollama_reachable(llm._ollama_base_url())


def _key_hint(value: str) -> str:
    """Enough to recognize which key is loaded, never enough to use it."""
    return ("…" + value[-4:]) if len(value) >= 8 else ("set" if value else "")


@app.route("/api/sync", methods=["POST"])
def api_sync():
    classes = _classes()
    scheduler.pull_all_deadlines(REPO_ROOT, classes)
    return jsonify({"ok": True, "synced_classes": [c.slug for c in classes]})


# -------------------------------------------------------------- classes --

@app.route("/api/classes", methods=["GET"])
def api_classes():
    return jsonify(
        [
            {"slug": c.slug, "name": c.name, "term": c.term, "instructor": c.instructor, "has_feed": bool(c.ics_feed_url)}
            for c in _classes()
        ]
    )


@app.route("/api/classes", methods=["POST"])
def api_add_class():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Class name is required."}), 400
    ics_feed_url = (body.get("ics_feed_url") or "").strip() or None
    course_filter = (body.get("course_filter") or "").strip() or None

    events_found = None
    if ics_feed_url:
        try:
            events = deadlines.fetch_deadlines(ics_feed_url, "validation-check", course_filter=course_filter)
            events_found = len(events)
        except Exception as exc:  # noqa: BLE001 - surfaced to the owner as a validation error, not a 500
            return jsonify({"error": f"Couldn't read that calendar feed: {exc}"}), 400

    new_class = add_class(
        CONFIG_PATH,
        name=name,
        term=(body.get("term") or "").strip(),
        instructor=(body.get("instructor") or "").strip(),
        ics_feed_url=ics_feed_url,
        course_filter=course_filter,
    )
    paths.ensure_class_dirs(REPO_ROOT, new_class.slug)
    if ics_feed_url:
        scheduler.pull_all_deadlines(REPO_ROOT, [new_class])
    notify(f"Added class: {new_class.name}", channel="console")
    return jsonify({"slug": new_class.slug, "events_found": events_found})


# ------------------------------------------------------------- deadlines --

@app.route("/api/deadlines")
def api_deadlines():
    # LOCAL time, via localtime.days_until — not UTC. This is the main board;
    # computing it in UTC here is what made everything due tonight render as
    # OVERDUE from 8pm Eastern onward, every single evening. Fixing it in
    # briefing.py alone left the bug live on the screen you actually look at.
    now = localtime.now_local()
    out = []
    for c in _classes():
        dismissed = deadlines.load_dismissed(paths.dismissed_path(REPO_ROOT, c.slug))
        done = deadlines.load_done(paths.done_path(REPO_ROOT, c.slug))
        scheme = grades.load_scheme(paths.grading_path(REPO_ROOT, c.slug))
        for d in deadlines.load_deadlines(paths.deadlines_path(REPO_ROOT, c.slug)):
            days_until = localtime.days_until(d.due, now)
            out.append(
                {
                    "uid": d.uid,
                    "class_slug": c.slug,
                    "class_name": c.name,
                    "title": d.title,
                    "due": d.due,
                    "description": d.description,
                    "link": d.link,
                    "days_until": days_until,
                    "dismissed": d.uid in dismissed,
                    "done": d.uid in done,
                    # What this is actually worth — the difference between
                    # "3 things due" and "do the 20% one first".
                    "impact": grades.deadline_impact(scheme, d.title),
                }
            )
    out.sort(key=lambda x: x["due"])
    return jsonify(out)


@app.route("/api/deadlines/done", methods=["POST"])
def api_mark_done():
    body = request.get_json(force=True) or {}
    slug, uid = body.get("class_slug"), body.get("uid")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    if not uid:
        return jsonify({"error": "uid is required"}), 400
    deadlines.set_done(paths.done_path(REPO_ROOT, slug), uid, bool(body.get("done", True)))
    return jsonify({"ok": True})


@app.route("/api/deadlines/dismiss", methods=["POST"])
def api_dismiss_deadline():
    body = request.get_json(force=True) or {}
    slug, uid = body.get("class_slug"), body.get("uid")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    if not uid:
        return jsonify({"error": "uid is required"}), 400
    deadlines.dismiss_deadline(paths.dismissed_path(REPO_ROOT, slug), uid)
    return jsonify({"ok": True})


@app.route("/api/deadlines/restore", methods=["POST"])
def api_restore_deadline():
    body = request.get_json(force=True) or {}
    slug, uid = body.get("class_slug"), body.get("uid")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    if not uid:
        return jsonify({"error": "uid is required"}), 400
    deadlines.restore_deadline(paths.dismissed_path(REPO_ROOT, slug), uid)
    return jsonify({"ok": True})


# ------------------------------------------------------------- materials --

@app.route("/api/materials/<slug>", methods=["GET"])
def api_materials(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    entries = materials.load_index(paths.materials_index_path(REPO_ROOT, slug))
    return jsonify([e.to_dict() for e in entries])


@app.route("/api/materials/<slug>", methods=["POST"])
def api_upload_material(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    if "file" not in request.files:
        return jsonify({"error": "no file in request"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400

    paths.ensure_class_dirs(REPO_ROOT, slug)
    mdir = paths.materials_dir(REPO_ROOT, slug)
    dest = mdir / Path(f.filename).name  # .name strips any path components — no traversal
    f.save(dest)

    index_path = paths.materials_index_path(REPO_ROOT, slug)
    entries = materials.reindex(mdir, materials.load_index(index_path))
    materials.save_index(index_path, entries)
    notify(f"{c.name}: ingested {f.filename} ({len(entries)} material(s) now indexed)")
    return jsonify([e.to_dict() for e in entries])


@app.route("/api/materials/<slug>/paste", methods=["POST"])
def api_paste_material(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "paste some text first"}), 400
    title = (body.get("title") or "").strip()

    paths.ensure_class_dirs(REPO_ROOT, slug)
    mdir = paths.materials_dir(REPO_ROOT, slug)
    dest = materials.save_pasted_text(mdir, title, text)

    index_path = paths.materials_index_path(REPO_ROOT, slug)
    entries = materials.reindex(mdir, materials.load_index(index_path))
    materials.save_index(index_path, entries)
    notify(f"{c.name}: saved pasted note {dest.name} ({len(entries)} material(s) now indexed)")
    return jsonify([e.to_dict() for e in entries])


@app.route("/api/materials/<slug>/delete", methods=["POST"])
def api_delete_material(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    relpath = (body.get("relpath") or "").strip()
    if not relpath:
        return jsonify({"error": "relpath is required"}), 400

    mdir = paths.materials_dir(REPO_ROOT, slug)
    if not materials.delete_material(mdir, relpath):
        return jsonify({"error": "file not found"}), 404

    index_path = paths.materials_index_path(REPO_ROOT, slug)
    entries = materials.reindex(mdir, materials.load_index(index_path))
    materials.save_index(index_path, entries)
    notify(f"{c.name}: deleted {Path(relpath).name}", channel="console")
    return jsonify([e.to_dict() for e in entries])


# ------------------------------------------------------------------ quiz --

@app.route("/api/quiz/due")
def api_quiz_due():
    class_filter = request.args.get("class")
    out = []
    for c in _classes():
        if class_filter and c.slug != class_filter:
            continue
        store = CardStore(paths.cards_path(REPO_ROOT, c.slug))
        for card in store.due_cards():
            out.append({"card_id": card.card_id, "class_slug": c.slug, "class_name": c.name, "question": card.question, "answer": card.answer})
    return jsonify(out)


@app.route("/api/quiz/review", methods=["POST"])
def api_quiz_review():
    body = request.get_json(force=True) or {}
    slug, card_id, rating = body.get("class_slug"), body.get("card_id"), body.get("rating")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    store = CardStore(paths.cards_path(REPO_ROOT, slug))
    try:
        updated = store.review(card_id, rating)
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    store.save()
    return jsonify({"card_id": updated.card_id, "due": updated.fsrs_state["due"]})


@app.route("/api/quiz/generate", methods=["POST"])
def api_quiz_generate():
    body = request.get_json(force=True) or {}
    slug = body.get("class_slug")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    entries = materials.load_index(paths.materials_index_path(REPO_ROOT, slug))
    topic = (body.get("topic") or "").strip() or None
    try:
        pairs = generate_cards_from_materials(
            slug,
            entries,
            default_llm_fn,
            materials_dir=paths.materials_dir(REPO_ROOT, slug),
            topic=topic,
        )
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    if not pairs:
        return jsonify({"added": 0, "message": "No usable material to generate questions from yet — upload a reading or your notes for this class first."})

    store = CardStore(paths.cards_path(REPO_ROOT, slug))
    added, skipped = 0, 0
    for q, a in pairs:
        if store.has_question(q, class_slug=slug):
            skipped += 1  # generation used to duplicate the whole deck on every click
            continue
        store.add_card(slug, q, a)
        added += 1
    store.save()
    if added:
        notify(f"{c.name}: generated {added} new quiz question(s)", channel="console")
    msg = None
    if not added and skipped:
        msg = f"All {skipped} generated question(s) were already in your deck — add more material for new ones."
    return jsonify({"added": added, "skipped": skipped, "message": msg})


@app.route("/api/quiz/card", methods=["DELETE", "PATCH"])
def api_quiz_card():
    """Without these, a hallucinated card was permanent — and FSRS made that
    worse, scheduling it MORE often each time the owner rated it "Again"."""
    body = request.get_json(force=True) or {}
    slug, card_id = body.get("class_slug"), body.get("card_id")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    store = CardStore(paths.cards_path(REPO_ROOT, slug))
    try:
        if request.method == "DELETE":
            store.delete_card(card_id)
        else:
            store.edit_card(card_id, question=body.get("question"), answer=body.get("answer"))
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    store.save()
    return jsonify({"ok": True})


@app.route("/api/quiz/deck/<slug>")
def api_quiz_deck(slug):
    """The full deck, not just what's due — there was previously no screen
    anywhere that showed every card, so a bad one couldn't even be found."""
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    store = CardStore(paths.cards_path(REPO_ROOT, slug))
    due_ids = {card.card_id for card in store.due_cards(class_slug=slug)}
    return jsonify([
        {"card_id": card.card_id, "question": card.question, "answer": card.answer, "due": card.card_id in due_ids}
        for card in store.all_cards(slug)
    ])


# ------------------------------------------------------------ study modes --
# Flashcards train recognition. Most of an engineering grade is production —
# so the app now has six kinds of study session and a deterministic
# recommendation that reads your real state to pick one. See study.py for
# what each mode is for and why it is chosen when it is.

@app.route("/api/study/modes")
def api_study_modes():
    return jsonify(study.list_modes())


@app.route("/api/study/<slug>")
def api_study_state(slug):
    """What to do next in this class, and everything the picker needs to
    render — recommendation, per-class override, and past sessions."""
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    rec = study.recommend_for_class(REPO_ROOT, slug)
    pinned = study.load_prefs(REPO_ROOT).get(slug) or ""
    return jsonify({
        "recommended": rec["mode"],
        "reason": rec["reason"],
        "then": rec["then"],
        "state": rec["state"],
        "pinned_mode": pinned,
        # The mode actually opened: an explicit pin always wins over the
        # heuristic, because you know your own courses better than it does.
        "active_mode": pinned or rec["mode"],
        "sessions": [
            {k: v for k, v in row.items() if k != "payload"}
            for row in study.load_sessions(REPO_ROOT, slug)[:12]
        ],
    })


@app.route("/api/study/<slug>/pin", methods=["POST"])
def api_study_pin(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    try:
        study.set_default_mode(REPO_ROOT, slug, (body.get("mode") or "").strip())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "pinned_mode": study.load_prefs(REPO_ROOT).get(slug) or ""})


@app.route("/api/study/<slug>/start", methods=["POST"])
def api_study_start(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    try:
        session = study.start_session(
            REPO_ROOT,
            slug,
            (body.get("mode") or "").strip(),
            body.get("topic") or "",
            default_llm_fn,
            student_input=body.get("student_input") or "",
        )
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    notify(f"{c.name}: {study.MODES[session.mode].label} session on {session.topic}", channel="console")
    return jsonify(session.to_dict())


@app.route("/api/study/<slug>/session/<session_id>", methods=["GET", "DELETE"])
def api_study_session(slug, session_id):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    if request.method == "DELETE":
        study.delete_session(REPO_ROOT, slug, session_id)
        return jsonify({"ok": True})
    for row in study.load_sessions(REPO_ROOT, slug):
        if row["session_id"] == session_id:
            return jsonify(row)
    return jsonify({"error": "no such session"}), 404


@app.route("/api/grades/<slug>/reassign", methods=["POST"])
def api_grades_reassign(slug):
    """Renaming a grading component used to silently stop six graded
    assignments from counting. Now they are surfaced as orphaned and moved
    with one click instead of re-entered."""
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    src, dst = (body.get("from") or "").strip(), (body.get("to") or "").strip()
    if not src or not dst:
        return jsonify({"error": "need both a source and a destination component"}), 400
    spath = paths.scores_path(REPO_ROOT, slug)
    moved = grades.reassign_component(grades.load_scores(spath), src, dst)
    grades.save_scores(spath, moved)
    return jsonify({"ok": True})


# --------------------------------------------------------- struggle ladder --
# "I struggle with X" -> a progression of generated problems whose support
# fades one piece at a time until you are solving cold. See ladder.py for the
# rules and why they are what they are.

@app.route("/api/ladder/rungs")
def api_ladder_rungs():
    return jsonify(ladder.rungs_for_ui())


@app.route("/api/ladder/<slug>")
def api_ladders(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    rows = ladder.load_ladders(REPO_ROOT, slug)
    return jsonify([{**l.to_dict(), "progress": ladder.progress(l)} for l in rows])


@app.route("/api/ladder/<slug>", methods=["POST"])
def api_ladder_start(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    try:
        l = ladder.start(REPO_ROOT, slug, body.get("struggle") or "", default_llm_fn)
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    notify(f"{c.name}: started a practice ladder on “{l.struggle}”", channel="console")
    return jsonify({**l.to_dict(), "progress": ladder.progress(l)})


@app.route("/api/ladder/<slug>/<ladder_id>", methods=["DELETE"])
def api_ladder_delete(slug, ladder_id):
    if _class_or_404(slug) is None:
        return jsonify({"error": "unknown class"}), 404
    ladder.delete_ladder(REPO_ROOT, slug, ladder_id)
    return jsonify({"ok": True})


@app.route("/api/ladder/<slug>/<ladder_id>/next", methods=["POST"])
def api_ladder_next(slug, ladder_id):
    """A fresh problem at the current rung — also the escape hatch for a
    generated problem that is broken, which must never cost a rung."""
    if _class_or_404(slug) is None:
        return jsonify({"error": "unknown class"}), 404
    try:
        l = ladder.next_problem(REPO_ROOT, slug, ladder_id, default_llm_fn)
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    except KeyError:
        return jsonify({"error": "no such ladder"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({**l.to_dict(), "progress": ladder.progress(l)})


@app.route("/api/ladder/<slug>/<ladder_id>/attempt", methods=["POST"])
def api_ladder_attempt(slug, ladder_id):
    if _class_or_404(slug) is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    try:
        result = ladder.attempt(
            REPO_ROOT, slug, ladder_id,
            body.get("answer") or "",
            default_llm_fn,
            used_solution=bool(body.get("used_solution")),
        )
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    except KeyError:
        return jsonify({"error": "no such ladder"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/ladder/<slug>/<ladder_id>/remark", methods=["POST"])
def api_ladder_remark(slug, ladder_id):
    """Disagree with the check. A tutor you can't argue with is one you stop
    using, and the model marking a right answer wrong is not hypothetical."""
    if _class_or_404(slug) is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    try:
        return jsonify(ladder.override_verdict(REPO_ROOT, slug, ladder_id, body.get("verdict") or ""))
    except KeyError:
        return jsonify({"error": "no such ladder"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/ladder/<slug>/<ladder_id>/card", methods=["POST"])
def api_ladder_to_card(slug, ladder_id):
    """Turn what a finished ladder taught you into a flashcard, so the thing
    you just fought for gets scheduled instead of quietly decaying."""
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    question = (body.get("question") or "").strip()
    answer = (body.get("answer") or "").strip()
    if not question or not answer:
        return jsonify({"error": "a card needs both a question and an answer"}), 400
    store = CardStore(paths.cards_path(REPO_ROOT, slug))
    if store.has_question(question, class_slug=slug):
        return jsonify({"added": 0, "message": "That card is already in your deck."})
    store.add_card(slug, question, answer)
    store.save()
    return jsonify({"added": 1})


# --------------------------------------------------------------- getahead --

@app.route("/api/getahead/<slug>")
def api_getahead_topics(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    topics = getahead.upcoming_topics(c, today=localtime.today_local())
    return jsonify([{"date": d.isoformat(), "topic": t} for d, t in topics])


@app.route("/api/getahead/<slug>/summarize", methods=["POST"])
def api_getahead_summarize(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    topic = body.get("topic")
    if not topic:
        return jsonify({"error": "topic is required"}), 400
    entries = materials.load_index(paths.materials_index_path(REPO_ROOT, slug))
    try:
        summary = getahead.summarize_topic(
            topic, entries, default_llm_fn, materials_dir=paths.materials_dir(REPO_ROOT, slug)
        )
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"summary": summary})


# ------------------------------------------------------------------ draft --

@app.route("/api/draft", methods=["POST"])
def api_draft():
    body = request.get_json(force=True) or {}
    slug = body.get("class_slug")
    assignment_slug = (body.get("assignment_slug") or "draft").strip() or "draft"
    prompt = body.get("prompt")
    c = _class_or_404(slug or "")
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400
    entries = materials.load_index(paths.materials_index_path(REPO_ROOT, slug))
    mdir = paths.materials_dir(REPO_ROOT, slug)
    # Context matched to what was actually asked for, rather than every
    # document's first 2,000 characters concatenated into a blob of front matter.
    chunks = materials.relevant_chunks(mdir, entries, prompt, k=6) or materials.sample_chunks(mdir, entries, max_chunks=4)
    context = materials.build_context(chunks)
    try:
        content = generate_draft(prompt, context, default_llm_fn)
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    out_path = save_draft(paths.drafts_dir(REPO_ROOT, slug), assignment_slug, content)
    notify(f"{c.name}: drafted {assignment_slug} — verify AI-use policy before submitting")
    return jsonify({"content": content, "path": str(out_path)})


@app.route("/api/drafts/<slug>")
def api_list_drafts(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    return jsonify(list_drafts(paths.drafts_dir(REPO_ROOT, slug)))


@app.route("/api/drafts/<slug>/<name>")
def api_read_draft(slug, name):
    """Drafts were write-only — generated, shown once, then unreachable if the
    page re-rendered. Reading one back needs the same traversal guard the
    materials delete path uses."""
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    ddir = paths.drafts_dir(REPO_ROOT, slug)
    target = (ddir / f"{Path(name).name}.md").resolve()
    if not target.is_relative_to(ddir.resolve()) or not target.is_file():
        return jsonify({"error": "draft not found"}), 404
    return jsonify({"name": target.stem, "content": target.read_text(encoding="utf-8")})


# ----------------------------------------------------------------- grades --

def _grade_payload(slug: str, c) -> dict:
    scheme = grades.load_scheme(paths.grading_path(REPO_ROOT, slug))
    scores = grades.load_scores(paths.scores_path(REPO_ROOT, slug))
    s = grades.summarize(scheme, scores)
    return {
        "class_slug": slug,
        "class_name": c.name,
        "scheme": scheme.to_dict(),
        "scores": [
            {"component": x.component, "name": x.name, "earned": x.earned, "possible": x.possible, "date": x.date}
            for x in scores
        ],
        "summary": {
            "has_data": s.has_data,
            "current_pct": s.current_pct,
            "current_letter": s.current_letter,
            "graded_weight": s.graded_weight,
            "remaining_weight": s.remaining_weight,
            "scheme_total_weight": s.scheme_total_weight,
            "scheme_confirmed": s.scheme_confirmed,
            # Scores filed under a component the scheme no longer has. These
            # count for nothing, which is correct — doing it silently was not.
            "orphaned": s.orphaned,
            "components": [
                {
                    "name": p.name, "weight_pct": p.weight_pct, "graded_items": p.graded_items,
                    "total_items": p.total_items, "fraction": round(p.fraction * 100, 1),
                    "graded_weight": round(p.graded_weight, 1),
                }
                for p in s.components
            ],
        },
        "targets": grades.targets_table(scheme, scores) if scheme.components else [],
    }


@app.route("/api/grades/<slug>")
def api_grades(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    return jsonify(_grade_payload(slug, c))


@app.route("/api/grades/<slug>/extract", methods=["POST"])
def api_extract_scheme(slug):
    """Reads the grading table out of the syllabus already on file. Returns an
    UNCONFIRMED proposal — a wrong weight produces confident wrong advice,
    which is worse than none, so nothing is trusted until the owner says so."""
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    entries = materials.load_index(paths.materials_index_path(REPO_ROOT, slug))
    try:
        scheme = grades.extract_scheme(paths.materials_dir(REPO_ROOT, slug), entries, default_llm_fn)
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    grades.save_scheme(paths.grading_path(REPO_ROOT, slug), scheme)
    notify(f"{c.name}: grading scheme extracted from syllabus — review and confirm it", channel="console")
    return jsonify(_grade_payload(slug, c))


@app.route("/api/grades/<slug>/scheme", methods=["POST"])
def api_save_scheme(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    scheme = grades.GradingScheme.from_dict(body)
    scheme.confirmed = bool(body.get("confirmed", True))
    grades.save_scheme(paths.grading_path(REPO_ROOT, slug), scheme)
    return jsonify(_grade_payload(slug, c))


@app.route("/api/grades/<slug>/scores", methods=["POST"])
def api_add_score(slug):
    c = _class_or_404(slug)
    if c is None:
        return jsonify({"error": "unknown class"}), 404
    body = request.get_json(force=True) or {}
    path = paths.scores_path(REPO_ROOT, slug)
    scores = grades.load_scores(path)

    if body.get("delete_index") is not None:
        try:
            scores.pop(int(body["delete_index"]))
        except (IndexError, ValueError, TypeError):
            return jsonify({"error": "no score at that position"}), 404
    else:
        try:
            possible = float(body.get("possible") or 0)
            earned = float(body.get("earned") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "earned and possible must be numbers"}), 400
        if possible <= 0:
            return jsonify({"error": "points possible must be greater than zero"}), 400
        scores.append(grades.Score(
            component=(body.get("component") or "").strip(),
            name=(body.get("name") or "").strip() or "item",
            earned=earned, possible=possible,
            date=(body.get("date") or localtime.today_local().isoformat()),
        ))
    grades.save_scores(path, scores)
    return jsonify(_grade_payload(slug, c))


# --------------------------------------------------------------- sessions --

@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    return jsonify(sessions.summary(paths.sessions_path(REPO_ROOT)))


@app.route("/api/sessions", methods=["POST"])
def api_record_session():
    body = request.get_json(force=True) or {}
    slug = body.get("class_slug") or ""
    c = _class_or_404(slug)
    now_iso = datetime.now(timezone.utc).isoformat()
    sessions.record(paths.sessions_path(REPO_ROOT), sessions.Session(
        started_at=str(body.get("started_at") or now_iso),
        ended_at=now_iso,
        class_slug=slug,
        class_name=c.name if c else slug,
        kind=str(body.get("kind") or "review"),
        label=str(body.get("label") or ""),
        items=int(body.get("items") or 0),
        minutes=round(float(body.get("minutes") or 0), 1),
    ))
    return jsonify(sessions.summary(paths.sessions_path(REPO_ROOT)))


# ------------------------------------------------------------------- chat --

@app.route("/api/chat")
def api_chat_list():
    convos = chat.load_all(paths.chats_path(REPO_ROOT))
    return jsonify([
        {"id": c.id, "title": c.title, "updated_at": c.updated_at, "turns": len(c.messages)}
        for c in convos
    ])


@app.route("/api/chat/mentionable")
def api_chat_mentionable():
    return jsonify(chat.mentionable(REPO_ROOT, _classes()))


@app.route("/api/chat/upload", methods=["POST"])
def api_chat_upload():
    """Share a file straight into a conversation, without filing it under a
    class first."""
    if "file" not in request.files:
        return jsonify({"error": "no file in request"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    entry = chat.save_upload(REPO_ROOT, f.filename, f.read())
    if not entry["extracted"]:
        # Honest, and actionable: the file is stored but the model can't read
        # it, which is very different from "upload failed".
        entry["warning"] = (
            f"{entry['name']} was saved but no readable text could be extracted from it — "
            "it's most likely a scanned image. The assistant won't be able to read it."
        )
    return jsonify(entry)


@app.route("/api/chat/uploads")
def api_chat_uploads():
    return jsonify([
        {"type": "upload", "relpath": u.relpath, "name": u.filename,
         "detail": "shared file", "extracted": u.extracted, "char_count": u.char_count}
        for u in chat.uploads(REPO_ROOT)
    ])


@app.route("/api/chat/upload/<path:relpath>", methods=["DELETE"])
def api_chat_upload_delete(relpath):
    if not chat.delete_upload(REPO_ROOT, relpath):
        return jsonify({"error": "file not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/chat/<conv_id>")
def api_chat_get(conv_id):
    convo = chat.find(chat.load_all(paths.chats_path(REPO_ROOT)), conv_id)
    if convo is None:
        return jsonify({"error": "conversation not found"}), 404
    return jsonify(convo.to_dict())


@app.route("/api/chat", methods=["POST"])
def api_chat_send():
    body = request.get_json(force=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "type a message first"}), 400

    path = paths.chats_path(REPO_ROOT)
    convos = chat.load_all(path)
    try:
        convo, convos = chat.send(
            REPO_ROOT, _classes(), convos,
            body.get("conversation_id"), text, body.get("mentions") or [],
            default_llm_fn,
        )
    except LLMNotConfiguredError as exc:
        return jsonify({"error": str(exc)}), 400
    # save_one, not save_all: `convos` was read BEFORE a model call that can
    # take a minute, and writing it wholesale destroys any conversation
    # started in another tab meanwhile and resurrects any deleted one.
    chat.save_one(path, convo)
    return jsonify(convo.to_dict())


@app.route("/api/chat/<conv_id>", methods=["DELETE"])
def api_chat_delete(conv_id):
    path = paths.chats_path(REPO_ROOT)
    if not chat.find(chat.load_all(path), conv_id):
        return jsonify({"error": "conversation not found"}), 404
    chat.delete_one(path, conv_id)
    return jsonify({"ok": True})


# --------------------------------------------------------------- briefing --

@app.route("/api/briefing")
def api_briefing():
    checked = sorted(briefing.load_checks(paths.briefing_checks_path(REPO_ROOT)))
    latest = briefing.load_latest(REPO_ROOT)
    if latest is None:
        return jsonify({"exists": False, "checked": checked})
    return jsonify({"exists": True, "checked": checked, **latest})


@app.route("/api/briefing/check", methods=["POST"])
def api_briefing_check():
    """Tick or untick one briefing line. The client sends the line's text and
    the server derives the key, so both sides can never disagree about how a
    line is normalized."""
    body = request.get_json(force=True) or {}
    line = (body.get("line") or "").strip()
    if not line:
        return jsonify({"error": "line is required"}), 400
    checks = briefing.set_check(
        paths.briefing_checks_path(REPO_ROOT),
        briefing.line_key(line),
        bool(body.get("checked", True)),
    )
    return jsonify({"checked": sorted(checks)})


@app.route("/api/briefing/generate", methods=["POST"])
def api_briefing_generate():
    classes = _classes()
    if not classes:
        return jsonify({"error": "Add a class first — there's nothing to brief on yet."}), 400
    result = briefing.generate_briefing(REPO_ROOT, classes, llm_fn=default_llm_fn)
    checked = sorted(briefing.load_checks(paths.briefing_checks_path(REPO_ROOT)))
    return jsonify({"exists": True, "checked": checked, **result})


# --------------------------------------------------------------- settings --

def _settings_payload() -> dict:
    active = llm.active_provider()
    providers = {}
    for name in ("openai", "anthropic", "gemini"):
        key = llm.api_key(name)
        providers[name] = {
            "label": llm.PROVIDER_LABELS[name],
            "key_set": bool(key),
            "key_hint": _key_hint(key),
            "model": llm.model_for(name),
            "suggestions": llm.MODEL_SUGGESTIONS.get(name, []),
        }
    providers["openai"]["base_url"] = llm._openai_base_url()
    providers["ollama"] = {
        "label": llm.PROVIDER_LABELS["ollama"],
        "key_set": False,
        "key_hint": "",
        "model": llm.model_for("ollama"),
        "base_url": llm._ollama_base_url(),
        "reachable": _ollama_quickcheck(),
        "suggestions": llm.list_ollama_models(),
    }
    return {
        "preferred": llm.preferred_provider(),
        "active_provider": active or "none",
        "active_label": llm.PROVIDER_LABELS.get(active, "Nothing configured"),
        "active_model": llm.model_for(active) if active else "",
        "configured": llm.configured_providers(),
        "providers": providers,
    }


@app.route("/api/settings", methods=["GET"])
def api_settings():
    return jsonify(_settings_payload())


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    body = request.get_json(force=True) or {}
    changes: dict[str, str] = {}

    if "preferred" in body:
        value = (body.get("preferred") or "auto").strip().lower()
        changes["SCHOOL_AGENT_PROVIDER"] = value if value in ("auto", "openai", "anthropic", "gemini", "ollama") else "auto"

    key_env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
    model_env = {
        "openai": "SCHOOL_AGENT_OPENAI_MODEL",
        "anthropic": "SCHOOL_AGENT_ANTHROPIC_MODEL",
        "gemini": "SCHOOL_AGENT_GEMINI_MODEL",
        "ollama": "SCHOOL_AGENT_OLLAMA_MODEL",
    }
    for provider, env_name in key_env.items():
        if body.get(f"clear_{provider}_key"):
            changes[env_name] = ""
        elif (body.get(f"{provider}_api_key") or "").strip():
            changes[env_name] = body[f"{provider}_api_key"].strip()
    for provider, env_name in model_env.items():
        field = f"{provider}_model"
        if field in body:
            changes[env_name] = (body.get(field) or "").strip()
    if "openai_base_url" in body:
        changes["OPENAI_BASE_URL"] = (body.get("openai_base_url") or "").strip()
    if "ollama_base_url" in body:
        changes["OLLAMA_BASE_URL"] = (body.get("ollama_base_url") or "").strip()

    if changes:
        env_settings.update_env_file(REPO_ROOT / ".env", changes)
        env_settings.apply_to_process(changes)
        # Names only — never key material — in the activity log.
        notify(f"Model settings updated ({', '.join(sorted(changes))})", channel="console")
    return jsonify(_settings_payload())


@app.route("/api/settings/models")
def api_settings_models():
    """What models this key can actually use, straight from the provider.

    A wrong model id is otherwise invisible — it fails the same way a bad key
    and a blocked network do."""
    provider = (request.args.get("provider") or "").strip() or None
    try:
        return jsonify({"models": llm.list_remote_models(provider or llm.active_provider() or "")})
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a result, not a 500
        return jsonify({"models": [], "error": str(exc)})


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    # Probe first, generate second. The probe is cheap and it separates the
    # three failures that otherwise look identical: can't reach the host,
    # reached it but the key was rejected, and key fine but the model id
    # doesn't exist. Only if all three pass do we spend a real generation.
    try:
        check = llm.diagnose()
    except Exception as exc:  # noqa: BLE001
        check = {"ok": True, "stage": "unchecked", "detail": str(exc)}
    # Only the three failures the probe can prove short-circuit. A "config"
    # verdict falls through on purpose: the real call raises a better-worded
    # LLMNotConfiguredError for that case, and the probe must never be the
    # thing that decides a request can't be made.
    if not check.get("ok") and check.get("stage") in {"network", "key", "model"}:
        return jsonify({
            "ok": False,
            "error": check.get("detail", "couldn't reach the model provider"),
            "stage": check.get("stage"),
            "available": check.get("available", []),
        }), 502

    try:
        reply = default_llm_fn("Reply with the single word OK and nothing else.", "")
    except LLMNotConfiguredError as exc:
        return jsonify({"ok": False, "error": str(exc), "stage": "config"}), 400
    except Exception as exc:  # noqa: BLE001 - surfaced to the owner as a test result, not a 500
        return jsonify({"ok": False, "error": str(exc), "stage": "generate"}), 502
    payload = _settings_payload()
    return jsonify({
        "ok": True,
        "provider": payload["active_label"],
        "model": payload["active_model"],
        "reply": reply.strip()[:120],
    })


# --------------------------------------------------------------- activity --

@app.route("/api/activity")
def api_activity():
    return jsonify(recent_messages(30))


def _find_chrome() -> str | None:
    """Best-effort path to a real Chrome install — the owner asked for the
    dashboard to open in Chrome specifically, not whatever the OS default
    happens to be (Edge, on a stock Windows install). Falls back to None
    (handled by _open_browser below) rather than guessing wrong."""
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            for base in (
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            )
            if base
        ]
        return next((p for p in candidates if os.path.isfile(p)), None)
    if system == "Darwin":
        mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        return mac_path if os.path.isfile(mac_path) else None
    return shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")


def _open_browser():
    url = f"http://{HOST}:{PORT}"
    chrome_path = _find_chrome()
    if chrome_path:
        try:
            subprocess.Popen([chrome_path, url])
            return
        except OSError:
            pass  # fall through to the OS default below
    webbrowser.open(url)


def main() -> int:
    scheduler.start_background_sync(REPO_ROOT, _classes, briefing_llm_fn=default_llm_fn)
    Timer(1.0, _open_browser).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
