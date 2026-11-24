"""Tests for wab_archiver.preservation."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_TEST_LOGGER = logging.getLogger("test_preservation")
_TEST_LOGGER.setLevel(logging.INFO)

from tests.preservation.conftest import _make_dummy_archive_db
from wab_archiver import preservation


def test_verify_account_match_android(tmp_path: Path) -> None:
    old_db = tmp_path / "old.db"
    new_db = tmp_path / "new.db"

    # Case 1: Fewer than 5 chats -> returns True
    with sqlite3.connect(old_db) as conn:
        conn.execute("CREATE TABLE jid (user TEXT)")
        conn.executemany("INSERT INTO jid VALUES (?)", [("111",), ("222",)])
    with sqlite3.connect(new_db) as conn:
        conn.execute("CREATE TABLE jid (user TEXT)")
        conn.executemany("INSERT INTO jid VALUES (?)", [("333",), ("444",)])

    assert (
        preservation.verify_account_match(str(old_db), str(new_db), "android", _TEST_LOGGER) is True
    )

    # Case 2: >= 5 chats with overlap -> returns True
    with sqlite3.connect(old_db) as conn:
        conn.executemany("INSERT INTO jid VALUES (?)", [("555",), ("666",), ("777",)])
    with sqlite3.connect(new_db) as conn:
        conn.executemany("INSERT INTO jid VALUES (?)", [("555",), ("888",), ("999",)])

    assert (
        preservation.verify_account_match(str(old_db), str(new_db), "android", _TEST_LOGGER) is True
    )

    # Case 3: >= 5 chats with 0 overlap -> returns False
    unrelated_db = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated_db) as conn:
        conn.execute("CREATE TABLE jid (user TEXT)")
        conn.executemany(
            "INSERT INTO jid VALUES (?)", [("a1",), ("a2",), ("a3",), ("a4",), ("a5",)]
        )

    assert (
        preservation.verify_account_match(str(old_db), str(unrelated_db), "android", _TEST_LOGGER)
        is False
    )


def test_record_preserved_messages_progressive_edit_disclosure(tmp_path: Path) -> None:
    output_dir = tmp_path / "output_edits"
    os.makedirs(output_dir, exist_ok=True)

    with preservation.open_preservation_db(str(output_dir)) as pres_conn:
        # Run 1: Message is originally preserved with pre-edit text
        run1_rows = [
            {
                "chat_id": "chat_edit_1",
                "chat_type": "contact",
                "stanza_id": "key_edit_test",
                "msg_id": 1,
                "timestamp_ms": 1700000000,
                "sender": "Alice",
                "from_me": 0,
                "text_body": "Original pre-edit message text",
                "is_edited": 0,
            }
        ]
        preservation.record_preserved_messages(pres_conn, run1_rows, "android", _TEST_LOGGER)

        row1 = pres_conn.execute(
            "SELECT text_body, original_text_body, is_edited FROM preserved_messages WHERE stanza_id = 'key_edit_test'"
        ).fetchone()
        assert row1[0] == "Original pre-edit message text"
        assert row1[1] is None
        assert row1[2] == 0

        # Run 2: Message is edited and deleted/revoked, updating the record
        run2_rows = [
            {
                "chat_id": "chat_edit_1",
                "chat_type": "contact",
                "stanza_id": "key_edit_test",
                "msg_id": 1,
                "timestamp_ms": 1700000000,
                "sender": "Alice",
                "from_me": 0,
                "text_body": "Updated edited message text",
                "is_edited": 1,
                "edited_ts": 1700005000,
            }
        ]
        preservation.record_preserved_messages(pres_conn, run2_rows, "android", _TEST_LOGGER)

        row2 = pres_conn.execute(
            "SELECT text_body, original_text_body, is_edited, edited_ts FROM preserved_messages WHERE stanza_id = 'key_edit_test'"
        ).fetchone()
        assert row2[0] == "Updated edited message text"
        assert row2[1] == "Original pre-edit message text"
        assert row2[2] == 1
        assert row2[3] == 1700005000


def test_preserve_deleted_messages_orchestrator_fresh_output_dir(tmp_path: Path) -> None:
    output_root = tmp_path / "fresh_run_out"
    output_root.mkdir()
    old_db = tmp_path / "old_orch.db"
    new_db = tmp_path / "new_orch.db"

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_a'), (2, 'user_b');
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Chat A');
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT,
                from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER,
                message_type INTEGER, text_data TEXT
            );
            INSERT INTO message VALUES (1, 1, 'msg_to_delete', 0, 2, 1000, 0, 'Will be deleted');
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_a'), (2, 'user_b');
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Chat A');
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT,
                from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER,
                message_type INTEGER, text_data TEXT
            );
        """)

    archive_db_path = output_root / ".wa_media_archiver.db"
    assert not archive_db_path.exists()

    preserved_result = preservation.preserve_deleted_messages(
        old_db_path=str(old_db),
        new_db_path=str(new_db),
        output_root=str(output_root),
        archive_db_path=str(archive_db_path),
        platform="android",
        logger=_TEST_LOGGER,
    )

    assert preserved_result == (1, 1)
    assert preserved_result.messages == 1
    assert preserved_result.chats == 1
    assert archive_db_path.exists()
    pres_db_path = output_root / "Whatsapp Databases" / "preserved_messages.db"
    assert pres_db_path.exists()

    with sqlite3.connect(pres_db_path) as pconn:
        row = pconn.execute(
            "SELECT stanza_id, text_body, preservation_reason FROM preserved_messages"
        ).fetchone()
        assert row == ("msg_to_delete", "Will be deleted", "deleted_on_phone")


