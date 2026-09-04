"""Turns a stored, deterministic analysis into a plain-language read.

Hard boundary: the AI receives only facts the engines already computed
(direction, confidence, evidence sentences, gates, the risk plan) and is
asked to interpret them -- never to compute a number, invent a price, or
override a gate. Every prompt tells it to separate FACTS from INTERPRETATION
from SCENARIOS, and to say so plainly when the data is too thin to read.

Calls are made at most once per stored analysis: the result is cached back
onto the analysis row (`payload["ai"]`) and only regenerated when a new
analysis (a new `as_of`) has landed, so opening a symbol repeatedly, or the
scheduler ticking every 60s, never re-spends an API call for nothing.
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field
from sqlalchemy import select

from analyst.ai.claude_provider import ClaudeProvider
from analyst.ai.provider import AIProvider, AIProviderError
from analyst.core.clock import now_utc, to_utc
from analyst.core.enums import EngineId
from analyst.storage.db import session_scope
from analyst.storage.models import Analysis

log = logging.getLogger(__name__)

_RESPONSE_KEYS = (
    "market_bias", "main_reason", "supporting_evidence", "bullish_scenario",
    "bearish_scenario", "key_levels", "invalidation_condition", "risk_warnings",
    "conflicting_evidence", "final_summary",
)

_SYSTEM_PROMPT = """You are a market analysis interpreter for a personal, single-user \
decision-support tool. You are given a JSON object of facts already computed by \
deterministic code (direction, confidence, per-engine evidence, gates, risk levels). \
You do not calculate anything and you do not know any prices or facts beyond what is \
in that JSON -- if something is not there, say the data is insufficient rather than \
inventing it.

Rules:
- Never call anything "guaranteed", "certain", or "risk-free". Use "potential", \
"bias", "scenario", "setup detected".
- Clearly separate FACTS (what the JSON says) from INTERPRETATION (your reading of \
it) from SCENARIOS (what could happen next, both directions).
- If gates are failing or confidence is low, say plainly that this is not a \
tradeable setup right now.
- Respond with ONLY a JSON object (no markdown fences, no prose outside it) with \
exactly these keys: market_bias, main_reason, supporting_evidence (array of \
strings), bullish_scenario, bearish_scenario, key_levels (array of strings), \
invalidation_condition, risk_warnings (array of strings), conflicting_evidence \
(array of strings), final_summary."""


class AIInterpretation(BaseModel):
    market_bias: str = ""
    main_reason: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)
    bullish_scenario: str = ""
    bearish_scenario: str = ""
    key_levels: list[str] = Field(default_factory=list)
    invalidation_condition: str = ""
    risk_warnings: list[str] = Field(default_factory=list)
    conflicting_evidence: list[str] = Field(default_factory=list)
    final_summary: str = ""


def _engine_label(engine_id: str) -> str:
    try:
        return EngineId(engine_id).label
    except ValueError:
        return engine_id


def build_facts(row: Analysis) -> dict:
    payload = row.payload or {}
    engines = payload.get("engines", [])
    gates = payload.get("gates", [])

    return {
        "symbol": row.symbol,
        "name": row.name,
        "as_of": to_utc(row.as_of).isoformat(),
        "spot": row.spot,
        "direction": row.direction,
        "confidence": row.confidence,
        "grade": row.grade,
        "regime": row.regime,
        "engines": [
            {
                "school": _engine_label(e.get("engine", "")),
                "direction": e.get("direction"),
                "strength": e.get("strength"),
                "skipped_reason": e.get("skipped_reason"),
                "top_evidence": [
                    {"label": ev.get("label"), "detail": ev.get("detail")}
                    for ev in sorted(
                        e.get("evidence", []),
                        key=lambda ev: abs(ev.get("contribution", 0)),
                        reverse=True,
                    )[:3]
                ],
                "notes": e.get("notes", []),
            }
            for e in engines
        ],
        "gates": [
            {
                "label": g.get("label"), "status": g.get("status"),
                "blocking": g.get("blocking"), "detail": g.get("detail"),
            }
            for g in gates
        ],
        "risk_plan": payload.get("risk"),
    }


def build_prompt(facts: dict) -> tuple[str, str]:
    user = (
        "Here are the facts for one instrument at one point in time. Interpret them "
        "per the rules above and return the JSON object.\n\n" + json.dumps(facts, indent=2)
    )
    return _SYSTEM_PROMPT, user


def parse_response(text: str) -> AIInterpretation:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    raw = match.group(0) if match else text
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("AI response was not valid JSON: %s", text[:200])
        return AIInterpretation(
            main_reason="The AI response could not be parsed.",
            final_summary="Try again — this was a formatting issue, not a data problem.",
        )
    return AIInterpretation(**{k: data.get(k) for k in _RESPONSE_KEYS if data.get(k) is not None})


def get_or_generate(symbol: str, provider: AIProvider | None = None) -> dict:
    """Return the cached AI read for the latest analysis, generating one if needed."""
    provider = provider or ClaudeProvider()

    with session_scope() as session:
        row = session.execute(
            select(Analysis).where(Analysis.symbol == symbol.upper())
            .order_by(Analysis.as_of.desc()).limit(1)
        ).scalars().first()
        if row is None:
            return {"status": "no_analysis", "message": f"No stored analysis for {symbol.upper()}"}

        as_of = to_utc(row.as_of).isoformat()
        cached = (row.payload or {}).get("ai")
        if cached and cached.get("as_of") == as_of:
            return {"status": "ok", **cached}

        if not getattr(provider, "configured", True):
            return {
                "status": "not_configured",
                "message": "Set ANTHROPIC_API_KEY to turn on the AI analyst.",
            }

        facts = build_facts(row)
        system, user = build_prompt(facts)
        try:
            text = provider.complete(system, user)
        except AIProviderError as exc:
            log.warning("AI interpretation failed for %s: %s", symbol, exc)
            return {"status": "error", "message": str(exc)}

        interpretation = parse_response(text).model_dump()
        interpretation["as_of"] = as_of
        interpretation["generated_at"] = now_utc().isoformat()
        interpretation["model"] = getattr(provider, "model", "unknown")

        row.payload = {**(row.payload or {}), "ai": interpretation}
        session.add(row)

        return {"status": "ok", **interpretation}
