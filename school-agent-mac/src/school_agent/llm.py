"""Model provider wiring — the one place a provider is chosen.

Every generation capability (quiz.py, getahead.py, draft.py, grades.py,
briefing.py) takes an injected `llm_fn` rather than calling a provider itself,
so this file is the only thing that knows an API exists. That was the point of
the original design and it's why adding two more providers here required no
change anywhere else.

Four providers, 2026-08-26:

- **openai**    — OpenAI or any OpenAI-compatible endpoint (`/chat/completions`)
- **anthropic** — Claude's Messages API
- **gemini**    — Google's generateContent API
- **ollama**    — a local model, free and private, no key

Selection is explicit-first: if SCHOOL_AGENT_PROVIDER names one, that one is
used and its failure is reported rather than silently falling through to a
different provider (silently answering from a model the owner didn't pick is
worse than an error). On "auto", the first provider with a key wins, in the
order above, then local Ollama if it's reachable.

Everything is read at CALL time, never at import time, because the dashboard's
Settings page mutates os.environ in the running process and a change has to
take effect on the very next call without a restart.
"""

from __future__ import annotations

import os
import time

import requests

from .notify import notify

# (connect, read). Split deliberately: a machine that cannot REACH the host
# should fail in seconds, not sit for a minute looking identical to a slow
# model. A read timeout after a successful connect means something between you
# and the provider swallowed the response — see _network_error_detail.
REQUEST_TIMEOUT_S = (10, 60)
# Reachability probes are not generation; they must answer fast or not at all.
PROBE_TIMEOUT_S = (10, 20)
MAX_OUTPUT_TOKENS = 2048

# Ordered: whichever of these has a key wins under "auto".
PROVIDER_ORDER = ("openai", "anthropic", "gemini")

PROVIDER_LABELS = {
    "openai": "OpenAI (or compatible)",
    "anthropic": "Claude",
    "gemini": "Gemini",
    "ollama": "Local (Ollama)",
}

DEFAULT_MODELS = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-3.7-flash",
    "ollama": "gemma4:12b",
}

# Suggested models offered in the Settings dropdowns. Free-text is still
# allowed — these are a convenience, not a whitelist, so a model released
# after this was written is typed in rather than blocked.
MODEL_SUGGESTIONS = {
    "openai": ["gpt-5.6-luna", "gpt-5.6-sol"],
    "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"],
    "gemini": [
        "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash",
    ],
}

# This rides on EVERY call the app makes, so its wording matters more than any
# individual feature's prompt. The original said "never invent facts not
# present in it", which the model correctly read as "do not tell them anything
# the excerpts don't say" — so asking what a scalar is got a refusal, because
# no uploaded PDF happened to define one. That is the opposite of useful.
#
# The honest line is not between "in the excerpts" and "not in the excerpts".
# It is between SUBJECT knowledge, which the model has and should use, and
# COURSE facts — this student's deadlines, weights, exam scope, their
# professor's particular definitions — which it cannot know and must never
# guess, because a confident wrong one costs a real grade.
SYSTEM_PROMPT = (
    "You are a study assistant helping one student with their actual coursework. "
    "Use your own knowledge of the subject fully and confidently: explain, derive, work "
    "examples, and answer questions the way a good tutor would. Never withhold or hedge an "
    "explanation on the grounds that it is not in the material you were given. "
    "When course material is provided, treat it as context that tunes you to THIS course — "
    "match its notation, conventions, depth and emphasis, and prefer its framing over generic "
    "phrasing, because the student's exam follows their course, not a generic textbook. "
    "The one thing you must never invent is something specific to their course that you cannot "
    "see: a due date, a grade weight, what will be on the exam, what their professor said, "
    "their scores. If asked for one of those and it is not in front of you, say you do not "
    "have it rather than guessing."
)


class LLMNotConfiguredError(RuntimeError):
    """No usable provider. Callers catch this and tell the owner exactly what
    to set, rather than the feature silently doing nothing."""


# ------------------------------------------------------------- env access --

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def preferred_provider() -> str:
    value = _env("SCHOOL_AGENT_PROVIDER", "auto").lower()
    return value if value in ("auto", "openai", "anthropic", "gemini", "ollama") else "auto"


def api_key(provider: str) -> str:
    return _env({
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }.get(provider, ""))


def model_for(provider: str) -> str:
    return _env({
        "openai": "SCHOOL_AGENT_OPENAI_MODEL",
        "anthropic": "SCHOOL_AGENT_ANTHROPIC_MODEL",
        "gemini": "SCHOOL_AGENT_GEMINI_MODEL",
        "ollama": "SCHOOL_AGENT_OLLAMA_MODEL",
    }.get(provider, ""), DEFAULT_MODELS.get(provider, ""))


