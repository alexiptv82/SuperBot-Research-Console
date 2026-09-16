#!/usr/bin/env python3
"""Run the read-only Phase 2 recovery audit.

Regenerates every artifact under ``backend/recovery/reports/``:

- ``golden_regression_summary.csv``
- ``golden_regression_failures.csv``
- ``recovery_rules.json``
- ``recovery_provenance.json``
- ``recovery_report.md``

Never modifies FrozenAnalysisEngine status. Never opens NEW36 raw ZIPs.
"""
from __future__ import annotations

import json
import os
import sys

# Runtime binding (this script inspects the real preview DB read-only).
os.environ.setdefault("SUPERBOT_ENV", "runtime")
os.environ.setdefault("SUPERBOT_DB_PATH", "/app/backend/data/superbot.db")

sys.path.insert(0, "/app/backend")

from recovery.harness import run_regression  # noqa: E402


def main() -> int:
    out = run_regression()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