def test_record_preserved_messages_idempotent(tmp_path: Path) -> None:
    pres_conn = preservation.open_preservation_db(str(tmp_path))

    rows = [
        {
            "chat_id": "chat_1",
            "chat_type": "contact",
            "stanza_id": "st_1",
            "msg_id": 1,
            "timestamp_ms": 1000000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Hello",
            "media_type": "text",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": None,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
        {
            "chat_id": "chat_1",
            "chat_type": "contact",
            "stanza_id": "st_2",
            "msg_id": 2,
            "timestamp_ms": 1005000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "World",
            "media_type": "text",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": None,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
    ]

    # First run
    inserted_1 = preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    assert inserted_1 == (2, 1)
    assert inserted_1.messages == 2
    assert inserted_1.chats == 1

    chat_stats = pres_conn.execute(
        "SELECT newest_ts, msg_count FROM preserved_chats WHERE chat_id = 'chat_1'"
    ).fetchone()
    assert chat_stats["newest_ts"] == 1005000
    assert chat_stats["msg_count"] == 2

    # Second run with same data (idempotency check)
    inserted_2 = preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    assert inserted_2 == (0, 0)
    assert inserted_2.messages == 0
    assert inserted_2.chats == 0

    total_msgs = pres_conn.execute("SELECT COUNT(*) FROM preserved_messages").fetchone()[0]
    assert total_msgs == 2

    runs_count = pres_conn.execute("SELECT COUNT(*) FROM preservation_runs").fetchone()[0]
    assert runs_count == 2
    pres_conn.close()


def test_viewer_preserved_messages_integration(tmp_path: Path) -> None:
    from wab_viewer.main import create_app

    output_root = tmp_path / "archive"
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir(parents=True)

    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    # 1. Setup live database with one contact chat
    live_db = db_dir / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'contact_1');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER DEFAULT 4000, display_message_row_id INTEGER DEFAULT 3);
            INSERT INTO chat VALUES (1, 1, NULL, 0, 4000, 3);

            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                key_id TEXT,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                file_path TEXT,
                media_name TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                text_data TEXT,
                from_me INTEGER DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER DEFAULT 0
            );
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            INSERT INTO message VALUES
                (1, 1, 'k1', 0, 1, 1000, 0, 'Live 1'),
                (2, 1, 'k2', 0, 1, 3000, 0, 'Live 2'),
                (3, 1, 'k3_rev', 0, 1, 4000, 15, '');
        """)

    # 2. Setup preserved database
    pres_conn = preservation.open_preservation_db(str(output_root))
    rows = [
        # Preserved message from live chat (deleted on phone)
        {
            "chat_id": "contact_1",
            "chat_type": "contact",
            "stanza_id": "k_deleted_on_phone",
            "msg_id": 10,
            "timestamp_ms": 2000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Deleted message restored",
            "media_type": "text",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": None,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
        # Preserved revoked message (pre-revocation content)
        {
            "chat_id": "contact_1",
            "chat_type": "contact",
            "stanza_id": "k3_rev",
            "msg_id": 3,
            "timestamp_ms": 4000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Original pre-revocation text",
            "media_type": "text",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": None,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "revoked_by_sender",
            "source_platform": "android",
        },
        # Preserved message from entirely deleted chat
        {
            "chat_id": "deleted_contact",
            "chat_type": "contact",
            "stanza_id": "k_del_chat_msg",
            "msg_id": 20,
            "timestamp_ms": 5000,
            "sender": "Bob",
            "from_me": 0,
            "text_body": "Msg from deleted chat",
            "media_type": "text",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": None,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "chat_deleted",
            "source_platform": "android",
        },
    ]
    preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    pres_conn.close()

    app = create_app(output_root)
    client = app.test_client()

    # Verify /api/chats returns both active and preserved-only chats
    chats_res = client.get("/api/chats")
    assert chats_res.status_code == 200
    chats = chats_res.get_json()
    chat_map = {c["id"]: c for c in chats}
    assert "contact_1" in chat_map
    assert chat_map["contact_1"]["is_preserved_only"] is False

    assert "deleted_contact" in chat_map
    assert chat_map["deleted_contact"]["is_preserved_only"] is True
    assert chat_map["deleted_contact"]["display_name"] == "+deleted_contact"
    assert chat_map["deleted_contact"]["last_msg_preview"] == "Msg from deleted chat"

    # Verify /api/messages merges and replaces revoked message
    msgs_res = client.get("/api/messages?chat_id=contact_1&chat_type=contact&limit=50")
    assert msgs_res.status_code == 200
    msgs = msgs_res.get_json()
    assert len(msgs) == 4
    ts_list = [m["timestamp_ms"] for m in msgs]
    assert ts_list == [4000, 3000, 2000, 1000]

    # Check 4000 message (revoked message replaced)
    m4000 = msgs[0]
    assert m4000["text_body"] == "Original pre-revocation text"
    assert m4000["is_preserved"] == 1
    assert m4000["preservation_reason"] == "revoked_by_sender"

    # Check 2000 message (deleted message restored)
    m2000 = msgs[2]
    assert m2000["text_body"] == "Deleted message restored"
    assert m2000["is_preserved"] == 1
    assert m2000["preservation_reason"] == "deleted_on_phone"

    # Check 1000 message (live message)
    m1000 = msgs[3]
    assert m1000["text_body"] == "Live 1"
    assert m1000.get("is_preserved", 0) == 0
    assert "is_preserved" not in m1000

    # Verify /api/messages for preserved-only chat
    del_chat_res = client.get("/api/messages?chat_id=deleted_contact&chat_type=contact&limit=50")
    assert del_chat_res.status_code == 200
    del_msgs = del_chat_res.get_json()
    assert len(del_msgs) == 1
    assert del_msgs[0]["text_body"] == "Msg from deleted chat"
    assert del_msgs[0]["is_preserved"] == 1

    # Verify /api/messages/at
    at_res = client.get("/api/messages/at?chat_id=contact_1&chat_type=contact&ts=2500&limit=50")
    assert at_res.status_code == 200
    at_msgs = at_res.get_json()
    assert len(at_msgs) == 4

    # Verify /api/chat_info for preserved-only chat
    info_res = client.get("/api/chat_info?chat_id=deleted_contact&chat_type=contact")
    assert info_res.status_code == 200
    info_data = info_res.get_json()
    assert info_data["total"] == 1
    assert info_data["display_name"] == "+deleted_contact"
    assert info_data["first_ts"] == 5000
    assert info_data["last_ts"] == 5000


def test_preserved_chat_info_excludes_service_messages(tmp_path: Path) -> None:
    """GET /api/chat-info on preserved messages excludes media_type='service' from first_ts/last_ts."""
    from wab_viewer.main import create_app

    output_root = tmp_path / "output"
    output_root.mkdir()
    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    # Minimal source db
    live_db = output_root / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER DEFAULT 0, display_message_row_id INTEGER DEFAULT 0);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY);
        """)

    pres_conn = preservation.open_preservation_db(str(output_root))

    rows = [
        # Leading service message
        {
            "chat_id": "pres_grp",
            "chat_type": "group",
            "stanza_id": "p_msg_1",
            "msg_id": 1,
            "timestamp_ms": 1000,
            "sender": "System",
            "from_me": 0,
            "text_body": "Group created",
            "media_type": "service",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": 6,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "chat_deleted",
            "source_platform": "android",
        },
        # Conversational text message
        {
            "chat_id": "pres_grp",
            "chat_type": "group",
            "stanza_id": "p_msg_2",
            "msg_id": 2,
            "timestamp_ms": 2500,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Real conversation message",
            "media_type": "text",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": None,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "chat_deleted",
            "source_platform": "android",
        },
        # Trailing service message
        {
            "chat_id": "pres_grp",
            "chat_type": "group",
            "stanza_id": "p_msg_3",
            "msg_id": 3,
            "timestamp_ms": 4000,
            "sender": "System",
            "from_me": 0,
            "text_body": "Bob left",
            "media_type": "service",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "quoted_sender": "",
            "quoted_ts": 0,
            "reactions": None,
            "is_edited": 0,
            "edited_ts": None,
            "action_type": 12,
            "participant_name": "",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "chat_deleted",
            "source_platform": "android",
        },
    ]
    preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    pres_conn.close()

    app = create_app(output_root)
    client = app.test_client()

    info_res = client.get("/api/chat-info?chat_id=pres_grp&chat_type=group")
    assert info_res.status_code == 200
    info_data = info_res.get_json()
    assert info_data["total"] == 1
    assert info_data["first_ts"] == 2500
    assert info_data["last_ts"] == 2500


