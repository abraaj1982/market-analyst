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
from analyst.data.context import DERIVED_FROM as DERIVED_TIMEFRAMES
from analyst.logging_setup import configure as configure_logging
from analyst.storage.db import init_db
from analyst.tracking import stats as stats_module
from analyst.version import __version__, code_version

app = typer.Typer(
    add_completion=False,
    help="Market Analyst — multi-engine analysis with a transparent confidence score",
)
console = Console()

_GRADE_STYLE = {
    Grade.A_PLUS: "bold green", Grade.A: "green", Grade.B: "yellow",
    Grade.C: "dim yellow", Grade.NO_TRADE: "dim",
}


@app.command()
def analyze(
    symbols: list[str] = typer.Argument(None, help="Specific symbols, or leave empty for the whole watchlist"),
    offline: bool = typer.Option(False, "--offline", help="Synthetic data — no internet, no keys"),
    full: bool = typer.Option(False, "--full", help="Print the complete report for each symbol"),
    no_save: bool = typer.Option(False, "--no-save", help="Do not persist to the database"),
    no_alert: bool = typer.Option(False, "--no-alert", help="Do not send alerts"),
    log_level: str = typer.Option("WARNING", "--log-level"),
) -> None:
    """Run the analysis over the watchlist."""
    configure_logging(log_level)
    from analyst.runner import build_service

    service = build_service(offline=offline)
    with console.status("[cyan]Analysing…"):
        results = service.run_once(
            persist=not no_save, alert=not no_alert, symbols=list(symbols) if symbols else None
        )

    if not results:
        console.print("[red]No analysis produced. Re-run with --log-level INFO to see why.[/red]")
        raise typer.Exit(1)

    _print_table(results, service.settings.timezone.display)
    if full:
        for result in sorted(results, key=lambda r: -r.confidence):
            console.print(Panel(result.report, border_style="cyan", expand=False))


@app.command()
def report(
    symbol: str = typer.Argument(..., help="A single symbol"),
    offline: bool = typer.Option(False, "--offline"),
    save_to: Path = typer.Option(None, "--save-to", help="Write the report to a file"),
) -> None:
    """Print the full report for one symbol."""
    configure_logging("WARNING")
    from analyst.runner import build_service

    service = build_service(offline=offline)
    instrument = _find_instrument(symbol)
    result = service.analyse_one(instrument)
    console.print(Panel(result.report, border_style="cyan", expand=False))
    if save_to:
        save_to.write_text(result.report, encoding="utf-8")
        console.print(f"[green]Written to {save_to}[/green]")


@app.command()
def digest(
    offline: bool = typer.Option(False, "--offline"),
    send: bool = typer.Option(False, "--send", help="Send it over Telegram"),
) -> None:
    """Build the daily digest."""
    configure_logging("WARNING")
    from analyst.reporting.telegram_fmt import format_digest
    from analyst.runner import build_service

    service = build_service(offline=offline)
    results = service.run_once(alert=False)
    text = format_digest(results, service.settings.timezone.display)
    console.print(Panel(_strip_html(text), title="Daily digest", border_style="cyan"))
    if send:
        console.print("[green]Sent[/green]" if service.send_digest(results)
                      else "[red]Send failed — check the Telegram credentials[/red]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    offline: bool = typer.Option(False, "--offline"),
    schedule: bool = typer.Option(True, "--schedule/--no-schedule", help="Run the periodic jobs"),
) -> None:
    """Serve the dashboard and the REST API."""
    import uvicorn

    configure_logging("INFO")
    from analyst.api.app import create_app

    console.print(f"[green]Dashboard:[/green] http://{host}:{port}")
    uvicorn.run(create_app(offline=offline, schedule=schedule), host=host, port=port,
                log_level="warning")


@app.command()
def stats(symbol: str = typer.Option(None, "--symbol")) -> None:
    """Live forward-test performance — not a backtest."""
    configure_logging("WARNING")
    init_db()
    result = stats_module.compute(symbol)

    console.print(Panel(result.headline, title="Forward-test performance", border_style="cyan"))
    if result.sample == 0:
        console.print(
            "[dim]No resolved signals yet. Run the system on a schedule and results accumulate.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value")
    rows = [
        ("Resolved trades", str(result.sample)),
        ("Currently open", str(result.open_count)),
        ("Wins / losses / expired", f"{result.wins} / {result.losses} / {result.expired}"),
        ("Expectancy per trade", f"{result.expectancy_r:+.3f}R" if result.expectancy_r is not None else "—"),
        ("Average win", f"{result.avg_win_r:+.2f}R" if result.avg_win_r else "—"),
        ("Average loss", f"{result.avg_loss_r:+.2f}R" if result.avg_loss_r else "—"),
        ("Profit factor", f"{result.profit_factor}" if result.profit_factor else "—"),
        ("Average MFE", f"{result.avg_mfe_r}R" if result.avg_mfe_r else "—"),
        ("Average MAE", f"{result.avg_mae_r}R" if result.avg_mae_r else "—"),
    ]
    if result.win_rate is not None:
        rows.insert(3, ("Win rate", f"{result.win_rate:.1%}"))
    for name, value in rows:
        table.add_row(name, value)
    console.print(table)

    if result.by_grade:
        grade_table = Table(title="By grade", show_header=True, header_style="bold")
        for col in ("Grade", "Sample", "Expectancy", "Win rate"):
            grade_table.add_column(col)
        for grade, data in result.by_grade.items():
            grade_table.add_row(
                grade, str(data["sample"]), f"{data['expectancy_r']:+.3f}R",
                f"{data['win_rate']:.0%}" if data["win_rate"] is not None else "sample too small",
            )
        console.print(grade_table)


