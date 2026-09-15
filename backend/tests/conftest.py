"""Shared pytest bootstrap.

Environment hardening (§ Test/Runtime DB isolation):

- Force ``SUPERBOT_ENV=test`` BEFORE any project module is imported.
- Force ``SUPERBOT_DB_PATH`` to a fresh path under ``/tmp`` for every
  pytest run. Never fall through to the runtime default.
- Force ``SUPERBOT_DATA_DIR`` to a ``/tmp`` sibling so upload staging
  never leaks into the runtime ``/app/backend/data`` tree.
- Import ``database`` last so its module-level guardrails (see
  ``database.py``) run against the already-set env.

This file lives under ``tests/`` on purpose: only pytest test files
inside this directory pick up its overrides. Loose scripts outside the
``tests/`` directory are moved to ``/app/backend/scripts/`` so pytest
cannot accidentally auto-collect them.
"""
import os
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = pathlib.Path(__file__).resolve().parent
for p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 1. Declare test environment so database.py refuses runtime paths.
os.environ["SUPERBOT_ENV"] = "test"

# 2. Give this run a dedicated /tmp DB per xdist worker so parallel
#    workers do not corrupt each other's counters.
_worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
DATA_DIR = f"/tmp/superbot-suite-data-{_worker}"
DB_PATH = f"{DATA_DIR}/superbot-{_worker}.db"

# Force (not setdefault) - we never want to inherit a real DB path.
os.environ["SUPERBOT_DB_PATH"] = DB_PATH
os.environ["SUPERBOT_DATA_DIR"] = DATA_DIR
os.environ.setdefault("SUPERBOT_PASSWORD", "test-pw-123")
os.environ.setdefault(
    "SUPERBOT_SESSION_SECRET", "stable-secret-for-tests-must-be-32chars-min"
)

pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# Fresh DB per full pytest run to keep counters predictable.
try:
    os.unlink(DB_PATH)
except FileNotFoundError:
    pass

from database import RUNTIME_DB_PATH, init_db  # noqa: E402

# 3. Refuse to boot the suite if the guardrail somehow lands on the
#    runtime DB (belt + suspenders).
assert os.path.realpath(DB_PATH) != os.path.realpath(RUNTIME_DB_PATH), (
    f"Test suite bound to runtime DB: {DB_PATH} == {RUNTIME_DB_PATH}"
)

init_db()
