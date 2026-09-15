"""SQLite + SQLAlchemy engine and session factory.

All persistence for SuperBot Research Console V1 goes through this module.
The DB path is fully env-configurable so we can move to a Railway persistent
volume without code changes, and later swap to Postgres by only changing the
SQLAlchemy URL.

Environment separation
----------------------

The runtime/preview database lives at ``RUNTIME_DB_PATH``. Automated
tests MUST use a temporary database on ``/tmp`` and MUST declare
themselves via ``SUPERBOT_ENV=test`` (the pytest ``conftest.py`` does
this automatically). If test-mode ever tries to bind to the runtime DB
path we raise at import time instead of silently contaminating the
project registry.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# The one and only runtime/preview DB location. Never override this
# constant from tests - use SUPERBOT_DB_PATH instead.
RUNTIME_DB_PATH = "/app/backend/data/superbot.db"

DB_PATH = os.environ.get("SUPERBOT_DB_PATH", RUNTIME_DB_PATH)
_ENV = os.environ.get("SUPERBOT_ENV", "runtime").lower()

if _ENV == "test":
    resolved = os.path.realpath(DB_PATH)
    runtime_resolved = os.path.realpath(RUNTIME_DB_PATH)
    if resolved == runtime_resolved:
        raise RuntimeError(
            "REFUSING to bind SUPERBOT_ENV=test to the runtime DB path "
            f"({RUNTIME_DB_PATH}). Point SUPERBOT_DB_PATH at a /tmp file."
        )
    # Extra safety: block any test DB path that lives under the runtime
    # data dir. This prevents accidental spillover into the project
    # registry from generated E2E scripts.
    runtime_dir = os.path.realpath(os.path.dirname(RUNTIME_DB_PATH))
    if resolved.startswith(runtime_dir + os.sep):
        raise RuntimeError(
            "REFUSING to bind test DB inside the runtime data dir "
            f"({runtime_dir}). Use /tmp/superbot-* instead."
        )

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