def _openai_base_url() -> str:
    return _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _ollama_base_url() -> str:
    return _env("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


# Kept for callers that imported the old constant.
DEFAULT_OLLAMA_BASE_URL = _ollama_base_url()


# ---------------------------------------------------------------- helpers --

def _raise_for_response(provider: str, resp) -> None:
    """One place that decides whether a bad response is worth retrying."""
    if resp.status_code in RETRYABLE_STATUSES:
        raise ProviderBusyError(_busy_detail(provider, resp.status_code))
    raise RuntimeError(_http_error_detail(resp))


def _http_error_detail(resp) -> str:
    """A 404 from Ollama means "model not pulled"; a 401 from any cloud
    provider means "bad key" — and in every case the useful part is in the
    RESPONSE BODY, which raise_for_status() throws away, leaving only
    "404 Client Error". Keep the body."""
    try:
        body = (resp.text or "").strip()[:300]
    except Exception:  # noqa: BLE001
        body = ""
    return f"HTTP {resp.status_code} from {resp.url}" + (f": {body}" if body else "")


# Statuses that mean "ask again in a moment", not "you did something wrong".
# Google's free Gemini tier returns 503 UNAVAILABLE ("This model is currently
# experiencing high demand") often enough that treating it as a hard failure
# makes the app look broken when nothing is.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRY_BACKOFF_S = (1.0, 3.0)
# A hard ceiling on ONE press of a button. Retries are worth having, but three
# 60-second read timeouts plus backoff plus a 60-second fallback measured at
# 244 seconds of a spinner with no way to cancel — and the browser has no
# fetch timeout, so it just sits there. Past this we stop and say so.
MAX_RECOVERY_SECONDS = 75.0


class ProviderBusyError(RuntimeError):
    """The provider is up and the key is fine — it is just overloaded."""


def _busy_detail(provider: str, status: int) -> str:
    who = PROVIDER_LABELS.get(provider, provider)
    if status == 429:
        return (f"{who} is rate-limiting this key — too many requests in a short window. "
                "Wait a minute and try again, or switch provider in Settings.")
    return (f"{who} is overloaded right now (HTTP {status}). Nothing is wrong with your key or "
            "your setup — free tiers do this at busy times of day. Try again in a minute.")


def _network_error_detail(provider: str, exc: Exception) -> str:
    """Turn a requests exception into something that says what to actually do.

    "HTTPSConnectionPool(...): Read timed out" tells a student nothing, and it
    reads like a broken app or a bad key. It is neither: a READ timeout means
    the connection opened and the request went out, and nothing came back —
    which is almost always a network in the middle (campus wifi, a VPN, or
    antivirus doing TLS inspection), not the key and not the model.
    """
    host = PROVIDER_HOSTS.get(provider, provider)
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return (
            f"Reached {host} but got no response back within the timeout. Your key and model are "
            "probably fine — this is what a network in the middle looks like. Try a phone hotspot "
            "instead of campus wifi; if that works, it's the network. A VPN or antivirus doing "
            "HTTPS inspection can do the same thing."
        )
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return f"Couldn't open a connection to {host} at all — check you're online, then try again."
    if isinstance(exc, requests.exceptions.SSLError):
        return (
            f"The secure connection to {host} was rejected. Usually antivirus or a corporate proxy "
            "intercepting HTTPS. Try another network to confirm."
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        return f"Couldn't reach {host} — no internet, DNS blocked, or the host is filtered on this network."
    return str(exc)


PROVIDER_HOSTS = {
    "openai": "the OpenAI API",
    "anthropic": "api.anthropic.com",
    "gemini": "generativelanguage.googleapis.com",
    "ollama": "your local Ollama",
}


def list_remote_models(provider: str) -> list[str]:
    """Ask the provider what models this key can actually use.

    Worth a round trip because a wrong model id is otherwise invisible: it
    looks exactly like every other failure. Gemini is the one implemented —
    its endpoint is cheap, needs no generation, and doubles as the most
    reliable "is this key any good and can I reach Google" probe there is.
    """
    if provider != "gemini":
        return []
    key = api_key("gemini")
    if not key:
        return []
    resp = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
        timeout=PROBE_TIMEOUT_S,
    )
    if not resp.ok:
        _raise_for_response("gemini", resp)
    names = []
    for m in resp.json().get("models", []):
        name = str(m.get("name", "")).split("/")[-1]
        if name and "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"]):
            names.append(name)
    return sorted(names)


