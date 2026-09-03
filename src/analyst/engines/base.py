"""Engine contract and shared scoring helpers.

An engine is a pure function `MarketContext -> EngineResult`. It performs no
I/O, mutates nothing, and must be safe to run on a truncated history — which is
what makes the whole system backtestable and unit-testable.

Every engine builds its verdict the same way: accumulate signed, weighted
`Evidence` items, then normalise. Keeping that mechanism shared means the report
can always answer "why?" with a number for each reason, and it keeps engines
comparable to one another.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from analyst.core.enums import Direction, EngineId, Timeframe
from analyst.core.models import EngineResult, Evidence, MarketContext


class Engine(ABC):
    """Base class. Subclasses implement `_run`; `analyse` handles the guard rails."""

    id: EngineId
    #: Timeframes that must be present for this engine to say anything.
    required_timeframes: tuple[Timeframe, ...] = ()

    @abstractmethod
    def _run(self, ctx: MarketContext) -> EngineResult:
        ...

    def applies_to(self, ctx: MarketContext) -> tuple[bool, str]:
        """Override to decline on asset class, market, or missing extras."""
        return True, ""

    def analyse(self, ctx: MarketContext) -> EngineResult:
        """Run the engine, converting any refusal or crash into a skipped result.

        A crashing engine must never take down the run: the aggregator simply
        redistributes weight to the engines that did produce an answer, and the
        report says which engine stood down and why.
        """
        ok, reason = self.applies_to(ctx)
        if not ok:
            return EngineResult.skipped(self.id, reason)

        missing = [tf for tf in self.required_timeframes if tf not in ctx.series]
        if missing:
            names = "، ".join(tf.arabic for tf in missing)
            return EngineResult.skipped(self.id, f"فريمات مفقودة: {names}")

        try:
            return self._run(ctx)
        except Exception as exc:  # noqa: BLE001 - deliberate isolation boundary
            return EngineResult.skipped(self.id, f"خطأ داخلي: {type(exc).__name__}: {exc}")


class ScoreBuilder:
    """Accumulates evidence into a normalised [-1, 1] score.

    `add(code, label, value, weight)` where `value` is in [-1, 1]. The final
    score is the weighted mean, so adding more evidence never inflates the
    result — a design choice that keeps engines from becoming "more confident"
    merely by looking at more things.
    """

    def __init__(self) -> None:
        self.items: list[Evidence] = []
        self._weighted_sum = 0.0
        self._weight_total = 0.0

    def add(
        self,
        code: str,
        label_ar: str,
        value: float,
        weight: float = 1.0,
        detail_ar: str = "",
        record_when_zero: bool = False,
    ) -> None:
        value = max(-1.0, min(1.0, float(value)))
        self._weighted_sum += value * weight
        self._weight_total += weight
        if value == 0.0 and not record_when_zero:
            return
        self.items.append(
            Evidence(
                code=code,
                label_ar=label_ar,
                detail_ar=detail_ar,
                direction=Direction.from_score(value, deadband=0.0),
                contribution=round(value * weight, 4),
            )
        )

    def note(self, code: str, label_ar: str, detail_ar: str = "", direction: Direction = Direction.NEUTRAL) -> None:
        """Record context that explains the reading without moving the score."""
        self.items.append(
            Evidence(code=code, label_ar=label_ar, detail_ar=detail_ar,
                     direction=direction, contribution=0.0)
        )

    @property
    def score(self) -> float:
        if self._weight_total <= 0:
            return 0.0
        return max(-1.0, min(1.0, self._weighted_sum / self._weight_total))

    @property
    def weight_total(self) -> float:
        return self._weight_total

    def result(
        self,
        engine: EngineId,
        quality: float,
        deadband: float = 0.10,
        metrics: dict[str, float] | None = None,
        notes_ar: list[str] | None = None,
    ) -> EngineResult:
        score = self.score
        # Strongest evidence first — the report reads top-down.
        evidence = sorted(self.items, key=lambda e: abs(e.contribution), reverse=True)
        return EngineResult(
            engine=engine,
            direction=Direction.from_score(score, deadband),
            strength=abs(score),
            quality=max(0.0, min(1.0, quality)),
            evidence=evidence,
            metrics={k: round(float(v), 4) for k, v in (metrics or {}).items()},
            notes_ar=notes_ar or [],
        )


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def scale(value: float, full_scale: float) -> float:
    """Map a raw quantity onto [-1, 1], saturating at `full_scale`.

    Used everywhere a raw number (ATR multiples, percentage distances, z-scores)
    needs to become a comparable score without a hand-tuned lookup table.
    """
    if full_scale <= 0:
        return 0.0
    return clamp(value / full_scale)
