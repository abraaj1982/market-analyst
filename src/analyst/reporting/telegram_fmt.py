"""Telegram message formatting (HTML parse mode).

HTML is used rather than MarkdownV2 because MarkdownV2 requires escaping 18
characters, several of which (`.`, `-`, `!`) appear constantly in price levels.
One missed escape means the whole message is rejected by the API — a silent
alert failure at exactly the wrong moment.
"""
from __future__ import annotations

from html import escape

from analyst.core.clock import format_display
from analyst.core.enums import Direction, Grade
from analyst.core.models import AnalysisResult
from analyst.reporting.narrative import confidence_bar

MAX_LENGTH = 4096


def format_alert(result: AnalysisResult, tz: str = "Asia/Muscat") -> str:
    r = result
    side = "LONG" if r.direction is Direction.BULLISH else "SHORT"
    lines = [
        f"<b>{escape(r.name)} ({escape(r.symbol)})</b>",
        f"{r.direction.emoji} {side} · <b>{r.grade.value}</b> — {r.grade.label}",
        "",
        f"Confidence: <code>{confidence_bar(r.confidence, 14)}</code> <b>{r.confidence:.0%}</b>",
        f"Spot: <code>{r.spot:,.5f}</code> · regime: {r.regime.label}",
    ]

    if r.risk is not None:
        lines += [
            "",
            "<b>Trade plan</b>",
            f"Entry: <code>{r.risk.entry:,.5f}</code>",
            f"Stop: <code>{r.risk.stop_loss:,.5f}</code>",
            f"Target 1: <code>{r.risk.take_profit_1:,.5f}</code> (2R)",
            f"Target 2: <code>{r.risk.take_profit_2:,.5f}</code> (3.5R)",
        ]

    top = [c for c in r.breakdown.contributions if abs(c.contribution) >= 0.05]
    top.sort(key=lambda c: abs(c.contribution), reverse=True)
    if top:
        lines += ["", "<b>Strongest engines</b>"]
        for c in top[:4]:
            lines.append(
                f"{c.direction.emoji} {escape(c.engine.label)} <code>{c.contribution:+.2f}</code>"
            )

    strongest = _strongest_evidence(r)
    if strongest:
        lines += ["", "<b>Key evidence</b>"]
        for text in strongest[:3]:
            lines.append(f"• {escape(text)}")

    warnings = [g for g in r.gates if not g.blocking and g.status.value != "passed"]
    if warnings:
        lines += ["", "<b>Warnings</b>"]
        for g in warnings[:3]:
            lines.append(f"⚠️ {escape(g.label)}: {escape(g.detail)}")

    lines += [
        "",
        f"<i>{format_display(r.as_of, tz)} — automated setup-quality read, "
        f"not investment advice.</i>",
    ]
    return _truncate("\n".join(lines))


def format_digest(results: list[AnalysisResult], tz: str = "Asia/Muscat") -> str:
    """Daily roundup, ranked by opportunity quality."""
    if not results:
        return "<b>Daily digest</b>\nNo analyses available."

    ranked = sorted(results, key=lambda r: (r.is_actionable, r.confidence), reverse=True)
    stamp = format_display(ranked[0].as_of, tz, "%Y-%m-%d")
    lines = [f"<b>Daily digest — {stamp}</b>", ""]

    actionable = [r for r in ranked if r.is_actionable]
    if actionable:
        lines.append("<b>Qualified setups</b>")
        for r in actionable:
            entry = f" · entry <code>{r.risk.entry:,.5f}</code>" if r.risk else ""
            lines.append(
                f"{r.direction.emoji} <b>{escape(r.symbol)}</b> · {r.grade.value} "
                f"· {r.confidence:.0%}{entry}"
            )
        lines.append("")
    else:
        lines += [
            "<b>No qualified setup today</b>",
            "Standing aside is a decision, not an absence of analysis.",
            "",
        ]

    lines.append("<b>Rest of the watchlist</b>")
    for r in ranked:
        if r.is_actionable:
            continue
        blocked = ""
        if r.blocking_failures and r.grade is not Grade.NO_TRADE:
            blocked = f" [blocked: {escape(r.blocking_failures[0].label)}]"
        lines.append(
            f"• {escape(r.symbol)}: {r.direction.emoji} {r.confidence:.0%} "
            f"({r.grade.value}){blocked}"
        )

    lines += ["", "<i>Automated setup-quality read. No system guarantees a fixed success rate.</i>"]
    return _truncate("\n".join(lines))


def _strongest_evidence(result: AnalysisResult) -> list[str]:
    items = [
        (abs(ev.contribution), f"{ev.label} — {ev.detail}" if ev.detail else ev.label)
        for engine in result.engines
        if engine.active
        for ev in engine.evidence
        if ev.contribution
    ]
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [text for _, text in items]


def _truncate(text: str) -> str:
    if len(text) <= MAX_LENGTH:
        return text
    return text[: MAX_LENGTH - 24].rsplit("\n", 1)[0] + "\n<i>… message truncated</i>"
