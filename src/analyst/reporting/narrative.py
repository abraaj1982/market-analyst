"""Report generation — deterministic templates over the computed numbers.

There is no language model anywhere in this file, and that is a design decision
rather than a limitation. Every sentence is derived from a value that already
exists in the `AnalysisResult`, which means:

  * the report can never contradict the numbers, or invent a reason
  * it costs nothing and needs no API key
  * it is byte-identical for identical inputs, so it can be diffed and tested

The structure mirrors how a careful analyst actually writes: the verdict first,
then what would invalidate it, then the evidence, then the caveats.
"""
from __future__ import annotations

from analyst.core.clock import format_display
from analyst.core.enums import Direction, GateStatus, Grade
from analyst.core.models import AnalysisResult

_BAR_FULL = "█"
_BAR_EMPTY = "░"


def confidence_bar(value: float, width: int = 20) -> str:
    filled = max(0, min(width, round(value * width)))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def build_report(result: AnalysisResult, tz: str = "Asia/Muscat") -> str:
    """Full analysis report as plain text with light markdown."""
    parts = [
        _header(result, tz),
        _verdict(result),
        _gates_section(result),
        _risk_section(result),
        _engines_section(result),
        _math_section(result),
        _caveats(result),
    ]
    return "\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------- #


def _header(r: AnalysisResult, tz: str) -> str:
    price = f"{r.spot:,.5f}".rstrip("0").rstrip(".")
    return (
        f"{'=' * 62}\n"
        f"  {r.name} ({r.symbol})\n"
        f"  {format_display(r.as_of, tz)} — {tz}\n"
        f"  Spot: {price}\n"
        f"{'=' * 62}"
    )


def _verdict(r: AnalysisResult) -> str:
    if r.direction is Direction.NEUTRAL or r.grade is Grade.NO_TRADE:
        headline = "NO SETUP — standing aside is the correct decision"
    else:
        bias = "LONG BIAS" if r.direction is Direction.BULLISH else "SHORT BIAS"
        headline = f"{r.direction.emoji} {bias} — {r.grade.label} ({r.grade.value})"

    lines = [
        "\n## Verdict",
        f"**{headline}**",
        "",
        f"Confidence: `{confidence_bar(r.confidence)}` **{r.confidence:.0%}**",
        f"Market regime: {r.regime.label}",
    ]

    blockers = r.blocking_failures
    if blockers:
        lines += ["", "**BLOCKED — hard gates not satisfied:**"]
        for gate in blockers:
            state = "not evaluated" if gate.status is GateStatus.NOT_EVALUATED else "failed"
            lines.append(f"   - {gate.label} ({state}): {gate.detail}")
        lines += [
            "",
            "> One failed gate cancels the trade no matter how high the confidence. "
            "That is deliberate.",
        ]
    elif r.is_actionable:
        lines += ["", "All hard gates satisfied — the setup is valid to act on."]
    return "\n".join(lines)


def _gates_section(r: AnalysisResult) -> str:
    if not r.gates:
        return ""
    lines = ["\n## Hard gates"]
    for gate in r.gates:
        tag = "" if gate.blocking else " _(advisory)_"
        lines.append(f"{gate.icon} **{gate.label}**{tag} — {gate.detail}")
    return "\n".join(lines)


def _risk_section(r: AnalysisResult) -> str:
    if r.risk is None:
        return ""
    p = r.risk
    side = "Buy" if r.direction is Direction.BULLISH else "Sell"
    lines = [
        "\n## Trade plan",
        "| Item | Value |",
        "|---|---|",
        f"| Side | {side} |",
        f"| Entry | {p.entry:,.5f} |",
        f"| Stop loss | {p.stop_loss:,.5f} |",
        f"| Target 1 (2R) | {p.take_profit_1:,.5f} |",
        f"| Target 2 (3.5R) | {p.take_profit_2:,.5f} |",
        f"| Stop distance | {p.stop_distance:,.5f} ({p.stop_distance / p.atr:.2f}x ATR) |",
        "",
        f"**Stop basis:** {p.basis}",
    ]
    if p.position_size_hint:
        lines.append(f"**Sizing:** {p.position_size_hint}")
    lines += [
        "",
        f"**What invalidates this read?** A close beyond {p.stop_loss:,.5f} breaks the "
        "structural basis of the signal — at that point the decision leaves analysis "
        "and becomes loss management.",
    ]
    return "\n".join(lines)


