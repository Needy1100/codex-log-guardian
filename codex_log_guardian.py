"""Bound Codex's local SQLite log store without printing log contents."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone


DB = pathlib.Path(r"C:\Users\33855\.codex\logs_2.sqlite")
BACKUP = pathlib.Path(
    r"C:\Users\33855\Documents\ChatGPT\skill部署\codex-log-backup-latest.sqlite"
)
TARGET_ESTIMATED_BYTES = 50 * 1024 * 1024
PHYSICAL_CAP_BYTES = 120 * 1024 * 1024


def stats(connection: sqlite3.Connection) -> tuple[int, int]:
    return connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(estimated_bytes), 0) FROM logs"
    ).fetchone()


def file_bytes(path: pathlib.Path) -> int:
    return path.stat().st_size if path.exists() else 0


def make_consistent_backup() -> str:
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=BACKUP.stem + ".", suffix=".sqlite.tmp", dir=BACKUP.parent
    )
    os.close(fd)
    temp_path = pathlib.Path(temp_name)
    try:
        source_uri = "file:" + DB.as_posix() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(temp_path)) as destination:
                source.backup(destination)
            with closing(sqlite3.connect(temp_path)) as verify:
                if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup integrity check failed")
        os.replace(temp_path, BACKUP)
        return str(BACKUP)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> int:
    if not DB.exists():
        print(json.dumps({"status": "missing_database"}))
        return 0

    before_db = file_bytes(DB)
    before_wal = file_bytes(DB.with_name(DB.name + "-wal"))
    backup_path = None
    deleted = 0

    try:
        with closing(sqlite3.connect(DB, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout=10000")
            before_rows, before_estimated = stats(connection)
            physical_before = before_db + before_wal

            if before_estimated > TARGET_ESTIMATED_BYTES or physical_before > PHYSICAL_CAP_BYTES:
                backup_path = make_consistent_backup()

            if before_estimated > TARGET_ESTIMATED_BYTES:
                connection.execute("BEGIN IMMEDIATE")
                deleted = connection.execute(
                    """
                    WITH ranked AS (
                        SELECT id,
                               SUM(estimated_bytes) OVER (
                                   ORDER BY ts DESC, ts_nanos DESC, id DESC
                               ) AS kept_bytes
                        FROM logs
                    )
                    DELETE FROM logs
                    WHERE id IN (SELECT id FROM ranked WHERE kept_bytes > ?)
                    """,
                    (TARGET_ESTIMATED_BYTES,),
                ).rowcount
                connection.commit()

            if deleted or physical_before > PHYSICAL_CAP_BYTES:
                connection.execute("VACUUM")

            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            after_rows, after_estimated = stats(connection)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]

        result = {
            "status": "ok",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "before_rows": before_rows,
            "after_rows": after_rows,
            "deleted_rows": deleted,
            "before_estimated_bytes": before_estimated,
            "after_estimated_bytes": after_estimated,
            "database_bytes": file_bytes(DB),
            "wal_bytes": file_bytes(DB.with_name(DB.name + "-wal")),
            "integrity": integrity,
            "backup": backup_path,
        }
        print(json.dumps(result, ensure_ascii=True))
        return 0 if integrity == "ok" else 2
    except sqlite3.OperationalError as error:
        print(json.dumps({"status": "busy_or_unavailable", "error_type": type(error).__name__}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