@app.command()
def backtest(
    symbol: str = typer.Argument(..., help="A single symbol"),
    step: int = typer.Option(4, "--step", help="Re-analyse every N bars of the entry timeframe"),
    expiry: int = typer.Option(21, "--expiry", help="Days before an unresolved signal expires"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """Replay stored history through the pipeline, point-in-time.

    Macro, COT, fundamentals and news are excluded: no free point-in-time
    archive exists for them, so including them would feed today's knowledge into
    a past decision.
    """
    configure_logging("WARNING")
    from analyst.backtest.runner import BacktestConfig, Backtester
    from analyst.core.config import load_gates
    from analyst.runner import build_service

    service = build_service(offline=offline)
    instrument = _find_instrument(symbol)

    with console.status("[cyan]Replaying history…"):
        report_ = Backtester(service.repository, service.settings, load_gates()).run(
            instrument, BacktestConfig(step_bars=step, expiry_days=expiry)
        )

    console.print(Panel(
        f"[bold]{report_.symbol}[/bold]  "
        f"{report_.start:%Y-%m-%d} → {report_.end:%Y-%m-%d}" if report_.start else report_.symbol,
        title="Backtest", border_style="cyan",
    ))
    for warning in report_.warnings:
        console.print(f"[yellow]! {warning}[/yellow]")
    if not report_.resolved:
        # Still show the counts: "0 signals from 200 steps" and "12 signals, none
        # resolved yet" are different problems and need different responses.
        console.print(
            f"[dim]{report_.steps} steps replayed, {report_.signals} signal(s) produced, "
            f"none resolved.[/dim]"
        )
        console.print(
            "[dim]Excluded engines (no point-in-time history): "
            + ", ".join(report_.excluded_engines) + "[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value")
    for name, value in [
        ("Steps replayed", str(report_.steps)),
        ("Signals", str(report_.signals)),
        ("Signals / month", str(report_.signals_per_month)),
        ("Resolved", str(report_.resolved)),
        ("Wins / losses / expired", f"{report_.wins} / {report_.losses} / {report_.expired}"),
        ("Win rate", f"{report_.win_rate:.1%}" if report_.win_rate is not None else "sample too small"),
        ("Expectancy", f"{report_.expectancy_r:+.3f}R"),
        ("Profit factor", str(report_.profit_factor)),
        ("Max drawdown", f"{report_.max_drawdown_r:.2f}R"),
        ("Average MFE / MAE", f"{report_.avg_mfe_r}R / {report_.avg_mae_r}R"),
    ]:
        table.add_row(name, value)
    console.print(table)

    if report_.reliability:
        rel = Table(title="Calibration — did stated confidence hold up?",
                    show_header=True, header_style="bold")
        for col in ("Confidence band", "N", "Stated", "Realised", "Gap", "Expectancy"):
            rel.add_column(col)
        for b in report_.reliability:
            gap_style = "green" if abs(b.gap) < 0.1 else "yellow" if abs(b.gap) < 0.2 else "red"
            rel.add_row(
                f"{b.low:.0%}–{b.high:.0%}", str(b.count), f"{b.predicted:.0%}",
                f"{b.realised:.0%}", f"[{gap_style}]{b.gap:+.0%}[/]", f"{b.expectancy_r:+.2f}R",
            )
        console.print(rel)

    if report_.by_period:
        per = Table(title="By quarter", show_header=True, header_style="bold")
        for col in ("Quarter", "N", "Expectancy", "Total R"):
            per.add_column(col)
        for row in report_.by_period:
            per.add_row(row["period"], str(row["sample"]),
                        f"{row['expectancy_r']:+.3f}R", f"{row['total_r']:+.2f}R")
        console.print(per)

    console.print(
        "[dim]Excluded engines (no point-in-time history): "
        + ", ".join(report_.excluded_engines) + "[/dim]"
    )
    console.print(
        "[dim]One path through one history, with no slippage, spread or commission "
        "modelled. A good result means 'not disqualified', never 'validated'.[/dim]"
    )


@app.command()
def calibrate(symbol: str = typer.Option(None, "--symbol")) -> None:
    """Suggest recalibrated confidence parameters from resolved signals.

    Suggests only. Auto-fitting on a small sample is overfitting in a nicer
    wrapper, so the decision stays with a person.
    """
    configure_logging("WARNING")
    init_db()
    from analyst.backtest.calibration import suggest

    proposal = suggest(symbol)
    console.print(Panel(proposal.headline, title="Calibration review", border_style="cyan"))
    if not proposal.rows:
        return

    table = Table(show_header=True, header_style="bold")
    for col in ("Confidence band", "N", "Stated", "Realised", "Gap"):
        table.add_column(col)
    for row in proposal.rows:
        table.add_row(row["band"], str(row["count"]), f"{row['predicted']:.0%}",
                      f"{row['realised']:.0%}", f"{row['gap']:+.0%}")
    console.print(table)

    if proposal.suggestion:
        console.print(Panel(proposal.suggestion, title="Suggested change to settings.yaml",
                            border_style="yellow"))


companies_app = typer.Typer(help="Manual company register (markets with no price feed)")
app.add_typer(companies_app, name="company")


@companies_app.command("list")
def company_list() -> None:
    """List every company in the manual register."""
    configure_logging("WARNING")
    init_db()
    from analyst.manual.service import list_companies

    rows = list_companies()
    if not rows:
        console.print("[dim]The register is empty. Add one with `analyst company add`.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Symbol", "Name", "Sector", "Price", "DPS", "EPS", "News"):
        table.add_column(col)
    for c in rows:
        table.add_row(
            c["symbol"], c["name"], c["sector"] or "—",
            _opt(c["price"]), _opt(c["dividend_per_share"]), _opt(c["eps"]),
            str(c["news_count"]),
        )
    console.print(table)


@companies_app.command("add")
def company_add(
    symbol: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    sector: str = typer.Option("", "--sector"),
    currency: str = typer.Option("OMR", "--currency"),
    price: float = typer.Option(None, "--price"),
    dividend: float = typer.Option(None, "--dividend", help="Dividend per share"),
    previous_dividend: float = typer.Option(None, "--previous-dividend"),
    eps: float = typer.Option(None, "--eps"),
    book_value: float = typer.Option(None, "--book-value"),
    debt_to_equity: float = typer.Option(None, "--debt-to-equity"),
    years_paid: int = typer.Option(None, "--years-paid"),
    years_cut: int = typer.Option(None, "--years-cut"),
) -> None:
    """Add or update a company. Every field except symbol and name is optional."""
    configure_logging("WARNING")
    init_db()
    from analyst.manual.service import upsert_company

    upsert_company(symbol, {
        "name": name, "sector": sector, "currency": currency, "price": price,
        "dividend_per_share": dividend, "previous_dividend_per_share": previous_dividend,
        "eps": eps, "book_value_per_share": book_value, "debt_to_equity": debt_to_equity,
        "dividend_years_paid": years_paid, "dividend_years_cut": years_cut,
    })
    console.print(f"[green]Saved {symbol.upper()}[/green]")


@companies_app.command("news")
def company_add_news(
    symbol: str = typer.Argument(...),
    headline: str = typer.Argument(..., help="English or Arabic"),
    source: str = typer.Option("", "--source"),
    date: str = typer.Option(None, "--date", help="YYYY-MM-DD, defaults to now"),
    sentiment: float = typer.Option(None, "--sentiment", help="Override the lexicon, -1 to 1"),
) -> None:
    """Attach an announcement to a company and score its tone."""
    configure_logging("WARNING")
    init_db()
    from datetime import datetime

    from analyst.core.clock import UTC
    from analyst.manual.service import add_news

    when = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC) if date else None
    try:
        add_news(symbol, headline, when, source, sentiment)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]Recorded[/green]")


