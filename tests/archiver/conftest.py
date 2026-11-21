"""Shared fixtures and test helpers for wab_archiver test suites."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3

import pytest
import wab_archiver.archive_db as arc
import wab_archiver.backup_reader as br


def _make_msgstore(extra_tables=None, missing_col=None):
    """Build an in-memory SQLite DB that looks like a minimal WhatsApp msgstore."""
    conn = sqlite3.connect(":memory:")
    schema = {
        "message": [
            "_id INTEGER PRIMARY KEY",
            "timestamp INTEGER",
            "sender_jid_row_id INTEGER",
            "from_me INTEGER",
        ],
        "message_media": [
            "file_path TEXT",
            "mime_type TEXT",
            "chat_row_id INTEGER",
            "message_row_id INTEGER",
            "message_url TEXT",
            "media_name TEXT",
        ],
        "chat": [
            "_id INTEGER PRIMARY KEY",
            "subject TEXT",
            "jid_row_id INTEGER",
        ],
        "jid": [
            "_id INTEGER PRIMARY KEY",
            "user TEXT",
        ],
        "jid_map": [
            "lid_row_id INTEGER",
            "jid_row_id INTEGER",
        ],
    }
    if extra_tables:
        for t in extra_tables:
            schema[t] = ["_id INTEGER PRIMARY KEY"]

    for table, cols in schema.items():
        if missing_col and missing_col[0] == table:
            cols = [c for c in cols if not c.startswith(missing_col[1])]
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")

    conn.commit()
    return conn


def _make_number_change_db(pairs):
    """Build an in-memory DB with jid and message_system_number_change tables."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
        CREATE TABLE message_system_number_change (
            old_jid_row_id INTEGER,
            new_jid_row_id INTEGER
        );
    """)
    jid_id = 1
    jid_map = {}
    for old, new in pairs:
        for num in (old, new):
            if num not in jid_map:
                conn.execute("INSERT INTO jid VALUES (?, ?)", (jid_id, num))
                jid_map[num] = jid_id
                jid_id += 1
    for old, new in pairs:
        conn.execute(
            "INSERT INTO message_system_number_change VALUES (?, ?)",
            (jid_map[old], jid_map[new]),
        )
    conn.commit()
    return conn


def _make_ios_msgstore(missing_table=None, missing_col=None):
    """Build an in-memory SQLite DB mimicking ChatStorage.sqlite."""
    conn = sqlite3.connect(":memory:")
    schema = {
        "ZWAMESSAGE": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZMESSAGEDATE REAL",
            "ZISFROMME INTEGER",
            "ZCHATSESSION INTEGER",
            "ZMEDIAITEM INTEGER",
            "ZGROUPMEMBER INTEGER",
            "ZPUSHNAME TEXT",
        ],
        "ZWACHATSESSION": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZCONTACTJID TEXT",
            "ZGROUPINFO INTEGER",
            "ZPARTNERNAME TEXT",
            "ZCONTACTABID INTEGER",
            "ZLASTMESSAGEDATE REAL",
        ],
        "ZWAMEDIAITEM": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZMEDIALOCALPATH TEXT",
            "ZMEDIAURL TEXT",
            "ZTITLE TEXT",
        ],
        "ZWAGROUPMEMBER": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZMEMBERJID TEXT",
        ],
    }
    for table, cols in schema.items():
        if table == missing_table:
            continue
        if missing_col and missing_col[0] == table:
            cols = [c for c in cols if not c.startswith(missing_col[1])]
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
    conn.commit()
    return conn


def _make_wa_file(root, rel_path, content: bytes):
    """Write content to <root>/<rel_path>, creating parent dirs."""
    full = root / rel_path.replace("/", os.sep)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    return str(full)


def _make_manifest_db(tmp_path, rows):
    """Create a synthetic Manifest.db with the given rows."""
    db_path = tmp_path / "Manifest.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE Files (
            fileID       TEXT PRIMARY KEY,
            domain       TEXT,
            relativePath TEXT
        )
    """)
    for file_id, relative_path in rows:
        conn.execute("INSERT INTO Files VALUES (?, ?, ?)", (file_id, br._WA_DOMAIN, relative_path))
    conn.commit()
    conn.close()
    return tmp_path


def _make_manifest_db_multi_domain(tmp_path, rows):
    """Create a Manifest.db with rows as (fileID, domain, relativePath) tuples."""
    db_path = tmp_path / "Manifest.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE Files (
            fileID       TEXT PRIMARY KEY,
            domain       TEXT,
            relativePath TEXT
        )
    """)
    for file_id, domain, relative_path in rows:
        conn.execute("INSERT INTO Files VALUES (?, ?, ?)", (file_id, domain, relative_path))
    conn.commit()
    conn.close()
    return tmp_path


def _make_contacts_db(tmp_path, rows):
    """Create a synthetic ContactsV2.sqlite."""
    db_path = tmp_path / "ContactsV2.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE ZWAADDRESSBOOKCONTACT (
            ZWHATSAPPID TEXT,
            ZFULLNAME   TEXT
        )
    """)
    for jid, name in rows:
        conn.execute("INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (?, ?)", (jid, name))
    conn.commit()
    conn.close()
    return str(db_path)


def _make_ios_sessions_db(pairs):
    """Build an in-memory ZWACHATSESSION DB with ZCONTACTABID grouping."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ZWACHATSESSION (
            Z_PK              INTEGER PRIMARY KEY,
            ZCONTACTJID       TEXT,
            ZGROUPINFO        INTEGER,
            ZCONTACTABID      INTEGER,
            ZLASTMESSAGEDATE  REAL
        )
    """)
    pk = 1
    ts = 1000.0
    for old_jid, new_jid, abid in pairs:
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTABID, ZLASTMESSAGEDATE) VALUES (?, ?, ?, ?)",
            (pk, old_jid, abid, ts),
        )
        pk += 1
        ts += 100.0
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTABID, ZLASTMESSAGEDATE) VALUES (?, ?, ?, ?)",
            (pk, new_jid, abid, ts),
        )
        pk += 1
        ts += 100.0
    conn.commit()
    return conn


def _restore_args(tmp_path, dry_run=False):
    return argparse.Namespace(output=str(tmp_path), dry_run=dry_run)


try:
    import wa_crypt_tools  # noqa: F401

    _HAS_WA_CRYPT_TOOLS = True
except ImportError:
    _HAS_WA_CRYPT_TOOLS = False

_requires_wa_crypt_tools = pytest.mark.skipif(
    not _HAS_WA_CRYPT_TOOLS,
    reason="wa-crypt-tools is not installed",
)


def _seed_archive(tmp_path, original_path, archive_rel, content=b"test data"):
    """Insert DB row and write the archive copy file."""
    conn = arc.open_archive_db(str(tmp_path))
    md5 = hashlib.md5(content).digest()
    cur = conn.cursor()
    arc.record_file_archived(cur, original_path, md5, archive_rel, len(content), 0, None)
    conn.commit()
    conn.close()
    archive_file = tmp_path.joinpath(*archive_rel.split("/"))
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_bytes(content)
    return archive_file


def _create_synthetic_crypt15(plain_bytes: bytes, key_hex: str) -> bytes:
    import zlib

    from wa_crypt_tools.lib.db.db15 import Database15
    from wa_crypt_tools.lib.key.keyfactory import KeyFactory
    from wa_crypt_tools.lib.props import Props

    key = KeyFactory.new(key_hex)
    db15 = Database15(key=key)
    compressed = zlib.compress(plain_bytes, 1)
    return db15.encrypt(key, Props(), compressed)
