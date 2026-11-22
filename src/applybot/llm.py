"""Tiny LLM client. Two real providers (ollama, anthropic) over plain HTTP.

Anything with a `complete_json(system, user) -> dict` method works, which is
how the tests inject a fake.
"""
from __future__ import annotations

import json
import os

import httpx


class LLMError(RuntimeError):
    """The model answered but not with usable JSON. Retried once."""


class LLMAPIError(RuntimeError):
    """The provider refused the request. Carries the provider's own message.
    Not retried: repeating a rejected request cannot succeed."""


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError(f"no JSON object in model output: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON from model: {e}") from e


def _check(r: httpx.Response, who: str) -> None:
    if r.status_code >= 400:
        raise LLMAPIError(f"{who} returned {r.status_code}: {r.text[:500]}")


def _post(who: str, url: str, timeout: float, **kw) -> httpx.Response:
    """httpx.post with failures turned into messages that say which provider and what to do."""
    try:
        return httpx.post(url, timeout=timeout, **kw)
    except httpx.TimeoutException:
        hint = ("The model is slow or still loading. Try again, check `ollama ps`, move this stage to anthropic on the Models page, "
                "or raise llm.ollama.timeout_seconds in settings.yaml." if who == "Ollama"
                else "Try again; if it keeps happening, raise llm.anthropic.timeout_seconds in settings.yaml.")
        raise LLMAPIError(f"{who} did not answer within {int(timeout)} seconds. {hint}") from None
    except httpx.ConnectError:
        raise LLMAPIError(f"could not connect to {who} at {url}. "
                          + ("Is Ollama running (`ollama serve`)?" if who == "Ollama" else "Check your internet connection.")) from None


PROVIDERS = ("ollama", "anthropic")
STAGES = ("score", "draft", "revise", "verify", "reply", "extract")


def for_stage(llm, name: str):
    """The client to use for one pipeline stage. Anything without per-stage
    routing (the test fakes) is used as it is."""
    pick = getattr(llm, "stage", None)
    return pick(name) if pick else llm


class LLM:
    def __init__(self, settings: dict, provider: str | None = None):
        self.settings = settings
        self.cfg = settings["llm"]
        self.provider = provider or self.cfg["provider"]
        self.temperature = self.cfg.get("temperature", 0)
        self._by_provider: dict[str, "LLM"] = {}
        if provider is None:  # top-level client: reject typos in the routing table
            if self.provider not in PROVIDERS:
                raise LLMAPIError(f"llm.provider must be one of {PROVIDERS}, got {self.provider!r}")
            for stage, prov in (self.cfg.get("stages") or {}).items():
                if stage not in STAGES:
                    raise LLMAPIError(f"llm.stages: unknown stage {stage!r}; use one of {STAGES}")
                if prov not in PROVIDERS:
                    raise LLMAPIError(f"llm.stages.{stage}: unknown provider {prov!r}; use one of {PROVIDERS}")

    def stage(self, name: str) -> "LLM":
        """Client for a pipeline stage: llm.stages.<name> if set, else the default provider."""
        stages = self.cfg.get("stages") or {}
        # Rewrites follow the drafting stage unless `revise` is set on its own.
        prov = stages.get(name) or (stages.get("draft") if name == "revise" else None) or self.provider
        if prov == self.provider:
            return self
        if prov not in self._by_provider:
            self._by_provider[prov] = LLM(self.settings, provider=prov)
        return self._by_provider[prov]

    def describe(self) -> str:
        return "  ".join(f"{s}={self.stage(s).provider}" for s in STAGES)

    def _raw(self, system: str, user: str) -> str:
        if self.provider == "ollama":
            c = self.cfg["ollama"]
            r = _post(
                "Ollama", f"{c['host']}/api/chat", c.get("timeout_seconds", 300),
                json={
                    "model": c["model"],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": self.temperature},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            _check(r, "Ollama")
            return r.json()["message"]["content"]
        if self.provider == "anthropic":
            c = self.cfg["anthropic"]
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise LLMAPIError("ANTHROPIC_API_KEY is not set")
            body = {
                "model": c["model"],
                "max_tokens": c.get("max_tokens", 2000),
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            # Newer Claude models reject `temperature` ("deprecated for this
            # model"), so it is only sent if you set llm.anthropic.temperature.
            if c.get("temperature") is not None:
                body["temperature"] = c["temperature"]
            r = _post(
                "Anthropic API", "https://api.anthropic.com/v1/messages", c.get("timeout_seconds", 120),
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json=body,
            )
            _check(r, "Anthropic API")
            return "".join(b.get("text", "") for b in r.json()["content"])
        raise LLMAPIError(f"unknown llm provider {self.provider!r}")

    def complete_json(self, system: str, user: str) -> dict:
        last: Exception | None = None
        for _ in range(2):  # one retry on malformed JSON
            try:
                return extract_json(self._raw(system, user))
            except LLMError as e:
                last = e
        raise LLMError(str(last))
