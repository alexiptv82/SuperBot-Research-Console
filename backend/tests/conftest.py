"""Shared pytest bootstrap.

- Puts /app/backend and this dir on sys.path so tests can import
  ``server``, ``qa_engine``, etc. as top-level modules.
- Provisions a single shared temporary SQLite DB before any test module
  imports server (so the module-level engine binds to it once and stays
  consistent across xdist workers).
- Calls init_db() so the schema exists before any request is made.
"""
import os
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = pathlib.Path(__file__).resolve().parent
for p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DATA_DIR = "/tmp/superbot-suite-data"
_worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
DB_PATH = f"{DATA_DIR}/superbot-{_worker}.db"

os.environ.setdefault("SUPERBOT_DB_PATH", DB_PATH)
os.environ.setdefault("SUPERBOT_PASSWORD", "test-pw-123")
os.environ.setdefault(
    "SUPERBOT_SESSION_SECRET", "stable-secret-for-tests-must-be-32chars-min"
)
os.environ.setdefault("SUPERBOT_DATA_DIR", DATA_DIR)

pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# Fresh DB per full pytest run to keep counters predictable.
try:
    os.unlink(DB_PATH)
except FileNotFoundError:
    pass

from database import init_db  # noqa: E402

init_db()
