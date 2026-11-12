"""Tests for cross-platform group mention resolution, persistence, and chat linking."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.conftest import make_archive_db, make_ios_db
from wab_archiver.increment_merger import apply_messages_bin, extract_context_info
from wab_archiver.preservation import (
    hydrate_android_mentions_for_preservation,
    hydrate_ios_mentions_for_preservation,
    open_preservation_db,
    record_preserved_messages,
)
from wab_viewer.context import ViewerContext
from wab_viewer.db_cache import open_and_migrate_archive_db
from wab_viewer.hydration import _hydrate_mentions
from wab_viewer.protobuf import _extract_ios_mentions


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_bytes(field_num: int, payload: bytes) -> bytes:
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(payload)) + payload


def _pb_str(field_num: int, text: str) -> bytes:
    return _pb_bytes(field_num, text.encode("utf-8"))


def test_extract_ios_mentions_from_zmetadata_and_nested_quote() -> None:
    """_extract_ios_mentions extracts Tag 15 JIDs on non-reply and reply messages."""
    inner_ctx = _pb_str(15, "3333333333@lid")
    inner_ext = _pb_str(1, "Quoted @3333333333") + _pb_bytes(17, inner_ctx)
    quoted_wmi = _pb_bytes(6, inner_ext)
    zmetadata = (
        _pb_str(5, "STANZA_QUOTED")
        + _pb_str(6, "2222222222@lid")
        + _pb_str(15, "1111111111@lid")
        + _pb_str(15, "5550001001@s.whatsapp.net")
        + _pb_bytes(19, quoted_wmi)
    )
    jids = _extract_ios_mentions(zmetadata)
    assert jids == ["1111111111@lid", "5550001001@s.whatsapp.net", "3333333333@lid"]

    non_reply_meta = _pb_str(15, "1111111111@lid")
    assert _extract_ios_mentions(non_reply_meta) == ["1111111111@lid"]


def test_ios_mentions_hydration_and_chat_linking(tmp_path: Path) -> None:
    """iOS mentions resolve via ExtChat/ContactsV2/PushName/You/(Unknown) and link 1-to-1 chats."""
    wa_dir = tmp_path / "Whatsapp Databases"
    wa_dir.mkdir(parents=True, exist_ok=True)
    wa_path = wa_dir / "ChatStorage.sqlite"
    arch_path = tmp_path / ".wa_media_archiver.db"
    ext_path = wa_dir / "ExtChatDatabase.sqlite"

    wa_conn = make_ios_db(wa_path)
    chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
    if "ZSESSIONTYPE" not in chat_cols:
        wa_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZSESSIONTYPE INTEGER DEFAULT 0")
    if "ZHIDDEN" not in chat_cols:
        wa_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZHIDDEN INTEGER DEFAULT 0")
    wa_conn.executescript("""
        CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
            Z_PK INTEGER PRIMARY KEY,
            ZJID TEXT,
            ZPUSHNAME TEXT
        );
        INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZHIDDEN)
        VALUES (10, '5550001001@s.whatsapp.net', 'Alice Smith', 0, 0),
               (11, '120363000001@g.us', 'Test Group', 1, 0),
               (12, '5550001002@s.whatsapp.net', 'Hidden Chat', 0, 1);
        INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME)
        VALUES (1, '2222222222@lid', 'Bob Push');
    """)
    wa_conn.commit()
    wa_conn.close()

    ext_conn = sqlite3.connect(str(ext_path))
    ext_conn.executescript("""
        CREATE TABLE contact_metadata (
            contact_lid TEXT,
            contact_pn TEXT,
            contact_push_name TEXT
        );
        INSERT INTO contact_metadata (contact_lid, contact_pn, contact_push_name)
        VALUES ('1111111111@lid', '5550001001', 'Alice Push');
    """)
    ext_conn.commit()
    ext_conn.close()

    arch_conn = make_archive_db(arch_path)
    arch_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('5550001001', 'Alice', 'Alice Smith')"
    )
    arch_conn.commit()
    arch_conn.close()

    import threading
    from unittest.mock import MagicMock

    arch_ro = sqlite3.connect(str(arch_path))
    ctx = ViewerContext(
        tmp_path,
        "ios",
        str(wa_path),
        arch_path,
        None,
        sqlite3.connect(":memory:"),
        arch_ro,
        {},
        threading.Lock(),
        {},
        None,
        MagicMock(),
        MagicMock(),
    )
    conn = ctx.get_wa()
    assert conn is not None

    meta = (
        _pb_str(15, "1111111111@lid")
        + _pb_str(15, "2222222222@lid")
        + _pb_str(15, "9999999999@lid")
    )
    rows = [
        {
            "msg_id": 1,
            "text_body": "Hi @1111111111 @2222222222 @9999999999?",
            "_raw_metadata": meta,
        }
    ]
    _hydrate_mentions(rows, "ios", conn, None)
    mentions = rows[0]["mentions"]
    assert mentions["1111111111"] == {
        "name": "Alice Smith",
        "chat_id": "10",
        "chat_type": "contact",
        "is_self": False,
    }
    assert mentions["2222222222"] == {
        "name": "Bob Push",
        "chat_id": None,
        "chat_type": None,
        "is_self": False,
    }
    assert mentions["9999999999"] == {
        "name": "9999999999 (Unknown)",
        "chat_id": None,
        "chat_type": None,
        "is_self": False,
    }


def test_android_mentions_hydration_masked_lid_and_self(tmp_path: Path) -> None:
    """Android mentions resolve contacts, reject masked lid_display_name, handle lid_me, and link 1-to-1 chats."""
    wa_path = tmp_path / "msgstore.db"
    arch_path = tmp_path / ".wa_media_archiver.db"

    conn = sqlite3.connect(str(wa_path))
    conn.executescript("""
        CREATE TABLE jid (
            _id INTEGER PRIMARY KEY,
            user TEXT,
            server TEXT,
            raw_string TEXT
        );
        CREATE TABLE jid_map (
            lid_row_id INTEGER PRIMARY KEY,
            jid_row_id INTEGER NOT NULL
        );
        CREATE TABLE lid_display_name (
            lid_row_id INTEGER PRIMARY KEY,
            display_name TEXT
        );
        CREATE TABLE chat (
            _id INTEGER PRIMARY KEY,
            jid_row_id INTEGER NOT NULL,
            subject TEXT,
            hidden INTEGER DEFAULT 0
        );
        CREATE TABLE message_mentions (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            jid_row_id INTEGER NOT NULL
        );
        CREATE TABLE message_quoted_mentions (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            jid_row_id INTEGER NOT NULL
        );

        INSERT INTO jid (_id, user, server, raw_string) VALUES
            (1, '5550001001', 's.whatsapp.net', '5550001001@s.whatsapp.net'),
            (2, '1111111111', 'lid', '1111111111@lid'),
            (3, '2222222222', 'lid', '2222222222@lid'),
            (4, '3333333333', 'lid', 'lid_me'),
            (5, '4444444444', 'lid', '4444444444@lid');
        INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 1);
        INSERT INTO lid_display_name (lid_row_id, display_name) VALUES
            (3, 'Bob Masked ∙∙∙'),
            (5, 'Carol Unmasked');
        INSERT INTO chat (_id, jid_row_id, subject, hidden) VALUES (50, 1, NULL, 0);
        INSERT INTO message_mentions (message_row_id, jid_row_id) VALUES (100, 2), (100, 3), (100, 4);
        INSERT INTO message_quoted_mentions (message_row_id, jid_row_id) VALUES (100, 5);
    """)
    conn.execute(
        "CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)"
    )
    conn.execute("INSERT INTO _jid_map_resolved VALUES (2, 1)")
    conn.execute(
        "CREATE TEMP TABLE _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
    )
    conn.execute("INSERT INTO _contacts_resolved VALUES ('5550001001', 'Alice Smith', 'Alice')")
    conn.commit()

    make_archive_db(arch_path).close()
    rows = [{"msg_id": 100, "text_body": "Are you there @1111111111 @2222222222 @3333333333?"}]
    _hydrate_mentions(rows, "android", conn, None)
    conn.close()

    mentions = rows[0]["mentions"]
    assert mentions["1111111111"] == {
        "name": "Alice Smith",
        "chat_id": "5550001001",
        "chat_type": "contact",
        "is_self": False,
    }
    assert mentions["2222222222"] == {
        "name": "2222222222 (Unknown)",
        "chat_id": None,
        "chat_type": None,
        "is_self": False,
    }
    assert mentions["3333333333"] == {
        "name": "You",
        "chat_id": None,
        "chat_type": None,
        "is_self": True,
    }
    assert mentions["4444444444"] == {
        "name": "Carol Unmasked",
        "chat_id": None,
        "chat_type": None,
        "is_self": False,
    }


def test_db_cache_migration_to_v5_adds_mentions(tmp_path: Path) -> None:
    """open_and_migrate_archive_db upgrades v4 recent_messages to v5 with mentions column."""
    arch_path = tmp_path / ".wa_media_archiver.db"
    c = sqlite3.connect(str(arch_path))
    c.executescript("""
        CREATE TABLE recent_messages (
            chat_id TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            msg_id INTEGER NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            sender TEXT NOT NULL,
            from_me INTEGER NOT NULL,
            archive_path TEXT,
            media_type TEXT NOT NULL DEFAULT 'text',
            media_name TEXT,
            text_body TEXT NOT NULL,
            quoted_text TEXT,
            quoted_sender TEXT,
            quoted_ts INTEGER,
            PRIMARY KEY (chat_id, chat_type, msg_id)
        );
        INSERT INTO recent_messages VALUES ('1', 'group', 1, 1000, 'A', 0, NULL, 'text', NULL, 'hi', NULL, NULL, NULL);
        PRAGMA user_version = 4;
    """)
    c.commit()
    c.close()

    migrated, cleared = open_and_migrate_archive_db(arch_path, False, False, False)
    try:
        assert cleared is True
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 5
        cols = {r[1] for r in migrated.execute("PRAGMA table_info(recent_messages)").fetchall()}
        assert "mentions" in cols
        assert migrated.execute("SELECT COUNT(*) FROM recent_messages").fetchone()[0] == 0
    finally:
        migrated.close()


def test_increment_merger_persists_context_mentions(tmp_path: Path) -> None:
    """extract_context_info and apply_messages_bin persist Tag 15 mentions into message_mentions."""
    ctx_bytes = _pb_str(15, "1111111111@lid")
    ext_msg = _pb_str(1, "Hello @1111111111") + _pb_bytes(17, ctx_bytes)
    extracted = extract_context_info({6: [(2, ext_msg)]})
    assert extracted is None  # payload_fields expects inner ext_msg fields (tag 17)
    extracted = extract_context_info({17: [(2, ctx_bytes)]})
    assert extracted is not None
    assert extracted["mentioned_jids"] == ["1111111111@lid"]
    assert extracted["stanza_id"] == ""

    db_path = tmp_path / "msgstore.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jid (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL DEFAULT '',
            server TEXT NOT NULL DEFAULT '',
            agent INTEGER NOT NULL DEFAULT 0,
            device INTEGER NOT NULL DEFAULT 0,
            type INTEGER NOT NULL DEFAULT 0,
            raw_string TEXT UNIQUE NOT NULL
        );
        CREATE TABLE chat (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            jid_row_id INTEGER NOT NULL,
            subject TEXT,
            sort_timestamp INTEGER DEFAULT 0,
            display_message_row_id INTEGER
        );
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL DEFAULT 0,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER NOT NULL DEFAULT 0,
            status INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL DEFAULT 0,
            message_type INTEGER NOT NULL DEFAULT 0,
            text_data TEXT,
            starred INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE message_mentions (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            jid_row_id INTEGER NOT NULL
        );
    """)
    conn.commit()

    wmi_key = _pb_str(1, "120363000001@g.us") + _pb_str(3, "MSG_MENTION_1")
    wmi_msg = _pb_bytes(6, ext_msg)
    wmi_bytes = (
        _pb_bytes(1, wmi_key) + _pb_bytes(2, wmi_msg) + _pb_str(5, "5550001002@s.whatsapp.net")
    )
    bin_path = tmp_path / "messages.bin"
    bin_path.write_bytes(_encode_varint(len(wmi_bytes)) + wmi_bytes)

    import logging

    apply_messages_bin(
        conn,
        str(bin_path),
        logging.getLogger("test_mentions"),
        {},
        {},
        {},
    )
    rows = conn.execute(
        """
        SELECT m.key_id, j.user, j.server
        FROM message_mentions mm
        JOIN message m ON m._id = mm.message_row_id
        JOIN jid j ON j._id = mm.jid_row_id
        """
    ).fetchall()
    conn.close()
    assert rows == [("MSG_MENTION_1", "1111111111", "lid")]


