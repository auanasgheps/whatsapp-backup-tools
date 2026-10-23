"""
Tests for WhatsApp message preservation across archival runs.
"""

import json
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
    assert "preserved_calls" in tables
    assert "preservation_runs" in tables

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2

    # Verify all v2 columns are present
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(preserved_messages)").fetchall()}
    for col_name, _ in preservation.NEW_V2_MESSAGE_COLUMNS:
        assert col_name in msg_cols, f"Missing v2 column {col_name} in preserved_messages"

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
    assert migrated_conn.execute("PRAGMA user_version").fetchone()[0] == 2

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


def test_diff_and_extract_android_polls(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES (1, '15550001', 's.whatsapp.net', '15550001@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Poll Chat');

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
            INSERT INTO message VALUES (501, 1, 'key_poll_del', 0, 1, 100050, 66, 'What is your favorite color?');

            CREATE TABLE message_poll (
                message_row_id INTEGER PRIMARY KEY,
                selectable_options_count INTEGER
            );
            INSERT INTO message_poll VALUES (501, 1);

            CREATE TABLE message_poll_option (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                option_name TEXT,
                vote_total INTEGER
            );
            INSERT INTO message_poll_option VALUES
                (1, 501, 'Blue', 4),
                (2, 501, 'Green', 2),
                (3, 501, 'Red', 0);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
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
    p_msg = extracted[0]
    assert p_msg["stanza_id"] == "key_poll_del"
    assert p_msg["media_type"] == "poll"
    assert p_msg["text_body"] == "What is your favorite color?"
    assert p_msg["poll_data"] is not None

    import json

    poll_dict = json.loads(p_msg["poll_data"])
    assert poll_dict["selectable_options_count"] == 1
    assert poll_dict["total_votes"] == 6
    assert len(poll_dict["options"]) == 3
    assert poll_dict["options"][0] == {"name": "Blue", "votes": 4}
    assert poll_dict["options"][1] == {"name": "Green", "votes": 2}
    assert poll_dict["options"][2] == {"name": "Red", "votes": 0}


def test_diff_and_extract_android_location(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES (1, '15550002', 's.whatsapp.net', '15550002@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, NULL);

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
            INSERT INTO message VALUES (601, 1, 'key_loc_del', 0, 1, 100060, 5, 'Check this location');

            CREATE TABLE message_location (
                message_row_id INTEGER PRIMARY KEY,
                latitude REAL,
                longitude REAL,
                place_name TEXT,
                place_address TEXT,
                url TEXT,
                live_location_share_duration INTEGER
            );
            INSERT INTO message_location VALUES (601, 48.8584, 2.2945, 'Eiffel Tower', 'Champ de Mars, Paris', 'https://maps.google.com/?q=48.8584,2.2945', 3600);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
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
    loc_msg = extracted[0]
    assert loc_msg["stanza_id"] == "key_loc_del"
    assert loc_msg["media_type"] == "location"
    assert loc_msg["latitude"] == 48.8584
    assert loc_msg["longitude"] == 2.2945
    assert loc_msg["location_name"] == "Eiffel Tower"
    assert loc_msg["location_address"] == "Champ de Mars, Paris"
    assert loc_msg["location_url"] == "https://maps.google.com/?q=48.8584,2.2945"
    assert loc_msg["live_location_share_duration"] == 3600


def test_diff_and_extract_android_admin_revoke(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(arch_db) as conn:
        conn.execute("INSERT INTO contacts VALUES ('15559999', 'Admin_Folder', 'Super Admin')")

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES
                (1, 'group1', 'g.us', 'group1@g.us'),
                (2, '15550003', 's.whatsapp.net', '15550003@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Community Group');

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
            INSERT INTO message VALUES (701, 1, 'key_admin_rev', 0, 2, 100070, 0, 'Spam message removed by admin');
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES
                (1, 'group1', 'g.us', 'group1@g.us'),
                (2, '15550003', 's.whatsapp.net', '15550003@s.whatsapp.net'),
                (9, '15559999', 's.whatsapp.net', '15559999@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Community Group');

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
            INSERT INTO message VALUES (801, 1, 'key_admin_rev', 0, 2, 100070, 64, '');

            CREATE TABLE message_revoked (
                message_row_id INTEGER PRIMARY KEY,
                admin_jid_row_id INTEGER,
                revoke_timestamp INTEGER
            );
            INSERT INTO message_revoked VALUES (801, 9, 100099);
        """)

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    rev_msg = extracted[0]
    assert rev_msg["stanza_id"] == "key_admin_rev"
    assert rev_msg["text_body"] == "Spam message removed by admin"
    assert rev_msg["preservation_reason"] == "revoked_by_admin"
    assert rev_msg["service_metadata"] is not None

    import json

    meta = json.loads(rev_msg["service_metadata"])
    assert meta["revoked_by"] == "admin"
    assert meta["admin_jid"] == "15559999@s.whatsapp.net"
    assert meta["admin_name"] == "Super Admin"
    assert meta["revoke_timestamp"] == 100099


def test_diff_and_extract_android_view_once_and_media_metrics(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES (1, '15550004', 's.whatsapp.net', '15550004@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, NULL);

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
                (801, 1, 'key_vo_photo', 0, 1, 100080, 42, 'Disappearing secret photo'),
                (802, 1, 'key_vo_video', 0, 1, 100081, 43, 'Disappearing secret video');

            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT,
                media_name TEXT,
                file_length INTEGER,
                width INTEGER,
                height INTEGER,
                media_duration INTEGER
            );
            INSERT INTO message_media VALUES
                (801, 'WhatsApp/Media/WhatsApp Images/IMG_vo.jpg', 'IMG_vo.jpg', 409600, 1920, 1080, 0),
                (802, 'WhatsApp/Media/WhatsApp Video/VID_vo.mp4', 'VID_vo.mp4', 2048000, 1280, 720, 15);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
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
    by_key = {r["stanza_id"]: r for r in extracted}
    assert len(by_key) == 2

    p1 = by_key["key_vo_photo"]
    assert p1["is_view_once"] == 1
    assert p1["media_type"] == "image"
    assert p1["media_width"] == 1920
    assert p1["media_height"] == 1080
    assert p1["media_size"] == 409600

    p2 = by_key["key_vo_video"]
    assert p2["is_view_once"] == 1
    assert p2["media_type"] == "video"
    assert p2["media_width"] == 1280
    assert p2["media_height"] == 720
    assert p2["media_duration"] == 15
    assert p2["media_size"] == 2048000


def test_diff_and_extract_android_quoted_placeholder_and_reply_link(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES
                (1, '15550005', 's.whatsapp.net', '15550005@s.whatsapp.net'),
                (2, '15550006', 's.whatsapp.net', '15550006@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, NULL);

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
            INSERT INTO message VALUES (901, 1, 'key_reply_del', 0, 1, 100090, 0, 'Nice picture!');

            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                key_id TEXT,
                sender_jid_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            INSERT INTO message_quoted VALUES (901, 'target_photo_key', 2, 0, 100085, 1, '');
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
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
    reply_msg = extracted[0]
    assert reply_msg["stanza_id"] == "key_reply_del"
    assert reply_msg["quoted_stanza_id"] == "target_photo_key"
    assert reply_msg["quoted_parent_type"] == "image"
    assert reply_msg["quoted_text"] == "📷 Photo"


def test_diff_and_extract_android_calls(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(arch_db) as conn:
        conn.execute("INSERT INTO contacts VALUES ('15557777', 'Caller_Folder', 'Bob Contact')")

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES
                (1, '15557777', 's.whatsapp.net', '15557777@s.whatsapp.net'),
                (2, 'group_call', 'g.us', 'group_call@g.us'),
                (3, '15558888', 's.whatsapp.net', '15558888@s.whatsapp.net');

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
            INSERT INTO call_log VALUES
                (1, 'call_1on1_del', 1, 0, 1600000000, 1, 142, 5, 524288, 0),
                (2, 'call_grp_del', 1, 1, 1600001000, 0, 310, 5, 1048576, 2);

            CREATE TABLE call_log_participant_v2 (
                _id INTEGER PRIMARY KEY,
                call_log_row_id INTEGER,
                jid_row_id INTEGER,
                call_result INTEGER
            );
            INSERT INTO call_log_participant_v2 VALUES
                (1, 2, 1, 5),
                (2, 2, 3, 5);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT
            );
        """)

    extracted_calls = preservation.diff_and_extract_android_calls(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted_calls) == 2
    by_id = {c["call_id"]: c for c in extracted_calls}

    c1 = by_id["call_1on1_del"]
    assert c1["caller_jid"] == "15557777@s.whatsapp.net"
    assert c1["caller_name"] == "Bob Contact"
    assert c1["is_video"] == 1
    assert c1["duration_seconds"] == 142
    assert c1["call_result"] == 5
    assert c1["is_group_call"] == 0

    c2 = by_id["call_grp_del"]
    assert c2["is_group_call"] == 1
    assert c2["chat_jid"] == "group_call@g.us"
    assert c2["duration_seconds"] == 310
    assert c2["participants"] is not None

    import json

    parts = json.loads(c2["participants"])
    assert len(parts) == 2
    assert parts[0]["jid"] == "15557777@s.whatsapp.net"
    assert parts[0]["name"] == "Bob Contact"


def test_diff_and_extract_android_deleted_group_members_roster(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(arch_db) as conn:
        conn.execute("INSERT INTO contacts VALUES ('15551111', 'Admin_F', 'Alice Boss')")
        conn.execute("INSERT INTO contacts VALUES ('15552222', 'Member_F', 'Charlie Member')")

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            INSERT INTO jid VALUES
                (10, 'mygroup', 'g.us', 'mygroup@g.us'),
                (11, '15551111', 's.whatsapp.net', '15551111@s.whatsapp.net'),
                (12, '15552222', 's.whatsapp.net', '15552222@s.whatsapp.net');

            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, created_timestamp INTEGER);
            INSERT INTO chat VALUES (1, 10, 'Project X Team', 1590000000);

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
            INSERT INTO message VALUES (1001, 1, 'key_grp_m1', 0, 11, 1590001000, 0, 'Welcome to Project X');

            CREATE TABLE group_participant_user (
                _id INTEGER PRIMARY KEY,
                group_jid_row_id INTEGER,
                user_jid_row_id INTEGER,
                rank INTEGER
            );
            INSERT INTO group_participant_user VALUES
                (1, 10, 11, 2),
                (2, 10, 12, 0);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
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
    assert extracted[0]["preservation_reason"] == "chat_deleted"
    assert extracted[0].get("_chat_members_json") is not None

    import json

    members = json.loads(extracted[0]["_chat_members_json"])
    assert len(members) == 2
    assert members[0]["name"] == "Alice Boss"
    assert members[0]["rank"] == 2
    assert members[1]["name"] == "Charlie Member"
    assert members[1]["rank"] == 0

    # Also verify recording to preservation db populates preserved_chats with members_json
    output_dir = tmp_path / "output"
    os.makedirs(output_dir, exist_ok=True)
    with preservation.open_preservation_db(str(output_dir)) as pres_conn:
        res = preservation.record_preserved_messages(pres_conn, extracted, "android", _TEST_LOGGER)
        assert res.messages == 1
        assert res.chats == 1

        chat_row = pres_conn.execute(
            "SELECT subject, members_json FROM preserved_chats WHERE chat_id = '1'"
        ).fetchone()
        assert chat_row is not None
        assert chat_row[0] == "Project X Team"
        saved_members = json.loads(chat_row[1])
        assert len(saved_members) == 2


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


def test_diff_and_extract_ios_location_and_view_once(tmp_path: Path) -> None:
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
            INSERT INTO ZWACHATSESSION VALUES (1, '15551234@s.whatsapp.net', 'Alice Partner', NULL);

            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZFILESIZE INTEGER,
                ZMOVIEDURATION INTEGER,
                ZLATITUDE REAL,
                ZLONGITUDE REAL,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT,
                ZVCARDSTRING TEXT
            );
            INSERT INTO ZWAMEDIAITEM VALUES
                (10, NULL, NULL, 40.7128, -74.0060, NULL, 'https://maps.apple.com/?q=40.7128,-74.0060', 'Statue of Liberty', NULL),
                (11, 307200, 0, NULL, NULL, 'Media/photo_vo.jpg', NULL, 'photo_vo.jpg', NULL),
                (12, 1536000, 14, NULL, NULL, 'Media/video_vo.mp4', NULL, 'video_vo.mp4', NULL);

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
                (101, 1, 'ios_loc', 0, '15551234@s.whatsapp.net', 1000.0, 5, 'Check location', 'Alice', 10, NULL),
                (102, 1, 'ios_vo_photo', 0, '15551234@s.whatsapp.net', 1001.0, 38, 'View once photo', 'Alice', 11, NULL),
                (103, 1, 'ios_vo_video', 0, '15551234@s.whatsapp.net', 1002.0, 39, 'View once video', 'Alice', 12, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
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
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert len(by_key) == 3

    loc = by_key["ios_loc"]
    assert loc["media_type"] == "location"
    assert loc["latitude"] == 40.7128
    assert loc["longitude"] == -74.0060
    assert loc["location_name"] == "Statue of Liberty"
    assert loc["location_url"] == "https://maps.apple.com/?q=40.7128,-74.0060"

    p_vo = by_key["ios_vo_photo"]
    assert p_vo["is_view_once"] == 1
    assert p_vo["media_type"] == "image"
    assert p_vo["media_size"] == 307200

    v_vo = by_key["ios_vo_video"]
    assert v_vo["is_view_once"] == 1
    assert v_vo["media_type"] == "video"
    assert v_vo["media_duration"] == 14
    assert v_vo["media_size"] == 1536000


def test_diff_and_extract_ios_quoted_placeholder(tmp_path: Path) -> None:
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
            INSERT INTO ZWACHATSESSION VALUES (1, '15555678@s.whatsapp.net', 'Bob Partner', NULL);

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
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (201, 1, 'parent_photo_stanza', 0, '15555678@s.whatsapp.net', 1000.0, 1, '', 'Bob', NULL, NULL, NULL),
                (202, 1, 'reply_stanza', 1, NULL, 1005.0, 0, 'Awesome photo!', 'You', NULL, NULL, 201);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
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
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "reply_stanza" in by_key
    rep = by_key["reply_stanza"]
    assert rep["quoted_stanza_id"] == "parent_photo_stanza"
    assert rep["quoted_parent_type"] == "image"
    assert rep["quoted_text"] == "📷 Photo"


def test_diff_and_extract_ios_polls_and_reactions(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    # Construct synthetic protobuf blobs for poll and reaction
    # Poll: question "Pizza?", options "Yes" / "No", 1 vote for "Yes"
    opt1 = bytes([0x0A, 3]) + b"Yes"
    opt2 = bytes([0x0A, 2]) + b"No"
    p_blob = (
        bytes([0x12, 6])
        + b"Pizza?"
        + bytes([0x1A, len(opt1)])
        + opt1
        + bytes([0x1A, len(opt2)])
        + opt2
        + bytes([0x2A, 2, 0x08, 0])
    )
    poll_blob = bytes([0x42, len(p_blob)]) + p_blob

    # Reaction: 👍 (F0 9F 91 8D)
    rx_blob = bytes([0x3A, 8, 0x0A, 6, 0x1A, 4, 0xF0, 0x9F, 0x91, 0x8D])

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, '15553333@s.whatsapp.net', 'Charlie', NULL);

            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZRECEIPTINFO BLOB
            );

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
                ZGROUPMEMBER INTEGER,
                ZMESSAGEINFO INTEGER
            );
            INSERT INTO ZWAMESSAGE VALUES
                (301, 1, 'ios_poll_msg', 0, '15553333@s.whatsapp.net', 1000.0, 46, 'Pizza?', 'Charlie', NULL, NULL, 501),
                (302, 1, 'ios_rx_msg', 0, '15553333@s.whatsapp.net', 1001.0, 0, 'Great idea', 'Charlie', NULL, NULL, 502);
        """)
        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (501, 301, ?)", (poll_blob,))
        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (502, 302, ?)", (rx_blob,))

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
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
                ZGROUPMEMBER INTEGER,
                ZMESSAGEINFO INTEGER
            );
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert len(by_key) == 2

    poll_msg = by_key["ios_poll_msg"]
    assert poll_msg["media_type"] == "poll"
    assert poll_msg["poll_data"] is not None

    import json

    p_dict = json.loads(poll_msg["poll_data"])
    assert p_dict["total_votes"] == 1
    assert p_dict["options"][0]["name"] == "Yes"
    assert p_dict["options"][0]["votes"] == 1
    assert p_dict["options"][1]["name"] == "No"
    assert p_dict["options"][1]["votes"] == 0

    rx_msg = by_key["ios_rx_msg"]
    assert rx_msg["reactions"] == "👍"


def test_diff_and_extract_ios_calls(tmp_path: Path) -> None:
    old_dir = tmp_path / "old_backup"
    new_dir = tmp_path / "new_backup"
    os.makedirs(old_dir, exist_ok=True)
    os.makedirs(new_dir, exist_ok=True)

    old_db = old_dir / "ChatStorage.sqlite"
    new_db = new_dir / "ChatStorage.sqlite"
    old_call_db = old_dir / "CallHistory.sqlite"
    new_call_db = new_dir / "CallHistory.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_call_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZINCOMING INTEGER,
                ZVIDEO INTEGER
            );
            INSERT INTO ZWAAGGREGATECALLEVENT VALUES
                (1, 1, 0),
                (2, 0, 1);

            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT,
                ZGROUPJIDSTRING TEXT,
                ZDATE REAL,
                ZDURATION INTEGER,
                ZOUTCOME INTEGER,
                ZBYTESRECEIVED INTEGER,
                ZBYTESSENT INTEGER,
                ZAGGREGATECALLEVENT INTEGER
            );
            INSERT INTO ZWACDCALLEVENT VALUES
                (10, 'call_ios_1', NULL, 1000.0, 75, 5, 100000, 50000, 1),
                (20, 'call_ios_grp', 'ios_group@g.us', 1010.0, 180, 5, 500000, 200000, 2);

            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER,
                ZJIDSTRING TEXT,
                ZOUTCOME INTEGER
            );
            INSERT INTO ZWACDCALLEVENTPARTICIPANT VALUES
                (1, 10, '15554444@s.whatsapp.net', 5),
                (2, 20, '15554444@s.whatsapp.net', 5),
                (3, 20, '15555555@s.whatsapp.net', 5);
        """)

    with sqlite3.connect(new_call_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT
            );
        """)

    extracted_calls = preservation.diff_and_extract_ios_calls(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted_calls) == 2
    by_id = {c["call_id"]: c for c in extracted_calls}

    c1 = by_id["call_ios_1"]
    assert c1["caller_jid"] == "15554444@s.whatsapp.net"
    assert c1["duration_seconds"] == 75
    assert c1["from_me"] == 0
    assert c1["is_video"] == 0
    assert c1["is_group_call"] == 0

    c2 = by_id["call_ios_grp"]
    assert c2["is_group_call"] == 1
    assert c2["chat_jid"] == "ios_group@g.us"
    assert c2["duration_seconds"] == 180
    assert c2["from_me"] == 1
    assert c2["is_video"] == 1
    assert c2["participants"] is not None

    import json

    parts = json.loads(c2["participants"])
    assert len(parts) == 2
    assert parts[0]["jid"] == "15554444@s.whatsapp.net"


def test_diff_and_extract_ios_deleted_group_roster(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT
            );
            INSERT INTO ZWAGROUPINFO VALUES (1, 1000.0, '15556666@s.whatsapp.net');

            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (10, 'ios_team@g.us', 'iOS Dream Team', 1);

            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT,
                ZFIRSTNAME TEXT,
                ZISACTIVE INTEGER,
                ZISADMIN INTEGER
            );
            INSERT INTO ZWAGROUPMEMBER VALUES
                (1, 10, '15556666@s.whatsapp.net', 'Captain Steve', 'Steve', 1, 2),
                (2, 10, '15557777@s.whatsapp.net', 'Tony Member', 'Tony', 1, 0),
                (3, 10, '15558888@s.whatsapp.net', 'Departed Member', 'Departed', 0, 0);

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
                (501, 10, 'ios_grp_m1', 0, '15556666@s.whatsapp.net', 1000.0, 0, 'Welcome Team', 'Steve', NULL, NULL);
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            );
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
        """)

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    assert extracted[0]["preservation_reason"] == "chat_deleted"
    assert extracted[0].get("_chat_members_json") is not None

    import json

    members = json.loads(extracted[0]["_chat_members_json"])
    # Departed member (ZISACTIVE = 0) must be excluded
    assert len(members) == 2
    assert members[0]["jid"] == "15556666@s.whatsapp.net"
    assert members[0]["name"] == "Captain Steve"
    assert members[0]["rank"] == 2
    assert members[1]["jid"] == "15557777@s.whatsapp.net"
    assert members[1]["name"] == "Tony Member"
    assert members[1]["rank"] == 0

    # Verify recording populates preserved_chats with members_json
    output_dir = tmp_path / "output_ios_roster"
    os.makedirs(output_dir, exist_ok=True)
    with preservation.open_preservation_db(str(output_dir)) as pres_conn:
        res = preservation.record_preserved_messages(pres_conn, extracted, "ios", _TEST_LOGGER)
        assert res.messages == 1
        assert res.chats == 1

        chat_row = pres_conn.execute(
            "SELECT subject, members_json FROM preserved_chats WHERE chat_id = '10'"
        ).fetchone()
        assert chat_row is not None
        assert chat_row[0] == "iOS Dream Team"
        saved_members = json.loads(chat_row[1])
        assert len(saved_members) == 2


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


