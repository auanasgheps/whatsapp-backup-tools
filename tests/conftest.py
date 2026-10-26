"""Root pytest configuration and shared test database fixtures.

Provides standardized, ground-truth-compliant SQLite schemas for Android
(msgstore.db) and iOS (ChatStorage.sqlite), strictly enforcing NOT NULL
constraints and schema fidelity as required by domain_testing.md and overrides.md.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def logger() -> logging.Logger:
    """Standardized test logger reporting at CRITICAL to suppress test noise."""
    log = logging.getLogger("wab_test_logger")
    log.setLevel(logging.CRITICAL)
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


def make_archive_db(path: Path | str) -> sqlite3.Connection:
    """Create a .wa_media_archiver.db with PRAGMA user_version = 6."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number       TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_row_id  TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            subject      TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS files (
            original_path TEXT PRIMARY KEY,
            md5           BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path TEXT NOT NULL REFERENCES files(original_path),
            archive_path  TEXT NOT NULL,
            PRIMARY KEY (original_path, archive_path)
        );
        CREATE TABLE IF NOT EXISTS recent_messages (
            chat_id          TEXT NOT NULL,
            chat_type        TEXT NOT NULL,
            msg_id           INTEGER NOT NULL,
            timestamp_ms     INTEGER NOT NULL,
            sender           TEXT NOT NULL,
            from_me          INTEGER NOT NULL,
            archive_path     TEXT,
            media_type       TEXT NOT NULL DEFAULT 'text',
            media_name       TEXT,
            text_body        TEXT NOT NULL,
            quoted_text      TEXT,
            quoted_sender    TEXT,
            quoted_ts        INTEGER,
            quoted_msg_id    INTEGER,
            reactions        TEXT,
            is_edited        INTEGER NOT NULL DEFAULT 0,
            edited_ts        INTEGER,
            is_view_once     INTEGER NOT NULL DEFAULT 0,
            quoted_has_thumb INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, chat_type, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recent_chat_ts
            ON recent_messages(chat_id, chat_type, timestamp_ms DESC);
        CREATE TABLE IF NOT EXISTS reactions_cache (
            chat_id    TEXT NOT NULL,
            msg_id     INTEGER NOT NULL,
            reactions  TEXT NOT NULL,
            PRIMARY KEY (chat_id, msg_id)
        );
    """)
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def make_android_db(path: Path | str) -> sqlite3.Connection:
    """Minimal msgstore.db with the baseline tables the viewer joins against."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jid (
            _id         INTEGER PRIMARY KEY,
            user        TEXT,
            raw_string  TEXT
        );
        CREATE TABLE IF NOT EXISTS chat (
            _id                     INTEGER PRIMARY KEY,
            jid_row_id              INTEGER,
            subject                 TEXT,
            hidden                  INTEGER DEFAULT 0,
            sort_timestamp          INTEGER,
            display_message_row_id  INTEGER
        );
        CREATE TABLE IF NOT EXISTS message (
            _id                 INTEGER PRIMARY KEY,
            chat_row_id         INTEGER NOT NULL,
            from_me             INTEGER NOT NULL DEFAULT 0,
            sender_jid_row_id   INTEGER,
            timestamp           INTEGER,
            text_data           TEXT,
            message_type        INTEGER DEFAULT 0,
            key_id              TEXT
        );
        CREATE TABLE IF NOT EXISTS message_media (
            _id             INTEGER PRIMARY KEY,
            message_row_id  INTEGER,
            chat_row_id     INTEGER,
            file_path       TEXT,
            media_name      TEXT,
            mime_type       TEXT,
            message_url     TEXT
        );
        CREATE TABLE IF NOT EXISTS message_quoted (
            message_row_id      INTEGER PRIMARY KEY,
            text_data           TEXT,
            from_me             INTEGER DEFAULT 0,
            sender_jid_row_id   INTEGER,
            timestamp           INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS jid_map (
            lid_row_id  INTEGER,
            jid_row_id  INTEGER
        );
    """)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def add_android_reaction_tables(conn: sqlite3.Connection) -> None:
    """Attach Android message reaction auxiliary tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_add_on (
            _id                   INTEGER PRIMARY KEY,
            parent_message_row_id INTEGER,
            chat_row_id           INTEGER,
            from_me               INTEGER DEFAULT 0,
            sender_jid_row_id     INTEGER,
            key_id                TEXT,
            timestamp             INTEGER,
            message_add_on_type   INTEGER DEFAULT 1,
            status                INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS message_add_on_reaction (
            _id                   INTEGER PRIMARY KEY,
            message_add_on_row_id INTEGER,
            reaction              TEXT,
            sender_timestamp      INTEGER
        );
    """)
    conn.commit()


def make_ios_db(path: Path | str) -> sqlite3.Connection:
    """Create a ground-truth compliant iOS ChatStorage.sqlite schema."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
            Z_PK             INTEGER PRIMARY KEY,
            ZGROUPINFO       INTEGER,
            ZCONTACTJID      TEXT,
            ZPARTNERNAME     TEXT,
            ZHIDDEN          INTEGER DEFAULT 0,
            ZCONTACTABID     INTEGER,
            ZLASTMESSAGEDATE REAL
        );
        CREATE TABLE IF NOT EXISTS ZWAMEDIAITEM (
            Z_PK            INTEGER PRIMARY KEY,
            ZMEDIALOCALPATH TEXT,
            ZMEDIAURL       TEXT,
            ZTITLE          TEXT,
            ZXMPPTHUMBPATH  TEXT,
            ZVCARDNAME      TEXT,
            ZVCARDSTRING    TEXT,
            ZMETADATA       BLOB
        );
        CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
            Z_PK         INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZMEMBERJID   TEXT,
            ZCONTACTNAME TEXT,
            ZISACTIVE    INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
            Z_PK      INTEGER PRIMARY KEY,
            ZJID      TEXT,
            ZPUSHNAME TEXT
        );
        CREATE TABLE IF NOT EXISTS ZWAGROUPINFO (
            Z_PK          INTEGER PRIMARY KEY,
            ZCREATIONDATE REAL,
            ZCREATORJID   TEXT
        );
        CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
            Z_PK            INTEGER PRIMARY KEY,
            ZCHATSESSION    INTEGER NOT NULL,
            ZISFROMME       INTEGER NOT NULL DEFAULT 0,
            ZMESSAGEDATE    REAL,
            ZSTANZAID       TEXT,
            ZMESSAGETYPE    INTEGER DEFAULT 0,
            ZGROUPEVENTTYPE INTEGER DEFAULT 0,
            ZMEDIAITEM      INTEGER,
            ZGROUPMEMBER    INTEGER,
            ZPARENTMESSAGE  INTEGER,
            ZPUSHNAME       TEXT,
            ZTEXT           TEXT,
            ZFROMJID        TEXT,
            ZTOJID          TEXT,
            ZRECEIPTINFO    BLOB,
            ZMESSAGESTATUS  INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def seed_android_db(wa_conn: sqlite3.Connection, archive_conn: sqlite3.Connection) -> None:
    """Insert one contact chat with one text message."""
    wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '123456789')")
    wa_conn.execute(
        "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
        "VALUES (10, 1, NULL, 0, 1700000000000, 1)"
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (1, 10, 0, 1, 1700000000000, 'Hello world', 0)"
    )
    wa_conn.commit()

    archive_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('123456789', 'Alice (00123456789)', 'Alice')"
    )
    archive_conn.commit()