def test_preservation_v3_migration_and_legacy_conservative_lookup(tmp_path: Path) -> None:
    """preserved_messages.db migrates v2->v3, persists mentions, and handles legacy rows conservatively."""
    pres_dir = tmp_path / "pres_out"
    pres_dir.mkdir(parents=True, exist_ok=True)
    pres_db_path = pres_dir / "preserved_messages.db"

    # Seed v2 preserved_messages.db without mentions column
    pconn = sqlite3.connect(str(pres_db_path))
    pconn.executescript("""
        CREATE TABLE preserved_messages (
            chat_id TEXT NOT NULL,
            chat_jid TEXT,
            chat_type TEXT NOT NULL,
            stanza_id TEXT NOT NULL,
            msg_id INTEGER NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            sender TEXT,
            sender_jid TEXT,
            sender_phone TEXT,
            from_me INTEGER NOT NULL DEFAULT 0,
            text_body TEXT,
            original_text_body TEXT,
            media_type TEXT NOT NULL DEFAULT 'text',
            media_name TEXT,
            archive_path TEXT,
            original_path TEXT,
            vcard_data TEXT,
            media_width INTEGER,
            media_height INTEGER,
            media_duration INTEGER,
            media_size INTEGER,
            is_view_once INTEGER NOT NULL DEFAULT 0,
            latitude REAL,
            longitude REAL,
            location_name TEXT,
            location_address TEXT,
            location_url TEXT,
            live_location_share_duration INTEGER,
            poll_data TEXT,
            quoted_text TEXT,
            quoted_sender TEXT,
            quoted_ts INTEGER,
            quoted_stanza_id TEXT,
            quoted_parent_type TEXT,
            reactions TEXT,
            is_edited INTEGER NOT NULL DEFAULT 0,
            edited_ts INTEGER,
            is_starred INTEGER NOT NULL DEFAULT 0,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            pin_expiry_ts INTEGER,
            action_type INTEGER,
            participant_name TEXT,
            service_metadata TEXT,
            preserved_at TEXT NOT NULL,
            preservation_reason TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            PRIMARY KEY (chat_id, stanza_id)
        );
        CREATE TABLE preserved_chats (
            chat_id TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            chat_jid TEXT,
            subject TEXT,
            description TEXT,
            created_ts INTEGER,
            creator_jid TEXT,
            newest_ts INTEGER,
            oldest_ts INTEGER,
            message_count INTEGER NOT NULL DEFAULT 0,
            members_json TEXT,
            PRIMARY KEY (chat_id, chat_type)
        );
        CREATE TABLE preservation_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            messages_added INTEGER NOT NULL DEFAULT 0,
            chats_affected INTEGER NOT NULL DEFAULT 0,
            calls_added INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        );
        PRAGMA user_version = 2;
    """)
    pconn.commit()
    pconn.close()

    migrated_p = open_preservation_db(pres_dir)
    assert migrated_p.execute("PRAGMA user_version").fetchone()[0] == 3
    p_cols = {r[1] for r in migrated_p.execute("PRAGMA table_info(preserved_messages)").fetchall()}
    assert "mentions" in p_cols

    # Verify record_preserved_messages stores mentions JSON
    import logging

    record_preserved_messages(
        migrated_p,
        [
            {
                "chat_id": "11",
                "chat_type": "group",
                "stanza_id": "PRES_1",
                "msg_id": 1,
                "timestamp_ms": 1000,
                "sender": "Bob",
                "from_me": 0,
                "text_body": "Hi @1111111111",
                "mentions": json.dumps(
                    {
                        "1111111111": {
                            "name": "Alice Smith",
                            "chat_id": None,
                            "chat_type": None,
                            "is_self": False,
                        }
                    }
                ),
            }
        ],
        "android",
        logging.getLogger("test_pres"),
    )
    stored = migrated_p.execute(
        "SELECT mentions FROM preserved_messages WHERE stanza_id = 'PRES_1'"
    ).fetchone()[0]
    assert json.loads(stored)["1111111111"]["name"] == "Alice Smith"
    migrated_p.close()

    # Verify legacy preserved row (mentions=None) uses conservative lookup on known backup IDs only
    wa_conn = sqlite3.connect(":memory:")
    wa_conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0);
        INSERT INTO jid VALUES (1, '5550001001', 's.whatsapp.net', '5550001001@s.whatsapp.net');
        INSERT INTO jid VALUES (2, '1111111111', 'lid', '1111111111@lid');
        CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
        INSERT INTO _jid_map_resolved VALUES (2, 1);
        CREATE TEMP TABLE _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
        INSERT INTO _contacts_resolved VALUES ('5550001001', 'Alice Smith', 'Alice');
    """)
    legacy_rows = [
        {
            "msg_id": -1,
            "is_preserved": 1,
            "text_body": "See @1111111111 in @2026 or @7654321?",
            "mentions": None,
        }
    ]
    _hydrate_mentions(legacy_rows, "android", wa_conn, None)
    wa_conn.close()
    assert list(legacy_rows[0]["mentions"].keys()) == ["1111111111"]
    assert legacy_rows[0]["mentions"]["1111111111"]["name"] == "Alice Smith"

    # Also verify Android & iOS preservation helpers populate row["mentions"]
    a_conn = sqlite3.connect(":memory:")
    a_conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
        CREATE TABLE message_mentions (message_row_id INTEGER, jid_row_id INTEGER);
        INSERT INTO jid VALUES (1, '5550001001', 's.whatsapp.net', '5550001001@s.whatsapp.net');
        INSERT INTO message_mentions VALUES (7, 1);
    """)
    a_rows = [{"msg_id": 7, "text_body": "Hey @5550001001"}]
    hydrate_android_mentions_for_preservation(a_conn, a_rows, "", {"5550001001": "Alice Smith"})
    a_conn.close()
    assert json.loads(a_rows[0]["mentions"])["5550001001"]["name"] == "Alice Smith"

    i_conn = sqlite3.connect(":memory:")
    i_conn.executescript("""
        CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMETADATA BLOB);
        CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMEDIAITEM INTEGER);
    """)
    i_conn.execute(
        "INSERT INTO ZWAMEDIAITEM VALUES (1, ?)",
        (_pb_str(15, "5550001001@s.whatsapp.net"),),
    )
    i_conn.execute("INSERT INTO ZWAMESSAGE VALUES (9, 1)")
    i_conn.execute("ATTACH DATABASE ':memory:' AS old_db")
    i_conn.executescript("""
        CREATE TABLE old_db.ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMETADATA BLOB);
        CREATE TABLE old_db.ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMEDIAITEM INTEGER);
    """)
    i_conn.execute(
        "INSERT INTO old_db.ZWAMEDIAITEM VALUES (1, ?)",
        (_pb_str(15, "5550001001@s.whatsapp.net"),),
    )
    i_conn.execute("INSERT INTO old_db.ZWAMESSAGE VALUES (9, 1)")
    i_rows = [{"msg_id": 9, "text_body": "Hey @5550001001"}]
    hydrate_ios_mentions_for_preservation(
        i_conn,
        i_rows,
        {"ZWAMEDIAITEM", "ZWAMESSAGE"},
        {"Z_PK", "ZMETADATA"},
        {"5550001001": "Alice Smith"},
    )
    i_conn.close()
    assert json.loads(i_rows[0]["mentions"])["5550001001"]["name"] == "Alice Smith"