def test_merge_messages_same_timestamp_burst_not_dropped() -> None:
    from wab_viewer.preserved import _merge_messages

    live_rows = [
        {
            "msg_id": 1,
            "stanza_id": "live_stanza_1",
            "timestamp_ms": 5000,
            "from_me": 0,
            "text_body": "Live burst 1",
        }
    ]
    pres_rows = [
        {
            "msg_id": -2,
            "stanza_id": "pres_stanza_2",
            "timestamp_ms": 5000,
            "from_me": 0,
            "text_body": "Preserved burst 2",
            "preservation_reason": "deleted_on_phone",
        }
    ]
    merged = _merge_messages(live_rows, pres_rows, False, 50)
    assert len(merged) == 2
    assert {m["stanza_id"] for m in merged} == {"live_stanza_1", "pres_stanza_2"}


def test_preserve_deleted_messages_logging_no_messages(tmp_path: Path, caplog) -> None:
    old_db = tmp_path / "old.db"
    new_db = tmp_path / "new.db"
    for db_p in (old_db, new_db):
        with sqlite3.connect(db_p) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT);
                INSERT INTO jid VALUES (1, 'user_a');
                INSERT INTO chat VALUES (1, 1, 'Chat A');
                INSERT INTO message VALUES (1, 1, 'k1', 0, 1, 1000, 0, 'Same');
            """)
    arch_db = tmp_path / ".wa_media_archiver.db"
    sqlite3.connect(arch_db).close()

    with caplog.at_level(logging.INFO):
        res = preservation.preserve_deleted_messages(
            old_db_path=str(old_db),
            new_db_path=str(new_db),
            output_root=str(tmp_path),
            archive_db_path=str(arch_db),
            platform="android",
            logger=_TEST_LOGGER,
        )

    assert res == (0, 0)
    assert res.messages == 0
    assert res.chats == 0
    assert (
        "No deleted or revoked messages detected (0 messages preserved across 0 chats)."
        in caplog.text
    )


def test_preserve_deleted_messages_logging_calls_only(tmp_path: Path, caplog) -> None:
    old_db = tmp_path / "old_calls.db"
    new_db = tmp_path / "new_calls.db"
    for db_p in (old_db, new_db):
        with sqlite3.connect(db_p) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT);
                INSERT INTO jid VALUES (1, '15557777', 's.whatsapp.net', '15557777@s.whatsapp.net');
                INSERT INTO chat VALUES (1, 1, 'Chat A');
                INSERT INTO message VALUES (1, 1, 'k1', 0, 1, 1000, 0, 'Same');
            """)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT,
                jid_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                bytes_transferred INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO call_log VALUES (1, 'call_del_1', 1, 0, 1600000000, 0, 60, 5, 1000, 0);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT,
                jid_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                bytes_transferred INTEGER,
                group_jid_row_id INTEGER
            );
        """)

    arch_db = tmp_path / ".wa_media_archiver.db"
    sqlite3.connect(arch_db).close()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        res = preservation.preserve_deleted_messages(
            old_db_path=str(old_db),
            new_db_path=str(new_db),
            output_root=str(tmp_path),
            archive_db_path=str(arch_db),
            platform="android",
            logger=_TEST_LOGGER,
        )

    assert res == (0, 0)
    assert res.messages == 0
    assert res.chats == 0
    assert (
        "No deleted or revoked messages detected (0 messages preserved across 0 chats)."
        in caplog.text
    )
    assert "new call(s) preserved" not in caplog.text

    pres_db_path = Path(preservation.get_preserved_db_path(str(tmp_path)))
    assert pres_db_path.is_file()
    with sqlite3.connect(pres_db_path) as pconn:
        calls = pconn.execute("SELECT call_id FROM preserved_calls").fetchall()
        assert len(calls) == 1
        assert calls[0][0] == "call_del_1"


def test_forward_mode_preservation_summary_logging(tmp_path: Path, caplog) -> None:

    from wab_archiver import main as wa

    db_path1 = tmp_path / "src1.db"
    with sqlite3.connect(db_path1) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER, key_id TEXT, message_type INTEGER, text_data TEXT);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_url TEXT, media_name TEXT);
            INSERT INTO jid VALUES (1, '390111222'), (2, '390333444');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO message VALUES
                (1, 1, 1718000000000, 2, 0, 'k1', 0, 'M1'),
                (2, 1, 1718001000000, 2, 0, 'k2', 0, 'M2');
        """)

    output_dir = tmp_path / "archive_out"
    output_dir.mkdir()
    root = tmp_path / "wa_root"
    (root / "Media" / "WhatsApp Images").mkdir(parents=True)

    args = argparse.Namespace(
        from_adb=False,
        e2e_key=None,
        ios_password=None,
        timezone=None,
        since=None,
        limit=None,
        ios_backup=None,
        dry_run=False,
        preserve_deleted=True,
        msgstore=str(db_path1),
        wa_roots=[str(root)],
        output=str(output_dir),
        contacts=None,
        log=None,
    )

    # Run 1: baseline run
    with caplog.at_level(logging.INFO):
        wa.run_forward_mode(args, _TEST_LOGGER)

    assert (
        "Preserve-deleted: First run detected in output directory; establishing baseline database snapshot (0 messages preserved across 0 chats)."
        in caplog.text
    )
    assert "Preserved           : 0 message(s) across 0 chat(s)" in caplog.text

    # Run 2: incremental run with a deleted message
    db_path2 = tmp_path / "src2.db"
    with sqlite3.connect(db_path2) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, subject TEXT, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, timestamp INTEGER, sender_jid_row_id INTEGER, from_me INTEGER, key_id TEXT, message_type INTEGER, text_data TEXT);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, mime_type TEXT, chat_row_id INTEGER, message_url TEXT, media_name TEXT);
            INSERT INTO jid VALUES (1, '390111222'), (2, '390333444');
            INSERT INTO chat VALUES (1, NULL, 1);
            INSERT INTO message VALUES
                (1, 1, 1718000000000, 2, 0, 'k1', 0, 'M1');
        """)

    args.msgstore = str(db_path2)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        wa.run_forward_mode(args, _TEST_LOGGER)

    assert "Preservation complete: 1 new message(s) preserved across 1 chat(s)." in caplog.text
    assert "Preserved           : 1 message(s) across 1 chat(s)" in caplog.text
    assert "Preserved DB        :" in caplog.text


def test_decrypt_msgstore_preservation_lifecycle_and_rollback(tmp_path: Path, monkeypatch) -> None:
    """Verify that _decrypt_msgstore_if_needed manages .old lifecycle and safely rolls back on failure."""
    import importlib.util

    from wab_archiver import main as wa

    if importlib.util.find_spec("wa_crypt_tools") is None:
        return

    import zlib

    from wa_crypt_tools.lib.db.db15 import Database15
    from wa_crypt_tools.lib.key.keyfactory import KeyFactory
    from wa_crypt_tools.lib.props import Props

    key_hex = "22" * 32
    key = KeyFactory.new(key_hex)
    db15 = Database15(key=key)

    # 1. Prepare original msgstore.db on disk
    db_dir = tmp_path / "archive_out" / "Whatsapp Databases"
    db_dir.mkdir(parents=True)
    canonical_db = db_dir / "msgstore.db"
    old_db_file = db_dir / "msgstore.db.old"
    incoming_db_file = db_dir / "msgstore.db.incoming"

    original_bytes = b"SQLite format 3\x00_ORIGINAL_BASELINE_DB_CONTENT"
    canonical_db.write_bytes(original_bytes)

    # 2. Prepare encrypted incoming file
    incoming_bytes = b"SQLite format 3\x00_NEW_INCOMING_DECRYPTED_DB_CONTENT"
    encrypted_payload = db15.encrypt(key, Props(), zlib.compress(incoming_bytes, 1))
    crypt_file = tmp_path / "msgstore.db.crypt15"
    crypt_file.write_bytes(encrypted_payload)

    args = argparse.Namespace(
        output=str(tmp_path / "archive_out"),
        msgstore=str(crypt_file),
        e2e_key=key_hex,
        preserve_deleted=True,
    )

    # --- Test Case A: Preservation fails -> original db MUST be restored, .old cleaned up ---
    def mock_preserve_fail(*_args, **_kwargs):
        raise RuntimeError("Simulated failure during message diffing")

    monkeypatch.setattr(preservation, "preserve_deleted_messages", mock_preserve_fail)

    with pytest.raises(RuntimeError, match="Simulated failure"):
        wa._decrypt_msgstore_if_needed(args, _TEST_LOGGER)

    # Database must have rolled back to original content
    assert canonical_db.is_file()
    assert canonical_db.read_bytes() == original_bytes
    assert not old_db_file.exists()
    assert not incoming_db_file.exists()

    # --- Test Case B: Preservation succeeds -> new db active, .old cleaned up ---
    def mock_preserve_success(*_args, **_kwargs):
        return preservation.PreservationResult(messages=2, chats=1)

    monkeypatch.setattr(preservation, "preserve_deleted_messages", mock_preserve_success)

    # Reset args.msgstore back to the crypt file
    args.msgstore = str(crypt_file)
    wa._decrypt_msgstore_if_needed(args, _TEST_LOGGER)

    assert canonical_db.is_file()
    assert canonical_db.read_bytes() == incoming_bytes
    assert not old_db_file.exists()
    assert not incoming_db_file.exists()
    assert args._preservation_stats.messages == 2
    assert args._preservation_stats.chats == 1


def test_forward_mode_unencrypted_preservation_rollback_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify that unencrypted forward mode restores dest_db from dest_db.old if diff fails."""

    from wab_archiver import main as wa

    db_dir = tmp_path / "archive_out" / "Whatsapp Databases"
    db_dir.mkdir(parents=True)
    dest_db = db_dir / "msgstore.db"
    old_dest_db = db_dir / "msgstore.db.old"

    import contextlib

    with contextlib.closing(sqlite3.connect(dest_db)) as conn:
        conn.execute("CREATE TABLE original_marker (id INTEGER)")
        conn.commit()
    original_bytes = dest_db.read_bytes()

    new_db_file = tmp_path / "incoming_src.db"
    with contextlib.closing(sqlite3.connect(new_db_file)) as conn:
        conn.execute("CREATE TABLE new_marker (id INTEGER)")
        conn.commit()

    root = tmp_path / "wa_root"
    (root / "Media" / "WhatsApp Images").mkdir(parents=True)

    args = argparse.Namespace(
        from_adb=False,
        e2e_key=None,
        ios_password=None,
        timezone=None,
        since=None,
        limit=None,
        ios_backup=None,
        dry_run=False,
        preserve_deleted=True,
        msgstore=str(new_db_file),
        wa_roots=[str(root)],
        output=str(tmp_path / "archive_out"),
        contacts=None,
        log=None,
    )

    def mock_preserve_fail(*_args, **_kwargs):
        raise RuntimeError("Diff failure in forward mode")

    monkeypatch.setattr(preservation, "preserve_deleted_messages", mock_preserve_fail)

    with pytest.raises(RuntimeError, match="Diff failure in forward mode"):
        wa.run_forward_mode(args, _TEST_LOGGER)

    # Dest db must be restored to original content
    assert dest_db.is_file()
    assert dest_db.read_bytes() == original_bytes
    assert not old_dest_db.exists()


