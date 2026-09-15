#!/usr/bin/env python3
"""Deduplicate retained raw ZIPs in the runtime database.

Consolidates every retained raw ZIP into ONE canonical file per
``source_file_sha256``:

1. Group all QA runs by ``source_file_sha256``.
2. For each group, verify on-disk files are byte-identical (checked
   via file size + real SHA256, not just the DB-declared value).
3. Move (or reuse) the canonical file at ``<raw_dir>/<sha>.zip``.
4. Repoint every ``raw_files.stored_path`` in the group at the
   canonical path.
5. Delete redundant on-disk copies.
6. Prune orphan ``*.zip`` blobs that no ``raw_files`` row references.

QA runs and audit history are never touched. Only physical files and
``raw_files.stored_path`` are updated.

Safety:
- Refuses to run without ``--yes``.
- Timestamped DB backup before mutation.
- ``--dry-run`` prints plan without touching anything.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["SUPERBOT_ENV"] = "runtime"
os.environ.setdefault("SUPERBOT_DB_PATH", "/app/backend/data/superbot.db")

sys.path.insert(0, "/app/backend")

from sqlalchemy import select, text  # noqa: E402

from database import DB_PATH, SessionLocal, engine  # noqa: E402
from models import QARun, RawFile  # noqa: E402
from raw_storage import canonical_path, prune_orphan_blobs  # noqa: E402

RAW_DIR = Path("/app/backend/data/raw_zips")


def _sha256_of_file(p: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} {unit}"
        n = n / 1024
    return f"{n:.2f} TiB"


def _snapshot_disk() -> int:
    if not RAW_DIR.exists():
        return 0
    total = 0
    for p in RAW_DIR.iterdir():
        if p.is_file():
            total += p.stat().st_size
    return total


def backup_db() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    src = Path(DB_PATH)
    dst = src.with_suffix(src.suffix + f".pre-storage-dedup-{ts}")
    shutil.copy2(src, dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    disk_before = _snapshot_disk()
    print(f"raw_zips disk usage BEFORE: {_human(disk_before)}")
    print(f"DB path: {DB_PATH}")

    # Load every QA run and its RawFile (if any).
    plan: dict[str, dict] = {}  # sha -> {"canonical": Path, "raw_ids": [], "on_disk": set()}
    orphan_ondisk: list[Path] = []

    with SessionLocal() as db:
        rows = db.execute(
            select(QARun.source_file_sha256, QARun.id, RawFile.id, RawFile.stored_path)
            .join(RawFile, RawFile.qa_run_id == QARun.id, isouter=True)
            .where(QARun.source_file_sha256.isnot(None))
        ).all()

        for sha, qa_id, rf_id, stored in rows:
            slot = plan.setdefault(sha, {"raw_ids": [], "on_disk": set(), "qa_ids": []})
            slot["qa_ids"].append(qa_id)
            if rf_id is not None:
                slot["raw_ids"].append(rf_id)
            if stored:
                slot["on_disk"].add(stored)

    print("\nGroups by source_file_sha256:")
    total_physical_duplicates = 0
    for sha, slot in plan.items():
        physical_copies = 0
        verified = []
        for p in slot["on_disk"]:
            if Path(p).exists():
                actual_sha = _sha256_of_file(Path(p))
                verified.append((p, actual_sha))
                physical_copies += 1
        print(
            f"  {sha[:16]}...: qa_runs={len(slot['qa_ids'])}  "
            f"raw_rows={len(slot['raw_ids'])}  physical_copies={physical_copies}"
        )
        for p, sha_actual in verified:
            marker = "OK" if sha_actual == sha else "!! sha mismatch"
            print(f"      {p}   {marker}")
        # Count anything above the first canonical as a duplicate.
        if physical_copies > 1:
            total_physical_duplicates += physical_copies - 1
        slot["canonical"] = canonical_path(RAW_DIR, sha)

    print(f"\nPhysical duplicates found: {total_physical_duplicates}")

    if args.dry_run:
        print("\n[dry-run] no changes written")
        return

    if not args.yes:
        print("\nRefusing to mutate without --yes")
        sys.exit(1)

    dst = backup_db()
    print(f"\nBackup written: {dst}")

    removed_physical = 0

    for sha, slot in plan.items():
        canonical = slot["canonical"]
        canonical.parent.mkdir(parents=True, exist_ok=True)

        # 1. Ensure the canonical file exists and its SHA matches.
        source_file = None
        for p in slot["on_disk"]:
            if Path(p).exists():
                actual = _sha256_of_file(Path(p))
                if actual == sha:
                    source_file = Path(p)
                    break
        if source_file is None:
            print(f"warn: no on-disk file matches sha {sha}; skipping group")
            continue

        if not canonical.exists():
            # Move or copy the verified source into place.
            if os.path.realpath(source_file) != os.path.realpath(canonical):
                shutil.copy2(source_file, canonical)
        # Sanity: canonical must now match sha
        actual_canonical = _sha256_of_file(canonical)
        if actual_canonical != sha:
            print(f"FATAL: canonical {canonical} sha={actual_canonical[:12]} != {sha[:12]}")
            sys.exit(3)

        # 2. Repoint every RawFile row in this group at the canonical.
        with engine.begin() as conn:
            for rf_id in slot["raw_ids"]:
                conn.execute(
                    text(
                        "UPDATE raw_files "
                        "SET stored_path = :cp, size_bytes = :sz, retained = 1 "
                        "WHERE id = :rf"
                    ),
                    {"cp": str(canonical), "sz": canonical.stat().st_size, "rf": rf_id},
                )

        # 3. Delete redundant on-disk copies.
        for p in slot["on_disk"]:
            pp = Path(p)
            if not pp.exists():
                continue
            if os.path.realpath(pp) == os.path.realpath(canonical):
                continue
            # Verify before deletion.
            try:
                actual = _sha256_of_file(pp)
            except OSError:
                continue
            if actual == sha:
                pp.unlink()
                removed_physical += 1

    # 4. Prune any *.zip in RAW_DIR that no raw_files.stored_path
    #    references (post-cleanup source of truth).
    with SessionLocal() as db:
        referenced = [
            rf.stored_path
            for rf in db.execute(select(RawFile)).scalars().all()
            if rf.stored_path
        ]
    removed_orphans = prune_orphan_blobs(RAW_DIR, referenced)
    for p in removed_orphans:
        print(f"  pruned orphan: {p}")

    disk_after = _snapshot_disk()
    print(f"\nDedup complete:")
    print(f"  physical duplicate ZIPs removed: {removed_physical}")
    print(f"  orphan ZIPs pruned            : {len(removed_orphans)}")
    print(f"  disk BEFORE : {_human(disk_before)}")
    print(f"  disk AFTER  : {_human(disk_after)}")
    print(f"  disk saved  : {_human(disk_before - disk_after)}")

    # Final sanity: per-session summary.
    with SessionLocal() as db:
        print("\nPost-cleanup summary:")
        for sha, slot in plan.items():
            canonical = slot["canonical"]
            n_rf = db.execute(
                select(RawFile).where(RawFile.stored_path == str(canonical))
            ).scalars().all()
            print(
                f"  {sha[:16]}...: canonical={canonical.name} "
                f"raw_rows_pointing={len(n_rf)}  "
                f"qa_runs_in_group={len(slot['qa_ids'])}  "
                f"exists={canonical.exists()}"
            )


if __name__ == "__main__":
    main()