@companies_app.command("assess")
def company_assess(symbol: str = typer.Argument(...)) -> None:
    """Print the dividend and news assessment for one company."""
    configure_logging("WARNING")
    init_db()
    from analyst.manual.service import assess_symbol

    assessment = assess_symbol(symbol)
    if assessment is None:
        console.print(f"[red]{symbol.upper()} is not in the register[/red]")
        raise typer.Exit(1)
    console.print(Panel(assessment.report, border_style="cyan", expand=False))


@companies_app.command("remove")
def company_remove(symbol: str = typer.Argument(...)) -> None:
    """Remove a company and all of its announcements."""
    configure_logging("WARNING")
    init_db()
    from analyst.manual.service import delete_company

    if not delete_company(symbol):
        console.print(f"[red]{symbol.upper()} is not in the register[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Removed {symbol.upper()}[/green]")


@app.command()
def status() -> None:
    """Check configuration health: data coverage, credentials, versions."""
    configure_logging("WARNING")
    init_db()
    from analyst.data.providers.synthetic import SyntheticProvider
    from analyst.data.repository import CandleRepository

    settings = load_settings()
    secrets = Secrets.from_env()

    console.print(Panel(
        f"Code version: {code_version()}\n"
        f"Config version: {settings.version}\n"
        f"Profile: {settings.profile} "
        f"({', '.join(tf.label for tf in settings.active_profile.timeframes)})\n"
        f"Database: {settings.resolved_db_url()}\n"
        f"Telegram: {'configured' if secrets.telegram_ready else 'not configured'}\n"
        f"Display timezone: {settings.timezone.display} — now "
        f"{format_display(now_utc(), settings.timezone.display)}",
        title="System status", border_style="cyan",
    ))

    repo = CandleRepository([SyntheticProvider()])
    table = Table(show_header=True, header_style="bold")
    for col in ("Symbol", "Market", "Timeframe", "Stored bars", "Latest bar"):
        table.add_column(col)
    for instrument in active_instruments():
        for tf in settings.active_profile.timeframes:
            if tf not in instrument.supported_timeframes:
                continue
            if tf in DERIVED_TIMEFRAMES:
                # Derived frames are rebuilt from their base on demand, so an
                # empty candle store for them is expected, not a gap in coverage.
                base = DERIVED_TIMEFRAMES[tf]
                _, newest = repo.coverage(instrument.symbol, base)
                table.add_row(
                    instrument.symbol, instrument.market.value, tf.value,
                    f"[dim]derived from {base.label}[/dim]",
                    f"{newest:%Y-%m-%d %H:%M}" if newest is not None else "—",
                )
                continue
            count, newest = repo.coverage(instrument.symbol, tf)
            table.add_row(
                instrument.symbol, instrument.market.value, tf.value, str(count),
                f"{newest:%Y-%m-%d %H:%M}" if newest is not None else "—",
            )
    console.print(table)
    console.print(
        "[dim]Derived timeframes are resampled from a lower frame at analysis "
        "time, so they are never stored.[/dim]"
    )


