"""Loads (and, since the 2026-08-25 dashboard, writes) config/classes.yaml —
one entry per active class.

v1 required hand-editing this file, including finding and pasting your own
ICS feed URL correctly — a UX review flagged that as a real onboarding
barrier for a tool that's supposed to be "very easy." The dashboard (ui/
server.py) now writes this file through `add_class`/`save_classes` below,
with the ICS URL validated (fetched and parsed) before it's ever saved —
hand-editing the YAML directly still works too, nothing about the format
changed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .storage import atomic_write_text


@dataclass
class ClassConfig:
    slug: str
    name: str
    term: str = ""
    instructor: str = ""
    ics_feed_url: str | None = None
    # Lets several classes point at the SAME feed URL (e.g. Brightspace's
    # "All Calendars and Tasks" subscription link, which covers every course
    # at once) instead of each needing its own per-course export — set this
    # to something unique to the course (its code, e.g. "COURSE.102") and
    # deadlines.py keeps only events that mention it. Leave blank when the
    # feed URL is already specific to one course.
    course_filter: str | None = None
    syllabus_path: str | None = None
    # ordered list of [YYYY-MM-DD, topic] pairs from the syllabus, used by
    # getahead.py — optional, filled in as the owner transcribes the syllabus
    topics: list[list[str]] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "ClassConfig":
        missing = [k for k in ("slug", "name") if k not in d]
        if missing:
            raise ValueError(f"class entry missing required field(s): {missing}: {d!r}")
        return ClassConfig(
            slug=d["slug"],
            name=d["name"],
            term=d.get("term", ""),
            instructor=d.get("instructor", ""),
            ics_feed_url=d.get("ics_feed_url"),
            course_filter=d.get("course_filter"),
            syllabus_path=d.get("syllabus_path"),
            topics=d.get("topics", []),
        )


def load_classes(path: Path | str) -> list[ClassConfig]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — copy config/classes.example.yaml to "
            "config/classes.yaml and fill in your actual current course load."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("classes", [])
    classes = [ClassConfig.from_dict(e) for e in entries]
    slugs = [c.slug for c in classes]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    if dupes:
        raise ValueError(f"duplicate class slug(s) in {path}: {sorted(dupes)}")
    return classes


def get_class(classes: list[ClassConfig], slug: str) -> ClassConfig:
    for c in classes:
        if c.slug == slug:
            return c
    raise KeyError(f"no class with slug {slug!r} in config")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "class"


def unique_slug(name: str, existing: list[str]) -> str:
    base = slugify(name)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def save_classes(path: Path | str, classes: list[ClassConfig]) -> None:
    """Atomic, like every other writer in the package (2026-08-26). This was
    the last direct write_text left, and it was on classes.yaml — the one
    file here that cannot be regenerated from anything else. A crash or a
    full disk mid-write truncated it, and the next start came up with no
    classes at all."""
    payload = {"classes": [asdict(c) for c in classes]}
    atomic_write_text(
        path,
        "# Managed by the school-agent dashboard — hand-editing still works,\n"
        "# the dashboard just reads/writes this same file.\n"
        + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
    )


def load_classes_or_empty(path: Path | str) -> list[ClassConfig]:
    """Like load_classes, but returns [] instead of raising when the file
    doesn't exist yet — the dashboard's normal state before a first class
    is added, not an error."""
    try:
        return load_classes(path)
    except FileNotFoundError:
        return []


def add_class(
    path: Path | str,
    name: str,
    term: str = "",
    instructor: str = "",
    ics_feed_url: str | None = None,
    course_filter: str | None = None,
    syllabus_path: str | None = None,
) -> ClassConfig:
    classes = load_classes_or_empty(path)
    slug = unique_slug(name, [c.slug for c in classes])
    new_class = ClassConfig(
        slug=slug,
        name=name,
        term=term,
        instructor=instructor,
        ics_feed_url=ics_feed_url,
        course_filter=course_filter,
        syllabus_path=syllabus_path,
    )
    classes.append(new_class)
    save_classes(path, classes)
    return new_class
