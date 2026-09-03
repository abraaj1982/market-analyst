"""Engines for the manual company path.

Two engines, both operating on what a person can actually obtain for a market
with no data feed: published financials and announcements.

Neither engine ever fills in a missing number. If book value was not entered,
the valuation component simply does not score and the engine's own quality
drops. That is the difference between "we do not know" and "we assumed zero",
and it is the difference that keeps the confidence honest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from analyst.core.enums import EngineId
from analyst.core.models import EngineResult
from analyst.engines.base import ScoreBuilder, clamp
from analyst.manual.lexicon import score_text

#: Announcements older than this contribute nothing.
NEWS_HORIZON = timedelta(days=180)
#: Half-life for recency weighting: a 30-day-old headline counts half as much.
NEWS_HALF_LIFE_DAYS = 30.0


@dataclass(slots=True)
class CompanyInputs:
    """Everything a person supplied about one company. All fields optional."""

    symbol: str
    name: str
    currency: str = "OMR"
    sector: str = ""
    price: float | None = None
    dividend_per_share: float | None = None
    previous_dividend_per_share: float | None = None
    dividend_years_paid: int | None = None
    dividend_years_cut: int | None = None
    eps: float | None = None
    previous_eps: float | None = None
    book_value_per_share: float | None = None
    debt_to_equity: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None

    @property
    def dividend_yield(self) -> float | None:
        if not self.price or self.dividend_per_share is None or self.price <= 0:
            return None
        return self.dividend_per_share / self.price

    @property
    def payout_ratio(self) -> float | None:
        if self.eps is None or self.dividend_per_share is None or self.eps <= 0:
            return None
        return self.dividend_per_share / self.eps

    @property
    def dividend_cover(self) -> float | None:
        if self.eps is None or not self.dividend_per_share:
            return None
        return self.eps / self.dividend_per_share

    @property
    def price_to_earnings(self) -> float | None:
        if not self.price or self.eps is None or self.eps <= 0:
            return None
        return self.price / self.eps

    @property
    def price_to_book(self) -> float | None:
        if not self.price or not self.book_value_per_share or self.book_value_per_share <= 0:
            return None
        return self.price / self.book_value_per_share


@dataclass(slots=True)
class NewsItem:
    published_at: datetime
    headline: str
    source: str = ""
    sentiment: float = 0.0
    matched_terms: dict | None = None
    manual_sentiment: float | None = None

    @property
    def effective_sentiment(self) -> float:
        return self.manual_sentiment if self.manual_sentiment is not None else self.sentiment


def score_news_item(headline: str) -> tuple[float, dict]:
    sentiment, matched = score_text(headline)
    return sentiment, matched


# --------------------------------------------------------------------------- #
# Dividend engine
# --------------------------------------------------------------------------- #


class DividendEngine:
    """Dividend quality: yield, cover, sustainability, direction of travel.

    Weighted toward *sustainability* rather than headline yield, because a high
    yield is most often the market pricing in a cut. A 12% yield with a payout
    ratio above 100% is a warning, not an opportunity, and this engine scores it
    that way.
    """

    id = EngineId.DIVIDENDS

    def analyse(self, inputs: CompanyInputs) -> EngineResult:
        builder = ScoreBuilder()
        metrics: dict[str, float] = {}
        notes: list[str] = []
        supplied = 0

        # --- yield -------------------------------------------------------
        dividend_yield = inputs.dividend_yield
        if dividend_yield is not None:
            supplied += 1
            metrics["dividend_yield"] = round(dividend_yield, 5)
            # 6% is treated as a full score; beyond ~10% the market is usually
            # saying something the dividend history has not caught up with.
            score = clamp(dividend_yield / 0.06)
            if dividend_yield > 0.10:
                score = min(score, 0.35)
                notes.append(
                    f"Yield of {dividend_yield:.1%} is unusually high — most often the "
                    "market pricing in a cut rather than a bargain"
                )
            builder.add("dividend_yield", "Dividend yield", score, 1.2,
                        detail=f"{dividend_yield:.2%} on the entered price")
        else:
            notes.append("Yield not computed — price or dividend per share is missing")

        # --- cover and payout --------------------------------------------
        cover = inputs.dividend_cover
        payout = inputs.payout_ratio
        if cover is not None:
            supplied += 1
            metrics["dividend_cover"] = round(cover, 3)
            # cover of 2.0x is comfortable; below 1.0 the dividend exceeds earnings
            score = clamp((cover - 1.0) / 1.0)
            builder.add("dividend_cover", "Dividend cover", score, 1.4,
                        detail=f"Earnings cover the dividend {cover:.2f}x")
            if cover < 1.0:
                notes.append(
                    f"Dividend cover of {cover:.2f}x means the payout exceeds earnings — "
                    "it is being funded from reserves or debt"
                )
        if payout is not None:
            supplied += 1
            metrics["payout_ratio"] = round(payout, 3)
            if payout > 0.9:
                builder.add("payout_ratio", "Payout ratio", -clamp((payout - 0.9) * 3), 1.0,
                            detail=f"Payout ratio {payout:.0%} — little margin for a bad year")

        # --- direction of travel -----------------------------------------
        if inputs.dividend_per_share is not None and inputs.previous_dividend_per_share:
            supplied += 1
            change = (
                inputs.dividend_per_share / inputs.previous_dividend_per_share - 1.0
            )
            metrics["dividend_change"] = round(change, 4)
            builder.add("dividend_trend", "Dividend trend", clamp(change / 0.15), 1.1,
                        detail=f"Dividend {change:+.1%} versus the prior period")

        # --- track record -------------------------------------------------
        if inputs.dividend_years_paid is not None:
            supplied += 1
            years = inputs.dividend_years_paid
            metrics["dividend_years_paid"] = float(years)
            builder.add("dividend_record", "Payment record", clamp(years / 8.0) * 0.8, 0.9,
                        detail=f"Paid in {years} of the recent years")
        if inputs.dividend_years_cut:
            builder.add("dividend_cuts", "Historical cuts", -clamp(inputs.dividend_years_cut / 3.0),
                        1.0, detail=f"Cut or suspended {inputs.dividend_years_cut} time(s)")

        # --- balance sheet support ----------------------------------------
        if inputs.debt_to_equity is not None:
            supplied += 1
            metrics["debt_to_equity"] = round(inputs.debt_to_equity, 2)
            penalty = -clamp((inputs.debt_to_equity - 100.0) / 150.0)
            if penalty < -0.05:
                builder.add("leverage", "Leverage pressure on the dividend", penalty, 0.9,
                            detail=f"Debt to equity {inputs.debt_to_equity:.0f}%")

        if builder.weight_total == 0:
            return EngineResult.skipped(
                self.id, "No dividend inputs supplied (need at least price and dividend per share)"
            )

        # Quality reflects how much of the intended panel was actually supplied.
        quality = clamp(supplied / 6.0, 0.0, 1.0)
        return builder.result(self.id, quality=quality, metrics=metrics, notes=notes)


# --------------------------------------------------------------------------- #
# Sentiment engine
# --------------------------------------------------------------------------- #


class SentimentEngine:
    """News tone, weighted by recency, with the matched terms as evidence.

    Deliberately conservative: it reports what a keyword lexicon found, and the
    report states in plain terms that the lexicon does not understand negation
    or context. Every item can be overridden by hand, and the override wins.
    """

    id = EngineId.SENTIMENT

    def analyse(self, items: list[NewsItem], as_of: datetime) -> EngineResult:
        recent = [i for i in items if as_of - i.published_at <= NEWS_HORIZON]
        if not recent:
            return EngineResult.skipped(
                self.id, "No announcements in the last 180 days"
            )

        builder = ScoreBuilder()
        weighted_sum = weight_total = 0.0
        overrides = 0

        for item in sorted(recent, key=lambda i: i.published_at, reverse=True):
            age_days = max(0.0, (as_of - item.published_at).total_seconds() / 86400.0)
            recency = 0.5 ** (age_days / NEWS_HALF_LIFE_DAYS)
            sentiment = item.effective_sentiment
            if item.manual_sentiment is not None:
                overrides += 1
            weighted_sum += sentiment * recency
            weight_total += recency

            if sentiment:
                terms = item.matched_terms or {}
                found = (terms.get("positive") or []) + (terms.get("negative") or [])
                detail = f"{item.published_at:%Y-%m-%d}"
                if item.source:
                    detail += f" · {item.source}"
                if item.manual_sentiment is not None:
                    detail += " · manually set"
                elif found:
                    detail += " · matched: " + ", ".join(found[:4])
                builder.add(
                    f"news_{item.published_at:%Y%m%d}_{abs(hash(item.headline)) % 1000}",
                    item.headline[:120],
                    sentiment,
                    recency,
                    detail=detail,
                )

        if weight_total == 0:
            return EngineResult.skipped(self.id, "All announcements are outside the horizon")

        neutral = sum(1 for i in recent if i.effective_sentiment == 0)
        metrics = {
            "items": float(len(recent)),
            "neutral_items": float(neutral),
            "manual_overrides": float(overrides),
            "weighted_sentiment": round(weighted_sum / weight_total, 4),
        }

        notes = [
            "Sentiment comes from a keyword lexicon, not a language model. It does "
            "not understand negation or context — check the matched terms before "
            "relying on any single reading, and override anything it gets wrong.",
        ]
        if neutral == len(recent):
            notes.append("No scoring terms matched in any announcement — tone is genuinely unread.")

        # More items and more recent items mean a more trustworthy read.
        quality = clamp(min(1.0, len(recent) / 5.0) * min(1.0, weight_total / 2.0), 0.0, 1.0)
        return builder.result(self.id, quality=max(quality, 0.15), metrics=metrics, notes=notes)