def test_viewer_preserved_poll_hydration_and_view_once(tmp_path: Path) -> None:
    from wab_viewer.main import create_app

    output_root = tmp_path / "archive"
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir(parents=True)
    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    # Setup empty live DB
    live_db = db_dir / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, key_id TEXT, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        """)

    pres_conn = preservation.open_preservation_db(str(output_root))
    poll_json = json.dumps(
        {
            "question": "Favorite language?",
            "multiple_answers": False,
            "options": [{"name": "Python", "vote_count": 5}, {"name": "Rust", "vote_count": 3}],
            "total_votes": 8,
        }
    )
    rows = [
        {
            "chat_id": "contact_poll",
            "chat_type": "contact",
            "stanza_id": "k_poll_1",
            "msg_id": 1,
            "timestamp_ms": 1000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Favorite language?",
            "media_type": "poll",
            "media_name": "",
            "archive_path": None,
            "poll_data": poll_json,
            "is_view_once": 0,
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
        {
            "chat_id": "contact_poll",
            "chat_type": "contact",
            "stanza_id": "k_vo_1",
            "msg_id": 2,
            "timestamp_ms": 2000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "",
            "media_type": "image",
            "media_name": "photo.jpg",
            "archive_path": "Media/photo.jpg",
            "media_width": 1920,
            "media_height": 1080,
            "is_view_once": 1,
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
        {
            "chat_id": "contact_poll",
            "chat_type": "contact",
            "stanza_id": "k_loc_1",
            "msg_id": 3,
            "timestamp_ms": 3000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "",
            "media_type": "location",
            "media_name": "",
            "archive_path": None,
            "latitude": 37.7749,
            "longitude": -122.4194,
            "location_name": "San Francisco",
            "location_address": "Market St",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
    ]
    preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    pres_conn.close()

    app = create_app(output_root)
    client = app.test_client()

    msgs_res = client.get("/api/messages?chat_id=contact_poll&chat_type=contact&limit=50")
    assert msgs_res.status_code == 200
    msgs = msgs_res.get_json()
    assert len(msgs) == 3
    by_stanza = {m["stanza_id"]: m for m in msgs}

    poll_msg = by_stanza["k_poll_1"]
    assert "poll" in poll_msg
    assert poll_msg["poll"]["question"] == "Favorite language?"
    assert poll_msg["poll"]["total_votes"] == 8
    assert len(poll_msg["poll"]["options"]) == 2
    assert poll_msg["poll"]["options"][0]["name"] == "Python"
    assert poll_msg["poll"]["options"][0]["vote_count"] == 5

    vo_msg = by_stanza["k_vo_1"]
    assert vo_msg["is_view_once"] == 1
    assert vo_msg["media_width"] == 1920
    assert vo_msg["media_height"] == 1080

    loc_msg = by_stanza["k_loc_1"]
    assert loc_msg["media_type"] == "location"
    assert loc_msg["latitude"] == 37.7749
    assert loc_msg["longitude"] == -122.4194
    assert loc_msg["location_name"] == "San Francisco"
    assert loc_msg["location_address"] == "Market St"


def test_viewer_preserved_progressive_disclosure_edit_history(tmp_path: Path) -> None:
    from wab_viewer.main import create_app

    output_root = tmp_path / "archive"
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir(parents=True)
    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    live_db = db_dir / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, key_id TEXT, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        """)

    pres_conn = preservation.open_preservation_db(str(output_root))
    rows = [
        {
            "chat_id": "contact_edit",
            "chat_type": "contact",
            "stanza_id": "k_edit_test",
            "msg_id": 1,
            "timestamp_ms": 1000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Final edited text",
            "original_text_body": "Original draft text",
            "media_type": "text",
            "is_edited": 1,
            "edited_ts": 1700000088000,
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        }
    ]
    preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    pres_conn.close()

    app = create_app(output_root)
    client = app.test_client()

    msgs_res = client.get("/api/messages?chat_id=contact_edit&chat_type=contact&limit=50")
    assert msgs_res.status_code == 200
    msgs = msgs_res.get_json()
    assert len(msgs) == 1
    m = msgs[0]
    assert m["text_body"] == "Final edited text"
    assert m["original_text_body"] == "Original draft text"
    assert m["is_edited"] == 1
    assert m["edited_ts"] == 1700000088000
    assert m["source_platform"] == "android"


