"""FrozenAnalysisEngine — recovery sandbox (Phase 2 preparation).

Hard access boundaries:

- Read-only access to the 11 OLD36_REFERENCE raw sessions ONLY.
- Read-only access to the CP24/CP36 golden checkpoint CSVs.
- Read-only access to the frozen collector source.
- Read-only access to the historical research registry.
- NO quantitative access to NEW36 raw sessions (operational metadata
  may remain visible via the main API; the recovery sandbox itself
  refuses to open any NEW36 raw ZIP).

Nothing in this package touches the runtime FrozenAnalysisEngine
status. FrozenAnalysisEngine remains NOT_CONFIGURED throughout
recovery.
"""
from __future__ import annotations

from pathlib import Path

RECOVERY_ROOT: Path = Path(__file__).resolve().parent
GOLDENS_DIR: Path = RECOVERY_ROOT / "goldens"
REPORTS_DIR: Path = RECOVERY_ROOT / "reports"

__all__ = [
    "RECOVERY_ROOT",
    "GOLDENS_DIR",
    "REPORTS_DIR",
]
