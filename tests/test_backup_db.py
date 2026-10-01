"""Regression tests for backup-db.sh skip-if-unchanged behavior.

Each backup is a full copy of the index, so an unchanged DB must not produce a
new copy on every scheduled run. These tests use a throwaway SQLite file and a
temp backup dir; they never touch the real index.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "backup-db.sh"


def run_backup(db: Path, backups: Path) -> str:
    env = {**os.environ, "CODE_SEARCH_DB": str(db), "CODE_SEARCH_BACKUP_DIR": str(backups)}
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=True)
    return result.stdout


def make_db(path: Path) -> None:
    """Create a WAL-mode DB, like the real index."""
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table t (a)")
    conn.commit()
    conn.close()


def test_unchanged_db_is_skipped(tmp_path: Path) -> None:
    db, backups = tmp_path / "index.db", tmp_path / "backups"
    make_db(db)

    assert "Backed up" in run_backup(db, backups)
    assert "skipping backup" in run_backup(db, backups)
    assert len(list(backups.glob("code_index_*.db"))) == 1


def test_changed_db_is_backed_up_again(tmp_path: Path) -> None:
    db, backups = tmp_path / "index.db", tmp_path / "backups"
    make_db(db)
    run_backup(db, backups)

    # No mtime bump: a same-second write must still be detected.
    conn = sqlite3.connect(db)
    conn.execute("insert into t values (1)")
    conn.commit()
    conn.close()

    assert "Backed up" in run_backup(db, backups)


def test_missing_stamp_forces_backup(tmp_path: Path) -> None:
    db, backups = tmp_path / "index.db", tmp_path / "backups"
    make_db(db)
    run_backup(db, backups)
    (backups / ".last-source-stamp").unlink()

    assert "Backed up" in run_backup(db, backups)


def test_pending_wal_forces_backup(tmp_path: Path) -> None:
    db, backups = tmp_path / "index.db", tmp_path / "backups"
    make_db(db)
    run_backup(db, backups)

    writer = sqlite3.connect(db)
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("insert into t values (2)")
    writer.commit()
    try:
        assert (tmp_path / "index.db-wal").stat().st_size > 0
        assert "Backed up" in run_backup(db, backups)
    finally:
        writer.close()


def test_deleted_newest_backup_forces_backup(tmp_path: Path) -> None:
    db, backups = tmp_path / "index.db", tmp_path / "backups"
    make_db(db)
    run_backup(db, backups)
    for backup in backups.glob("code_index_*.db"):
        backup.unlink()

    assert "Backed up" in run_backup(db, backups)
    assert not list(backups.glob("*.tmp"))
