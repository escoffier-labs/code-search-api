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
    with sqlite3.connect(path) as conn:
        conn.execute("create table t (a)")


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

    with sqlite3.connect(db) as conn:
        conn.execute("insert into t values (1)")
    stat = db.stat()
    os.utime(db, (stat.st_atime, stat.st_mtime + 5))

    assert "Backed up" in run_backup(db, backups)


def test_missing_stamp_forces_backup(tmp_path: Path) -> None:
    db, backups = tmp_path / "index.db", tmp_path / "backups"
    make_db(db)
    run_backup(db, backups)
    (backups / ".last-source-stamp").unlink()

    assert "Backed up" in run_backup(db, backups)
