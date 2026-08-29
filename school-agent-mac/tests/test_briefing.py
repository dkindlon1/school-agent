from datetime import datetime, timedelta, timezone

from school_agent import briefing, deadlines, paths
from school_agent.config import ClassConfig
from school_agent.deadlines import Deadline
from school_agent.quiz import CardStore

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _seed_class(tmp_path, slug="course-102", name="COURSE.102 — Statics"):
    c = ClassConfig(slug=slug, name=name, topics=[["2026-09-01", "Moments and couples"]])
    paths.ensure_class_dirs(tmp_path, slug)
    deadlines.save_deadlines(
        paths.deadlines_path(tmp_path, slug),
        [
            Deadline(uid="1", class_slug=slug, title="Overdue worksheet", due="2026-08-24T23:59:00+00:00"),
            Deadline(uid="2", class_slug=slug, title="HW 2", due="2026-08-28T23:59:00+00:00"),
            Deadline(uid="3", class_slug=slug, title="Quiz 3", due="2026-09-05T23:59:00+00:00"),
            Deadline(uid="4", class_slug=slug, title="Final exam", due="2026-12-15T08:00:00+00:00"),
        ],
    )
    return c


def test_build_facts_buckets_deadlines_and_topics(tmp_path):
    c = _seed_class(tmp_path)
    facts = briefing.build_facts(tmp_path, [c], now=NOW)
    cls = facts["classes"][0]
    assert [d["title"] for d in cls["overdue"]] == ["Overdue worksheet"]
    assert [d["title"] for d in cls["due_this_week"]] == ["HW 2"]
    assert [d["title"] for d in cls["upcoming"]] == ["Quiz 3"]  # 14d window; final exam excluded
    assert cls["upcoming_topics"] == [{"date": "2026-09-01", "topic": "Moments and couples"}]


def test_build_facts_counts_recent_reviews(tmp_path):
    c = _seed_class(tmp_path)
    store = CardStore(paths.cards_path(tmp_path, c.slug))
    qc = store.add_card(c.slug, "What is a couple moment?", "Two equal opposite forces")
    store.save()
    store.review(qc.card_id, "good", now=NOW - timedelta(days=2))
    store.save()
    facts = briefing.build_facts(tmp_path, [c], now=NOW)
    assert facts["classes"][0]["recently_reviewed_questions"] == ["What is a couple moment?"]


def test_deterministic_briefing_mentions_real_items_only(tmp_path):
    c = _seed_class(tmp_path)
    result = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)
    assert result["used_model"] is False
    assert "Overdue worksheet" in result["content"]
    assert "HW 2" in result["content"]
    assert "Moments and couples" in result["content"]


def test_model_briefing_receives_facts_and_is_saved(tmp_path):
    c = _seed_class(tmp_path)
    seen = {}

    def fake_llm(prompt, context):
        seen["context"] = context
        return "Here is your briefing."

    result = briefing.generate_briefing(tmp_path, [c], llm_fn=fake_llm, now=NOW)
    assert result["used_model"] is True
    assert "Overdue worksheet" in seen["context"]  # model only ever sees real facts
    latest = briefing.load_latest(tmp_path)
    assert latest["content"] == "Here is your briefing."


def test_model_failure_falls_back_to_deterministic_digest(tmp_path):
    c = _seed_class(tmp_path)

    def broken_llm(prompt, context):
        raise RuntimeError("provider down")

    result = briefing.generate_briefing(tmp_path, [c], llm_fn=broken_llm, now=NOW)
    assert result["used_model"] is False
    assert "Overdue worksheet" in result["content"]  # loop never yields nothing


def test_maybe_generate_respects_staleness(tmp_path):
    c = _seed_class(tmp_path)
    assert briefing.maybe_generate(tmp_path, [c], llm_fn=None, now=NOW) is True  # nothing yet
    assert briefing.maybe_generate(tmp_path, [c], llm_fn=None, now=NOW + timedelta(hours=2)) is False  # fresh
    assert briefing.maybe_generate(tmp_path, [c], llm_fn=None, now=NOW + timedelta(hours=25)) is True  # stale


# --- 2026-08-26: grade-aware prioritization ---

def _seed_with_grading(tmp_path, slug="mece-110"):
    from school_agent.grades import Component, GradingScheme, Score, save_scheme, save_scores

    c = ClassConfig(slug=slug, name="COURSE.101 — Thermodynamics I")
    paths.ensure_class_dirs(tmp_path, slug)
    deadlines.save_deadlines(
        paths.deadlines_path(tmp_path, slug),
        [
            # Due sooner, worth almost nothing.
            Deadline(uid="post", class_slug=slug, title="Discussion Post 3", due="2026-08-26T23:59:00+00:00"),
            # Due later, worth 25x more — must be ranked first.
            Deadline(uid="mid", class_slug=slug, title="Midterm 1", due="2026-08-29T09:00:00+00:00"),
        ],
    )
    save_scheme(paths.grading_path(tmp_path, slug), GradingScheme(
        components=[
            Component(name="Discussion", weight_pct=5, count=10),
            Component(name="Midterm", weight_pct=25, count=1),
        ],
        confirmed=True,
    ))
    save_scores(paths.scores_path(tmp_path, slug), [Score("Discussion", "Post 1", 4, 5)])
    return c


