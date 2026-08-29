"""School Agent — a study assistant that runs entirely on your own machine.

See README.md for the
full design. This package is intentionally lightweight: filesystem + JSON
storage, no database server, no Docker, so it works today rather than
waiting on Phase 4's kernel gate.
"""

__all__ = [
    "config",
    "deadlines",
    "quiz",
    "materials",
    "draft",
    "getahead",
    "notify",
    "paths",
]
