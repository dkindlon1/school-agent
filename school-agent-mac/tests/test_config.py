import pytest
from school_agent.config import (
    ClassConfig,
    add_class,
    get_class,
    load_classes,
    load_classes_or_empty,
    save_classes,
    slugify,
    unique_slug,
)


def test_load_classes_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_classes(tmp_path / "does-not-exist.yaml")


def test_load_classes_roundtrip(tmp_path):
    p = tmp_path / "classes.yaml"
    p.write_text(
        """
classes:
  - slug: cs401
    name: "CS 401"
    term: "Fall 2026"
    ics_feed_url: "https://example.edu/feed.ics"
    topics:
      - ["2026-09-02", "Overview"]
""",
        encoding="utf-8",
    )
    classes = load_classes(p)
    assert len(classes) == 1
    assert classes[0].slug == "cs401"
    assert classes[0].topics == [["2026-09-02", "Overview"]]
    assert get_class(classes, "cs401").name == "CS 401"


def test_load_classes_rejects_duplicate_slugs(tmp_path):
    p = tmp_path / "classes.yaml"
    p.write_text(
        """
classes:
  - slug: cs401
    name: "CS 401 Section A"
  - slug: cs401
    name: "CS 401 Section B"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate class slug"):
        load_classes(p)


def test_class_config_requires_slug_and_name():
    with pytest.raises(ValueError):
        ClassConfig.from_dict({"name": "missing slug"})


def test_get_class_unknown_slug_raises():
    with pytest.raises(KeyError):
        get_class([ClassConfig(slug="a", name="A")], "b")


# --- 2026-08-25 additions: dashboard-driven class creation, no hand-edit required ---

def test_slugify_basic():
    assert slugify("CS 401 - Algorithms") == "cs-401-algorithms"
    assert slugify("  Weird!!  Name??  ") == "weird-name"
    assert slugify("") == "class"


def test_unique_slug_disambiguates_collisions():
    assert unique_slug("Algorithms", []) == "algorithms"
    assert unique_slug("Algorithms", ["algorithms"]) == "algorithms-2"
    assert unique_slug("Algorithms", ["algorithms", "algorithms-2"]) == "algorithms-3"


def test_load_classes_or_empty_returns_empty_list_when_missing(tmp_path):
    assert load_classes_or_empty(tmp_path / "nope.yaml") == []


def test_save_classes_roundtrip(tmp_path):
    p = tmp_path / "classes.yaml"
    classes = [ClassConfig(slug="cs401", name="CS 401", ics_feed_url="https://example.edu/feed.ics")]
    save_classes(p, classes)
    reloaded = load_classes(p)
    assert reloaded == classes


def test_add_class_creates_file_and_assigns_unique_slug(tmp_path):
    p = tmp_path / "classes.yaml"
    c1 = add_class(p, "CS 401 - Algorithms", term="Fall 2026", ics_feed_url="https://example.edu/a.ics")
    assert c1.slug == "cs-401-algorithms"

    c2 = add_class(p, "CS 401 - Algorithms", term="Fall 2026 Section B")
    assert c2.slug == "cs-401-algorithms-2"

    classes = load_classes(p)
    assert {c.slug for c in classes} == {"cs-401-algorithms", "cs-401-algorithms-2"}


# --- 2026-08-25: course_filter, for sharing one feed URL across classes ---

def test_add_class_stores_course_filter(tmp_path):
    p = tmp_path / "classes.yaml"
    c = add_class(p, "Statics", ics_feed_url="https://example.edu/all.ics", course_filter="COURSE.102")
    assert c.course_filter == "COURSE.102"
    assert load_classes(p)[0].course_filter == "COURSE.102"


def test_class_config_course_filter_defaults_to_none():
    assert ClassConfig(slug="a", name="A").course_filter is None
    assert ClassConfig.from_dict({"slug": "a", "name": "A"}).course_filter is None
