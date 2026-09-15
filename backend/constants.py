"""Frozen constants for SuperBot Research Console V1.

These values come directly from the SuperBot Trading Project Handoff
(2026-09-15). They are frozen through the 72H validation and MUST NOT be
changed silently.
"""
from __future__ import annotations

# Section 6 — Frozen MultiVenue Microstructure Collector V2
FROZEN_COLLECTOR_SHA256 = (
    "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3"
)

# Verdict vocabulary (§12.6). Do NOT invent new values.
VERDICT_PASS = "PASS"
VERDICT_PASS_WITH_WARNING = "PASS_WITH_WARNING"
VERDICT_FAIL = "FAIL"
VERDICT_UNRESOLVED = "UNRESOLVED"
VERDICTS = (
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
    VERDICT_FAIL,
    VERDICT_UNRESOLVED,
)

# Duplicate detection states (§12.4)
DUP_NEW = "NEW"
DUP_EXACT_DUPLICATE = "EXACT_DUPLICATE"
DUP_SAME_SESSION_DIFFERENT_FILE = "SAME_SESSION_DIFFERENT_FILE"
DUP_CONFLICT = "CONFLICT"
DUPLICATE_STATES = (
    DUP_NEW,
    DUP_EXACT_DUPLICATE,
    DUP_SAME_SESSION_DIFFERENT_FILE,
    DUP_CONFLICT,
)

# Checkpoint labels (§12.8)
CHECKPOINT_OLD36 = "OLD36"
CHECKPOINT_NEW12 = "NEW12"
CHECKPOINT_TOTAL48 = "TOTAL48"
CHECKPOINT_NEW36 = "NEW36"
CHECKPOINT_TOTAL72 = "TOTAL72"

# Target hours per checkpoint (from the handoff arithmetic)
CHECKPOINT_TARGETS = {
    CHECKPOINT_OLD36: 36.0,
    CHECKPOINT_NEW12: 12.0,
    CHECKPOINT_TOTAL48: 48.0,
    CHECKPOINT_NEW36: 36.0,
    CHECKPOINT_TOTAL72: 72.0,
}

# Expected duration of a single MultiVenue session (§1 / §12.1: "3H session").
SESSION_HOURS = 3.0

# FrozenAnalysisEngine status vocabulary (§12.8).
ENGINE_NOT_CONFIGURED = "NOT_CONFIGURED"

# Dataset structure directories expected inside a MultiVenue 3H ZIP
# (§12.6 Dataset structure).
DATASET_DIRS = (
    "sync_grid_100ms",
    "normalized_books",
    "normalized_trades",
)

# Parquet magic bytes
PARQUET_MAGIC = b"PAR1"

# ZIP security limits
MAX_ENTRIES_PER_ZIP = 200_000
MAX_EXTRACTED_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB safety ceiling
MAX_ENTRY_PATH_LEN = 512
