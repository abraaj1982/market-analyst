"""Calibration review — comparing stated confidence against realised outcomes.

The confidence score is only worth printing if it means something: when the
system says 75%, roughly 75% of those setups should reach target. This module
measures that gap and *suggests* an adjustment.

It deliberately stops at suggesting. Automatically re-fitting parameters on a
small live sample is overfitting in a nicer wrapper: the numbers would track
whatever the last twenty trades happened to do, and the system would chase its
own tail. A person applies the change, or does not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import select

from analyst.core.config import load_settings
from analyst.storage.db import session_scope
from analyst.storage.models import Signal
from analyst.tracking.stats import MIN_SAMPLE, wilson_interval

#: Below this many resolved signals, no parameter change should be considered.
MIN_FOR_RECALIBRATION = 100

WIN_STATUSES = {"tp1_hit", "tp2_hit"}
_BANDS = ((0.45, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01))


@dataclass(slots=True)
class CalibrationProposal:
    headline: str
    rows: list[dict] = field(default_factory=list)
    suggestion: str | None = None
    sample: int = 0


def suggest(symbol: str | None = None) -> CalibrationProposal:
    with session_scope() as session:
        stmt = select(Signal).where(Signal.status.in_(WIN_STATUSES | {"stopped", "expired"}))
        if symbol:
            stmt = stmt.where(Signal.symbol == symbol.upper())
        rows = list(session.execute(stmt).scalars().all())

    resolved = [r for r in rows if r.r_multiple is not None]
    if len(resolved) < MIN_SAMPLE:
        return CalibrationProposal(
            headline=(
                f"{len(resolved)} resolved signals — below the {MIN_SAMPLE} minimum for any "
                "read at all. Keep the system running; nothing to review yet."
            ),
            sample=len(resolved),
        )

    band_rows: list[dict] = []
    for low, high in _BANDS:
        group = [r for r in resolved if low <= r.confidence < high]
        if len(group) < 10:
            continue
        predicted = sum(r.confidence for r in group) / len(group)
        wins = sum(1 for r in group if r.status in WIN_STATUSES)
        realised = wins / len(group)
        ci = wilson_interval(wins, len(group))
        band_rows.append({
            "band": f"{low:.0%}–{min(high, 1.0):.0%}",
            "count": len(group),
            "predicted": round(predicted, 4),
            "realised": round(realised, 4),
            "gap": round(predicted - realised, 4),
            "ci": ci,
            "significant": not (ci[0] <= predicted <= ci[1]),
        })

    if not band_rows:
        return CalibrationProposal(
            headline="No confidence band has 10 or more resolved signals yet.",
            sample=len(resolved),
        )

    # Weighted mean gap across bands. Positive = the system is overconfident.
    total = sum(r["count"] for r in band_rows)
    gap = sum(r["gap"] * r["count"] for r in band_rows) / total

    headline = (
        f"{len(resolved)} resolved signals across {len(band_rows)} confidence bands. "
        f"Mean gap {gap:+.1%} — the system is "
        f"{'overconfident' if gap > 0 else 'underconfident' if gap < 0 else 'well calibrated'}."
    )

    if len(resolved) < MIN_FOR_RECALIBRATION:
        headline += (
            f" Below {MIN_FOR_RECALIBRATION} resolved signals, no parameter change is "
            "recommended — the estimate is still noise."
        )
        return CalibrationProposal(headline=headline, rows=band_rows, sample=len(resolved))

    if abs(gap) < 0.05 or not any(r["significant"] for r in band_rows):
        headline += " Within tolerance; no change recommended."
        return CalibrationProposal(headline=headline, rows=band_rows, sample=len(resolved))

    return CalibrationProposal(
        headline=headline,
        rows=band_rows,
        suggestion=_render_suggestion(gap),
        sample=len(resolved),
    )


def _render_suggestion(gap: float) -> str:
    """Shift the logistic midpoint to absorb a persistent calibration gap.

    Moving the midpoint right makes the same consensus produce a lower
    confidence, which is the correction for overconfidence. The step is
    deliberately small and clamped: a large jump fitted to one sample is exactly
    the mistake this whole module exists to avoid.
    """
    cfg = load_settings().scoring.calibration
    # Convert the probability gap into a modest shift in |S| space.
    shift = max(-0.06, min(0.06, gap * 0.25))
    proposed = round(max(0.15, min(0.60, cfg.midpoint + shift)), 3)

    direction = "raise" if shift > 0 else "lower"
    return (
        f"Consider a small change to config/settings.yaml:\n\n"
        f"  scoring:\n"
        f"    calibration:\n"
        f"      midpoint: {proposed}   # currently {cfg.midpoint}\n"
        f"      steepness: {cfg.steepness}\n\n"
        f"Rationale: {direction} the midpoint so the same consensus yields a "
        f"{'lower' if shift > 0 else 'higher'} confidence, closing roughly "
        f"{abs(gap):.0%} of the observed gap.\n"
        f"Re-run `analyst calibrate` after another 50 resolved signals before "
        f"changing it again. Do not chase the number."
    )


def brier_score(predicted: list[float], outcomes: list[int]) -> float:
    """Mean squared error of probabilistic forecasts. Lower is better.

    Kept here because it is the right metric for scoring engine usefulness once
    per-engine outcome attribution is added: an engine that improves the Brier
    score earns weight, one that does not should lose it.
    """
    if not predicted or len(predicted) != len(outcomes):
        return math.nan
    return sum((p - o) ** 2 for p, o in zip(predicted, outcomes, strict=True)) / len(predicted)