def test_briefing_orders_by_grade_impact_not_due_date(tmp_path):
    c = _seed_with_grading(tmp_path)
    content = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)["content"]
    attention = content.split("# Where you stand")[0]
    # The 25% midterm must appear above the 0.5% post, despite being due later.
    assert attention.index("Midterm 1") < attention.index("Discussion Post 3")
    assert "25% of grade" in attention


def test_briefing_reports_current_standing(tmp_path):
    c = _seed_with_grading(tmp_path)
    content = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)["content"]
    assert "# Where you stand" in content
    assert "80.0%" in content  # 4/5 on the only graded item


def test_facts_carry_weight_so_the_model_can_prioritize(tmp_path):
    c = _seed_with_grading(tmp_path)
    facts = briefing.build_facts(tmp_path, [c], now=NOW)
    items = {i["title"]: i for i in facts["classes"][0]["due_this_week"]}
    assert items["Midterm 1"]["worth_pct"] == 25
    assert items["Discussion Post 3"]["worth_pct"] == 0.5
    assert facts["classes"][0]["grade"]["current_pct"] == 80.0


def test_briefing_without_a_grading_scheme_still_works(tmp_path):
    c = _seed_class(tmp_path)  # no grading.json at all
    content = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)["content"]
    assert "# What needs your attention" in content
    assert "# Where you stand" not in content  # nothing invented


def test_briefing_counts_focus_study_time(tmp_path):
    from school_agent import sessions

    c = _seed_class(tmp_path)
    sessions.record(paths.sessions_path(tmp_path), sessions.Session(
        started_at=(NOW - timedelta(minutes=40)).isoformat(), ended_at=NOW.isoformat(),
        class_slug=c.slug, class_name=c.name, kind="review", label="Review", items=12, minutes=40,
    ))
    content = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)["content"]
    assert "40 minutes of focused study" in content


def test_maybe_generate_skips_with_no_classes(tmp_path):
    assert briefing.maybe_generate(tmp_path, [], llm_fn=None, now=NOW) is False
    assert briefing.load_latest(tmp_path) is None


# --- 2026-08-26: check-offs on briefing lines ---

def test_line_key_is_stable_across_cosmetic_differences():
    a = briefing.line_key("- Work on “Midterm 1” (COURSE.101) — 25% of grade")
    b = briefing.line_key("Work on   “Midterm 1” (COURSE.101) - 25% of grade")
    assert a == b
    assert a != briefing.line_key("- Work on “Final” (COURSE.101) — 40% of grade")


def test_check_roundtrip_and_untick(tmp_path):
    p = tmp_path / "briefing_checks.json"
    key = briefing.line_key("- Review 12 cards")
    assert briefing.load_checks(p) == {}

    briefing.set_check(p, key, True, now=NOW)
    assert key in briefing.load_checks(p, now=NOW)

    briefing.set_check(p, key, False, now=NOW)
    assert key not in briefing.load_checks(p, now=NOW)


def test_checks_expire_so_last_weeks_ticks_do_not_hide_this_weeks_work(tmp_path):
    p = tmp_path / "briefing_checks.json"
    key = briefing.line_key("- Work on HW 4")
    briefing.set_check(p, key, True, now=NOW)

    assert key in briefing.load_checks(p, now=NOW + timedelta(days=13))
    assert key not in briefing.load_checks(p, now=NOW + timedelta(days=15))


def test_checks_survive_a_briefing_regeneration(tmp_path):
    # The point of keying by text: regenerate the briefing and a line you
    # already handled comes back already struck through.
    c = _seed_class(tmp_path)
    first = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)["content"]
    line = next(l for l in first.splitlines() if l.startswith("- "))

    p = paths.briefing_checks_path(tmp_path)
    briefing.set_check(p, briefing.line_key(line), True, now=NOW)

    second = briefing.generate_briefing(tmp_path, [c], llm_fn=None, now=NOW)["content"]
    assert line in second
    assert briefing.line_key(line) in briefing.load_checks(p, now=NOW)


def test_corrupt_checks_file_does_not_crash(tmp_path):
    p = tmp_path / "briefing_checks.json"
    p.write_text("not json", encoding="utf-8")
    assert briefing.load_checks(p) == {}
