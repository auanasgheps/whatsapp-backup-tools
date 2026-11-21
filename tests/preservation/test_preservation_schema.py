"""Tests for wab_archiver.preservation."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_TEST_LOGGER = logging.getLogger("test_preservation")
_TEST_LOGGER.setLevel(logging.INFO)

from wab_archiver import preservation


def test_open_preservation_db(tmp_path: Path) -> None:
    conn = preservation.open_preservation_db(str(tmp_path))
    assert conn is not None

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "preserved_messages" in tables
    assert "preserved_chats" in tables
    assert "preserved_calls" in tables
    assert "preservation_runs" in tables

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 3

    # Verify all v2 & v3 columns are present
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(preserved_messages)").fetchall()}
    for col_name, _ in preservation.NEW_V2_MESSAGE_COLUMNS:
        assert col_name in msg_cols, f"Missing v2 column {col_name} in preserved_messages"
    for col_name, _ in preservation.NEW_V3_MESSAGE_COLUMNS:
        assert col_name in msg_cols, f"Missing v3 column {col_name} in preserved_messages"

    chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(preserved_chats)").fetchall()}
    for col_name, _ in preservation.NEW_V2_CHAT_COLUMNS:
        assert col_name in chat_cols, f"Missing v2 column {col_name} in preserved_chats"

    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(preservation_runs)").fetchall()}
    assert "calls_preserved" in run_cols

    conn.close()


def test_preservation_db_v1_to_v2_migration(tmp_path: Path) -> None:
    """Verify automated backward-compatible migration from PRAGMA user_version = 1 to 2."""
    db_path = preservation.get_preserved_db_path(str(tmp_path))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # 1. Pre-seed a strictly v1 database
    v1_conn = sqlite3.connect(db_path)
    v1_conn.executescript("""
        CREATE TABLE preserved_messages (
            chat_id              TEXT NOT NULL,
            chat_type            TEXT NOT NULL,
            stanza_id            TEXT NOT NULL,
            msg_id               INTEGER NOT NULL,
            timestamp_ms         INTEGER NOT NULL,
            sender               TEXT NOT NULL,
            from_me              INTEGER NOT NULL,
            text_body            TEXT NOT NULL DEFAULT '',
            media_type           TEXT NOT NULL DEFAULT 'text',
            media_name           TEXT,
            archive_path         TEXT,
            quoted_text          TEXT,
            quoted_sender        TEXT,
            quoted_ts            INTEGER,
            reactions            TEXT,
            is_edited            INTEGER NOT NULL DEFAULT 0,
            edited_ts            INTEGER,
            action_type          INTEGER,
            participant_name     TEXT,
            preserved_at         TEXT NOT NULL,
            preservation_reason  TEXT NOT NULL,
            source_platform      TEXT NOT NULL,
            PRIMARY KEY (chat_id, stanza_id)
        );

        CREATE TABLE preserved_chats (
            chat_id              TEXT NOT NULL,
            chat_type            TEXT NOT NULL,
            newest_ts            INTEGER NOT NULL,
            msg_count            INTEGER NOT NULL,
            PRIMARY KEY (chat_id, chat_type)
        );

        CREATE TABLE preservation_runs (
            run_id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at               TEXT NOT NULL,
            source_platform      TEXT NOT NULL,
            messages_preserved   INTEGER NOT NULL,
            chats_affected       INTEGER NOT NULL,
            notes                TEXT
        );

        PRAGMA user_version = 1;
    """)

    # Insert sample v1 data
    v1_conn.execute("""
        INSERT INTO preserved_messages (
            chat_id, chat_type, stanza_id, msg_id, timestamp_ms, sender, from_me,
            text_body, preserved_at, preservation_reason, source_platform
        ) VALUES
            ('chat_100', 'contact', 's1', 1, 1000, 'Alice', 0, 'First msg', '2026-01-01T00:00:00Z', 'deleted_on_phone', 'android'),
            ('chat_100', 'contact', 's2', 2, 5000, 'Alice', 0, 'Second msg', '2026-01-01T00:00:00Z', 'deleted_on_phone', 'android');
    """)
    v1_conn.execute("""
        INSERT INTO preserved_chats (chat_id, chat_type, newest_ts, msg_count)
        VALUES ('chat_100', 'contact', 5000, 2);
    """)
    v1_conn.commit()
    v1_conn.close()

    # 2. Open with open_preservation_db to trigger automated migration
    migrated_conn = preservation.open_preservation_db(str(tmp_path))
    assert migrated_conn.execute("PRAGMA user_version").fetchone()[0] == 3

    # Check that existing data is intact
    msgs = migrated_conn.execute(
        "SELECT * FROM preserved_messages ORDER BY timestamp_ms ASC"
    ).fetchall()
    assert len(msgs) == 2
    assert msgs[0]["text_body"] == "First msg"
    assert msgs[0]["poll_data"] is None  # New column defaults safely to NULL
    assert msgs[0]["is_view_once"] == 0  # New column defaults safely to 0

    # Check that oldest_ts was backfilled
    c_row = migrated_conn.execute(
        "SELECT * FROM preserved_chats WHERE chat_id = 'chat_100'"
    ).fetchone()
    assert c_row["newest_ts"] == 5000
    assert c_row["oldest_ts"] == 1000
    assert c_row["msg_count"] == 2

    # Check preserved_calls table exists and is writable
    call_count = preservation.record_preserved_calls(
        migrated_conn,
        [
            {
                "call_id": "call_abc_123",
                "chat_id": "chat_100",
                "chat_jid": "alice@s.whatsapp.net",
                "caller_jid": "alice@s.whatsapp.net",
                "caller_name": "Alice",
                "from_me": 0,
                "timestamp_ms": 6000,
                "duration_seconds": 120,
                "is_video": 1,
                "call_result": 1,
                "is_group_call": 0,
                "participants": None,
                "bytes_transferred": 500000,
            }
        ],
        "android",
        _TEST_LOGGER,
    )
    assert call_count == 1

    calls = migrated_conn.execute("SELECT * FROM preserved_calls").fetchall()
    assert len(calls) == 1
    assert calls[0]["call_id"] == "call_abc_123"
    assert calls[0]["duration_seconds"] == 120
    assert calls[0]["is_video"] == 1

    migrated_conn.close()


def test_preserved_db_pragmas_and_health(tmp_path: Path) -> None:
    """Verify that open_preservation_db sets memory and durability PRAGMAs and passes health check."""
    conn = preservation.open_preservation_db(str(tmp_path))
    try:
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy == 5000

        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        # In SQLite, 1 corresponds to NORMAL
        assert sync == 1

        cache = conn.execute("PRAGMA cache_size").fetchone()[0]
        assert cache == -32000

        temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
        # In SQLite, 2 corresponds to MEMORY
        assert temp_store == 2

        mmap = conn.execute("PRAGMA mmap_size").fetchone()[0]
        assert mmap >= 268435456

        # Health check must succeed without exception
        preservation.check_preserved_db_health(conn, _TEST_LOGGER)

        # Index order alignment: idx_pres_chat_type_ts must start with (chat_id, chat_type)
        idx_info = conn.execute("PRAGMA index_info(idx_pres_chat_type_ts)").fetchall()
        cols = [r[2] for r in idx_info]
        assert cols[:2] == ["chat_id", "chat_type"]
    finally:
        conn.close()


def test_check_preserved_db_health_failures(tmp_path: Path) -> None:
    """Verify check_preserved_db_health raises DatabaseError on quick_check failure and IntegrityError on FK failure."""
    db_file = tmp_path / "test_fk.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES parent(id))"
        )
        # Insert child referencing non-existent parent with foreign_keys temporarily off to simulate inconsistency
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO child VALUES (1, 999)")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        try:
            preservation.check_preserved_db_health(conn, _TEST_LOGGER)
            raise AssertionError("Expected IntegrityError for foreign key violation")
        except sqlite3.IntegrityError:
            pass


def test_preserved_index_order_migration(tmp_path: Path) -> None:
    """Verify existing preservation DB with old index column order is migrated and re-created."""
    db_path = preservation.get_preserved_db_path(str(tmp_path))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(preservation.PRESERVED_SCHEMA_TABLES)
        # Create legacy index order (chat_type, chat_id, timestamp_ms DESC)
        conn.execute(
            "CREATE INDEX idx_pres_chat_type_ts ON preserved_messages(chat_type, chat_id, timestamp_ms DESC)"
        )
        conn.commit()

        idx_info = conn.execute("PRAGMA index_info(idx_pres_chat_type_ts)").fetchall()
        assert [r[2] for r in idx_info[:2]] == ["chat_type", "chat_id"]

    # Re-open via open_preservation_db which must detect and re-align the index
    reopened = preservation.open_preservation_db(str(tmp_path))
    try:
        new_info = reopened.execute("PRAGMA index_info(idx_pres_chat_type_ts)").fetchall()
        assert [r[2] for r in new_info[:2]] == ["chat_id", "chat_type"]
    finally:
        reopened.close()
