"""Tests for wab_archiver.preservation."""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_TEST_LOGGER = logging.getLogger("test_preservation")
_TEST_LOGGER.setLevel(logging.INFO)

from tests.preservation.conftest import _make_dummy_archive_db
from wab_archiver import preservation


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


def test_diff_and_extract_android_stubs_and_tombstones_not_preserved(tmp_path: Path) -> None:
    old_db = tmp_path / "old_stubs.db"
    new_db = tmp_path / "new_stubs.db"
    arch_db = tmp_path / "arch_stubs.db"
    _make_dummy_archive_db(arch_db)

    with sqlite3.connect(old_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, '390000000001'), (2, '390000000002');

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
                (101, 1, 'key_sys_stub_transition', 1, 0, 100000, 7, NULL),
                (102, 1, 'key_empty_text_transition', 1, 0, 100001, 0, ''),
                (103, 1, 'key_revoked_purged', 1, 0, 100002, 15, ''),
                (104, 1, 'key_empty_sys_purged', 1, 0, 100003, 7, NULL),
                (105, 1, 'key_real_revoked', 1, 0, 100004, 0, 'Real message content');
        """)

    with sqlite3.connect(new_db) as conn:
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
            INSERT INTO jid VALUES (1, '390000000001'), (2, '390000000002');

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
                (201, 1, 'key_sys_stub_transition', 1, 0, 100000, 15, ''),
                (202, 1, 'key_empty_text_transition', 1, 0, 100001, 15, ''),
                (205, 1, 'key_real_revoked', 1, 0, 100004, 15, '');
        """)

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )

    by_key = {r["stanza_id"]: r for r in extracted}

    # Stubs that transitioned from type 7 or empty text to 15 must NOT be preserved
    assert "key_sys_stub_transition" not in by_key
    assert "key_empty_text_transition" not in by_key

    # Already-revoked stubs or empty system messages that were purged must NOT be preserved as deleted_on_phone
    assert "key_revoked_purged" not in by_key
    assert "key_empty_sys_purged" not in by_key

    # Legitimate message with real text that was revoked MUST be preserved
    assert "key_real_revoked" in by_key
    assert by_key["key_real_revoked"]["text_body"] == "Real message content"
    assert by_key["key_real_revoked"]["preservation_reason"] == "revoked_by_sender"


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
        assert "idx_message_key_id" in indexes
        assert "idx_pres_cur_key_id" not in indexes


def test_diff_and_extract_android_link_preview(tmp_path: Path) -> None:
    """Verify Android link preview metadata and thumbnail in message_text/message_thumbnail are preserved."""
    old_db = tmp_path / "old_msgstore.db"
    new_db = tmp_path / "new_msgstore.db"
    arch_db = tmp_path / "arch.db"
    _make_dummy_archive_db(arch_db)

    dummy_thumb = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"

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
                CREATE TABLE message_thumbnail (
                    message_row_id INTEGER PRIMARY KEY,
                    thumbnail BLOB
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
        conn.execute(
            "INSERT INTO message_thumbnail VALUES (1, ?)",
            (dummy_thumb,),
        )
        # Message 2 has null URL in message_text, relies on regex fallback from text_data
        conn.execute("""
            INSERT INTO message VALUES (2, 1, 0, 1, 'key_link_2', 1001, 0, 'See https://github.com/test')
        """)
        conn.execute("""
            INSERT INTO message_text VALUES (2, 'Git repo', 'GitHub', NULL, 0)
        """)
        conn.commit()

    extracted = preservation.diff_and_extract_android(
        str(old_db), str(new_db), str(arch_db), _TEST_LOGGER
    )
    assert len(extracted) == 2
    row1 = next(r for r in extracted if r["stanza_id"] == "key_link_1")
    meta1 = json.loads(row1["service_metadata"])
    assert "link_preview" in meta1
    assert meta1["link_preview"]["title"] == "Page Title"
    assert meta1["link_preview"]["desc"] == "Page description"
    assert meta1["link_preview"]["url"] == "https://example.com"
    assert meta1["link_preview"]["preview_type"] == 1
    assert "thumbnail_b64" in meta1["link_preview"]
    assert base64.b64decode(meta1["link_preview"]["thumbnail_b64"]) == dummy_thumb

    row2 = next(r for r in extracted if r["stanza_id"] == "key_link_2")
    meta2 = json.loads(row2["service_metadata"])
    assert "link_preview" in meta2
    assert meta2["link_preview"]["title"] == "GitHub"
    assert meta2["link_preview"]["desc"] == "Git repo"
    assert meta2["link_preview"]["url"] == "https://github.com/test"
    assert meta2["link_preview"]["preview_type"] == 0


def test_preservation_retains_waveform(tmp_path: Path) -> None:
    """Verify that voice message waveform BLOB is correctly stored in preserved_messages."""
    pres_conn = preservation.open_preservation_db(str(tmp_path))
    try:
        dummy_wave = bytes([i % 100 for i in range(64)])
        voice_msg = {
            "chat_id": "chat_voice",
            "stanza_id": "WAVE_PRESERVE_1",
            "msg_id": 999,
            "timestamp_ms": 1700000000000,
            "from_me": 0,
            "text_body": "Voice message",
            "media_type": "audio",
            "media_name": "PTT.opus",
            "archive_path": "WhatsApp Voice Notes/PTT.opus",
            "waveform": dummy_wave,
            "preservation_reason": "deleted_on_phone",
        }
        res = preservation.record_preserved_messages(
            pres_conn, [voice_msg], "android", _TEST_LOGGER
        )
        assert res.messages == 1

        row = pres_conn.execute(
            "SELECT stanza_id, waveform FROM preserved_messages WHERE stanza_id = 'WAVE_PRESERVE_1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "WAVE_PRESERVE_1"
        assert bytes(row[1]) == dummy_wave
    finally:
        pres_conn.close()
