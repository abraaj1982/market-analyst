"""Claude implementation of AIProvider, called directly over HTTP.

A plain REST call rather than the vendor SDK -- one dependency (httpx,
already required elsewhere) instead of a second HTTP stack, and the contract
is small enough (one endpoint, one shape) that the SDK buys little.
"""
from __future__ import annotations

import os

import httpx

from analyst.ai.provider import AIProviderError

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class ClaudeProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or os.getenv("ANALYST_AI_MODEL", _DEFAULT_MODEL)
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str) -> str:
        if not self.configured:
            raise AIProviderError("ANTHROPIC_API_KEY is not set")
        try:
            resp = httpx.post(
                _ENDPOINT,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": _API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1200,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AIProviderError(f"Claude request failed: {exc}") from exc

        data = resp.json()
        parts = data.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        if not text:
            raise AIProviderError("Claude returned no text content")
        return text
