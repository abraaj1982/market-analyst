"""Command line interface.

Every command works offline with `--offline`, which uses deterministic synthetic
data. That is what makes it possible to try the system before configuring
anything at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from analyst.core.clock import format_display, now_utc
from analyst.core.config import (
    Secrets,
    active_instruments,
    load_settings,
    reset_caches,
)
from analyst.core.enums import Direction, Grade
from analyst.logging_setup import configure as configure_logging
from analyst.storage.db import init_db
from analyst.tracking import stats as stats_module
from analyst.version import __version__, code_version

app = typer.Typer(
    add_completion=False,
    help="محلل الأسواق — تحليل متعدد المحركات بدرجة ثقة رقمية شفافة",
)
console = Console()

_GRADE_STYLE = {
    Grade.A_PLUS: "bold green", Grade.A: "green", Grade.B: "yellow",
    Grade.C: "dim yellow", Grade.NO_TRADE: "dim",
}


@app.command()
def analyze(
    symbols: list[str] = typer.Argument(None, help="رموز محددة، أو اتركها فارغة لكل القائمة"),
    offline: bool = typer.Option(False, "--offline", help="بيانات تركيبية بدون إنترنت أو مفاتيح"),
    full: bool = typer.Option(False, "--full", help="اطبع التقرير الكامل لكل رمز"),
    no_save: bool = typer.Option(False, "--no-save", help="لا تحفظ في قاعدة البيانات"),
    no_alert: bool = typer.Option(False, "--no-alert", help="لا ترسل تنبيهات"),
    log_level: str = typer.Option("WARNING", "--log-level"),
) -> None:
    """تشغيل التحليل على قائمة المتابعة."""
    configure_logging(log_level)
    from analyst.runner import build_service

    service = build_service(offline=offline)
    with console.status("[cyan]جارٍ التحليل…"):
        results = service.run_once(
            persist=not no_save, alert=not no_alert, symbols=list(symbols) if symbols else None
        )

    if not results:
        console.print("[red]لم يُنتج أي تحليل. راجع السجل بـ --log-level INFO[/red]")
        raise typer.Exit(1)

    _print_table(results, service.settings.timezone.display)
    if full:
        for result in sorted(results, key=lambda r: -r.confidence):
            console.print(Panel(result.report_ar, border_style="cyan", expand=False))


@app.command()
def report(
    symbol: str = typer.Argument(..., help="رمز واحد"),
    offline: bool = typer.Option(False, "--offline"),
    save_to: Path = typer.Option(None, "--save-to", help="احفظ التقرير في ملف"),
) -> None:
    """طباعة التقرير الكامل لرمز واحد."""
    configure_logging("WARNING")
    from analyst.runner import build_service

    service = build_service(offline=offline)
    instrument = next(
        (i for i in active_instruments() if i.symbol.upper() == symbol.upper()), None
    )
    if instrument is None:
        console.print(f"[red]الرمز {symbol} غير موجود في قائمة المتابعة[/red]")
        raise typer.Exit(1)

    result = service.analyse_one(instrument)
    console.print(Panel(result.report_ar, border_style="cyan", expand=False))
    if save_to:
        save_to.write_text(result.report_ar, encoding="utf-8")
        console.print(f"[green]حُفظ في {save_to}[/green]")


@app.command()
def digest(
    offline: bool = typer.Option(False, "--offline"),
    send: bool = typer.Option(False, "--send", help="أرسله عبر تيليجرام"),
) -> None:
    """توليد التقرير اليومي المجمّع."""
    configure_logging("WARNING")
    from analyst.reporting.telegram_fmt import format_digest
    from analyst.runner import build_service

    service = build_service(offline=offline)
    results = service.run_once(alert=False)
    text = format_digest(results, service.settings.timezone.display)
    console.print(Panel(_strip_html(text), title="التقرير اليومي", border_style="cyan"))
    if send:
        console.print("[green]أُرسل[/green]" if service.send_digest(results)
                      else "[red]تعذّر الإرسال — راجع مفاتيح تيليجرام[/red]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    offline: bool = typer.Option(False, "--offline"),
    schedule: bool = typer.Option(True, "--schedule/--no-schedule", help="شغّل الجدولة الدورية"),
) -> None:
    """تشغيل لوحة التحكم العربية + واجهة البيانات."""
    import uvicorn

    configure_logging("INFO")
    from analyst.api.app import create_app

    console.print(f"[green]لوحة التحكم:[/green] http://{host}:{port}")
    uvicorn.run(create_app(offline=offline, schedule=schedule), host=host, port=port, log_level="warning")


@app.command()
def stats(symbol: str = typer.Option(None, "--symbol")) -> None:
    """إحصائيات الأداء الفعلي من التتبّع الأمامي (وليس backtest)."""
    configure_logging("WARNING")
    init_db()
    result = stats_module.compute(symbol)

    console.print(Panel(result.headline_ar, title="أداء التتبّع الأمامي", border_style="cyan"))
    if result.sample == 0:
        console.print(
            "[dim]لا توجد إشارات محسومة بعد. شغّل النظام دورياً وستتراكم النتائج تلقائياً.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    for col in ("المقياس", "القيمة"):
        table.add_column(col)
    rows = [
        ("عدد الصفقات المحسومة", str(result.sample)),
        ("مفتوحة الآن", str(result.open_count)),
        ("رابحة / خاسرة / منتهية", f"{result.wins} / {result.losses} / {result.expired}"),
        ("التوقّع لكل صفقة", f"{result.expectancy_r:+.3f}R" if result.expectancy_r is not None else "—"),
        ("متوسط الرابحة", f"{result.avg_win_r:+.2f}R" if result.avg_win_r else "—"),
        ("متوسط الخاسرة", f"{result.avg_loss_r:+.2f}R" if result.avg_loss_r else "—"),
        ("عامل الربح", f"{result.profit_factor}" if result.profit_factor else "—"),
        ("متوسط أقصى ربح غير محقق (MFE)", f"{result.avg_mfe_r}R" if result.avg_mfe_r else "—"),
        ("متوسط أقصى خسارة غير محققة (MAE)", f"{result.avg_mae_r}R" if result.avg_mae_r else "—"),
    ]
    if result.win_rate is not None:
        rows.insert(3, ("نسبة الإصابة", f"{result.win_rate:.1%}"))
    for name, value in rows:
        table.add_row(name, value)
    console.print(table)

    if result.by_grade:
        grade_table = Table(title="حسب التصنيف", show_header=True, header_style="bold")
        for col in ("التصنيف", "العدد", "التوقّع", "نسبة الإصابة"):
            grade_table.add_column(col)
        for grade, data in result.by_grade.items():
            grade_table.add_row(
                grade, str(data["sample"]), f"{data['expectancy_r']:+.3f}R",
                f"{data['win_rate']:.0%}" if data["win_rate"] is not None else "عينة صغيرة",
            )
        console.print(grade_table)


@app.command()
def status() -> None:
    """فحص صحة الإعداد: البيانات، المفاتيح، التغطية."""
    configure_logging("WARNING")
    init_db()
    from analyst.data.providers.synthetic import SyntheticProvider
    from analyst.data.repository import CandleRepository

    settings = load_settings()
    secrets = Secrets.from_env()

    console.print(Panel(
        f"إصدار الكود: {code_version()}\n"
        f"إصدار الإعدادات: {settings.version}\n"
        f"الملف التعريفي: {settings.profile} "
        f"({'، '.join(tf.arabic for tf in settings.active_profile.timeframes)})\n"
        f"قاعدة البيانات: {settings.resolved_db_url()}\n"
        f"تيليجرام: {'✅ مضبوط' if secrets.telegram_ready else '❌ غير مضبوط'}\n"
        f"وقت العرض: {settings.timezone.display} — الآن "
        f"{format_display(now_utc(), settings.timezone.display)}",
        title="حالة النظام", border_style="cyan",
    ))

    repo = CandleRepository([SyntheticProvider()])
    table = Table(show_header=True, header_style="bold")
    for col in ("الرمز", "السوق", "الفريم", "الشموع المخزّنة", "آخر شمعة"):
        table.add_column(col)
    for instrument in active_instruments():
        for tf in settings.active_profile.timeframes:
            if tf not in instrument.supported_timeframes:
                continue
            count, newest = repo.coverage(instrument.symbol, tf)
            table.add_row(
                instrument.symbol, instrument.market.value, tf.value, str(count),
                f"{newest:%Y-%m-%d %H:%M}" if newest is not None else "—",
            )
    console.print(table)


@app.command("init-db")
def init_database() -> None:
    """إنشاء قاعدة البيانات والجداول."""
    configure_logging("INFO")
    init_db()
    console.print("[green]قاعدة البيانات جاهزة[/green]")


@app.command("test-alert")
def test_alert() -> None:
    """اختبار إرسال رسالة تيليجرام."""
    configure_logging("WARNING")
    from analyst.alerts.telegram import TelegramNotifier

    notifier = TelegramNotifier()
    ok, detail = notifier.send(
        "<b>✅ اختبار الاتصال</b>\nمحلل الأسواق متصل بنجاح بقناة التنبيهات."
    )
    console.print(f"[green]{detail}[/green]" if ok else f"[red]{detail}[/red]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def export(
    symbol: str = typer.Argument(...),
    output: Path = typer.Option(Path("analysis.json"), "--output", "-o"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """تصدير تحليل كامل بصيغة JSON للتدقيق أو الأرشفة."""
    configure_logging("WARNING")
    from analyst.runner import build_service

    service = build_service(offline=offline)
    instrument = next((i for i in active_instruments() if i.symbol.upper() == symbol.upper()), None)
    if instrument is None:
        console.print(f"[red]الرمز {symbol} غير موجود[/red]")
        raise typer.Exit(1)
    result = service.analyse_one(instrument)
    output.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"[green]صُدّر إلى {output}[/green]")


@app.command()
def version() -> None:
    """طباعة الإصدار."""
    console.print(f"محلل الأسواق {__version__} ({code_version()})")


# --------------------------------------------------------------------------- #


def _print_table(results, tz: str) -> None:
    table = Table(show_header=True, header_style="bold", title="نتائج التحليل")
    for col in ("الرمز", "الاسم", "الاتجاه", "الثقة", "التصنيف", "حالة السوق", "الوقت"):
        table.add_column(col)
    for r in sorted(results, key=lambda x: (x.is_actionable, x.confidence), reverse=True):
        arrow = {Direction.BULLISH: "🔺 صاعد", Direction.BEARISH: "🔻 هابط",
                 Direction.NEUTRAL: "⚪ محايد"}[r.direction]
        blocked = " ⛔" if r.blocking_failures else ""
        table.add_row(
            r.symbol, r.name_ar, arrow, f"{r.confidence:.0%}",
            f"[{_GRADE_STYLE[r.grade]}]{r.grade.value}{blocked}[/]",
            r.regime.arabic, format_display(r.as_of, tz, "%m-%d %H:%M"),
        )
    console.print(table)


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)


def main() -> None:
    reset_caches()
    app()


if __name__ == "__main__":
    main()
