"""Live performance statistics, computed only from resolved forward-test signals.

Nothing here comes from a backtest. Every number is derived from signals that
were written down before the outcome existed, which is the only kind of number
worth quoting.

Deliberately reported alongside every statistic:

  * **Sample size.** A 70% win rate over 10 trades is noise. The reporting layer
    refuses to state a win rate below `MIN_SAMPLE` and says so instead.
  * **Expectancy in R**, not win rate. A 40% win rate at 3R beats a 70% win rate
    at 0.5R, and only expectancy shows that.
  * **A Wilson confidence interval** on the win rate, so the uncertainty is
    visible rather than implied.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy import select

from analyst.storage.db import session_scope
from analyst.storage.models import Signal

#: Below this many resolved signals, no win rate is reported at all.
MIN_SAMPLE = 30

_WIN_STATUSES = {"tp1_hit", "tp2_hit"}
_RESOLVED = _WIN_STATUSES | {"stopped", "expired"}


@dataclass(slots=True)
class PerformanceStats:
    sample: int = 0
    wins: int = 0
    losses: int = 0
    expired: int = 0
    win_rate: float | None = None
    win_rate_ci: tuple[float, float] | None = None
    expectancy_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    profit_factor: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    by_grade: dict[str, dict] = field(default_factory=dict)
    by_symbol: dict[str, dict] = field(default_factory=dict)
    open_count: int = 0

    @property
    def is_significant(self) -> bool:
        return self.sample >= MIN_SAMPLE

    @property
    def headline_ar(self) -> str:
        if not self.is_significant:
            return (
                f"العينة {self.sample} صفقة محسومة فقط — أقل من الحد الأدنى "
                f"({MIN_SAMPLE}) لأي استنتاج إحصائي. لا تُقرأ النسب بعد."
            )
        lo, hi = self.win_rate_ci or (0.0, 0.0)
        return (
            f"عبر {self.sample} صفقة محسومة: نسبة الإصابة {self.win_rate:.0%} "
            f"(مجال ثقة 95%: {lo:.0%}–{hi:.0%}) · التوقّع {self.expectancy_r:+.2f}R لكل صفقة"
        )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — correct for small samples, unlike the normal one."""
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def compute(symbol: str | None = None) -> PerformanceStats:
    with session_scope() as session:
        stmt = select(Signal)
        if symbol:
            stmt = stmt.where(Signal.symbol == symbol)
        rows = list(session.execute(stmt).scalars().all())

    resolved = [r for r in rows if r.status in _RESOLVED and r.r_multiple is not None]
    stats = PerformanceStats(
        sample=len(resolved),
        open_count=sum(1 for r in rows if r.status == "open"),
    )
    if not resolved:
        return stats

    wins = [r for r in resolved if r.status in _WIN_STATUSES]
    losses = [r for r in resolved if r.status == "stopped"]
    stats.wins, stats.losses = len(wins), len(losses)
    stats.expired = sum(1 for r in resolved if r.status == "expired")

    r_values = [float(r.r_multiple) for r in resolved]
    stats.expectancy_r = round(sum(r_values) / len(r_values), 4)
    stats.avg_win_r = round(sum(float(r.r_multiple) for r in wins) / len(wins), 4) if wins else None
    stats.avg_loss_r = (
        round(sum(float(r.r_multiple) for r in losses) / len(losses), 4) if losses else None
    )

    gross_win = sum(max(0.0, v) for v in r_values)
    gross_loss = abs(sum(min(0.0, v) for v in r_values))
    stats.profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else None

    mfes = [float(r.max_favourable_r) for r in resolved if r.max_favourable_r is not None]
    maes = [float(r.max_adverse_r) for r in resolved if r.max_adverse_r is not None]
    stats.avg_mfe_r = round(sum(mfes) / len(mfes), 3) if mfes else None
    stats.avg_mae_r = round(sum(maes) / len(maes), 3) if maes else None

    if stats.sample >= MIN_SAMPLE:
        stats.win_rate = round(len(wins) / len(resolved), 4)
        stats.win_rate_ci = tuple(round(v, 4) for v in wilson_interval(len(wins), len(resolved)))

    stats.by_grade = _group(resolved, lambda r: r.grade)
    stats.by_symbol = _group(resolved, lambda r: r.symbol)
    return stats


def _group(rows, key) -> dict[str, dict]:
    buckets: dict[str, list] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    out: dict[str, dict] = {}
    for name, group in sorted(buckets.items()):
        values = [float(r.r_multiple) for r in group]
        wins = sum(1 for r in group if r.status in _WIN_STATUSES)
        out[name] = {
            "sample": len(group),
            "expectancy_r": round(sum(values) / len(values), 4),
            # A win rate on a thin bucket is worse than no number at all.
            "win_rate": round(wins / len(group), 4) if len(group) >= MIN_SAMPLE else None,
        }
    return out
