"""Telegram message formatting (HTML parse mode).

HTML is used rather than MarkdownV2 because MarkdownV2 requires escaping 18
characters, several of which (`.`, `-`, `!`) appear constantly in price levels
and Arabic punctuation. One missed escape means the whole message is rejected by
the API — a silent alert failure at exactly the wrong moment.
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
    arrow = "🔺 شراء" if r.direction is Direction.BULLISH else "🔻 بيع"
    lines = [
        f"<b>{escape(r.name_ar)} ({escape(r.symbol)})</b>",
        f"{arrow} · <b>{r.grade.value}</b> — {r.grade.arabic}",
        "",
        f"الثقة: <code>{confidence_bar(r.confidence, 14)}</code> <b>{r.confidence:.0%}</b>",
        f"السعر: <code>{r.spot:,.5f}</code> · حالة السوق: {r.regime.arabic}",
    ]

    if r.risk is not None:
        lines += [
            "",
            "<b>خطة الصفقة</b>",
            f"الدخول: <code>{r.risk.entry:,.5f}</code>",
            f"الوقف: <code>{r.risk.stop_loss:,.5f}</code>",
            f"الهدف 1: <code>{r.risk.take_profit_1:,.5f}</code> (2R)",
            f"الهدف 2: <code>{r.risk.take_profit_2:,.5f}</code> (3.5R)",
        ]

    top = [c for c in r.breakdown.contributions if abs(c.contribution) >= 0.05]
    top.sort(key=lambda c: abs(c.contribution), reverse=True)
    if top:
        lines += ["", "<b>أقوى المحركات</b>"]
        for c in top[:4]:
            lines.append(
                f"{c.direction.emoji} {escape(c.engine.arabic)} <code>{c.contribution:+.2f}</code>"
            )

    strongest = _strongest_evidence(r)
    if strongest:
        lines += ["", "<b>أبرز الأدلة</b>"]
        for text in strongest[:3]:
            lines.append(f"• {escape(text)}")

    warnings = [g for g in r.gates if not g.blocking and g.status.value != "passed"]
    if warnings:
        lines += ["", "<b>تحذيرات</b>"]
        for g in warnings[:3]:
            lines.append(f"⚠️ {escape(g.label_ar)}: {escape(g.detail_ar)}")

    lines += [
        "",
        f"<i>{format_display(r.as_of, tz)} — تحليل آلي لجودة الإعداد، ليس توصية استثمارية.</i>",
    ]
    return _truncate("\n".join(lines))


def format_digest(results: list[AnalysisResult], tz: str = "Asia/Muscat") -> str:
    """Daily roundup, ranked by opportunity quality."""
    if not results:
        return "<b>التقرير اليومي</b>\nلا توجد تحليلات متاحة."

    ranked = sorted(results, key=lambda r: (r.is_actionable, r.confidence), reverse=True)
    stamp = format_display(ranked[0].as_of, tz, "%Y-%m-%d")
    lines = [f"<b>📊 التقرير اليومي — {stamp}</b>", ""]

    actionable = [r for r in ranked if r.is_actionable]
    if actionable:
        lines.append("<b>فرص مؤهلة</b>")
        for r in actionable:
            icon = "🔺" if r.direction is Direction.BULLISH else "🔻"
            lines.append(
                f"{icon} <b>{escape(r.symbol)}</b> · {r.grade.value} · ثقة {r.confidence:.0%}"
                + (f" · دخول <code>{r.risk.entry:,.5f}</code>" if r.risk else "")
            )
        lines.append("")
    else:
        lines += ["<b>لا توجد فرصة مؤهلة اليوم</b>", "الانتظار قرار صحيح، وليس غياباً للتحليل.", ""]

    lines.append("<b>بقية المتابعة</b>")
    for r in ranked:
        if r.is_actionable:
            continue
        blocked = ""
        if r.blocking_failures and r.grade is not Grade.NO_TRADE:
            blocked = f" ⛔ {escape(r.blocking_failures[0].label_ar)}"
        lines.append(
            f"• {escape(r.symbol)}: {r.direction.emoji} {r.confidence:.0%} "
            f"({r.grade.value}){blocked}"
        )

    lines += ["", "<i>تحليل آلي لجودة الإعداد. لا يضمن أي نظام نسبة نجاح ثابتة.</i>"]
    return _truncate("\n".join(lines))


def _strongest_evidence(result: AnalysisResult) -> list[str]:
    items = [
        (abs(ev.contribution), f"{ev.label_ar} — {ev.detail_ar}" if ev.detail_ar else ev.label_ar)
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
    return text[: MAX_LENGTH - 20].rsplit("\n", 1)[0] + "\n<i>… اختُصرت الرسالة</i>"
