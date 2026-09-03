"""The manual company path: lexicon, dividend scoring, and the assessment."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from analyst.core.clock import UTC, now_utc
from analyst.core.enums import Direction, GateStatus, Grade
from analyst.manual.engines import CompanyInputs, DividendEngine, NewsItem, SentimentEngine
from analyst.manual.lexicon import normalise, score_text
from analyst.manual.service import (
    MAX_MANUAL_CONFIDENCE,
    add_news,
    assess,
    assess_symbol,
    delete_company,
    list_companies,
    news_rows,
    upsert_company,
)

# -------------------------------------------------------------------- lexicon


def test_lexicon_reads_both_languages():
    positive_en, _ = score_text("Bank reports record profit and raises dividend")
    positive_ar, _ = score_text("الشركة تعلن عن ارتفاع الأرباح وتوزيعات نقدية")
    negative_en, _ = score_text("Company announces net loss and dividend cut")
    negative_ar, _ = score_text("خسائر كبيرة وخفض التوزيعات")

    assert positive_en > 0.3 and positive_ar > 0.3
    assert negative_en < -0.3 and negative_ar < -0.3


def test_lexicon_normalises_arabic_orthography():
    """Real headlines spell alef and ta-marbuta inconsistently; without
    normalisation half of them match nothing."""
    assert normalise("الأرباح") == normalise("الارباح")
    a, _ = score_text("ارتفاع الارباح")
    b, _ = score_text("ارتفاع الأرباح")
    assert a == b > 0


def test_longer_phrases_win_over_their_substrings():
    """'dividend cut' must not also score the positive term 'dividend'."""
    sentiment, matched = score_text("Board announces a dividend cut")
    assert sentiment < 0
    assert "dividend cut" in matched["negative"]
    assert matched["positive"] == []


def test_neutral_text_scores_zero():
    sentiment, matched = score_text("Annual general meeting scheduled for next month")
    assert sentiment == 0.0
    assert not matched["positive"] and not matched["negative"]


def test_sentiment_saturates_rather_than_accumulating():
    one, _ = score_text("net loss")
    many, _ = score_text("net loss, dividend cut, investigation, downgrade, fraud")
    assert many <= -0.9
    assert abs(many) >= abs(one)


# ------------------------------------------------------------------- dividend


def base_inputs(**overrides) -> CompanyInputs:
    values = dict(
        symbol="TEST", name="Test Co", price=0.240, dividend_per_share=0.018,
        previous_dividend_per_share=0.015, eps=0.030, dividend_years_paid=7,
        book_value_per_share=0.32, debt_to_equity=85.0,
    )
    values.update(overrides)
    return CompanyInputs(**values)


def test_dividend_derived_metrics():
    i = base_inputs()
    assert i.dividend_yield == pytest.approx(0.075)
    assert i.payout_ratio == pytest.approx(0.6)
    assert i.dividend_cover == pytest.approx(1.667, rel=1e-3)
    assert i.price_to_earnings == pytest.approx(8.0)
    assert i.price_to_book == pytest.approx(0.75)


def test_missing_inputs_yield_none_not_zero():
    """The distinction the whole manual path rests on: 'not supplied' is not 0."""
    i = base_inputs(price=None, eps=None)
    assert i.dividend_yield is None
    assert i.payout_ratio is None
    assert i.price_to_earnings is None


def test_dividend_engine_rewards_a_covered_dividend():
    result = DividendEngine().analyse(base_inputs())
    assert result.direction is Direction.BULLISH
    assert result.quality > 0.8


def test_dividend_engine_penalises_an_uncovered_dividend():
    result = DividendEngine().analyse(
        base_inputs(dividend_per_share=0.014, eps=0.009, previous_dividend_per_share=0.020,
                    dividend_years_cut=2, debt_to_equity=210.0)
    )
    assert result.direction is Direction.BEARISH
    codes = {e.code for e in result.evidence}
    assert "dividend_cover" in codes


def test_extreme_yield_is_a_warning_not_a_bonus():
    """A 15% yield is usually the market pricing in a cut."""
    normal = DividendEngine().analyse(base_inputs(dividend_per_share=0.012))   # 5%
    extreme = DividendEngine().analyse(base_inputs(dividend_per_share=0.036, eps=0.040))  # 15%
    normal_yield = next(e for e in normal.evidence if e.code == "dividend_yield")
    extreme_yield = next(e for e in extreme.evidence if e.code == "dividend_yield")
    assert extreme_yield.contribution < normal_yield.contribution
    assert any("unusually high" in note for note in extreme.notes)


def test_dividend_quality_tracks_supplied_fields():
    full = DividendEngine().analyse(base_inputs())
    sparse = DividendEngine().analyse(
        CompanyInputs(symbol="S", name="Sparse", price=0.5, dividend_per_share=0.01)
    )
    assert sparse.quality < full.quality


def test_dividend_engine_declines_without_inputs():
    result = DividendEngine().analyse(CompanyInputs(symbol="X", name="Empty"))
    assert result.skipped_reason is not None


# ------------------------------------------------------------------ sentiment


def news(days_ago: int, headline: str) -> NewsItem:
    sentiment, matched = score_text(headline)
    return NewsItem(
        published_at=now_utc() - timedelta(days=days_ago),
        headline=headline, sentiment=sentiment, matched_terms=matched,
    )


def test_sentiment_weights_recent_items_more():
    recent_bad = SentimentEngine().analyse(
        [news(1, "net loss and dividend cut"), news(150, "record profit and dividend increase")],
        now_utc(),
    )
    old_bad = SentimentEngine().analyse(
        [news(150, "net loss and dividend cut"), news(1, "record profit and dividend increase")],
        now_utc(),
    )
    assert recent_bad.direction is Direction.BEARISH
    assert old_bad.direction is Direction.BULLISH


def test_sentiment_declines_without_recent_items():
    result = SentimentEngine().analyse([news(400, "record profit")], now_utc())
    assert result.skipped_reason is not None


def test_manual_override_wins_over_the_lexicon():
    item = news(2, "record profit and dividend increase")
    assert item.effective_sentiment > 0
    item.manual_sentiment = -0.8
    result = SentimentEngine().analyse([item], now_utc())
    assert result.direction is Direction.BEARISH


def test_sentiment_states_its_own_limits():
    result = SentimentEngine().analyse([news(2, "record profit")], now_utc())
    assert any("lexicon" in note for note in result.notes)


# ----------------------------------------------------------------- assessment


def test_assessment_never_offers_a_trade_plan():
    """No price history means no entry, stop or target — and the report says so."""
    assessment = assess(base_inputs(), [news(3, "record profit and dividend increase")])
    assert not hasattr(assessment, "risk")
    assert "not a trade signal" in assessment.report


def test_confidence_is_capped_on_the_manual_path():
    """Two agreeing engines on hand-entered figures cannot justify near-certainty."""
    strong = [news(i, "record profit, dividend increase, new contract awarded") for i in (1, 2, 3, 4, 5)]
    assessment = assess(base_inputs(), strong)
    assert assessment.confidence <= MAX_MANUAL_CONFIDENCE


def test_uncovered_dividend_fails_a_blocking_gate():
    assessment = assess(
        base_inputs(dividend_per_share=0.040, eps=0.030), [news(3, "record profit")]
    )
    gate = next(g for g in assessment.gates if g.gate == "dividend_sustainable")
    assert gate.status is GateStatus.FAILED
    assert gate.blocking is True
    assert assessment.grade not in (Grade.A_PLUS, Grade.A)


def test_missing_core_inputs_block_and_are_listed():
    assessment = assess(
        CompanyInputs(symbol="S", name="Sparse", price=0.5), [news(3, "record profit")]
    )
    assert "EPS" in assessment.missing_inputs
    assert any(g.gate == "input_completeness" and g.status is GateStatus.FAILED
               for g in assessment.gates)


def test_absent_news_is_unknown_not_neutral():
    assessment = assess(base_inputs(), [])
    gate = next(g for g in assessment.gates if g.gate == "news_coverage")
    assert gate.status is GateStatus.NOT_EVALUATED
    assert "not neutral" in gate.detail


# ------------------------------------------------------------------- register


def test_register_round_trip():
    upsert_company("BKMB", {"name": "Bank Muscat", "price": 0.24,
                            "dividend_per_share": 0.018, "eps": 0.030})
    add_news("BKMB", "Bank Muscat raises dividend after record profit", source="MSX")

    rows = list_companies()
    assert any(r["symbol"] == "BKMB" and r["news_count"] == 1 for r in rows)

    stored = news_rows("BKMB")
    assert stored[0]["sentiment"] > 0
    assert stored[0]["matched_terms"]["positive"]

    assessment = assess_symbol("BKMB")
    assert assessment is not None and assessment.symbol == "BKMB"

    assert delete_company("BKMB") is True
    assert assess_symbol("BKMB") is None


def test_upsert_updates_rather_than_duplicates():
    upsert_company("DUP", {"name": "First", "price": 1.0})
    upsert_company("DUP", {"name": "Second", "price": 2.0})
    rows = [r for r in list_companies() if r["symbol"] == "DUP"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Second" and rows[0]["price"] == 2.0


def test_news_requires_a_registered_company():
    with pytest.raises(ValueError):
        add_news("NOPE", "some headline")


def test_deleting_a_company_removes_its_news():
    upsert_company("TMP", {"name": "Temp"})
    add_news("TMP", "record profit")
    assert news_rows("TMP")
    delete_company("TMP")
    assert news_rows("TMP") == []


def test_explicit_publication_date_is_honoured():
    upsert_company("DATED", {"name": "Dated"})
    when = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    add_news("DATED", "record profit", published_at=when)
    assert news_rows("DATED")[0]["published_at"].startswith("2026-01-15")
