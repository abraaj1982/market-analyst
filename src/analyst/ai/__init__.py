"""Optional AI interpretation layer.

Everything here is additive: the deterministic engines and reports work
exactly as before with no key configured. See `interpreter.py` for the
contract this layer must honour (facts in, interpretation out — never the
other way around).
"""
from __future__ import annotations

from analyst.ai.interpreter import AIInterpretation, chat_about_analysis, get_or_generate

__all__ = ["AIInterpretation", "chat_about_analysis", "get_or_generate"]
