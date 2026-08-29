import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ui"))

# ui/server.py calls load_dotenv() at import time, and pytest imports every
# test module (test_ui_server.py included) during collection. So on a machine
# that has a real .env, collecting the suite pulled the owner's actual
# provider keys into os.environ and the LLM tests then exercised whichever
# provider happened to be configured - green on a clean checkout, red on the
# developer's own machine, and one bad monkeypatch away from spending real
# money on a real key. Every test now starts from a blank model config.
MODEL_ENV_VARS = (
    "SCHOOL_AGENT_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "SCHOOL_AGENT_OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "SCHOOL_AGENT_ANTHROPIC_MODEL",
    "GEMINI_API_KEY",
    "SCHOOL_AGENT_GEMINI_MODEL",
    "OLLAMA_BASE_URL",
    "SCHOOL_AGENT_OLLAMA_MODEL",
    "SCHOOL_AGENT_TIMEZONE",
)


@pytest.fixture(autouse=True)
def _blank_model_environment(monkeypatch):
    """Neutralise any real .env the collection step leaked into the process."""
    for name in MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
