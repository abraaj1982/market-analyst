"""Assessment of a manual company, and the CRUD around the register.

The verdict deliberately answers a *different question* from the market
pipeline. With no price history there is no trend, no structure, no entry and
no stop — so this path never produces a trade plan, and says so. What it can
answer is:

    "Is the dividend sound, and what has the news been saying?"

Those two things are worth knowing about an Omani bank or utility, and they are
obtainable without a data feed. Pretending they add up to a trade signal would
be the dishonest part.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import select

from analyst.core.clock import now_utc, to_utc
from analyst.core.config import load_settings
from analyst.core.enums import Direction, EngineId, GateStatus, Grade
from analyst.core.models import EngineResult, GateResult
from analyst.manual.engines import (
    CompanyInputs,
    DividendEngine,
    NewsItem,
    SentimentEngine,
    score_news_item,
)
from analyst.storage.db import session_scope
from analyst.storage.models import CompanyNews, ManualCompany

#: This path needs both engines to say anything at all; with one it is a
#: single opinion wearing a verdict's clothes.
MIN_ACTIVE_ENGINES = 2

#: Hard ceiling on confidence for the manual path.
#:
#: Two engines reading hand-entered figures cannot justify near-certainty, no
#: matter how strongly they agree. The market pipeline earns high confidence by
#: having six to nine independent engines corroborate each other against data
#: the system fetched itself; none of that is true here. Without this cap the
#: maths happily returns 97% for two agreeing engines, which would be the most
#: misleading number in the product.
MAX_MANUAL_CONFIDENCE = 0.85


class CompanyAssessment(BaseModel):
    """A verdict about a company, with no trade plan attached — by design."""

    symbol: str
    name: str
    currency: str = "OMR"
    sector: str = ""
    as_of: datetime
    direction: Direction
    confidence: float
    grade: Grade
    engines: list[EngineResult] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    report: str = ""
    missing_inputs: list[str] = Field(default_factory=list)

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [g for g in self.gates if g.blocking and g.status is not GateStatus.PASSED]


@dataclass(slots=True)
class _Aggregate:
    direction: Direction
    confidence: float
    contributions: list[tuple[EngineId, float, float]] = field(default_factory=list)


def assess(inputs: CompanyInputs, news: list[NewsItem], as_of: datetime | None = None) -> CompanyAssessment:
    as_of = as_of or now_utc()
    settings = load_settings()

    dividend = DividendEngine().analyse(inputs)
    sentiment = SentimentEngine().analyse(news, as_of)
    engines = [dividend, sentiment]

    aggregate = _combine(engines, settings)
    gates = _gates(inputs, news, engines, aggregate, as_of)
    grade = _grade(aggregate, engines, gates, settings)

    metrics = {**dividend.metrics, **{f"news_{k}": v for k, v in sentiment.metrics.items()}}
    missing = _missing_inputs(inputs)

    assessment = CompanyAssessment(
        symbol=inputs.symbol,
        name=inputs.name,
        currency=inputs.currency,
        sector=inputs.sector,
        as_of=as_of,
        direction=aggregate.direction,
        confidence=round(aggregate.confidence, 4),
        grade=grade,
        engines=engines,
        gates=gates,
        metrics=metrics,
        missing_inputs=missing,
    )
    assessment.report = render_report(assessment, inputs)
    return assessment


# --------------------------------------------------------------------------- #


def _combine(engines: list[EngineResult], settings) -> _Aggregate:
    """Same weighted-mean maths as the market aggregator, minus the regime and
    asset-class multipliers, which have no meaning without price history."""
    from analyst.scoring.aggregator import calibrate

    weighted_sum = weight_total = 0.0
    contributions: list[tuple[EngineId, float, float]] = []

    for result in engines:
        base = float(settings.weights.base.get(result.engine.value, 0.0))
        effective = base * result.quality if result.active else 0.0
        if effective > 0:
            weighted_sum += effective * result.signed
            weight_total += effective
        contributions.append((result.engine, effective, effective * result.signed))

    if weight_total <= 0:
        return _Aggregate(Direction.NEUTRAL, 0.0, contributions)

    consensus = weighted_sum / weight_total
    dispersion = sum(
        w * (r.signed - consensus) ** 2 for r, (_, w, _) in zip(engines, contributions, strict=True)
        if w > 0
    ) / weight_total
    coherence = max(0.0, 1.0 - settings.scoring.dispersion_lambda * dispersion)

    # How much the engines trusted their own inputs. On this path that is the
    # dominant uncertainty: a dividend read from three supplied figures is not
    # the same claim as one read from a full filing.
    active = [r for r in engines if r.active]
    input_quality = sum(r.quality for r in active) / len(active) if active else 0.0

    confidence = min(
        MAX_MANUAL_CONFIDENCE,
        calibrate(abs(consensus), settings.scoring.calibration) * coherence * input_quality,
    )

    return _Aggregate(
        Direction.from_score(consensus, settings.scoring.direction_deadband),
        max(0.0, min(1.0, confidence)),
        contributions,
    )


def _gates(
    inputs: CompanyInputs,
    news: list[NewsItem],
    engines: list[EngineResult],
    aggregate: _Aggregate,
    as_of: datetime,
) -> list[GateResult]:
    """A small, honest gate set for a path with no price data."""
    gates: list[GateResult] = []

    missing = _missing_inputs(inputs)
    gates.append(GateResult(
        gate="input_completeness",
        label="Core financial inputs supplied",
        status=GateStatus.PASSED if not missing else GateStatus.FAILED,
        detail="All core fields present" if not missing else "Missing: " + ", ".join(missing),
        blocking=True,
    ))

    fresh = [i for i in news if (as_of - i.published_at).days <= 90]
    gates.append(GateResult(
        gate="news_coverage",
        label="Recent announcements on file",
        status=GateStatus.PASSED if fresh else GateStatus.NOT_EVALUATED,
        detail=(
            f"{len(fresh)} announcement(s) in the last 90 days"
            if fresh else "No announcements in the last 90 days — tone is unknown, not neutral"
        ),
        blocking=False,
    ))

    payout = inputs.payout_ratio
    if payout is None:
        status, detail = GateStatus.NOT_EVALUATED, "Payout ratio not computable (EPS or DPS missing)"
    elif payout > 1.0:
        status, detail = GateStatus.FAILED, f"Payout ratio {payout:.0%} — the dividend exceeds earnings"
    else:
        status, detail = GateStatus.PASSED, f"Payout ratio {payout:.0%}"
    gates.append(GateResult(
        gate="dividend_sustainable", label="Dividend is covered by earnings",
        status=status, detail=detail, blocking=True,
    ))

    active = sum(1 for e in engines if e.active)
    gates.append(GateResult(
        gate="engine_coverage",
        label="Both assessment engines ran",
        status=GateStatus.PASSED if active >= MIN_ACTIVE_ENGINES else GateStatus.FAILED,
        detail=f"{active} of {len(engines)} engines active (minimum {MIN_ACTIVE_ENGINES})",
        blocking=True,
    ))
    return gates


def _grade(aggregate: _Aggregate, engines: list[EngineResult], gates: list[GateResult], settings) -> Grade:
    active = sum(1 for e in engines if e.active)
    if active < MIN_ACTIVE_ENGINES:
        return Grade.NO_TRADE
    thresholds = settings.scoring.grades
    c = aggregate.confidence
    grade = (
        Grade.A_PLUS if c >= thresholds.A_PLUS else
        Grade.A if c >= thresholds.A else
        Grade.B if c >= thresholds.B else
        Grade.C if c >= thresholds.C else
        Grade.NO_TRADE
    )
    # A failed hard gate caps the grade, exactly as in the market pipeline.
    if any(g.blocking and g.status is not GateStatus.PASSED for g in gates):
        return Grade.B if grade in (Grade.A_PLUS, Grade.A) else grade
    return grade


def _missing_inputs(inputs: CompanyInputs) -> list[str]:
    required = {
        "price": inputs.price,
        "dividend per share": inputs.dividend_per_share,
        "EPS": inputs.eps,
    }
    return [name for name, value in required.items() if value in (None, 0)]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def render_report(assessment: CompanyAssessment, inputs: CompanyInputs) -> str:
    a = assessment
    lines = [
        "=" * 62,
        f"  {a.name} ({a.symbol})  ·  {a.currency}" + (f"  ·  {a.sector}" if a.sector else ""),
        f"  {a.as_of:%Y-%m-%d %H:%M} UTC",
        "=" * 62,
        "",
        "## Verdict",
        f"**{a.direction.label} view — {a.grade.label} ({a.grade.value})** · "
        f"confidence {a.confidence:.0%}",
        "",
        "> This is a dividend and news assessment, not a trade signal. Without "
        "price history there is no trend, no structure, no entry and no stop, "
        "so none are offered.",
    ]

    if a.missing_inputs:
        lines += [
            "",
            "**Missing inputs:** " + ", ".join(a.missing_inputs)
            + " — supply these for a fuller assessment.",
        ]

    lines += ["", "## Key figures", "| Metric | Value |", "|---|---|"]
    figures = [
        ("Price", f"{inputs.price:,.4f}" if inputs.price else "—"),
        ("Dividend per share", f"{inputs.dividend_per_share:,.4f}" if inputs.dividend_per_share else "—"),
        ("Dividend yield", f"{inputs.dividend_yield:.2%}" if inputs.dividend_yield is not None else "—"),
        ("EPS", f"{inputs.eps:,.4f}" if inputs.eps is not None else "—"),
        ("Payout ratio", f"{inputs.payout_ratio:.0%}" if inputs.payout_ratio is not None else "—"),
        ("Dividend cover", f"{inputs.dividend_cover:.2f}x" if inputs.dividend_cover is not None else "—"),
        ("P/E", f"{inputs.price_to_earnings:.1f}" if inputs.price_to_earnings is not None else "—"),
        ("P/B", f"{inputs.price_to_book:.2f}" if inputs.price_to_book is not None else "—"),
        ("Debt / equity", f"{inputs.debt_to_equity:.0f}%" if inputs.debt_to_equity is not None else "—"),
    ]
    lines += [f"| {name} | {value} |" for name, value in figures]

    lines += ["", "## Gates"]
    for gate in a.gates:
        tag = "" if gate.blocking else " _(advisory)_"
        lines.append(f"{gate.icon} **{gate.label}**{tag} — {gate.detail}")

    for engine in a.engines:
        lines += ["", f"## {engine.engine.label}"]
        if engine.skipped_reason:
            lines.append(f"Stood aside: {engine.skipped_reason}")
            continue
        lines.append(
            f"Direction: **{engine.direction.label}** · strength {engine.strength:.2f} · "
            f"quality {engine.quality:.0%}"
        )
        for ev in engine.evidence[:8]:
            detail = f" — {ev.detail}" if ev.detail else ""
            lines.append(f"   {ev.icon} {ev.label} `{ev.contribution:+.3f}`{detail}")
        for note in engine.notes:
            lines.append(f"   (i) {note}")

    lines += [
        "",
        "## Caveats",
        "   (i) Every figure above was entered by hand. The system verifies none "
        "of them against a source.",
        "   (i) Sentiment is a keyword lexicon, not comprehension. Read the "
        "matched terms before trusting any single item.",
        "   (i) Not investment advice, and not a promise of any outcome.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Register CRUD
# --------------------------------------------------------------------------- #

_FIELDS = (
    "name", "market", "sector", "currency", "notes", "price",
    "dividend_per_share", "previous_dividend_per_share", "dividend_years_paid",
    "dividend_years_cut", "eps", "previous_eps", "book_value_per_share",
    "debt_to_equity", "profit_margin", "revenue_growth",
)


def upsert_company(symbol: str, values: dict) -> int:
    """Create or update a company. Returns its row id."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Symbol is required")
    payload = {k: v for k, v in values.items() if k in _FIELDS}
    with session_scope() as session:
        row = session.execute(
            select(ManualCompany).where(ManualCompany.symbol == symbol)
        ).scalar_one_or_none()
        if row is None:
            row = ManualCompany(symbol=symbol, name=payload.get("name") or symbol)
            session.add(row)
        for key, value in payload.items():
            setattr(row, key, value)
        row.updated_at = now_utc()
        session.flush()
        return int(row.id)