def test_decrypt_msgstore_preservation_lifecycle_and_rollback(tmp_path: Path, monkeypatch) -> None:
    """Verify that _decrypt_msgstore_if_needed manages .old lifecycle and safely rolls back on failure."""
    import argparse
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

    import pytest

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
    import argparse

    import pytest
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


def test_diff_indexes_creation_and_chunked_progress_android(tmp_path: Path) -> None:
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_a');
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Group');
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
        # Insert 12,000 rows (exceeds single chunk minimum of 5,000)
        rows_old = [(i, 1, f"key_{i}", 0, 1, 100000 + i, 0, f"msg {i}") for i in range(1, 12001)]
        conn.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows_old)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, 'user_a');
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            INSERT INTO chat VALUES (1, 1, 'Group');
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
        # Omit key_500 (deleted) and make key_600 revoked (message_type=15)
        rows_new = [
            (
                i,
                1,
                f"key_{i}",
                0,
                1,
                100000 + i,
                15 if i == 600 else 0,
                "" if i == 600 else f"msg {i}",
            )
            for i in range(1, 12001)
            if i != 500
        ]
        conn.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows_new)

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "key_500" in by_key
    assert by_key["key_500"]["preservation_reason"] == "deleted_on_phone"
    assert "key_600" in by_key
    assert by_key["key_600"]["preservation_reason"] == "revoked_by_sender"

    # Verify that performance indexes were created
    with sqlite3.connect(new_db) as conn:
        indexes = {
            r[1] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_pres_cur_key_id" in indexes


def test_diff_indexes_creation_and_chunked_progress_ios(tmp_path: Path) -> None:
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

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
        """)
        rows_old = [
            (
                i,
                1,
                f"ios_key_{i}",
                0,
                "partner@s.whatsapp.net",
                1000.0 + i,
                0,
                f"msg {i}",
                "Partner",
                None,
                None,
            )
            for i in range(1, 12001)
        ]
        conn.executemany(
            "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows_old
        )

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
        rows_new = [
            (
                i,
                1,
                f"ios_key_{i}",
                0,
                "partner@s.whatsapp.net",
                1000.0 + i,
                14 if i == 700 else 0,
                "" if i == 700 else f"msg {i}",
                "Partner",
                None,
                None,
            )
            for i in range(1, 12001)
            if i != 500
        ]
        conn.executemany(
            "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows_new
        )

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    by_key = {r["stanza_id"]: r for r in extracted}
    assert "ios_key_500" in by_key
    assert by_key["ios_key_500"]["preservation_reason"] == "deleted_on_phone"
    assert "ios_key_700" in by_key
    assert by_key["ios_key_700"]["preservation_reason"] == "revoked_by_sender"

    # Verify that performance indexes were created
    with sqlite3.connect(new_db) as conn:
        indexes = {
            r[1] for r in conn.execute("SELECT * FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_pres_cur_stanza" in indexes


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


def test_diff_and_extract_android_link_preview(tmp_path: Path) -> None:
    """Verify Android link preview metadata in message_text is preserved in service_metadata."""
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    for p in (old_db, new_db):
        with sqlite3.connect(p) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                    sender_jid_row_id INTEGER, key_id TEXT, timestamp INTEGER,
                    message_type INTEGER, text_data TEXT
                );
                CREATE TABLE message_text (
                    message_row_id INTEGER PRIMARY KEY,
                    description TEXT,
                    page_title TEXT,
                    url TEXT,
                    preview_type INTEGER
                );
                INSERT INTO jid VALUES (1, 'alice', 'alice@s.whatsapp.net');
                INSERT INTO chat VALUES (1, 1);
            """)

    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            INSERT INTO message VALUES (1, 1, 0, 1, 'key_link_1', 1000, 0, 'Check this link')
        """)
        conn.execute("""
            INSERT INTO message_text VALUES (1, 'Page description', 'Page Title', 'https://example.com', 1)
        """)
        conn.commit()

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    meta = json.loads(extracted[0]["service_metadata"])
    assert "link_preview" in meta
    assert meta["link_preview"]["title"] == "Page Title"
    assert meta["link_preview"]["desc"] == "Page description"
    assert meta["link_preview"]["url"] == "https://example.com"
    assert meta["link_preview"]["preview_type"] == 1


def test_diff_and_extract_ios_link_preview(tmp_path: Path) -> None:
    """Verify iOS link preview metadata in ZWAMEDIAITEM is preserved in service_metadata."""
    old_db = tmp_path / "old_chatstorage.sqlite"
    new_db = tmp_path / "new_chatstorage.sqlite"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    for p in (old_db, new_db):
        with sqlite3.connect(p) as conn:
            conn.executescript("""
                CREATE TABLE ZWACHATSESSION (
                    Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER
                );
                CREATE TABLE ZWAMEDIAITEM (
                    Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZMEDIALOCALPATH TEXT, ZMETADATA BLOB
                );
                CREATE TABLE ZWAMESSAGE (
                    Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZSTANZAID TEXT,
                    ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGEDATE REAL,
                    ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZPUSHNAME TEXT,
                    ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER
                );
                INSERT INTO ZWACHATSESSION VALUES (1, 'partner@s.whatsapp.net', 'Partner', NULL);
            """)

    with sqlite3.connect(old_db) as conn:
        desc_bytes = b"iOS page snippet"
        metadata_blob = b"\x1a" + bytes([len(desc_bytes)]) + desc_bytes
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 'iOS Title', NULL, ?)",
            (metadata_blob,),
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE VALUES (1, 1, 'ios_link_key', 0, 'partner@s.whatsapp.net', 1000.0, 7, 'https://example.org', 'Partner', 1, NULL)
        """)
        conn.commit()

    extracted = preservation.diff_and_extract_ios(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 1
    meta = json.loads(extracted[0]["service_metadata"])
    assert "link_preview" in meta
    assert meta["link_preview"]["title"] == "iOS Title"
    assert meta["link_preview"]["desc"] == "iOS page snippet"
    assert meta["link_preview"]["url"] == "https://example.org"
