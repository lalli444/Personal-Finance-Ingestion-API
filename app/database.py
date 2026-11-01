"""
database.py
-----------
SQLAlchemy engine and session configuration.
Supports both async-compatible SQLite (for testing) and PostgreSQL (production).
"""

from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base class for all SQLAlchemy ORM models."""
    pass


def _build_engine(database_url: str) -> Any:
    """
    Create and configure a SQLAlchemy engine.

    - SQLite gets ``check_same_thread=False`` and WAL mode for concurrency.
    - PostgreSQL gets a connection pool with pre-ping for robustness.
    """
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            echo=get_settings().debug,
        )

        # Enable WAL mode and foreign key constraints on every SQLite connection
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    else:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            echo=get_settings().debug,
        )

    return engine


# Initialise the default engine from settings
engine = _build_engine(get_settings().database_url)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def init_db() -> None:
    """Create all tables if they don't already exist (used for SQLite dev/test)."""
    from app.models import Base as _Base  # noqa: F401 – ensures models are imported

    _Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a database session and ensures it is closed
    after the request, even on exceptions.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
