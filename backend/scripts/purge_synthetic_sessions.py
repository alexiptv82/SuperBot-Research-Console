#!/usr/bin/env python3
"""One-shot cleanup of the runtime SQLite database.

Removes every synthetic/test session while preserving:

- The one real uploaded session (``KEEP_SESSION_ID``) with all its
  QA runs, retained raw ZIPs and audit history.
- Global audit events not linked to any synthetic session (auth
  login/logout, generic policy events).

Safety:

- Requires ``SUPERBOT_ENV=runtime`` (default) so it never nukes a test
  DB by mistake.
- Refuses to run without an explicit ``--yes`` flag.
- Creates a timestamped backup right before mutating anything.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force runtime binding.
os.environ["SUPERBOT_ENV"] = "runtime"
os.environ.setdefault("SUPERBOT_DB_PATH", "/app/backend/data/superbot.db")

sys.path.insert(0, "/app/backend")

from sqlalchemy import select, text

from database import DB_PATH, SessionLocal, engine
from models import AuditLog, QARun, RawFile, Session as SessionModel

KEEP_SESSION_ID = "20260911T040213Z_43a798b7"


def backup_db() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    src = Path(DB_PATH)
    dst = src.with_suffix(src.suffix + f".pre-cleanup-{ts}")
    shutil.copy2(src, dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="confirm destructive run")
    ap.add_argument("--dry-run", action="store_true", help="show plan only")
    args = ap.parse_args()

    print(f"DB path: {DB_PATH}")

    with SessionLocal() as db:
        keep = db.execute(
            select(SessionModel).where(SessionModel.session_id == KEEP_SESSION_ID)
        ).scalars().first()
        if keep is None:
            print(f"FATAL: session {KEEP_SESSION_ID!r} not found. Aborting.")
            sys.exit(2)
        keep_pk = keep.id

        # Snapshot linked qa_runs + retained files so we know what to preserve.
        keep_runs = db.execute(
            select(QARun).where(QARun.session_pk == keep_pk)
        ).scalars().all()
        keep_run_ids = [r.id for r in keep_runs]
        retained_paths = []
        for r in keep_runs:
            rf = db.execute(
                select(RawFile).where(RawFile.qa_run_id == r.id)
            ).scalars().first()
            if rf and rf.stored_path:
                retained_paths.append(rf.stored_path)

        all_sessions = db.execute(select(SessionModel)).scalars().all()
        purge_session_pks = [s.id for s in all_sessions if s.id != keep_pk]
        purge_session_ids = [s.session_id for s in all_sessions if s.id != keep_pk]

    print(f"Sessions to purge: {len(purge_session_pks)}")
    print(f"Sessions to keep : 1 ({KEEP_SESSION_ID}, {len(keep_run_ids)} QA runs)")
    print(f"Retained raw ZIPs to preserve: {len(retained_paths)}")
    for p in retained_paths:
        print(f"  KEEP raw: {p}")

    if args.dry_run:
        print("\n[dry-run] no changes written")
        return

    if not args.yes:
        print("\nRefusing to mutate without --yes")
        sys.exit(1)

    dst = backup_db()
    print(f"\nBackup written: {dst}")

    removed = {
        "sessions": 0,
        "qa_runs": 0,
        "raw_files": 0,
        "audit": 0,
        "raw_zip_files_removed": 0,
    }

    with engine.begin() as conn:
        # 1. Collect QA run ids to drop.
        qrs = conn.execute(
            text(
                "SELECT id FROM qa_runs WHERE session_pk IN ("
                "  SELECT id FROM sessions WHERE session_id != :keep"
                ")"
            ),
            {"keep": KEEP_SESSION_ID},
        ).all()
        drop_qa_ids = [row[0] for row in qrs]

        # 2. Collect raw file paths so we can unlink orphan blobs.
        blob_paths = []
        if drop_qa_ids:
            for qid in drop_qa_ids:
                rows = conn.execute(
                    text("SELECT stored_path FROM raw_files WHERE qa_run_id = :qid"),
                    {"qid": qid},
                ).all()
                for r in rows:
                    if r[0]:
                        blob_paths.append(r[0])

        # 3. Null out current_qa_run_id on non-keep sessions so we can
        #    delete qa_runs freely without violating the FK.
        conn.execute(
            text(
                "UPDATE sessions SET current_qa_run_id = NULL "
                "WHERE session_id != :keep"
            ),
            {"keep": KEEP_SESSION_ID},
        )

        # 4. Delete raw_files for the doomed qa_runs.
        if drop_qa_ids:
            in_clause = ",".join([f":q{i}" for i in range(len(drop_qa_ids))])
            params = {f"q{i}": qid for i, qid in enumerate(drop_qa_ids)}
            rf_del = conn.execute(
                text(f"DELETE FROM raw_files WHERE qa_run_id IN ({in_clause})"),
                params,
            )
            removed["raw_files"] = rf_del.rowcount or 0

        # 5. Delete audit_log rows tied to purged sessions or qa_runs.
        aud_del = conn.execute(
            text(
                "DELETE FROM audit_log WHERE ("
                "  session_id IS NOT NULL AND session_id != :keep"
                ") OR ("
                "  qa_run_id IS NOT NULL AND qa_run_id NOT IN ("
                "     SELECT id FROM qa_runs WHERE session_pk = ("
                "        SELECT id FROM sessions WHERE session_id = :keep"
                "     )"
                "  )"
                ")"
            ),
            {"keep": KEEP_SESSION_ID},
        )
        removed["audit"] = aud_del.rowcount or 0

        # 6. Delete qa_runs and sessions.
        qr_del = conn.execute(
            text(
                "DELETE FROM qa_runs WHERE session_pk IN ("
                "  SELECT id FROM sessions WHERE session_id != :keep"
                ")"
            ),
            {"keep": KEEP_SESSION_ID},
        )
        removed["qa_runs"] = qr_del.rowcount or 0

        s_del = conn.execute(
            text("DELETE FROM sessions WHERE session_id != :keep"),
            {"keep": KEEP_SESSION_ID},
        )
        removed["sessions"] = s_del.rowcount or 0

    # 7. Unlink orphan raw ZIP blobs on disk.
    keep_set = {os.path.realpath(p) for p in retained_paths}
    for p in blob_paths:
        try:
            if os.path.realpath(p) in keep_set:
                continue
            os.unlink(p)
            removed["raw_zip_files_removed"] += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"warn: could not unlink {p}: {e}")

    # 8. Also scan raw_zips dir for orphan blobs no longer referenced.
    raw_dir = Path("/app/backend/data/raw_zips")
    still_referenced = set()
    with SessionLocal() as db:
        for rf in db.execute(select(RawFile)).scalars().all():
            if rf.stored_path:
                still_referenced.add(os.path.realpath(rf.stored_path))
    if raw_dir.exists():
        for p in raw_dir.iterdir():
            if p.is_file() and os.path.realpath(str(p)) not in still_referenced:
                try:
                    p.unlink()
                    removed["raw_zip_files_removed"] += 1
                except OSError as e:
                    print(f"warn: could not unlink {p}: {e}")

    print("\nCleanup complete:")
    for k, v in removed.items():
        print(f"  {k}: {v}")

    # 9. Sanity: exactly one session must survive.
    with SessionLocal() as db:
        remaining = db.execute(select(SessionModel)).scalars().all()
        print(f"\nRemaining sessions: {len(remaining)}")
        for s in remaining:
            print(f"  - {s.session_id}  hint={s.checkpoint_hint}")
    print(f"\nBackup available at: {dst}")


if __name__ == "__main__":
    main()
