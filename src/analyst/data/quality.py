"""Data quality assessment.

A model fed bad data does not fail loudly — it produces a confident, wrong
answer. So every timeframe is scored before any engine sees it, and that score
multiplies straight into the final confidence.

Five independent checks, each capped so one problem cannot zero out the score
by itself:

  1. **Sufficiency** — enough bars to warm up the longest indicator (EMA200).
  2. **Freshness**  — the newest closed bar is recent for this timeframe.
  3. **Continuity** — few missing bars relative to the expected grid.
  4. **Integrity**  — OHLC relationships hold; no non-positive prices.
  5. **Sanity**     — no absurd single-bar ranges (bad ticks, split artefacts).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from analyst.core.clock import now_utc
from analyst.core.config import DataQualitySettings
from analyst.core.enums import Timeframe
from analyst.core.models import DataQualityReport
from analyst.indicators.trend import atr

#: Weekend gaps are normal for everything except crypto; expected-bar maths
#: below counts calendar time, so these caps keep FX/equities from being
#: penalised for markets simply being closed.
_SESSION_COVERAGE = {
    Timeframe.M15: 0.72, Timeframe.H1: 0.72, Timeframe.H4: 0.72,
    Timeframe.D1: 0.72, Timeframe.W1: 1.0,
}


def assess_timeframe(
    frame: pd.DataFrame,
    timeframe: Timeframe,
    settings: DataQualitySettings,
    as_of: datetime | None = None,
) -> tuple[float, list[str]]:
    """Score one timeframe in [0, 1] and list the human-readable problems."""
    as_of = as_of or now_utc()
    issues: list[str] = []
    score = 1.0

    if frame.empty:
        return 0.0, [f"{timeframe.label}: no data"]

    # 1. sufficiency -----------------------------------------------------
    if len(frame) < settings.min_bars_required:
        deficit = 1.0 - len(frame) / settings.min_bars_required
        score -= min(0.45, deficit * 0.6)
        issues.append(
            f"{timeframe.label}: {len(frame)} bars, below the {settings.min_bars_required} required"
        )

    # 2. freshness -------------------------------------------------------
    age = as_of - frame.index[-1].to_pydatetime()
    max_age = timedelta(minutes=timeframe.minutes * (settings.max_staleness_bars + 1))
    if age > max_age:
        bars_late = age.total_seconds() / 60.0 / timeframe.minutes
        score -= min(0.35, 0.06 * bars_late)
        issues.append(
            f"{timeframe.label}: last bar is {bars_late:.0f} bars stale"
        )

    # 3. continuity ------------------------------------------------------
    span_minutes = (frame.index[-1] - frame.index[0]).total_seconds() / 60.0
    expected = (span_minutes / timeframe.minutes + 1) * _SESSION_COVERAGE[timeframe]
    if expected > 0:
        gap_ratio = max(0.0, 1.0 - len(frame) / expected)
        if gap_ratio > settings.max_gap_ratio:
            score -= min(0.25, (gap_ratio - settings.max_gap_ratio) * 1.5)
            issues.append(f"{timeframe.label}: {gap_ratio:.1%} of expected bars are missing")

    # 4. integrity -------------------------------------------------------
    body_max = frame[["open", "close"]].max(axis=1)
    body_min = frame[["open", "close"]].min(axis=1)
    broken = int(((frame["high"] < body_max) | (frame["low"] > body_min)).sum())
    nonpositive = int((frame[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    if broken or nonpositive:
        score -= min(0.40, 0.02 * (broken + nonpositive))
        issues.append(
            f"{timeframe.label}: {broken + nonpositive} bars with invalid OHLC relationships"
        )

    # 5. sanity ----------------------------------------------------------
    atr_series = atr(frame["high"], frame["low"], frame["close"], 14)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = (frame["high"] - frame["low"]) / atr_series.replace(0.0, np.nan)
    spikes = int((ratio > settings.spike_atr_multiple).sum())
    if spikes:
        score -= min(0.20, 0.04 * spikes)
        issues.append(f"{timeframe.label}: {spikes} bars with an implausible range (suspected bad ticks)")

    return float(max(0.0, min(1.0, score))), issues


def assess(
    frames: dict[Timeframe, pd.DataFrame],
    settings: DataQualitySettings,
    as_of: datetime | None = None,
) -> DataQualityReport:
    """Aggregate per-timeframe scores into one report.

    The overall score is the *weighted* mean pulled toward the worst timeframe:
    a pristine daily frame cannot rescue a broken hourly one, because the
    engines read them together.
    """
    per_tf: dict[Timeframe, float] = {}
    issues: list[str] = []
    for tf, frame in frames.items():
        s, tf_issues = assess_timeframe(frame, tf, settings, as_of)
        per_tf[tf] = s
        issues.extend(tf_issues)

    if not per_tf:
        return DataQualityReport(score=0.0, issues=["No usable timeframes"], per_timeframe={})

    values = list(per_tf.values())
    overall = 0.6 * float(np.mean(values)) + 0.4 * float(min(values))
    return DataQualityReport(score=round(overall, 4), issues=issues, per_timeframe=per_tf)
