"""Arabic report generation — deterministic templates over the computed numbers.

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
    """Full Arabic analysis report as plain text with light markdown."""
    parts: list[str] = []
    parts.append(_header(result, tz))
    parts.append(_verdict(result))
    parts.append(_gates_section(result))
    parts.append(_risk_section(result))
    parts.append(_engines_section(result))
    parts.append(_math_section(result))
    parts.append(_caveats(result))
    return "\n".join(p for p in parts if p).strip()


# --------------------------------------------------------------------------- #


def _header(r: AnalysisResult, tz: str) -> str:
    return (
        f"{'═' * 58}\n"
        f"  {r.name_ar} ({r.symbol})\n"
        f"  {format_display(r.as_of, tz)} — بتوقيت {tz.split('/')[-1]}\n"
        f"  السعر الحالي: {r.spot:,.5f}".rstrip("0").rstrip(".") + "\n"
        f"{'═' * 58}"
    )


def _verdict(r: AnalysisResult) -> str:
    if r.direction is Direction.NEUTRAL or r.grade is Grade.NO_TRADE:
        headline = "⚪ لا توجد فرصة — الانتظار هو القرار الصحيح"
    else:
        arrow = "🔺 تحيّز شرائي" if r.direction is Direction.BULLISH else "🔻 تحيّز بيعي"
        headline = f"{arrow} — {r.grade.arabic} ({r.grade.value})"

    lines = [
        "\n## الخلاصة",
        f"**{headline}**",
        "",
        f"درجة الثقة: `{confidence_bar(r.confidence)}` **{r.confidence:.0%}**",
        f"حالة السوق: {r.regime.arabic}",
    ]

    blockers = r.blocking_failures
    if blockers:
        lines.append("")
        lines.append("**⛔ الإشارة موقوفة بسبب بوابات صلبة لم تتحقق:**")
        for gate in blockers:
            mark = "لم تُقيَّم" if gate.status is GateStatus.NOT_EVALUATED else "لم تتحقق"
            lines.append(f"   • {gate.label_ar} — {mark}: {gate.detail_ar}")
        lines.append("")
        lines.append(
            "> بوابة واحدة ساقطة تكفي لإلغاء الصفقة مهما ارتفعت درجة الثقة. "
            "هذا مقصود في التصميم."
        )
    elif r.is_actionable:
        lines.append("")
        lines.append("✅ كل البوابات الصلبة متحققة — الإعداد صالح للمتابعة.")
    return "\n".join(lines)


def _gates_section(r: AnalysisResult) -> str:
    if not r.gates:
        return ""
    lines = ["\n## البوابات الصلبة"]
    for gate in r.gates:
        tag = "" if gate.blocking else " _(تحذيرية)_"
        lines.append(f"{gate.icon} **{gate.label_ar}**{tag} — {gate.detail_ar}")
    return "\n".join(lines)


def _risk_section(r: AnalysisResult) -> str:
    if r.risk is None:
        return ""
    p = r.risk
    verb = "شراء" if r.direction is Direction.BULLISH else "بيع"
    lines = [
        "\n## خطة الصفقة",
        "| العنصر | القيمة |",
        "|---|---|",
        f"| الاتجاه | {verb} |",
        f"| الدخول | {p.entry:,.5f} |",
        f"| وقف الخسارة | {p.stop_loss:,.5f} |",
        f"| الهدف الأول (2R) | {p.take_profit_1:,.5f} |",
        f"| الهدف الثاني (3.5R) | {p.take_profit_2:,.5f} |",
        f"| مسافة الوقف | {p.stop_distance:,.5f} ({p.stop_distance / p.atr:.2f}× ATR) |",
        "",
        f"**أساس الوقف:** {p.basis_ar}",
    ]
    if p.position_size_hint:
        lines.append(f"**الحجم:** {p.position_size_hint}")
    lines.append("")
    lines.append(
        f"**ما الذي يُلغي هذه القراءة؟** إغلاق شمعة خلف {p.stop_loss:,.5f} "
        f"يُبطل الأساس البنيوي للإشارة — عندها يخرج القرار من التحليل إلى إدارة الخسارة."
    )
    return "\n".join(lines)


def _engines_section(r: AnalysisResult) -> str:
    lines = ["\n## تفصيل المحركات"]
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
        lines.append("")
        lines.append(
            f"### {engine.direction.emoji} {engine.engine.arabic}"
        )
        lines.append(
            f"الاتجاه: **{engine.direction.arabic}** · القوة: {engine.strength:.2f} · "
            f"الجودة: {engine.quality:.0%} · الوزن الفعّال: {c.effective_weight:.2f} · "
            f"المساهمة: **{c.contribution:+.3f}**"
        )
        for ev in engine.evidence[:6]:
            detail = f" — {ev.detail_ar}" if ev.detail_ar else ""
            if ev.contribution:
                lines.append(f"   {ev.icon} {ev.label_ar} `{ev.contribution:+.3f}`{detail}")
            else:
                lines.append(f"   ▫️ {ev.label_ar}{detail}")
        for note in engine.notes_ar:
            lines.append(f"   ⓘ {note}")

    for engine in non_voting:
        lines.append("")
        lines.append(f"### 📅 {engine.engine.arabic}")
        lines.append(
            "_لا يصوّت على الاتجاه بالتصميم — يعمل كمُعدِّل للثقة وكبوابة صلبة فقط._"
        )
        for ev in engine.evidence[:6]:
            detail = f" — {ev.detail_ar}" if ev.detail_ar else ""
            lines.append(f"   ▫️ {ev.label_ar}{detail}")
        for note in engine.notes_ar:
            lines.append(f"   ⓘ {note}")

    if skipped:
        lines.append("")
        lines.append("### المحركات التي لم تشارك")
        for engine in skipped:
            lines.append(f"   ➖ {engine.engine.arabic}: {engine.skipped_reason}")
    return "\n".join(lines)


def _math_section(r: AnalysisResult) -> str:
    b = r.breakdown
    return "\n".join([
        "\n## كيف حُسبت درجة الثقة",
        "```",
        f"إجماع المحركات   S = {b.raw_signed_score:+.4f}",
        f"بعد المعايرة     K = L(|S|) = {b.calibrated_consensus:.4f}",
        f"التشتت           D = {b.dispersion:.4f}",
        f"التماسك          C = 1 - λ·D = {b.coherence:.4f}",
        f"جودة البيانات      = {b.data_quality:.4f}",
        f"مُعدِّل الأخبار      = {b.news_factor:.4f}",
        f"ملاءمة نظام السوق  = {b.regime_fit:.4f}",
        "-" * 46,
        f"الثقة = K × C × أخبار × جودة × ملاءمة = {b.confidence:.4f}",
        "",
        f"الوزن الفعّال {b.total_effective_weight:.2f} من {b.available_weight:.2f} متاح "
        f"({b.coverage_ratio:.0%}) عبر {b.active_engines} محرك",
        "```",
    ])


def _caveats(r: AnalysisResult) -> str:
    lines = ["\n## ملاحظات وتحذيرات"]
    if r.data_quality_issues:
        for issue in r.data_quality_issues[:6]:
            lines.append(f"   ⚠️ {issue}")
    lines.append(
        "   ⓘ هذا تحليل آلي لجودة الإعداد، وليس توصية استثمارية ولا وعداً بنتيجة. "
        "لا يوجد نظام يضمن نسبة نجاح ثابتة."
    )
    lines.append(f"   ⓘ إصدار الإعدادات {r.config_version} · إصدار الكود {r.code_version}")
    return "\n".join(lines)


def summary_line(r: AnalysisResult) -> str:
    """One-line summary used in the daily digest and the dashboard table."""
    if r.grade is Grade.NO_TRADE or r.direction is Direction.NEUTRAL:
        return f"{r.symbol}: ⚪ لا فرصة ({r.confidence:.0%})"
    icon = "🔺" if r.direction is Direction.BULLISH else "🔻"
    flag = "" if not r.blocking_failures else " ⛔"
    return f"{r.symbol}: {icon} {r.grade.value} · ثقة {r.confidence:.0%}{flag}"
