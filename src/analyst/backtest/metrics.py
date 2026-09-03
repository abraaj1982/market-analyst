"""Backtest metrics.

Every metric here is chosen to resist the two ways a backtest flatters itself:
reporting only the average, and reporting only the win rate.

  * **Expectancy in R** is the headline, not win rate. A 40% hit rate at 3R
    beats 70% at 0.5R, and the win rate alone cannot see that.
  * **Per-period breakdown** replaces a single aggregate. A strategy that made
    everything in one quarter and bled for the rest is not the same as a steady
    one, and an aggregate number hides the difference completely.
  * **A reliability curve** answers the only question that matters for a
    confidence score: when the system said 70%, did roughly 70% of those work?
    A confident-but-uncalibrated system is worse than an unconfident one.
  * **Max drawdown in R** because survivability, not total return, decides
    whether a system is usable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

WIN_STATUSES = {"tp1_hit", "tp2_hit"}


@dataclass(slots=True)
class ReliabilityBucket:
    """One confidence band and what actually happened inside it."""

    low: float
    high: float
    count: int
    predicted: float      # mean stated confidence in the bucket
    realised: float       # fraction that reached target
    expectancy_r: float

    @property
    def gap(self) -> float:
        """Positive = overconfident, negative = underconfident."""
        return self.predicted - self.realised


@dataclass(slots=True)
class BacktestReport:
    symbol: str
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    steps: int = 0
    signals: int = 0
    resolved: int = 0
    wins: int = 0
    losses: int = 0
    expired: int = 0
    win_rate: float | None = None
    expectancy_r: float | None = None
    profit_factor: float | None = None
    max_drawdown_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    signals_per_month: float | None = None
    by_grade: dict[str, dict] = field(default_factory=dict)
    by_period: list[dict] = field(default_factory=list)
    reliability: list[ReliabilityBucket] = field(default_factory=list)
    excluded_engines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_significant(self) -> bool:
        return self.resolved >= 30


def summarise(
    symbol: str,
    trades: list[dict],
    steps: int,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    excluded_engines: list[str],
) -> BacktestReport:
    report = BacktestReport(
        symbol=symbol, start=start, end=end, steps=steps, signals=len(trades),
        excluded_engines=excluded_engines,
    )
    resolved = [t for t in trades if t["status"] != "open" and t["r_multiple"] is not None]
    report.resolved = len(resolved)
    if not resolved:
        report.warnings.append("No resolved trades in this period")
        return report

    r_values = np.array([t["r_multiple"] for t in resolved], dtype=float)
    report.wins = int(sum(1 for t in resolved if t["status"] in WIN_STATUSES))
    report.losses = int(sum(1 for t in resolved if t["status"] == "stopped"))
    report.expired = int(sum(1 for t in resolved if t["status"] == "expired"))

    report.expectancy_r = round(float(r_values.mean()), 4)
    gross_win = float(r_values[r_values > 0].sum())
    gross_loss = float(abs(r_values[r_values < 0].sum()))
    report.profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else None
    report.max_drawdown_r = round(_max_drawdown(r_values), 4)

    mfe = [t["max_favourable_r"] for t in resolved if t.get("max_favourable_r") is not None]
    mae = [t["max_adverse_r"] for t in resolved if t.get("max_adverse_r") is not None]
    report.avg_mfe_r = round(float(np.mean(mfe)), 3) if mfe else None
    report.avg_mae_r = round(float(np.mean(mae)), 3) if mae else None

    # A win rate under 30 resolved trades is noise dressed as a statistic.
    if report.resolved >= 30:
        report.win_rate = round(report.wins / report.resolved, 4)
    else:
        report.warnings.append(
            f"Only {report.resolved} resolved trades — under 30, the win rate is not readable"
        )

    if start is not None and end is not None:
        months = max((end - start).days / 30.44, 1e-9)
        report.signals_per_month = round(len(trades) / months, 2)

    report.by_grade = _group(resolved, lambda t: t["grade"])
    report.by_period = _by_period(resolved)
    report.reliability = _reliability(resolved)
    return report


def _max_drawdown(r_values: np.ndarray) -> float:
    """Deepest peak-to-trough decline of the cumulative R curve."""
    equity = np.cumsum(r_values)
    peak = np.maximum.accumulate(equity)
    return float((peak - equity).max())


def _group(trades: list[dict], key) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = {}
    for trade in trades:
        buckets.setdefault(key(trade), []).append(trade)
    out: dict[str, dict] = {}
    for name, group in sorted(buckets.items()):
        values = np.array([t["r_multiple"] for t in group], dtype=float)
        wins = sum(1 for t in group if t["status"] in WIN_STATUSES)
        out[name] = {
            "sample": len(group),
            "expectancy_r": round(float(values.mean()), 4),
            "win_rate": round(wins / len(group), 4) if len(group) >= 30 else None,
        }
    return out


def _by_period(trades: list[dict], freq: str = "QE") -> list[dict]:
    """Quarterly breakdown. One aggregate number hides everything that matters."""
    frame = pd.DataFrame(trades)
    frame["issued_at"] = pd.to_datetime(frame["issued_at"], utc=True)
    frame = frame.set_index("issued_at").sort_index()

    out: list[dict] = []
    for period, group in frame.resample(freq):
        if group.empty:
            continue
        values = group["r_multiple"].astype(float)
        wins = int((group["status"].isin(WIN_STATUSES)).sum())
        out.append({
            "period": str(pd.Timestamp(period).date()),
            "sample": int(len(group)),
            "expectancy_r": round(float(values.mean()), 4),
            "total_r": round(float(values.sum()), 3),
            "win_rate": round(wins / len(group), 4) if len(group) >= 30 else None,
        })
    return out


def _reliability(trades: list[dict], edges: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9, 1.01)) -> list[ReliabilityBucket]:
    """Did stated confidence match realised outcome? The calibration question."""
    buckets: list[ReliabilityBucket] = []
    low = 0.0
    for high in edges:
        group = [t for t in trades if low <= t["confidence"] < high]
        if group:
            predicted = float(np.mean([t["confidence"] for t in group]))
            realised = sum(1 for t in group if t["status"] in WIN_STATUSES) / len(group)
            expectancy = float(np.mean([t["r_multiple"] for t in group]))
            buckets.append(
                ReliabilityBucket(low, min(high, 1.0), len(group),
                                  round(predicted, 4), round(realised, 4), round(expectancy, 4))
            )
        low = high
    return buckets