def _engines_section(r: AnalysisResult) -> str:
    lines = ["\n## Engine breakdown"]
    contributions = {c.engine: c for c in r.breakdown.contributions}

    # The news engine appears among the engines but never votes, so it has no
    # contribution row. It gets its own section rather than a misleading 0.000.
    voters = [e for e in r.engines if e.active and e.engine in contributions]
    non_voting = [e for e in r.engines if e.active and e.engine not in contributions]
    skipped = [e for e in r.engines if not e.active]

    for engine in sorted(
        voters, key=lambda e: abs(contributions[e.engine].contribution), reverse=True
    ):
        c = contributions[engine.engine]
        lines += [
            "",
            f"### {engine.direction.emoji} {engine.engine.label}",
            f"Direction: **{engine.direction.label}** · strength {engine.strength:.2f} · "
            f"quality {engine.quality:.0%} · effective weight {c.effective_weight:.2f} · "
            f"contribution **{c.contribution:+.3f}**",
        ]
        for ev in engine.evidence[:6]:
            detail = f" — {ev.detail}" if ev.detail else ""
            if ev.contribution:
                lines.append(f"   {ev.icon} {ev.label} `{ev.contribution:+.3f}`{detail}")
            else:
                lines.append(f"   - {ev.label}{detail}")
        for note in engine.notes:
            lines.append(f"   (i) {note}")

    for engine in non_voting:
        lines += [
            "",
            f"### {engine.engine.label}",
            "_Does not vote on direction by design — it acts as a confidence "
            "modifier and a hard gate only._",
        ]
        for ev in engine.evidence[:6]:
            detail = f" — {ev.detail}" if ev.detail else ""
            lines.append(f"   - {ev.label}{detail}")
        for note in engine.notes:
            lines.append(f"   (i) {note}")

    if skipped:
        lines += ["", "### Engines that stood aside"]
        for engine in skipped:
            lines.append(f"   - {engine.engine.label}: {engine.skipped_reason}")
    return "\n".join(lines)


def _math_section(r: AnalysisResult) -> str:
    b = r.breakdown
    return "\n".join([
        "\n## How the confidence was computed",
        "```",
        f"consensus        S = {b.raw_signed_score:+.4f}",
        f"calibrated       K = L(|S|) = {b.calibrated_consensus:.4f}",
        f"dispersion       D = {b.dispersion:.4f}",
        f"coherence        C = 1 - lambda*D = {b.coherence:.4f}",
        f"data quality       = {b.data_quality:.4f}",
        f"news factor        = {b.news_factor:.4f}",
        f"regime fit         = {b.regime_fit:.4f}",
        "-" * 52,
        f"confidence = K x C x news x quality x regime = {b.confidence:.4f}",
        "",
        f"effective weight {b.total_effective_weight:.2f} of {b.available_weight:.2f} available "
        f"({b.coverage_ratio:.0%}) across {b.active_engines} engines",
        "```",
    ])


def _caveats(r: AnalysisResult) -> str:
    lines = ["\n## Notes and caveats"]
    for issue in r.data_quality_issues[:6]:
        lines.append(f"   ! {issue}")
    lines += [
        "   (i) This is an automated read of setup quality. It is not investment "
        "advice and not a promise of any outcome. No system guarantees a fixed "
        "success rate.",
        f"   (i) Config version {r.config_version} · code version {r.code_version}",
    ]
    return "\n".join(lines)


def summary_line(r: AnalysisResult) -> str:
    """One-line summary used in the daily digest and the dashboard table."""
    if r.grade is Grade.NO_TRADE or r.direction is Direction.NEUTRAL:
        return f"{r.symbol}: no setup ({r.confidence:.0%})"
    flag = "" if not r.blocking_failures else " [BLOCKED]"
    return f"{r.symbol}: {r.direction.emoji} {r.grade.value} · {r.confidence:.0%}{flag}"
