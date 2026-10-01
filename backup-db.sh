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

# Skip when nothing changed since the last good backup. Each copy is a full
# 15G file. The stamp records the source state (size, ns mtime, inode) and the
# backup file it produced; skip only if that file still exists, the source
# state matches, and there are no pending WAL frames.
STAMP_FILE="$BACKUP_DIR/.last-source-stamp"
source_state() { stat -c '%s %.9Y %i' "$DB_PATH"; }
wal_size() { stat -c %s "$DB_PATH-wal" 2>/dev/null || echo 0; }

BEFORE="$(source_state)"
WAL_BEFORE="$(wal_size)"
if [ "$WAL_BEFORE" -eq 0 ] && [ -f "$STAMP_FILE" ]; then
    IFS=$'\t' read -r LAST_STATE LAST_BACKUP < "$STAMP_FILE" || true
    if [ "${LAST_STATE:-}" = "$BEFORE" ] && [ -n "${LAST_BACKUP:-}" ] \
        && [ -f "$BACKUP_DIR/$LAST_BACKUP" ]; then
        echo "Unchanged since $LAST_BACKUP; skipping backup"
        exit 0
    fi
fi

# Use Python sqlite3 backup for consistency (sqlite3 CLI not installed).
# Write to a temp name and rename on success so a killed run never leaves a
# truncated file that looks like a valid backup.
BACKUP_NAME="code_index_$TIMESTAMP.db"
TMP_BACKUP="$BACKUP_DIR/$BACKUP_NAME.tmp"
trap 'rm -f "$TMP_BACKUP"' EXIT
rm -f "$TMP_BACKUP"
python3 -c "
import sqlite3, sys
src = sqlite3.connect('$DB_PATH')
dst = sqlite3.connect('$TMP_BACKUP')
src.backup(dst)
dst.close()
src.close()
"
mv -f "$TMP_BACKUP" "$BACKUP_DIR/$BACKUP_NAME"
echo "Backed up to $BACKUP_DIR/$BACKUP_NAME ($(du -sh "$BACKUP_DIR/$BACKUP_NAME" | cut -f1))"

# Record the stamp only if the source did not move during the copy.
if [ "$WAL_BEFORE" -eq 0 ] && [ "$(wal_size)" -eq 0 ] && [ "$(source_state)" = "$BEFORE" ]; then
    printf '%s\t%s\n' "$BEFORE" "$BACKUP_NAME" > "$STAMP_FILE"
else
    rm -f "$STAMP_FILE"
fi

# Rotate: keep only the newest MAX_BACKUPS
cd "$BACKUP_DIR"
ls -1t code_index_*.db 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f
echo "Backups retained: $(ls -1 code_index_*.db 2>/dev/null | wc -l)"