def test_viewer_bidirectional_reply_quote_linking(tmp_path: Path) -> None:
    from wab_viewer.main import create_app

    output_root = tmp_path / "archive"
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir(parents=True)
    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    # Live DB has 2 messages:
    # 1: key_id = "live_target", regular live message
    # 2: key_id = "live_reply_to_del", quoting "del_target" (deleted from live DB!)
    live_db = db_dir / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'contact_1');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER DEFAULT 2000, display_message_row_id INTEGER DEFAULT 2);
            INSERT INTO chat VALUES (1, 1, NULL, 0, 2000, 2);

            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                key_id TEXT,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                file_path TEXT,
                media_name TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                key_id TEXT,
                text_data TEXT,
                from_me INTEGER DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER DEFAULT 0
            );
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            INSERT INTO message VALUES
                (1, 1, 'live_target', 0, 1, 1000, 0, 'Live target message'),
                (2, 1, 'live_reply_to_del', 1, 1, 2000, 0, 'Live reply to deleted');
            INSERT INTO message_quoted VALUES
                (2, 'del_target', 'Deleted original quote', 0, 1, 500);
        """)

    # Preserved DB has:
    # del_target (the deleted message quoted by live_reply_to_del)
    # pres_reply_to_live (quoting live_target)
    # pres_reply_to_pres (quoting del_target)
    pres_conn = preservation.open_preservation_db(str(output_root))
    rows = [
        {
            "chat_id": "contact_1",
            "chat_type": "contact",
            "stanza_id": "del_target",
            "msg_id": 100,
            "timestamp_ms": 500,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Deleted original quote",
            "media_type": "text",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
        {
            "chat_id": "contact_1",
            "chat_type": "contact",
            "stanza_id": "pres_reply_to_live",
            "msg_id": 101,
            "timestamp_ms": 1500,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Preserved reply to live target",
            "quoted_text": "Live target message",
            "quoted_stanza_id": "live_target",
            "media_type": "text",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
        {
            "chat_id": "contact_1",
            "chat_type": "contact",
            "stanza_id": "pres_reply_to_pres",
            "msg_id": 102,
            "timestamp_ms": 1800,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Preserved reply to preserved target",
            "quoted_text": "Deleted original quote",
            "quoted_stanza_id": "del_target",
            "media_type": "text",
            "preserved_at": "2026-10-15T10:00:00Z",
            "preservation_reason": "deleted_on_phone",
            "source_platform": "android",
        },
    ]
    preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    del_target_rowid = pres_conn.execute(
        "SELECT rowid FROM preserved_messages WHERE stanza_id = 'del_target'"
    ).fetchone()[0]
    pres_conn.close()

    app = create_app(output_root)
    client = app.test_client()

    msgs_res = client.get("/api/messages?chat_id=contact_1&chat_type=contact&limit=50")
    assert msgs_res.status_code == 200
    msgs = msgs_res.get_json()
    assert len(msgs) == 5
    by_stanza = {m["stanza_id"]: m for m in msgs}

    # 1. Live message quoting deleted-and-preserved target
    live_reply = by_stanza["live_reply_to_del"]
    assert live_reply["quoted_msg_id"] == -del_target_rowid

    # 2. Preserved message quoting live target
    pres_to_live = by_stanza["pres_reply_to_live"]
    assert pres_to_live["quoted_msg_id"] == 1

    # 3. Preserved message quoting another preserved target
    pres_to_pres = by_stanza["pres_reply_to_pres"]
    assert pres_to_pres["quoted_msg_id"] == -del_target_rowid

    # 4. Also verify /api/messages/at around timestamp 1600
    at_res = client.get("/api/messages/at?chat_id=contact_1&chat_type=contact&ts=1600&limit=50")
    assert at_res.status_code == 200
    at_msgs = at_res.get_json()
    at_by_stanza = {m["stanza_id"]: m for m in at_msgs}
    assert at_by_stanza["live_reply_to_del"]["quoted_msg_id"] == -del_target_rowid
    assert at_by_stanza["pres_reply_to_live"]["quoted_msg_id"] == 1
    assert at_by_stanza["pres_reply_to_pres"]["quoted_msg_id"] == -del_target_rowid


def test_viewer_chat_info_preserved_roster_and_calls(tmp_path: Path) -> None:
    from wab_viewer.main import create_app

    output_root = tmp_path / "archive"
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir(parents=True)
    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    # Setup empty live DB
    live_db = db_dir / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, key_id TEXT, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        """)

    pres_conn = preservation.open_preservation_db(str(output_root))

    # Add preserved chat with members roster and call records
    members = [
        {"name": "Mom", "number": "15550001", "role": "admin"},
        {"name": "Dad", "number": "15550002", "role": "member"},
    ]
    pres_conn.execute(
        """
        INSERT INTO preserved_chats (
            chat_id, chat_type, subject, description, created_ts, creator_jid,
            oldest_ts, newest_ts, msg_count, members_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "del_group_99",
            "group",
            "Preserved Family",
            "Family group chat",
            1600000000000,
            "15550001@s.whatsapp.net",
            1600000001000,
            1700000000000,
            150,
            json.dumps(members),
        ),
    )

    # Add 2 calls for this chat
    for i in range(1, 3):
        pres_conn.execute(
            """
            INSERT INTO preserved_calls (
                call_id, chat_id, chat_jid, caller_jid, caller_name, from_me,
                timestamp_ms, duration_seconds, is_video, call_result, is_group_call,
                preserved_at, source_platform
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"call_{i}",
                "del_group_99",
                "del_group_99@g.us",
                "15550001@s.whatsapp.net",
                "Mom",
                0,
                1650000000000 + i * 1000,
                120,
                1,
                1,
                1,
                "2026-10-15T10:00:00Z",
                "android",
            ),
        )
    pres_conn.commit()
    pres_conn.close()

    app = create_app(output_root)
    client = app.test_client()

    info_res = client.get("/api/chat-info?chat_id=del_group_99&chat_type=group")
    assert info_res.status_code == 200
    data = info_res.get_json()

    assert data["display_name"] == "Preserved Family"
    assert data["description"] == "Family group chat"
    assert data["created_ts"] == 1600000000000
    assert data["first_ts"] == 1600000001000
    assert data["last_ts"] == 1700000000000
    assert data["total"] == 150
    assert data["calls_count"] == 2
    assert len(data["members"]) == 2
    assert data["members"][0]["name"] == "Mom"
    assert data["members"][1]["name"] == "Dad"