def delete_company(symbol: str) -> bool:
    with session_scope() as session:
        row = session.execute(
            select(ManualCompany).where(ManualCompany.symbol == symbol.strip().upper())
        ).scalar_one_or_none()
        if row is None:
            return False
        session.delete(row)
        return True


def add_news(symbol: str, headline: str, published_at: datetime | None = None,
             source: str = "", manual_sentiment: float | None = None) -> int:
    headline = (headline or "").strip()
    if not headline:
        raise ValueError("Headline is required")
    sentiment, matched = score_news_item(headline)
    with session_scope() as session:
        company = session.execute(
            select(ManualCompany).where(ManualCompany.symbol == symbol.strip().upper())
        ).scalar_one_or_none()
        if company is None:
            raise ValueError(f"{symbol} is not in the manual register")
        row = CompanyNews(
            company_id=company.id,
            published_at=to_utc(published_at or now_utc()),
            headline=headline,
            source=source,
            sentiment=sentiment,
            matched_terms=matched,
            manual_sentiment=manual_sentiment,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def delete_news(news_id: int) -> bool:
    with session_scope() as session:
        row = session.get(CompanyNews, news_id)
        if row is None:
            return False
        session.delete(row)
        return True


def list_companies() -> list[dict]:
    with session_scope() as session:
        rows = list(session.execute(select(ManualCompany).order_by(ManualCompany.symbol)).scalars())
        return [_company_dict(r, len(r.news)) for r in rows]


def load_company(symbol: str) -> tuple[CompanyInputs, list[NewsItem]] | None:
    with session_scope() as session:
        row = session.execute(
            select(ManualCompany).where(ManualCompany.symbol == symbol.strip().upper())
        ).scalar_one_or_none()
        if row is None:
            return None
        inputs = CompanyInputs(
            symbol=row.symbol, name=row.name, currency=row.currency, sector=row.sector,
            price=row.price, dividend_per_share=row.dividend_per_share,
            previous_dividend_per_share=row.previous_dividend_per_share,
            dividend_years_paid=row.dividend_years_paid,
            dividend_years_cut=row.dividend_years_cut,
            eps=row.eps, previous_eps=row.previous_eps,
            book_value_per_share=row.book_value_per_share,
            debt_to_equity=row.debt_to_equity, profit_margin=row.profit_margin,
            revenue_growth=row.revenue_growth,
        )
        news = [
            NewsItem(
                published_at=to_utc(n.published_at), headline=n.headline, source=n.source,
                sentiment=n.sentiment, matched_terms=n.matched_terms,
                manual_sentiment=n.manual_sentiment,
            )
            for n in sorted(row.news, key=lambda n: n.published_at, reverse=True)
        ]
        return inputs, news


def news_rows(symbol: str) -> list[dict]:
    with session_scope() as session:
        row = session.execute(
            select(ManualCompany).where(ManualCompany.symbol == symbol.strip().upper())
        ).scalar_one_or_none()
        if row is None:
            return []
        return [
            {
                "id": n.id,
                "published_at": to_utc(n.published_at).isoformat(),
                "headline": n.headline,
                "source": n.source,
                "sentiment": round(n.effective_sentiment, 4),
                "auto_sentiment": round(n.sentiment, 4),
                "manual_sentiment": n.manual_sentiment,
                "matched_terms": n.matched_terms or {},
            }
            for n in sorted(row.news, key=lambda n: n.published_at, reverse=True)
        ]


def _company_dict(row: ManualCompany, news_count: int) -> dict:
    return {
        "symbol": row.symbol, "name": row.name, "market": row.market,
        "sector": row.sector, "currency": row.currency, "notes": row.notes,
        "price": row.price, "dividend_per_share": row.dividend_per_share,
        "previous_dividend_per_share": row.previous_dividend_per_share,
        "dividend_years_paid": row.dividend_years_paid,
        "dividend_years_cut": row.dividend_years_cut,
        "eps": row.eps, "previous_eps": row.previous_eps,
        "book_value_per_share": row.book_value_per_share,
        "debt_to_equity": row.debt_to_equity, "profit_margin": row.profit_margin,
        "revenue_growth": row.revenue_growth,
        "news_count": news_count,
        "updated_at": to_utc(row.updated_at).isoformat(),
    }


def assess_symbol(symbol: str) -> CompanyAssessment | None:
    loaded = load_company(symbol)
    if loaded is None:
        return None
    inputs, news = loaded
    return assess(inputs, news)


