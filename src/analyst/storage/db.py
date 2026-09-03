"""Engine/session management. SQLite by default; the URL is the only change
needed to move to PostgreSQL later."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from analyst.core.config import load_settings
from analyst.storage.models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    """WAL + busy timeout so the scheduler and the web API can share the file."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is not None:
        return _engine

    url = load_settings().resolved_db_url()
    if url.startswith("sqlite:///"):
        Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(url, future=True, echo=False)
    if url.startswith("sqlite"):
        _configure_sqlite(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


#: Columns renamed in 1.1.0 when the locale suffix was dropped from the schema.
#: `create_all` only creates missing tables — it never alters an existing one —
#: so an upgraded install would otherwise fail on the first insert with
#: "table analyses has no column named name".
_RENAMES: dict[str, list[tuple[str, str]]] = {
    "analyses": [("name_ar", "name"), ("report_ar", "report")],
}


def _migrate_legacy_columns(engine: Engine) -> None:
    """Rename pre-1.1.0 columns in place. Safe to run on every start."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, renames in _RENAMES.items():
        if table not in existing_tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        for old, new in renames:
            if old in columns and new not in columns:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"'))
                log.info("Migrated %s.%s -> %s", table, old, new)


def init_db() -> None:
    engine = get_engine()
    _migrate_legacy_columns(engine)
    Base.metadata.create_all(engine)
    log.info("Database ready: %s", load_settings().resolved_db_url())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Test hook — drops cached engine so a new DATABASE_URL takes effect."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