def test_preserve_incremental_deletions_flow(tmp_path: Path) -> None:
    """Verify preserve_incremental_deletions extracts deleting messages into preserved_messages.db."""
    db_file = tmp_path / "test_msgstore.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                key_id TEXT,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            INSERT INTO jid VALUES (1, '5550100', 's.whatsapp.net', '5550100@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, 'Test Chat');
            INSERT INTO message VALUES (42, 1, 0, 'INC_DEL_KEY_42', 1, 1729000100000, 0, 'To be preserved');
        """)
        conn.commit()

        count = preservation.preserve_incremental_deletions(conn, [42], str(tmp_path), _TEST_LOGGER)
        assert count == 1

    pres_db = Path(preservation.get_preserved_db_path(str(tmp_path)))
    assert pres_db.exists()
    with sqlite3.connect(pres_db) as pconn:
        row = pconn.execute(
            "SELECT stanza_id, text_body, preservation_reason, chat_jid FROM preserved_messages WHERE stanza_id = 'INC_DEL_KEY_42'"
        ).fetchone()
        assert row is not None
        assert row[0] == "INC_DEL_KEY_42"
        assert row[1] == "To be preserved"
        assert row[2] == "deleted_on_phone"
        assert row[3] == "5550100@s.whatsapp.net"


def test_preserve_incremental_deletions_link_preview(tmp_path: Path) -> None:
    """Verify preserve_incremental_deletions captures message_text and message_thumbnail into service_metadata."""
    db_file = tmp_path / "test_msgstore.db"
    dummy_thumb = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"

    with sqlite3.connect(db_file) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                key_id TEXT,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_text (
                message_row_id INTEGER PRIMARY KEY,
                description TEXT,
                page_title TEXT,
                url TEXT,
                preview_type INTEGER
            );
            CREATE TABLE message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
            INSERT INTO jid VALUES (1, '5550100', 's.whatsapp.net', '5550100@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, 'Test Chat');
            INSERT INTO message VALUES (101, 1, 0, 'INC_LINK_KEY_101', 1, 1729000100000, 0, 'Check https://python.org');
            INSERT INTO message_text VALUES (101, 'Python official website', 'Python.org', 'https://python.org', 1);
        """)
        conn.execute("INSERT INTO message_thumbnail VALUES (101, ?)", (dummy_thumb,))
        conn.commit()

        count = preservation.preserve_incremental_deletions(
            conn, [101], str(tmp_path), _TEST_LOGGER
        )
        assert count == 1

    pres_db = Path(preservation.get_preserved_db_path(str(tmp_path)))
    assert pres_db.exists()
    with sqlite3.connect(pres_db) as pconn:
        row = pconn.execute(
            "SELECT stanza_id, service_metadata FROM preserved_messages WHERE stanza_id = 'INC_LINK_KEY_101'"
        ).fetchone()
        assert row is not None
        assert row[0] == "INC_LINK_KEY_101"
        meta = json.loads(row[1])
        assert "link_preview" in meta
        assert meta["link_preview"]["title"] == "Python.org"
        assert meta["link_preview"]["desc"] == "Python official website"
        assert meta["link_preview"]["url"] == "https://python.org"
        assert meta["link_preview"]["preview_type"] == 1
        assert "thumbnail_b64" in meta["link_preview"]
        assert base64.b64decode(meta["link_preview"]["thumbnail_b64"]) == dummy_thumb


