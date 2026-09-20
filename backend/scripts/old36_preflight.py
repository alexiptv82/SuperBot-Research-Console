"""OLD36 Golden Regression Gate — Phase 1 authoritative preflight (READ-ONLY).

Verifies runtime/storage/firewall/engine invariants directly against the
runtime SQLite DB and the physical content-addressed RAW store. Writes and
mutates NOTHING. Prints a machine-checkable JSON evidence blob at the end.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import database
from models import Session as SessionRow, QARun, RawFile
from checkpoints import compute_checkpoints
from checkpoint_registry import (
    OLD36_REFERENCE_SESSIONS,
    OLD36_REFERENCE_NOMINAL_HOURS,
    NEW36_SESSION_IDS,
    is_old36_reference,
    is_new36_registered,
)
from recovery.allowlist import assert_recovery_allowed
from recovery.sandbox import availability_snapshot
from recovery.goldens import counts as golden_counts
from recovery.goldens import list_cp24_csvs, list_cp36_csvs, read_golden_rows
import frozen_engine


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    report: dict = {"gates": {}, "errors": []}

    # --- DB path binding evidence ---
    report["db_path"] = database.DB_PATH
    report["db_exists"] = Path(database.DB_PATH).exists()

    db = database.SessionLocal()
    try:
        # Gate 6-baseline: frozen registry integrity
        report["frozen_old36_count"] = len(OLD36_REFERENCE_SESSIONS)
        report["frozen_old36_sessions"] = list(OLD36_REFERENCE_SESSIONS)
        report["gates"]["frozen_registry_count_11"] = (
            len(OLD36_REFERENCE_SESSIONS) == 11
        )

        ck = compute_checkpoints(db)["old36_reference"]
        report["checkpoint_present_sessions"] = ck["present_sessions"]
        report["checkpoint_raw_retained_sessions"] = ck["raw_retained_sessions"]
        report["checkpoint_raw_ready"] = ck["raw_ready"]
        report["checkpoint_expected_sessions"] = ck["expected_sessions"]

        # Gate 1: metadata sessions 11/11
        report["gates"]["metadata_sessions_11"] = ck["present_sessions"] == 11
        # Gate 2/3: physical RAW retained 11/11, missing 0
        report["gates"]["raw_retained_11"] = ck["raw_retained_sessions"] == 11
        report["gates"]["raw_missing_0"] = (
            ck["expected_sessions"] - ck["raw_retained_sessions"] == 0
        )
        # Gate 4: raw_ready true
        report["gates"]["raw_ready_true"] = ck["raw_ready"] is True

        # Gate 5 + 7 + 8: per-session stored_path existence/readability,
        # SHA256 content-address integrity, identity match, no ambiguity.
        per_session = []
        path_seen: dict[str, list[str]] = {}
        all_paths_ok = True
        all_identity_ok = True
        all_integrity_ok = True

        for sid in OLD36_REFERENCE_SESSIONS:
            entry: dict = {"session_id": sid}
            entry["identity_is_old36"] = is_old36_reference(sid)
            entry["identity_not_new36"] = not is_new36_registered(sid)
            if not (entry["identity_is_old36"] and entry["identity_not_new36"]):
                all_identity_ok = False

            srow = (
                db.query(SessionRow)
                .filter(SessionRow.session_id == sid)
                .one_or_none()
            )
            entry["metadata_present"] = srow is not None

            # Collect retained raw files across all QA runs for this session
            retained_paths = []
            if srow is not None:
                qruns = (
                    db.query(QARun)
                    .filter(QARun.session_pk == srow.id)
                    .all()
                )
                for qr in qruns:
                    rf = (
                        db.query(RawFile)
                        .filter(RawFile.qa_run_id == qr.id)
                        .one_or_none()
                    )
                    if rf is not None and rf.retained and rf.stored_path:
                        retained_paths.append(rf.stored_path)

            uniq = sorted(set(retained_paths))
            entry["retained_raw_count"] = len(uniq)
            entry["stored_paths"] = uniq

            # Ambiguity: must be exactly one distinct retained path
            entry["no_ambiguity"] = len(uniq) == 1
            if len(uniq) != 1:
                all_paths_ok = False

            path_ok = False
            integrity_ok = False
            if len(uniq) == 1:
                p = Path(uniq[0])
                path_seen.setdefault(uniq[0], []).append(sid)
                exists = p.exists()
                is_file = p.is_file() if exists else False
                size = p.stat().st_size if is_file else 0
                readable = False
                digest = None
                if is_file and size > 0:
                    try:
                        with open(p, "rb") as fh:
                            fh.read(4096)
                        readable = True
                    except Exception as exc:  # noqa: BLE001
                        report["errors"].append(f"read {sid}: {exc}")
                    # Content-address integrity: filename stem == sha256(content)
                    expected = p.stem
                    digest = sha256_file(p)
                    integrity_ok = digest == expected
                entry.update(
                    {
                        "exists": exists,
                        "is_file": is_file,
                        "size_bytes": size,
                        "readable": readable,
                        "filename_stem": p.stem,
                        "sha256_computed": digest,
                        "sha256_matches_filename": integrity_ok,
                    }
                )
                path_ok = exists and is_file and size > 0 and readable
            if not path_ok:
                all_paths_ok = False
            if not integrity_ok:
                all_integrity_ok = False
            per_session.append(entry)

        report["per_session"] = per_session
        report["gates"]["all_stored_paths_exist_readable"] = all_paths_ok
        report["gates"]["all_identity_match_frozen"] = all_identity_ok
        report["gates"]["all_raw_sha256_integrity"] = all_integrity_ok

        # Duplicate physical RAW across different sessions?
        dup = {p: sids for p, sids in path_seen.items() if len(sids) > 1}
        report["duplicate_raw_paths"] = dup
        report["gates"]["no_duplicate_raw_across_sessions"] = len(dup) == 0

        # Gate 9: disk space
        usage = shutil.disk_usage("/app")
        report["disk_free_bytes"] = usage.free
        report["disk_free_gb"] = round(usage.free / (1024**3), 3)
        report["disk_total_gb"] = round(usage.total / (1024**3), 3)
        # Regression reads small golden CSVs + no RAW materialization; require >=200MB headroom
        report["gates"]["disk_space_ok"] = usage.free > 200 * 1024 * 1024

        # Gate 10: NEW36 firewall still enforced
        fw = {"old36_allowed": True, "new36_blocked": True, "none_blocked": True}
        try:
            for sid in OLD36_REFERENCE_SESSIONS:
                assert_recovery_allowed(sid)  # must NOT raise
        except Exception as exc:  # noqa: BLE001
            fw["old36_allowed"] = False
            report["errors"].append(f"firewall wrongly blocked OLD36: {exc}")
        for nsid in NEW36_SESSION_IDS:
            try:
                assert_recovery_allowed(nsid)
                fw["new36_blocked"] = False
                report["errors"].append(f"firewall FAILED to block NEW36 {nsid}")
            except Exception:
                pass
        try:
            assert_recovery_allowed(None)
            fw["none_blocked"] = False
        except Exception:
            pass
        report["firewall"] = fw
        report["gates"]["new36_firewall_enforced"] = all(fw.values())

        # Gate 11: FrozenAnalysisEngine status
        es = frozen_engine.current_status()
        report["engine_status"] = es.status
        report["engine_accepts_input"] = es.accepts_input
        report["gates"]["engine_not_configured"] = (
            es.status == "NOT_CONFIGURED" and es.accepts_input is False
        )

        # Availability snapshot (recovery sandbox authoritative view)
        report["availability_snapshot"] = availability_snapshot()

        # Golden artifact authoritative row count (the ~11,449 claim)
        gc = golden_counts()
        report["golden_counts"] = gc
        total_golden_rows = 0
        per_file = []
        for path in list_cp24_csvs() + list_cp36_csvs():
            n = sum(1 for _ in read_golden_rows([path]))
            per_file.append({"file": path.name, "rows": n})
            total_golden_rows += n
        report["golden_total_rows"] = total_golden_rows
        report["golden_per_file"] = per_file

    finally:
        db.close()

    report["ALL_GATES_PASS"] = all(report["gates"].values())
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
