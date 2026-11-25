"""SQLite state persistence for wab-sweeper (.wab-sweeper.db) and read-only archive openers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

SWEEPER_DB_NAME = ".wab-sweeper.db"


def open_readonly_sqlite(db_path: str) -> sqlite3.Connection:
    """Open an existing SQLite database strictly in read-only URI mode (?mode=ro)."""
    abs_path = os.path.abspath(db_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Required database not found: {abs_path}")
    uri = Path(abs_path).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_sweeper_db(archive_dir: str) -> sqlite3.Connection:
    """Open or initialize .wab-sweeper.db in archive_dir with safe durability PRAGMAs."""
    db_path = os.path.join(archive_dir, SWEEPER_DB_NAME)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adb_sweep_state (
            sweep_id       TEXT NOT NULL,
            device_serial  TEXT NOT NULL,
            remote_path    TEXT NOT NULL,
            archive_path   TEXT NOT NULL,
            chat_jid       TEXT,
            size_bytes     INTEGER NOT NULL,
            status         TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            completed_at   TEXT,
            undone_at      TEXT,
            PRIMARY KEY (sweep_id, remote_path, device_serial)
        );

        CREATE INDEX IF NOT EXISTS idx_sweep_state_status
            ON adb_sweep_state(device_serial, status, created_at);

        CREATE TABLE IF NOT EXISTS adb_restore_state (
            restore_id     TEXT NOT NULL,
            device_serial  TEXT NOT NULL,
            remote_path    TEXT NOT NULL,
            archive_path   TEXT NOT NULL,
            size_bytes     INTEGER NOT NULL,
            status         TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            completed_at   TEXT,
            PRIMARY KEY (restore_id, remote_path, device_serial)
        );

        CREATE INDEX IF NOT EXISTS idx_restore_state_status
            ON adb_restore_state(device_serial, status, created_at);
        """
    )

    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        conn.execute("PRAGMA user_version = 1")
    conn.commit()

    check = conn.execute("PRAGMA quick_check").fetchall()
    if [tuple(r) for r in check] != [("ok",)]:
        raise sqlite3.DatabaseError(f"Sweeper DB integrity check failed: {check}")

    return conn