def test_viewer_preserved_link_preview_and_thumbnail_endpoint(tmp_path: Path) -> None:
    """Verify viewer unpacks link preview fields and serves thumbnail from service_metadata."""
    from wab_viewer.main import create_app

    output_root = tmp_path / "archive"
    db_dir = output_root / "Whatsapp Databases"
    db_dir.mkdir(parents=True)

    arch_db = output_root / ".wa_media_archiver.db"
    _make_dummy_archive_db(arch_db)

    dummy_thumb = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
    dummy_b64 = base64.b64encode(dummy_thumb).decode("ascii")

    # Live database with chat
    live_db = db_dir / "msgstore.db"
    with sqlite3.connect(live_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'contact_link');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER DEFAULT 4000, display_message_row_id INTEGER DEFAULT 1);
            INSERT INTO chat VALUES (1, 1, NULL, 0, 4000, 1);

            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                key_id TEXT,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                file_path TEXT,
                media_name TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                text_data TEXT,
                from_me INTEGER DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER DEFAULT 0
            );
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            INSERT INTO message VALUES (1, 1, 'live_1', 0, 1, 1000, 0, 'Hello');
        """)

    # Preserved database with a preserved link message
    pres_conn = preservation.open_preservation_db(str(output_root))
    link_meta = {
        "link_preview": {
            "title": "Example Domain",
            "desc": "Illustrative examples",
            "url": "https://example.com",
            "preview_type": 1,
            "thumbnail_b64": dummy_b64,
        }
    }
    rows = [
        {
            "chat_id": "contact_link",
            "chat_type": "contact",
            "stanza_id": "k_preserved_link",
            "msg_id": 99,
            "timestamp_ms": 2000,
            "sender": "Alice",
            "from_me": 0,
            "text_body": "Here is https://example.com",
            "media_type": "link",
            "media_name": "",
            "archive_path": None,
            "quoted_text": "",
            "service_metadata": json.dumps(link_meta),
            "preservation_reason": "deleted_on_phone",
        }
    ]
    preservation.record_preserved_messages(pres_conn, rows, "android", _TEST_LOGGER)
    pres_conn.close()

    app = create_app(output_root)
    app.config["TESTING"] = True
    client = app.test_client()

    # 1. Test /api/messages returns link preview fields and has_thumbnail: 1
    resp = client.get("/api/messages?chat_id=contact_link&chat_type=contact")
    assert resp.status_code == 200
    messages = resp.get_json()
    pres_msg = next((m for m in messages if m.get("is_preserved") == 1), None)
    assert pres_msg is not None
    assert pres_msg["link_title"] == "Example Domain"
    assert pres_msg["link_desc"] == "Illustrative examples"
    assert pres_msg["link_url"] == "https://example.com"
    assert pres_msg["link_preview_type"] == 1
    assert pres_msg["has_thumbnail"] == 1
    assert pres_msg["msg_id"] < 0

    # 2. Test /api/thumbnail/<negative_id> serves decoded JPEG thumbnail
    thumb_resp = client.get(f"/api/thumbnail/{pres_msg['msg_id']}")
    assert thumb_resp.status_code == 200
    assert thumb_resp.mimetype == "image/jpeg"
    assert thumb_resp.data == dummy_thumb

    # 3. Test /api/thumbnail/<stanza_id> fallback works
    thumb_stanza_resp = client.get(f"/api/thumbnail/{pres_msg['stanza_id']}")
    assert thumb_stanza_resp.status_code == 200
    assert thumb_stanza_resp.mimetype == "image/jpeg"
    assert thumb_stanza_resp.data == dummy_thumb


def test_record_preserved_messages_batch_lookups(tmp_path: Path) -> None:
    """Verify record_preserved_messages batch lookups handle inserts, edits, reactions, and maintenance."""
    with preservation.open_preservation_db(str(tmp_path)) as pres_conn:
        cand1 = {
            "chat_id": "chat1",
            "stanza_id": "STANZA_1",
            "chat_type": "contact",
            "timestamp_ms": 1700000000000,
            "text_body": "Original text",
        }
        cand2 = {
            "chat_id": "chat1",
            "stanza_id": "STANZA_2",
            "chat_type": "contact",
            "timestamp_ms": 1700000001000,
            "text_body": "Second message",
        }
        res = preservation.record_preserved_messages(
            pres_conn, [cand1, cand2], "android", _TEST_LOGGER
        )
        assert res.messages == 2
        assert res.chats == 1

        # Now test updates to existing rows via batch lookup: cand1 edited, cand2 gets reactions
        cand1_edited = {
            "chat_id": "chat1",
            "stanza_id": "STANZA_1",
            "chat_type": "contact",
            "timestamp_ms": 1700000000000,
            "text_body": "Edited text",
            "is_edited": 1,
            "edited_ts": 1700000005000,
        }
        cand2_reacted = {
            "chat_id": "chat1",
            "stanza_id": "STANZA_2",
            "chat_type": "contact",
            "timestamp_ms": 1700000001000,
            "text_body": "Second message",
            "reactions": "👍",
        }
        res2 = preservation.record_preserved_messages(
            pres_conn, [cand1_edited, cand2_reacted], "android", _TEST_LOGGER
        )
        assert res2.messages == 0  # No new messages

        row1 = pres_conn.execute(
            "SELECT text_body, original_text_body, is_edited FROM preserved_messages WHERE stanza_id = 'STANZA_1'"
        ).fetchone()
        assert row1[0] == "Edited text"
        assert row1[1] == "Original text"
        assert row1[2] == 1

        row2 = pres_conn.execute(
            "SELECT reactions FROM preserved_messages WHERE stanza_id = 'STANZA_2'"
        ).fetchone()
        assert row2[0] == "👍"


def test_record_preserved_calls_batch_lookups(tmp_path: Path) -> None:
    """Verify record_preserved_calls batch lookups insert new calls and ignore existing ones."""
    with preservation.open_preservation_db(str(tmp_path)) as pres_conn:
        call1 = {"call_id": "CALL_1", "chat_id": "chat1", "timestamp_ms": 1700000000000}
        call2 = {"call_id": "CALL_2", "chat_id": "chat1", "timestamp_ms": 1700000001000}
        inserted = preservation.record_preserved_calls(
            pres_conn, [call1, call2], "android", _TEST_LOGGER
        )
        assert inserted == 2

        # Re-recording same calls plus one new call
        call3 = {"call_id": "CALL_3", "chat_id": "chat1", "timestamp_ms": 1700000002000}
        inserted2 = preservation.record_preserved_calls(
            pres_conn, [call1, call2, call3], "android", _TEST_LOGGER
        )
        assert inserted2 == 1


def test_merge_messages_empty_revoked_not_preserved() -> None:
    """Verify that a live revoked message matched against an empty preserved stub is not marked as preserved."""
    from wab_viewer.preserved import _merge_messages

    live_rows = [
        {
            "msg_id": 1825644,
            "stanza_id": "STANZA_REVOKED_1",
            "timestamp_ms": 1792161052000,
            "from_me": 0,
            "media_type": "deleted",
            "text_body": "This message was deleted",
        }
    ]
    empty_pres_rows = [
        {
            "msg_id": -1,
            "stanza_id": "STANZA_REVOKED_1",
            "timestamp_ms": 1792161052000,
            "from_me": 0,
            "media_type": "text",
            "text_body": "",
            "is_preserved": 1,
            "preservation_reason": "revoked_by_sender",
        }
    ]

    # Empty preserved stub should NOT re-hydrate the live deleted message
    merged = _merge_messages(live_rows, empty_pres_rows, False, 50)
    assert len(merged) == 1
    assert merged[0]["msg_id"] == 1825644
    assert merged[0]["media_type"] == "deleted"
    assert merged[0]["text_body"] == "This message was deleted"
    assert "is_preserved" not in merged[0] or merged[0].get("is_preserved") is None

    # Preserved message with actual pre-revocation text SHOULD re-hydrate
    hydrated_pres_rows = [
        {
            "msg_id": -1,
            "stanza_id": "STANZA_REVOKED_1",
            "timestamp_ms": 1792161052000,
            "from_me": 0,
            "media_type": "text",
            "text_body": "Original pre-revocation text",
            "is_preserved": 1,
            "preservation_reason": "revoked_by_sender",
        }
    ]
    merged2 = _merge_messages(live_rows, hydrated_pres_rows, False, 50)
    assert len(merged2) == 1
    assert merged2[0]["is_preserved"] == 1
    assert merged2[0]["text_body"] == "Original pre-revocation text"


def test_record_preserved_messages_empty_revoked_ignored(tmp_path: Path) -> None:
    """Verify that record_preserved_messages discards revoked messages with no content."""
    pres_conn = preservation.open_preservation_db(str(tmp_path))
    try:
        empty_revoked = {
            "chat_id": "chat1",
            "stanza_id": "REV_EMPTY_1",
            "msg_id": 100,
            "timestamp_ms": 1700000000000,
            "from_me": 0,
            "text_body": "",
            "media_type": "text",
            "preservation_reason": "revoked_by_sender",
        }
        valid_revoked = {
            "chat_id": "chat1",
            "stanza_id": "REV_VALID_1",
            "msg_id": 101,
            "timestamp_ms": 1700000001000,
            "from_me": 0,
            "text_body": "I had text before revoking",
            "media_type": "text",
            "preservation_reason": "revoked_by_sender",
        }
        res = preservation.record_preserved_messages(
            pres_conn, [empty_revoked, valid_revoked], "android", _TEST_LOGGER
        )
        assert res.messages == 1

        rows = pres_conn.execute("SELECT stanza_id, text_body FROM preserved_messages").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "REV_VALID_1"
        assert rows[0][1] == "I had text before revoking"
    finally:
        pres_conn.close()
