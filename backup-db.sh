#!/bin/bash
# Backup the SQLite index with simple rotation
set -euo pipefail

DB_DIR="$(dirname "$(realpath "$0")")"
DB_PATH="${CODE_SEARCH_DB:-$DB_DIR/code_index.db}"
BACKUP_DIR="${CODE_SEARCH_BACKUP_DIR:-$DB_DIR/backups}"
TIMESTAMP=$(date +%Y-%m-%d_%H%M)
MAX_BACKUPS="${CODE_SEARCH_MAX_BACKUPS:-4}"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH not found"
    exit 1
fi

# Skip when nothing changed since the newest backup: same DB size and mtime,
# and no pending WAL frames. Each copy is a full 15G file.
STAMP_FILE="$BACKUP_DIR/.last-source-stamp"
WAL_SIZE=$(stat -c %s "$DB_PATH-wal" 2>/dev/null || echo 0)
STAMP="$(stat -c '%s %Y' "$DB_PATH")"
NEWEST=$(ls -1t "$BACKUP_DIR"/code_index_*.db 2>/dev/null | head -n 1 || true)
if [ -n "$NEWEST" ] && [ "$WAL_SIZE" -eq 0 ] && [ -f "$STAMP_FILE" ] \
    && [ "$(cat "$STAMP_FILE")" = "$STAMP" ]; then
    echo "Unchanged since $(basename "$NEWEST"); skipping backup"
    exit 0
fi

# Use Python sqlite3 backup for consistency (sqlite3 CLI not installed)
python3 -c "
import sqlite3, sys
src = sqlite3.connect('$DB_PATH')
dst = sqlite3.connect('$BACKUP_DIR/code_index_$TIMESTAMP.db')
src.backup(dst)
dst.close()
src.close()
"
echo "Backed up to $BACKUP_DIR/code_index_$TIMESTAMP.db ($(du -sh "$BACKUP_DIR/code_index_$TIMESTAMP.db" | cut -f1))"
[ "$WAL_SIZE" -eq 0 ] && echo "$STAMP" > "$STAMP_FILE" || rm -f "$STAMP_FILE"

# Rotate: keep only the newest MAX_BACKUPS
cd "$BACKUP_DIR"
ls -1t code_index_*.db 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f
echo "Backups retained: $(ls -1 code_index_*.db 2>/dev/null | wc -l)"