def diagnose(provider: str | None = None) -> dict:
    """A cheap, specific answer to "why won't it connect".

    Deliberately separate from a real generation call: it distinguishes
    "can't reach the host", "reached it, key rejected", and "reached it, key
    fine, that model doesn't exist" — three things that all surface as one
    unhelpful error otherwise.
    """
    provider = provider or active_provider()
    if provider is None:
        return {"ok": False, "stage": "config", "detail": "No provider is configured."}
    model = model_for(provider)
    if provider == "gemini":
        try:
            available = list_remote_models("gemini")
        except requests.RequestException as exc:
            return {"ok": False, "stage": "network", "provider": provider, "model": model,
                    "detail": _network_error_detail(provider, exc)}
        except RuntimeError as exc:
            return {"ok": False, "stage": "key", "provider": provider, "model": model,
                    "detail": f"Google answered, but rejected the request: {exc}"}
        if not available:
            # list_remote_models returns [] with no key and no network call —
            # claiming "reachable" there was a lie about work never done.
            return {"ok": False, "stage": "key", "provider": provider, "model": model,
                    "detail": "Google didn't return any usable models for this key. Check the key on "
                              "the Model & keys tab."}
        if model not in available:
            close = [m for m in available if m.startswith(model.rsplit("-", 1)[0])][:5]
            return {
                "ok": False, "stage": "model", "provider": provider, "model": model,
                "available": available,
                "detail": f"Your key works, but “{model}” isn't a model it can use."
                          + (f" Try one of: {', '.join(close)}." if close else ""),
            }
        return {"ok": True, "stage": "reachable", "provider": provider, "model": model,
                "available": available}
    return {"ok": True, "stage": "unchecked", "provider": provider, "model": model, "available": []}


def _ollama_reachable(base_url: str) -> bool:
    try:
        return requests.get(f"{base_url}/api/tags", timeout=3).ok
    except requests.RequestException:
        return False


def list_ollama_models() -> list[str]:
    """Names of models actually pulled locally, so Settings can offer real
    choices instead of a guessed default."""
    try:
        resp = requests.get(f"{_ollama_base_url()}/api/tags", timeout=3)
        if not resp.ok:
            return []
        return sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))
    except (requests.RequestException, ValueError):
        return []


def _combined(prompt: str, context: str) -> str:
    return f"{prompt}\n\nCourse material:\n{context}" if context else prompt


# -------------------------------------------------------------- providers --

