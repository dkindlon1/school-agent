#!/usr/bin/env python3
"""Copy a file into a class's materials/ dir and refresh its index.

Usage: python scripts/add_material.py <class-slug> <path-to-file>
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from school_agent import materials, paths  # noqa: E402
from school_agent.config import get_class, load_classes  # noqa: E402
from school_agent.notify import notify  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    slug, src = sys.argv[1], Path(sys.argv[2])
    classes = load_classes(REPO_ROOT / "config" / "classes.yaml")
    get_class(classes, slug)  # raises KeyError with a clear message if unknown
    paths.ensure_class_dirs(REPO_ROOT, slug)
    mdir = paths.materials_dir(REPO_ROOT, slug)
    dest = mdir / src.name
    shutil.copy2(src, dest)
    entries = materials.scan_materials(mdir)
    materials.save_index(paths.materials_index_path(REPO_ROOT, slug), entries)
    notify(f"{slug}: ingested {src.name} ({len(entries)} material(s) now indexed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
