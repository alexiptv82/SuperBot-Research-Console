"""Regression tests: automated tests must NEVER touch the runtime DB.

These tests are the deterministic guardrail behind the § "TEST vs
PREVIEW/runtime DB isolation" project directive. If any of them fails,
the whole suite is unsafe to run against the preview machine.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from database import DB_PATH, RUNTIME_DB_PATH, engine


class TestRuntimeIsolation:
    def test_test_env_is_declared(self):
        """The pytest bootstrap must have set SUPERBOT_ENV=test."""
        assert os.environ.get("SUPERBOT_ENV") == "test", (
            "SUPERBOT_ENV must be 'test' during pytest runs. "
            "Check tests/conftest.py."
        )

    def test_db_path_is_not_runtime(self):
        """The active DB path must not be the runtime/preview DB."""
        active = os.path.realpath(DB_PATH)
        runtime = os.path.realpath(RUNTIME_DB_PATH)
        assert active != runtime, (
            f"Test DB {active} points at runtime DB {runtime}. "
            "This would contaminate the project registry."
        )

    def test_db_path_is_under_tmp(self):
        """The active DB path must live under /tmp for pytest."""
        assert DB_PATH.startswith("/tmp/"), (
            f"Test DB path {DB_PATH!r} is not under /tmp. "
            "conftest.py should have forced it."
        )

    def test_engine_url_matches(self):
        """SQLAlchemy engine must be bound to the /tmp DB, not runtime."""
        url = str(engine.url)
        assert RUNTIME_DB_PATH not in url, (
            f"SQLAlchemy engine bound to runtime DB: {url}"
        )
        assert DB_PATH in url

    def test_database_refuses_test_env_against_runtime(self, monkeypatch, tmp_path):
        """Direct assertion: importing database with SUPERBOT_ENV=test +
        SUPERBOT_DB_PATH=runtime must raise before any table is touched.

        We use a subprocess so the guardrail runs during module import
        (the same lifecycle real tests would exercise) without breaking
        this already-imported process.
        """
        script = (
            "import os, sys;"
            f"os.environ['SUPERBOT_ENV']='test';"
            f"os.environ['SUPERBOT_DB_PATH']={RUNTIME_DB_PATH!r};"
            "sys.path.insert(0, '/app/backend');"
            "import database"  # should raise at import time
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        assert proc.returncode != 0, (
            "database.py DID NOT raise when tests tried to bind to the "
            "runtime DB. Stdout:\n" + proc.stdout + "\nStderr:\n" + proc.stderr
        )
        assert "REFUSING" in proc.stderr, proc.stderr

    def test_runtime_db_untouched_by_this_run(self):
        """Sanity: the pytest run must not have modified the real
        preview DB file mtime while these tests were executing.

        We snapshot mtime at test start, do a bunch of writes in a
        separate temp DB, and reassert. If a test path secretly opens
        the runtime DB behind our back, this will catch it.
        """
        p = Path(RUNTIME_DB_PATH)
        if not p.exists():
            pytest.skip("runtime DB not present in this environment")
        mtime_before = p.stat().st_mtime
        # Do heavy work against the TEST DB.
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS _probe (id INTEGER)"))
            for i in range(10):
                conn.execute(text("INSERT INTO _probe VALUES (:i)"), {"i": i})
        mtime_after = p.stat().st_mtime
        assert mtime_after == mtime_before, (
            f"Runtime DB mtime changed during a pytest run! "
            f"{mtime_before} -> {mtime_after}. This is exactly the "
            "contamination pattern we hardened against."
        )
