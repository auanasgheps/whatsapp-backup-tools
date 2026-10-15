"""
Tests for WhatsApp message preservation across archival runs.
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from wab_archiver import preservation

_TEST_LOGGER = logging.getLogger("test_preservation")
_TEST_LOGGER.setLevel(logging.INFO)


def _make_dummy_archive_db(path: Path) -> None:
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
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path TEXT PRIMARY KEY,
            archive_path  TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def test_open_preservation_db(tmp_path: Path) -> None:
    conn = preservation.open_preservation_db(str(tmp_path))
    assert conn is not None

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "preserved_messages" in tables
    assert "preserved_chats" in tables
    assert "preservation_runs" in tables

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    conn.close()


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


def test_diff_and_extract_android_deleted_and_revoked(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    # Setup old database with:
    # 1. Normal message that will stay in new db
    # 2. Message that will be deleted in new db ("Delete for me")
    # 3. Message that will be revoked in new db ("Delete for everyone")
    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_a'), (2, 'user_b');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Test Group');

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
            INSERT INTO message VALUES
                (101, 1, 'key_stay', 0, 2, 100000, 0, 'Keep me'),
                (102, 1, 'key_deleted', 0, 2, 100001, 0, 'Delete me'),
                (103, 1, 'key_revoked', 0, 2, 100002, 0, 'Revoke me');
        """)

    # Setup new database:
    # key_stay is still message_type 0
    # key_deleted is absent
    # key_revoked is now message_type 15
    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_a'), (2, 'user_b');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Test Group');

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
            INSERT INTO message VALUES
                (201, 1, 'key_stay', 0, 2, 100000, 0, 'Keep me'),
                (203, 1, 'key_revoked', 0, 2, 100002, 15, '');
        """)

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )

    by_key = {r["stanza_id"]: r for r in extracted}
    assert "key_stay" not in by_key

    assert "key_deleted" in by_key
    assert by_key["key_deleted"]["text_body"] == "Delete me"
    assert by_key["key_deleted"]["preservation_reason"] == "deleted_on_phone"

    assert "key_revoked" in by_key
    assert by_key["key_revoked"]["text_body"] == "Revoke me"
    assert by_key["key_revoked"]["preservation_reason"] == "revoked_by_sender"


def test_diff_and_extract_android_chat_deleted(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_c');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Deleted Chat');

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
            INSERT INTO message VALUES (301, 1, 'key_chat_del', 0, 1, 100005, 0, 'In deleted chat');
        """)

    # New DB is completely empty of this chat and message
    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
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
        """)

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "key_chat_del"
    assert extracted[0]["preservation_reason"] == "chat_deleted"


def test_diff_and_extract_ios(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_stay', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Stay', 'Partner', NULL, NULL),
                (2, 1, 'ios_del', 0, 'partner@s.whatsapp.net', 1001.0, 0, 'iOS Deleted', 'Partner', NULL, NULL),
                (3, 1, 'ios_rev', 0, 'partner@s.whatsapp.net', 1002.0, 0, 'iOS Revoked', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);

            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZSTANZAID TEXT,
                ZISFROMME INTEGER,
                ZFROMJID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_stay', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Stay', 'Partner', NULL, NULL),
                (3, 1, 'ios_rev', 0, 'partner@s.whatsapp.net', 1002.0, 14, '', 'Partner', NULL, NULL);
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "ios_stay" not in by_key
    assert "ios_del" in by_key
    assert by_key["ios_del"]["preservation_reason"] == "deleted_on_phone"
    assert "ios_rev" in by_key
    assert by_key["ios_rev"]["preservation_reason"] == "revoked_by_sender"


def test_diff_and_extract_android_fresh_archive_db(tmp_path: Path) -> None:
    old_db = tmp_path / "old_fresh.db"
    new_db = tmp_path / "new_fresh.db"
    arch_db = tmp_path / "empty_arch.db"
    sqlite3.connect(arch_db).close()

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
            INSERT INTO message VALUES (1, 1, 'k1', 0, 2, 1000, 0, 'Deleted');
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

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "k1"
    assert extracted[0]["text_body"] == "Deleted"
    assert extracted[0]["sender"] == "+user_b"


def test_diff_and_extract_android_rowid_shift(tmp_path: Path) -> None:
    old_db = tmp_path / "old_shift.db"
    new_db = tmp_path / "new_shift.db"
    arch_db = tmp_path / "empty_arch_shift.db"
    sqlite3.connect(arch_db).close()

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'group-family@g.us'), (2, '39111');
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (5, 1, 'Family Group');
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT,
                from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER,
                message_type INTEGER, text_data TEXT
            );
            INSERT INTO message VALUES (10, 5, 'msg_group_1', 0, 2, 1000, 0, 'Group message');
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (90, 'group-family@g.us'), (91, '39111');
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (99, 90, 'Family Group');
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY, chat_row_id INTEGER, key_id TEXT,
                from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER,
                message_type INTEGER, text_data TEXT
            );
        """)

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "msg_group_1"
    # chat_id is updated to the new database's chat_row_id (99), not 5
    assert extracted[0]["chat_id"] == "99"
    # preservation_reason is deleted_on_phone since group still exists
    assert extracted[0]["preservation_reason"] == "deleted_on_phone"


def test_diff_and_extract_ios_rowid_shift(tmp_path: Path) -> None:
    old_db = tmp_path / "old_ios_shift.db"
    new_db = tmp_path / "new_ios_shift.db"
    arch_db = tmp_path / "empty_arch_ios_shift.db"
    sqlite3.connect(arch_db).close()

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (10, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 10, 'ios_shift_k1', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Message', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (88, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "ios_shift_k1"
    # chat_id is updated to 88
    assert extracted[0]["chat_id"] == "88"
    assert extracted[0]["preservation_reason"] == "deleted_on_phone"


def test_diff_and_extract_ios_fresh_archive_db(tmp_path: Path) -> None:
    old_db = tmp_path / "old_ios_fresh.db"
    new_db = tmp_path / "new_ios_fresh.db"
    arch_db = tmp_path / "empty_arch_ios.db"
    sqlite3.connect(arch_db).close()

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (1, 1, 'ios_k1', 0, 'partner@s.whatsapp.net', 1000.0, 0, 'iOS Deleted', 'Partner', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["stanza_id"] == "ios_k1"
    assert extracted[0]["text_body"] == "iOS Deleted"


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


def test_merge_messages_same_timestamp_burst_not_dropped() -> None:
    from wab_viewer.main import _merge_messages

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


def test_forward_mode_preservation_summary_logging(tmp_path: Path, caplog) -> None:
    import argparse

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
