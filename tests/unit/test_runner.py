"""timeframe_profile() and AnalystService.analyse_at_timeframe().

The point being tested: switching the dashboard's chart timeframe must
produce a genuinely different trade plan (a real bug report — the entry/stop
were frozen to the swing profile no matter which button was clicked), and
must never mutate the service's own settings/pipeline that the scheduler's
swing-profile analysis depends on.
"""
from __future__ import annotations

import pytest

from analyst.core.config import active_instruments
from analyst.core.enums import Timeframe
from analyst.runner import build_service, timeframe_profile


@pytest.mark.parametrize(
    "selected,expected_stack",
    [
        (Timeframe.M1, [Timeframe.M1, Timeframe.M5, Timeframe.M15]),
        (Timeframe.M5, [Timeframe.M5, Timeframe.M15, Timeframe.H1]),
        (Timeframe.M15, [Timeframe.M15, Timeframe.H1, Timeframe.H4]),
        (Timeframe.H1, [Timeframe.H1, Timeframe.H4, Timeframe.D1]),
        (Timeframe.H4, [Timeframe.H4, Timeframe.D1, Timeframe.W1]),
        (Timeframe.D1, [Timeframe.D1, Timeframe.W1]),
    ],
)
def test_timeframe_profile_anchors_on_the_selected_timeframe(selected, expected_stack):
    profile = timeframe_profile(selected)
    assert profile.timeframes == expected_stack
    # the anchor (selected timeframe) must never outweigh its confirmations
    assert profile.mtf_weights[selected] == min(profile.mtf_weights.values())
    assert sum(profile.mtf_weights.values()) == pytest.approx(1.0)


def test_timeframe_profile_rejects_weekly_as_a_selectable_anchor():
    """W1 only ever appears as a *confirmation* timeframe (H4 and D1's
    stacks reach up to it) -- there is nothing above it to confirm it with,
    so it is deliberately not a valid chart-button selection."""
    with pytest.raises(ValueError, match="No timeframe stack"):
        timeframe_profile(Timeframe.W1)


def test_analyse_at_timeframe_gives_different_timeframes_different_plans():
    service = build_service(offline=True)
    instrument = next(i for i in active_instruments() if i.symbol == "XAUUSD")

    h1_result = service.analyse_at_timeframe(instrument, Timeframe.H1)
    d1_result = service.analyse_at_timeframe(instrument, Timeframe.D1)

    assert h1_result.confidence != d1_result.confidence
    if h1_result.risk and d1_result.risk:
        assert h1_result.risk.stop_loss != d1_result.risk.stop_loss


def test_analyse_at_timeframe_does_not_mutate_the_service():
    service = build_service(offline=True)
    original_profile = service.settings.profile
    original_pipeline = service.pipeline

    instrument = next(i for i in active_instruments() if i.symbol == "XAUUSD")
    service.analyse_at_timeframe(instrument, Timeframe.M15)

    assert service.settings.profile == original_profile
    assert service.pipeline is original_pipeline
    assert "_adhoc" not in service.settings.profiles


def test_analyse_at_timeframe_covers_every_selectable_timeframe():
    """Every timeframe the dashboard offers as a chart button must actually
    resolve to a working analysis, not just the ones spot-checked above."""
    service = build_service(offline=True)
    instrument = next(i for i in active_instruments() if i.symbol == "XAUUSD")
    for tf in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1):
        result = service.analyse_at_timeframe(instrument, tf)
        assert result.symbol == "XAUUSD"
        assert 0.0 <= result.confidence <= 1.0
