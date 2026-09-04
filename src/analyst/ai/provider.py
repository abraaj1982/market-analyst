"""AI provider abstraction.

The interpreter never talks to a vendor SDK directly -- it calls this
interface, so the model or vendor can be swapped (Claude today, OpenAI or
Gemini later) without touching anything upstream. A provider's only job is
"send this prompt, return the text response"; it must not parse, validate or
otherwise understand the content -- that is the interpreter's job.
"""
from __future__ import annotations

from typing import Protocol


class AIProviderError(RuntimeError):
    """Raised when a provider cannot produce a response (network, auth, quota)."""


class AIProvider(Protocol):
    def complete(self, system: str, user: str) -> str:
        """Send a system + user prompt, return the raw text response."""
        ...