def create_sweep_batch(
    conn: sqlite3.Connection,
    sweep_id: str,
    device_serial: str,
    entries: list[dict[str, Any]],
    created_at: str,
) -> None:
    """Stage candidate sweep entries atomically inside a transaction."""
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(
            """
            INSERT INTO adb_sweep_state (
                sweep_id, device_serial, remote_path, archive_path,
                chat_jid, size_bytes, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'staged', ?)
            ON CONFLICT(sweep_id, remote_path, device_serial) DO NOTHING
            """,
            [
                (
                    sweep_id,
                    device_serial,
                    item["remote_path"],
                    item["archive_path"],
                    item["chat_jid"],
                    item["size_bytes"],
                    created_at,
                )
                for item in entries
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_latest_incomplete_sweep_id(conn: sqlite3.Connection, device_serial: str) -> str | None:
    """Return the most recent sweep_id that still has 'staged' entries, or None."""
    row = conn.execute(
        """
        SELECT sweep_id
        FROM adb_sweep_state
        WHERE device_serial = ? AND status = 'staged'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (device_serial,),
    ).fetchone()
    return str(row["sweep_id"]) if row else None


def get_staged_sweep_entries(
    conn: sqlite3.Connection, sweep_id: str, device_serial: str
) -> list[dict[str, Any]]:
    """Return all 'staged' entries for a given sweep_id and device_serial."""
    rows = conn.execute(
        """
        SELECT sweep_id, device_serial, remote_path, archive_path, chat_jid, size_bytes, status
        FROM adb_sweep_state
        WHERE sweep_id = ? AND device_serial = ? AND status = 'staged'
        ORDER BY rowid ASC
        """,
        (sweep_id, device_serial),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_sweep_entries_status(
    conn: sqlite3.Connection,
    sweep_id: str,
    device_serial: str,
    remote_paths: list[str],
    status: str,
    completed_at: str,
) -> None:
    """Update status and completed_at for a batch of sweep entries."""
    if not remote_paths:
        return
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(
            """
            UPDATE adb_sweep_state
            SET status = ?, completed_at = ?
            WHERE sweep_id = ? AND remote_path = ? AND device_serial = ?
            """,
            [(status, completed_at, sweep_id, rp, device_serial) for rp in remote_paths],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_last_undoable_sweep_id(conn: sqlite3.Connection, device_serial: str) -> str | None:
    """Return the most recent sweep_id that has deleted entries not yet undone."""
    row = conn.execute(
        """
        SELECT sweep_id
        FROM adb_sweep_state
        WHERE device_serial = ? AND status = 'deleted' AND undone_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (device_serial,),
    ).fetchone()
    return str(row["sweep_id"]) if row else None


def get_undo_candidates(
    conn: sqlite3.Connection, sweep_id: str, device_serial: str
) -> list[dict[str, Any]]:
    """Return deleted entries from sweep_id that have not yet been undone."""
    rows = conn.execute(
        """
        SELECT sweep_id, device_serial, remote_path, archive_path, chat_jid, size_bytes
        FROM adb_sweep_state
        WHERE sweep_id = ? AND device_serial = ? AND status = 'deleted' AND undone_at IS NULL
        ORDER BY rowid ASC
        """,
        (sweep_id, device_serial),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_sweep_entries_undone(
    conn: sqlite3.Connection,
    sweep_id: str,
    device_serial: str,
    remote_paths: list[str],
    undone_at: str,
) -> None:
    """Mark restored sweep entries with undone_at for strict idempotency."""
    if not remote_paths:
        return
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(
            """
            UPDATE adb_sweep_state
            SET undone_at = ?
            WHERE sweep_id = ? AND remote_path = ? AND device_serial = ?
            """,
            [(undone_at, sweep_id, rp, device_serial) for rp in remote_paths],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def create_restore_batch(
    conn: sqlite3.Connection,
    restore_id: str,
    device_serial: str,
    entries: list[dict[str, Any]],
    created_at: str,
) -> None:
    """Stage candidate restore entries atomically inside a transaction."""
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(
            """
            INSERT INTO adb_restore_state (
                restore_id, device_serial, remote_path, archive_path,
                size_bytes, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'staged', ?)
            ON CONFLICT(restore_id, remote_path, device_serial) DO NOTHING
            """,
            [
                (
                    restore_id,
                    device_serial,
                    item["remote_path"],
                    item["archive_path"],
                    item["size_bytes"],
                    created_at,
                )
                for item in entries
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_latest_incomplete_restore_id(conn: sqlite3.Connection, device_serial: str) -> str | None:
    """Return the most recent restore_id with 'staged' or 'pushed' entries."""
    row = conn.execute(
        """
        SELECT restore_id
        FROM adb_restore_state
        WHERE device_serial = ? AND status IN ('staged', 'pushed')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (device_serial,),
    ).fetchone()
    return str(row["restore_id"]) if row else None


def get_pending_restore_entries(
    conn: sqlite3.Connection, restore_id: str, device_serial: str
) -> list[dict[str, Any]]:
    """Return restore entries that have not yet reached 'verified'."""
    rows = conn.execute(
        """
        SELECT restore_id, device_serial, remote_path, archive_path, size_bytes, status
        FROM adb_restore_state
        WHERE restore_id = ? AND device_serial = ? AND status IN ('staged', 'pushed')
        ORDER BY rowid ASC
        """,
        (restore_id, device_serial),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_restore_entry_status(
    conn: sqlite3.Connection,
    restore_id: str,
    device_serial: str,
    remote_path: str,
    status: str,
    completed_at: str | None,
) -> None:
    """Update status for a single restore entry inside an explicit transaction."""
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            """
            UPDATE adb_restore_state
            SET status = ?, completed_at = ?
            WHERE restore_id = ? AND remote_path = ? AND device_serial = ?
            """,
            (status, completed_at, restore_id, remote_path, device_serial),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
