"""Persisting analyses and signals.

An analysis is stored in full (payload JSON) so that a signal can be re-examined
months later exactly as it was produced. A `Signal` row is created only for
actionable results — the ones the system would actually have acted on — which is
what keeps the live statistics honest: no cherry-picking after the fact.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from analyst.core.clock import now_utc, to_utc
from analyst.core.models import AnalysisResult
from analyst.storage.db import session_scope
from analyst.storage.models import Analysis, Signal

log = logging.getLogger(__name__)


def save_analysis(result: AnalysisResult, create_signal: bool = True) -> int:
    """Persist one analysis; returns its row id."""
    with session_scope() as session:
        row = Analysis(
            symbol=result.symbol,
            name_ar=result.name_ar,
            market=result.market.value,
            as_of=to_utc(result.as_of),
            spot=result.spot,
            direction=int(result.direction.value),
            confidence=result.confidence,
            grade=result.grade.value,
            regime=result.regime.value,
            actionable=result.is_actionable,
            report_ar=result.report_ar,
            payload=result.model_dump(mode="json"),
            config_version=result.config_version,
            code_version=result.code_version,
        )
        session.add(row)
        session.flush()

        if create_signal and result.is_actionable and result.risk is not None:
            session.add(
                Signal(
                    analysis_id=row.id,
                    symbol=result.symbol,
                    issued_at=to_utc(result.as_of),
                    direction=int(result.direction.value),
                    grade=result.grade.value,
                    confidence=result.confidence,
                    entry=result.risk.entry,
                    stop_loss=result.risk.stop_loss,
                    take_profit_1=result.risk.take_profit_1,
                    take_profit_2=result.risk.take_profit_2,
                    risk_reward=result.risk.risk_reward,
                    status="open",
                )
            )
        return int(row.id)


def latest_analyses(limit: int = 50) -> list[Analysis]:
    """Most recent analysis per symbol, newest first."""
    with session_scope() as session:
        rows = session.execute(
            select(Analysis).order_by(Analysis.as_of.desc()).limit(limit * 4)
        ).scalars().all()
    seen: set[str] = set()
    out: list[Analysis] = []
    for row in rows:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def history_for(symbol: str, limit: int = 200) -> list[Analysis]:
    with session_scope() as session:
        return list(
            session.execute(
                select(Analysis)
                .where(Analysis.symbol == symbol)
                .order_by(Analysis.as_of.desc())
                .limit(limit)
            ).scalars().all()
        )


def open_signals() -> list[Signal]:
    with session_scope() as session:
        return list(
            session.execute(select(Signal).where(Signal.status == "open")).scalars().all()
        )


def last_alert_state(symbol: str, within: timedelta) -> tuple[datetime, int, str] | None:
    """(sent_at, direction, grade) of the most recent alert inside the window."""
    from analyst.storage.models import AlertLog

    cutoff = now_utc() - within
    with session_scope() as session:
        row = session.execute(
            select(AlertLog)
            .where(AlertLog.symbol == symbol, AlertLog.sent_at >= cutoff, AlertLog.ok.is_(True))
            .order_by(AlertLog.sent_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        return None
    return to_utc(row.sent_at), int(row.direction), str(row.grade)
