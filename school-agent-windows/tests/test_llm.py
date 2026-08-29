import pytest
import requests
from school_agent import llm


def test_default_llm_fn_raises_clear_error_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def fake_get(*args, **kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(llm.requests, "get", fake_get)

    with pytest.raises(llm.LLMNotConfiguredError, match="No model provider is configured"):
        llm.default_llm_fn("prompt", "context")


def test_default_llm_fn_prefers_openai_when_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    called = {}

    def fake_post(url, headers, json, timeout):
        called["url"] = url
        called["json"] = json

        class FakeResp:
            ok = True

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "generated text"}}]}

        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    result = llm.default_llm_fn("summarize this", "some course material")

    assert result == "generated text"
    assert "chat/completions" in called["url"]
    assert "some course material" in called["json"]["messages"][1]["content"]


def test_ollama_http_error_surfaces_response_body(monkeypatch):
    # A 404 from /api/generate means "model not pulled" — the body carries
    # that; the error the caller sees must include it (2026-08-25 fix).
    import requests

    from school_agent import llm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_ollama_reachable", lambda url: True)

    class FakeResp:
        ok = False
        status_code = 404
        url = "http://localhost:11434/api/generate"
        text = '{"error":"model \'gemma4:12b\' not found, try pulling it first"}'

    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp())
    with pytest.raises(RuntimeError, match="not found, try pulling it first"):
        llm.default_llm_fn("prompt", "context")