@app.command("init-db")
def init_database() -> None:
    """Create the database and its tables."""
    configure_logging("INFO")
    init_db()
    console.print("[green]Database ready[/green]")


@app.command("test-alert")
def test_alert() -> None:
    """Send a test message over Telegram."""
    configure_logging("WARNING")
    from analyst.alerts.telegram import TelegramNotifier

    notifier = TelegramNotifier()
    ok, detail = notifier.send(
        "<b>Connection test</b>\nMarket Analyst reached the alert channel successfully."
    )
    console.print(f"[green]{detail}[/green]" if ok else f"[red]{detail}[/red]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def export(
    symbol: str = typer.Argument(...),
    output: Path = typer.Option(Path("analysis.json"), "--output", "-o"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """Export a full analysis as JSON for auditing or archiving."""
    configure_logging("WARNING")
    from analyst.runner import build_service

    service = build_service(offline=offline)
    result = service.analyse_one(_find_instrument(symbol))
    output.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"[green]Exported to {output}[/green]")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"Market Analyst {__version__} ({code_version()})")


# --------------------------------------------------------------------------- #


def _find_instrument(symbol: str):
    instrument = next(
        (i for i in active_instruments() if i.symbol.upper() == symbol.upper()), None
    )
    if instrument is None:
        console.print(f"[red]{symbol} is not in the watchlist[/red]")
        raise typer.Exit(1)
    return instrument


def _print_table(results, tz: str) -> None:
    table = Table(show_header=True, header_style="bold", title="Analysis results")
    for col in ("Symbol", "Name", "Direction", "Confidence", "Grade", "Regime", "As of"):
        table.add_column(col)
    for r in sorted(results, key=lambda x: (x.is_actionable, x.confidence), reverse=True):
        arrow = {
            Direction.BULLISH: "[green]Bullish[/green]",
            Direction.BEARISH: "[red]Bearish[/red]",
            Direction.NEUTRAL: "[dim]Neutral[/dim]",
        }[r.direction]
        blocked = " [red]BLOCKED[/red]" if r.blocking_failures else ""
        table.add_row(
            r.symbol, r.name, arrow, f"{r.confidence:.0%}",
            f"[{_GRADE_STYLE[r.grade]}]{r.grade.value}[/]{blocked}",
            r.regime.label, format_display(r.as_of, tz, "%m-%d %H:%M"),
        )
    console.print(table)


def _opt(value, digits: int = 4) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)


def main() -> None:
    reset_caches()
    app()


if __name__ == "__main__":
    main()