def _call_openai(prompt: str, context: str) -> str:
    resp = requests.post(
        f"{_openai_base_url()}/chat/completions",
        headers={"Authorization": f"Bearer {api_key('openai')}", "Content-Type": "application/json"},
        json={
            "model": model_for("openai"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _combined(prompt, context)},
            ],
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if not resp.ok:
        _raise_for_response("openai", resp)
    return resp.json()["choices"][0]["message"]["content"]


def _call_anthropic(prompt: str, context: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key("anthropic"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model_for("anthropic"),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _combined(prompt, context)}],
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if not resp.ok:
        _raise_for_response("anthropic", resp)
    blocks = resp.json().get("content", [])
    # Messages API returns a list of content blocks; concatenate the text ones
    # so a reply split across blocks doesn't silently lose its tail.
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        raise RuntimeError("Claude returned no text content")
    return text


def _call_gemini(prompt: str, context: str) -> str:
    model = model_for("gemini")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        # Header rather than ?key= so the key never lands in a URL, which is
        # where keys end up in logs and error messages.
        headers={"x-goog-api-key": api_key("gemini"), "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": _combined(prompt, context)}]}],
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if not resp.ok:
        _raise_for_response("gemini", resp)
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        # Usually a safety block — say so instead of an IndexError.
        raise RuntimeError(f"Gemini returned no candidates: {data.get('promptFeedback', 'no detail')}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise RuntimeError(f"Gemini returned an empty reply (finishReason: {candidates[0].get('finishReason')})")
    return text


def _call_ollama(prompt: str, context: str) -> str:
    resp = requests.post(
        f"{_ollama_base_url()}/api/generate",
        json={"model": model_for("ollama"), "prompt": _combined(prompt, context), "stream": False},
        timeout=REQUEST_TIMEOUT_S,
    )
    if not resp.ok:
        _raise_for_response("ollama", resp)
    return resp.json()["response"]


_CALLERS = {
    "openai": _call_openai,
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
    "ollama": _call_ollama,
}


# -------------------------------------------------------------- selection --

def active_provider() -> str | None:
    """Which provider a call would use right now, or None if none is usable."""
    chosen = preferred_provider()
    if chosen != "auto":
        if chosen == "ollama":
            return "ollama" if _ollama_reachable(_ollama_base_url()) else None
        return chosen if api_key(chosen) else None
    for provider in PROVIDER_ORDER:
        if api_key(provider):
            return provider
    return "ollama" if _ollama_reachable(_ollama_base_url()) else None


def configured_providers() -> list[str]:
    """Every provider that could serve a request right now."""
    out = [p for p in PROVIDER_ORDER if api_key(p)]
    if _ollama_reachable(_ollama_base_url()):
        out.append("ollama")
    return out


def default_llm_fn(prompt: str, context: str) -> str:
    """The function every generation capability is wired to by default."""
    provider = active_provider()
    if provider is None:
        chosen = preferred_provider()
        if chosen != "auto":
            raise LLMNotConfiguredError(
                f"{PROVIDER_LABELS.get(chosen, chosen)} is selected in Settings but isn't usable — "
                + ("start Ollama and make sure it's reachable at " + _ollama_base_url()
                   if chosen == "ollama" else "add its API key on the Model & keys tab.")
            )
        raise LLMNotConfiguredError(
            "No model provider is configured. Add an API key for OpenAI, Claude, or Gemini on the "
            f"Model & keys tab, or start Ollama locally (expected at {_ollama_base_url()}) with "
            f"'{model_for('ollama')}' pulled."
        )
    return _call_with_recovery(provider, prompt, context)


def _attempt(provider: str, prompt: str, context: str) -> str:
    try:
        return _CALLERS[provider](prompt, context)
    except requests.exceptions.JSONDecodeError as exc:
        # A 200 with a non-JSON body is a captive portal or a proxy sign-in
        # page, not a busy provider. Retrying it three times just makes the
        # student wait longer for the same nothing, and the raw message —
        # "Expecting value: line 1 column 1 (char 0)" — explains nothing.
        raise RuntimeError(
            f"Got a reply from {PROVIDER_HOSTS.get(provider, provider)} that wasn't from the API — "
            "usually a wifi sign-in page in the way. Open a browser, get past the network's login, "
            "then try again."
        ) from exc
    except (KeyError, IndexError, TypeError) as exc:
        # An OpenAI-COMPATIBLE endpoint with a different envelope produced
        # KeyError('choices'), which str()s to the four characters 'choices'
        # and was the entire error the student saw.
        raise RuntimeError(
            f"{PROVIDER_LABELS.get(provider, provider)} replied in a shape this doesn't understand "
            f"({exc!r}). If you're pointing at a non-official endpoint, check the base URL."
        ) from exc
    except requests.RequestException as exc:
        # A read timeout is the same class of problem as a 503 — the provider
        # did not say no, it just did not answer — so it retries too.
        raise ProviderBusyError(_network_error_detail(provider, exc)) from exc


def _call_with_recovery(provider: str, prompt: str, context: str) -> str:
    """Retry a busy provider, then fall back to a local model if there is one.

    Both halves come from a real failure (2026-08-26): Google's free Gemini
    tier answered a chat request with HTTP 503 "This model is currently
    experiencing high demand" after 37 seconds. Nothing was wrong — not the
    key, not the model, not the network — but the app surfaced a raw HTTP dump
    and the student sat watching "Thinking…" for most of a minute first.

    Google itself calls those spikes temporary, so: retry with a short backoff.
    And if a local Ollama is running, use it rather than failing — a slower
    answer from a local model beats no answer, and it costs nothing. Only when
    the provider was chosen automatically: an explicit pick in Settings is a
    decision, and quietly answering from a different model would undermine it.
    """
    started = time.monotonic()
    last: Exception | None = None
    for delay in (*RETRY_BACKOFF_S, None):
        try:
            return _attempt(provider, prompt, context)
        except ProviderBusyError as exc:
            last = exc
            # Budget check BEFORE sleeping and before another full-timeout
            # attempt: the point is to bound the wait, not to bound the count.
            if delay is None or time.monotonic() - started + delay >= MAX_RECOVERY_SECONDS:
                break
            time.sleep(delay)

    if provider != "ollama" and preferred_provider() == "auto" and _ollama_reachable(_ollama_base_url()):
        try:
            reply = _attempt("ollama", prompt, context)
        except Exception as exc:  # noqa: BLE001 - the backup's failure is the actionable one
            # Do NOT discard this. The original error says "the cloud is
            # busy, try later"; the fallback's says "that model isn't
            # pulled" — which is the only one the student can act on, and it
            # was being thrown away while the activity log claimed the local
            # model had answered.
            raise RuntimeError(f"{last}\n\nYour local model couldn't take over either: {exc}") from exc
        # Announced only on success, and only after the fact — announcing
        # before the attempt logged a handover that never happened.
        notify(
            f"{PROVIDER_LABELS.get(provider, provider)} was busy — this answer came from your local "
            f"{model_for('ollama')} instead.",
            channel="console",
        )
        return reply
    raise RuntimeError(str(last) if last else "the model provider did not answer")


# Backwards-compatible aliases for the pre-2026-08-26 single-provider names.
_call_openai_compatible = _call_openai
_openai_model = lambda: model_for("openai")  # noqa: E731
_ollama_model = lambda: model_for("ollama")  # noqa: E731
