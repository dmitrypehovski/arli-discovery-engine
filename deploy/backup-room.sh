#!/bin/sh
# Copy the room's store, and prove the copy opens.
#
# Why this exists. The room's memory is one sqlite file: every registration, introduction, agreement and the
# evidence under it. There was no copy of it anywhere and no timer making one, so losing that file would lose the
# record of who is in the network and what they agreed to, which is the one thing here that cannot be recomputed.
#
# The copy is taken with sqlite's own backup call rather than cp, because copying a database file while it is
# being written can produce a file that opens and is wrong. After copying, the copy is opened and asked for its
# tables: a backup that is never read back is a hope, not a backup.
#
# Usage: backup-room.sh [destination directory] [how many to keep]

set -eu

STORE="${ARLI_MATCH_DB:-/var/lib/arli-match/match.sqlite3}"
DEST="${1:-/var/backups/arli-match}"
KEEP="${2:-14}"
PY="${ARLI_PY:-/home/paperclip/arli-org/arli-engine/.venv/bin/python}"

if [ ! -f "$STORE" ]; then
  echo "no store at $STORE: nothing to copy" >&2
  exit 1
fi

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$DEST/match-$STAMP.sqlite3"

"$PY" - "$STORE" "$OUT" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1], sys.argv[2]
with sqlite3.connect(source) as source_connection, sqlite3.connect(destination) as destination_connection:
    source_connection.backup(destination_connection)
    # Read the copy back before it is trusted: a copy nobody reads is not a backup.
    tables = {row[0] for row in destination_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not tables:
        raise SystemExit(f"the copy at {destination} has no tables")
    counts = {table: destination_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("agents", "remotes", "introductions", "agreements") if table in tables}
print(f"copied {source} to {destination}; tables {len(tables)}; rows {counts}")
PY

# Keep the last few, oldest first, and say what was removed rather than deleting quietly.
ls -1t "$DEST"/match-*.sqlite3 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
  echo "removed the oldest copy: $old"
done

echo "kept: $(ls -1 "$DEST"/match-*.sqlite3 2>/dev/null | wc -l) copies in $DEST"