"""The AI interpreter: facts building, response parsing, and caching.

No real network call is ever made here — a FakeProvider stands in for
Claude, so these tests are as offline and deterministic as the rest of the
suite.
"""
from __future__ import annotations

import json

import pandas as pd

from analyst.ai.interpreter import build_facts, get_or_generate, parse_response
from analyst.ai.provider import AIProviderError
from analyst.storage.analyses import save_analysis
from tests.conftest import ANCHOR

_VALID_RESPONSE = {
    "market_bias": "Bullish",
    "main_reason": "Multi-timeframe trend and structure agree.",
    "supporting_evidence": ["4H trend is up", "Daily structure making higher highs"],
    "bullish_scenario": "Continuation toward the next liquidity pool above.",
    "bearish_scenario": "A break of the last swing low would invalidate this.",
    "key_levels": ["Support at recent swing low", "Resistance at prior high"],
    "invalidation_condition": "A daily close below the last swing low.",
    "risk_warnings": ["High-impact news release within the next 24h"],
    "conflicting_evidence": ["RSI is near overbought on the 1H"],
    "final_summary": "A potential bullish setup, not a guarantee.",
}


class FakeProvider:
    """Records what it was asked and returns a canned response."""

    configured = True
    model = "fake-model"

    def __init__(self, text: str | None = None, raise_error: bool = False):
        self.text = text if text is not None else json.dumps(_VALID_RESPONSE)
        self.raise_error = raise_error
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if self.raise_error:
            raise AIProviderError("simulated failure")
        return self.text


class NotConfiguredProvider:
    configured = False

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - never called
        raise AssertionError("must not be called when not configured")


def test_build_facts_pulls_engine_and_gate_detail(pipeline, gold):
    result = pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime())
    save_analysis(result)
    from analyst.storage.analyses import history_for

    row = history_for("XAUUSD", limit=1)[0]

    facts = build_facts(row)
    assert facts["symbol"] == "XAUUSD"
    assert len(facts["engines"]) == len(result.engines)
    assert len(facts["gates"]) == len(result.gates)
    # every engine entry either has evidence or explains why it stood aside
    for engine in facts["engines"]:
        assert engine["skipped_reason"] or "top_evidence" in engine


def test_parse_response_reads_a_clean_json_object():
    parsed = parse_response(json.dumps(_VALID_RESPONSE))
    assert parsed.market_bias == "Bullish"
    assert parsed.supporting_evidence == _VALID_RESPONSE["supporting_evidence"]


def test_parse_response_strips_markdown_fences():
    fenced = "```json\n" + json.dumps(_VALID_RESPONSE) + "\n```"
    parsed = parse_response(fenced)
    assert parsed.market_bias == "Bullish"


def test_parse_response_degrades_gracefully_on_garbage():
    parsed = parse_response("not json at all")
    assert "could not be parsed" in parsed.main_reason.lower()


def test_get_or_generate_without_a_key_reports_not_configured(pipeline, gold):
    save_analysis(pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime()))
    result = get_or_generate("XAUUSD", provider=NotConfiguredProvider())
    assert result["status"] == "not_configured"


def test_get_or_generate_with_no_stored_analysis():
    result = get_or_generate("NOPE", provider=FakeProvider())
    assert result["status"] == "no_analysis"


def test_get_or_generate_calls_the_provider_once_then_caches(pipeline, gold):
    save_analysis(pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime()))
    provider = FakeProvider()

    first = get_or_generate("XAUUSD", provider=provider)
    assert first["status"] == "ok"
    assert first["market_bias"] == "Bullish"
    assert provider.calls == 1

    second = get_or_generate("XAUUSD", provider=provider)
    assert second["status"] == "ok"
    assert provider.calls == 1, "a second call against the same analysis must be served from cache"


def test_get_or_generate_regenerates_after_a_new_analysis(pipeline, gold):
    save_analysis(pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime()))
    provider = FakeProvider()
    get_or_generate("XAUUSD", provider=provider)
    assert provider.calls == 1

    later = ANCHOR + pd.Timedelta(hours=1)
    save_analysis(pipeline.analyse(gold, as_of=later.to_pydatetime()))
    get_or_generate("XAUUSD", provider=provider)
    assert provider.calls == 2, "a fresh analysis must invalidate the cached read"


def test_get_or_generate_surfaces_provider_errors(pipeline, gold):
    save_analysis(pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime()))
    result = get_or_generate("XAUUSD", provider=FakeProvider(raise_error=True))
    assert result["status"] == "error"
