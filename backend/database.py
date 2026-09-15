"""SQLite + SQLAlchemy engine and session factory.

All persistence for SuperBot Research Console V1 goes through this module.
The DB path is fully env-configurable so we can move to a Railway persistent
volume without code changes, and later swap to Postgres by only changing the
SQLAlchemy URL.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_PATH = os.environ.get("SUPERBOT_DB_PATH", "/app/backend/data/superbot.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

SQLALCHEMY_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_URL,
    connect_args={"check_same_thread": False},
    future=True,
)

SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine, future=True
)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency yielding a request-scoped Session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they do not exist. Idempotent."""
    # Import models so SQLAlchemy knows about them before create_all.
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