def test_list_ollama_models_returns_pulled_model_names(monkeypatch):
    import requests

    from school_agent import llm

    class FakeResp:
        ok = True

        @staticmethod
        def json():
            return {"models": [{"name": "qwen2.5:7b"}, {"name": "gemma4:12b"}]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    assert llm.list_ollama_models() == ["gemma4:12b", "qwen2.5:7b"]


def test_list_ollama_models_empty_when_unreachable(monkeypatch):
    import requests

    from school_agent import llm

    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    assert llm.list_ollama_models() == []


def test_default_llm_fn_falls_back_to_ollama_when_reachable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def fake_get(url, timeout):
        class FakeResp:
            ok = True

        return FakeResp()

    def fake_post(url, json, timeout):
        assert "/api/generate" in url

        class FakeResp:
            ok = True

            def raise_for_status(self):
                pass

            def json(self):
                return {"response": "ollama generated text"}

        return FakeResp()

    monkeypatch.setattr(llm.requests, "get", fake_get)
    monkeypatch.setattr(llm.requests, "post", fake_post)

    result = llm.default_llm_fn("prompt", "context")
    assert result == "ollama generated text"


# --- 2026-08-26: Claude and Gemini providers alongside OpenAI and Ollama ---

class _Resp:
    def __init__(self, payload, ok=True, status=200, text=""):
        self.ok, self.status_code, self._payload = ok, status, payload
        self.url = "https://example.test/api"
        self.text = text or ""

    def json(self):
        return self._payload


def _clear(monkeypatch):
    from school_agent.env_settings import MANAGED_KEYS
    for k in MANAGED_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_ollama_reachable", lambda url: False)


def test_anthropic_request_shape_and_text_extraction(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SCHOOL_AGENT_ANTHROPIC_MODEL", "claude-sonnet-5")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _Resp({"content": [{"type": "text", "text": "entropy "}, {"type": "text", "text": "explained"}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.default_llm_fn("Explain", "chapter 4") == "entropy explained"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["model"] == "claude-sonnet-5"
    assert seen["body"]["messages"][0]["role"] == "user"
    assert "chapter 4" in seen["body"]["messages"][0]["content"]


def test_gemini_request_shape_and_text_extraction(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test-key")
    monkeypatch.setenv("SCHOOL_AGENT_GEMINI_MODEL", "gemini-3.7-flash")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _Resp({"candidates": [{"content": {"parts": [{"text": "the second law"}]}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.default_llm_fn("Explain", "chapter 4") == "the second law"
    assert "models/gemini-3.7-flash:generateContent" in seen["url"]
    # The key goes in a header, never the URL — URLs leak into logs.
    assert seen["headers"]["x-goog-api-key"] == "g-test-key"
    assert "g-test-key" not in seen["url"]
    assert "chapter 4" in seen["body"]["contents"][0]["parts"][0]["text"]


def test_gemini_safety_block_reports_a_real_reason(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test-key")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(RuntimeError, match="SAFETY"):
        llm.default_llm_fn("prompt", "")


def test_bad_key_surfaces_the_provider_message(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad")
    monkeypatch.setattr(
        llm.requests, "post",
        lambda *a, **k: _Resp({}, ok=False, status=401, text='{"error":{"message":"invalid x-api-key"}}'),
    )
    with pytest.raises(RuntimeError, match="invalid x-api-key"):
        llm.default_llm_fn("prompt", "")


def test_auto_order_is_openai_then_anthropic_then_gemini(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert llm.active_provider() == "gemini"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert llm.active_provider() == "anthropic"
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert llm.active_provider() == "openai"


def test_explicit_preference_wins_over_auto_order(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("SCHOOL_AGENT_PROVIDER", "gemini")
    assert llm.active_provider() == "gemini"


def test_explicit_preference_without_key_errors_rather_than_falling_back(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "o")  # usable, but not what was picked
    monkeypatch.setenv("SCHOOL_AGENT_PROVIDER", "gemini")
    assert llm.active_provider() is None
    with pytest.raises(llm.LLMNotConfiguredError, match="Gemini"):
        llm.default_llm_fn("prompt", "")


def test_configured_providers_lists_everything_usable(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert llm.configured_providers() == ["anthropic", "gemini"]


# --- 2026-08-26: a busy provider is not a broken one ----------------------

def test_a_503_is_retried_then_answered(monkeypatch):
    """Google's free Gemini tier returns 503 "experiencing high demand" at busy
    times. Nothing is wrong — treating it as fatal made the app look broken."""
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp({}, ok=False, status=503, text='{"error":{"status":"UNAVAILABLE"}}')
        return _Resp({"candidates": [{"content": {"parts": [{"text": "the answer"}]}}]})

    monkeypatch.setattr(llm.requests, "post", flaky)
    assert llm.default_llm_fn("q", "") == "the answer"
    assert calls["n"] == 3


def test_a_busy_provider_falls_back_to_a_local_model(monkeypatch):
    """A slower answer from Ollama beats no answer, and it costs nothing."""
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(llm, "_ollama_reachable", lambda url: True)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def post(url, headers=None, json=None, timeout=None):
        if "generativelanguage" in url:
            return _Resp({}, ok=False, status=503, text="busy")
        return _Resp({"response": "local answer"})

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.default_llm_fn("q", "") == "local answer"


def test_an_explicitly_pinned_provider_never_silently_switches(monkeypatch):
    """Picking a provider in Settings is a decision; answering from a
    different model behind the student's back would undermine it."""
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("SCHOOL_AGENT_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "_ollama_reachable", lambda url: True)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: _Resp({}, ok=False, status=503, text="busy"))
    with pytest.raises(RuntimeError, match="overloaded"):
        llm.default_llm_fn("q", "")


def test_a_read_timeout_is_retried_like_a_busy_provider(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("read timeout=60")
        return _Resp({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    monkeypatch.setattr(llm.requests, "post", flaky)
    assert llm.default_llm_fn("q", "") == "ok"


def test_a_real_error_is_not_retried(monkeypatch):
    """A bad key is not going to fix itself — retrying it three times just
    makes the student wait three times as long for the same answer."""
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "bad")
    calls = {"n": 0}

    def bad(*a, **k):
        calls["n"] += 1
        return _Resp({}, ok=False, status=400, text='{"error":{"message":"API key not valid"}}')

    monkeypatch.setattr(llm.requests, "post", bad)
    with pytest.raises(RuntimeError, match="API key not valid"):
        llm.default_llm_fn("q", "")
    assert calls["n"] == 1


def test_a_rate_limit_says_what_to_do(monkeypatch):
    assert "Wait a minute" in llm._busy_detail("gemini", 429)
    assert "overloaded" in llm._busy_detail("gemini", 503)
    assert "your key" in llm._busy_detail("gemini", 503)


def test_the_global_system_prompt_does_not_gate_subject_knowledge():
    """This prompt rides on EVERY call. It used to say "never invent facts not
    present in it", which the model read as "don't tell them anything the
    excerpts don't say" — so "what is a scalar" got a refusal."""
    from school_agent import llm
    p = llm.SYSTEM_PROMPT
    assert "never invent facts not present in it" not in p
    assert "Use your own knowledge of the subject fully" in p
    assert "Never withhold or hedge" in p
    # The prohibition that actually protects the student's grade stays.
    assert "never invent is something specific to their course" in p
    for course_fact in ("due date", "grade weight", "exam"):
        assert course_fact in p
