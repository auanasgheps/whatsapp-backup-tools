"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import sqlite3
import time

from tests.conftest import (
    make_android_db,
    make_archive_db,
    make_ios_db,
    seed_android_db,
)
from tests.fixtures.protobuf_factory import (
    encode_varint,
    lid_field1_bytes,
    make_compact_receipt_blob,
    make_ios_reaction_blob,
    make_receipt_blob,
    make_zreceipt_blob_with_reactions,
)
from tests.viewer.conftest import (
    _get_app_css,
    _get_app_js,
    _seed_android_db_large,
)
from wab_viewer import main as viewer
from wab_viewer.queries import sql_builder as viewer_sql_builder

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


class TestRecentMessagesSchema:
    def test_recent_messages_table_created(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = make_archive_db(archive_path)
        conn.close()

        conn = sqlite3.connect(str(archive_path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(recent_messages)")}
        finally:
            conn.close()
        expected = {
            "chat_id",
            "chat_type",
            "msg_id",
            "timestamp_ms",
            "sender",
            "from_me",
            "archive_path",
            "media_type",
            "media_name",
            "text_body",
            "quoted_text",
            "quoted_sender",
            "quoted_ts",
            "is_edited",
            "edited_ts",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_recent_messages_pk_replace(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = make_archive_db(archive_path)

        conn.execute("""
            INSERT INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
            VALUES ('1', 'contact', 1, 1000, 'Alice', 0, NULL, 'text', '', 'Hello')
        """)
        conn.commit()

        # Same PK: REPLACE, not duplicate
        conn.execute("""
            INSERT OR REPLACE INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
            VALUES ('1', 'contact', 1, 2000, 'Bob', 1, NULL, 'text', '', 'Updated')
        """)
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM recent_messages WHERE chat_id='1' AND msg_id=1"
        ).fetchone()[0]
        assert count == 1
        sender = conn.execute(
            "SELECT sender FROM recent_messages WHERE chat_id='1' AND msg_id=1"
        ).fetchone()[0]
        assert sender == "Bob"  # replaced, not duplicated

    def test_recent_messages_index_exists(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        make_archive_db(archive_path)

        conn = sqlite3.connect(str(archive_path))
        try:
            idxs = [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='recent_messages'"
                )
            ]
        finally:
            conn.close()
        assert any("timestamp_ms" in (sql or "") for _, sql in idxs)


class TestRecentMessagesRouting:
    def test_initial_load_served_from_recent_messages(self, tmp_path):
        """When recent_messages has rows, /api/messages returns them directly (no WA DB)."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        # Pre-populate recent_messages with known sender names (distinct from WA DB)
        for i in range(10):
            archive_conn.execute(
                """
                INSERT OR REPLACE INTO recent_messages
                (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
                VALUES (?, 'contact', ?, ?, 'FROM_CACHE', 0, NULL, 'text', '', ?)
            """,
                ("111111111", i + 1, 1700000000000 + i, f"cached msg {i}"),
            )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=111111111&chat_type=contact&limit=50")
        data = resp.get_json()

        assert resp.status_code == 200
        assert len(data) == 10
        # All messages have sender='FROM_CACHE' — proves they came from recent_messages, not WA DB
        assert all(r["sender"] == "FROM_CACHE" for r in data)
        # WA DB sender would be '+111111111', so this check is definitive

    def test_fallback_and_backfill(self, tmp_path):
        """When recent_messages is empty, /api/messages falls back to WA DB and backfills."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=111111111&chat_type=contact&limit=50")
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data) == 50  # all 50 messages for chat_row_id=1

        # Wait for the daemon backfill thread to finish
        time.sleep(0.3)

        # recent_messages should now be backfilled
        archive_conn2 = make_archive_db(archive_path)
        count = archive_conn2.execute(
            "SELECT COUNT(*) FROM recent_messages WHERE chat_id='111111111' AND chat_type='contact'"
        ).fetchone()[0]
        archive_conn2.close()
        assert count == 50

    def test_pagination_hits_wa_db(self, tmp_path):
        """Scroll-up (?before=) always hits WA DB regardless of recent_messages content."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        # Pre-populate recent_messages with the 5 OLDEST messages (timestamps 1700000000000..)
        # WA DB will return the 50 NEWEST messages (timestamps 170000045000..) — no overlap
        for i in range(5):
            archive_conn.execute(
                """
                INSERT OR REPLACE INTO recent_messages
                (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
                VALUES (?, 'contact', ?, ?, 'FROM_CACHE', 0, NULL, 'text', '', ?)
            """,
                ("111111111", 100 + i, 1700000000000 + i * 1000, f"cached {i}"),
            )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            # ?before=9999999999999 orders all messages DESC and returns the newest 50.
            # recent_messages has the oldest 5 messages (100..104, ts 1700000000000..170000004000)
            # WA DB returns the newest 50 (100..149, ts 170000045000..170000049000) — no overlap.
            # Sender for WA DB rows is '+111111111', not 'FROM_CACHE'.
            resp = client.get(
                "/api/messages?chat_id=111111111&chat_type=contact&limit=50&before=9999999999999"
            )
        data = resp.get_json()
        assert len(data) > 0, "pagination should hit WA DB and return messages"
        assert all(r["sender"] != "FROM_CACHE" for r in data), (
            "pagination returned cached messages — WA DB was not used"
        )

    def test_recent_messages_response_schema(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=111111111&chat_type=contact&limit=50")
        data = resp.get_json()
        assert len(data) > 0
        expected_keys = {
            "msg_id",
            "chat_id",
            "chat_type",
            "timestamp_ms",
            "sender",
            "from_me",
            "archive_path",
            "media_type",
            "media_name",
            "text_body",
            "quoted_text",
            "quoted_sender",
            "quoted_ts",
            "quoted_msg_id",
            "quoted_has_thumb",
            "reactions",
            "is_edited",
            "edited_ts",
            "is_view_once",
            "stanza_id",
            "media_width",
            "media_height",
            "media_size",
            "media_duration",
            "aspect_ratio",
            "has_thumbnail",
            "link_title",
            "link_desc",
            "link_url",
            "link_preview_type",
            "latitude",
            "longitude",
            "location_name",
            "location_address",
            "location_url",
            "live_location_share_duration",
            "is_starred",
            "is_pinned",
            "is_video_note",
            "is_voice_note",
            "waveform",
        }
        assert set(data[0].keys()) == expected_keys


class TestIncrementViewerMessages:
    """Verify viewer displays messages ingested from incremental backups."""

    def test_increment_messages_display_with_lid_and_media(self, tmp_path):
        """Messages added to msgstore with LID remote_jid and null file_path appear in /api/messages."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)

        # Base contact and chat
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '5550100', '5550100@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (2, '100200300', '100200300@lid')"
        )
        wa_conn.execute("INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 1)")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (1, 1, NULL, 0, 1729000000000, 100)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (100, 1, 0, 1, 1729000000000, 'Base message', 0)"
        )
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('5550100', '+5550100', 'Alice')"
        )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        # Step 1: Open viewer and fetch initial messages to populate recent_messages cache
        app1 = viewer.create_app(tmp_path, rescan=False)
        with app1.test_client() as client1:
            resp1 = client1.get("/api/messages?chat_id=5550100&chat_type=contact")
            assert resp1.status_code == 200
            data1 = resp1.get_json()
            assert len(data1) == 1
            assert data1[0]["text_body"] == "Base message"

        # Step 2: Incremental backup replay simulates new messages: text message & media message
        wa_conn2 = sqlite3.connect(str(wa_path))
        wa_conn2.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (101, 1, 0, 2, 1729000050000, 'Increment text via LID', 0)"
        )
        wa_conn2.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (102, 1, 0, 2, 1729000060000, 'Increment photo', 1)"
        )
        wa_conn2.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (102, NULL, 'Increment photo')"
        )
        wa_conn2.execute("UPDATE chat SET sort_timestamp = 1729000060000 WHERE _id = 1")
        wa_conn2.commit()
        wa_conn2.close()

        # Ensure mtime is visibly newer than initial scan
        st = os.stat(str(wa_path))
        os.utime(str(wa_path), (st.st_atime, st.st_mtime + 5))

        # Step 3: Viewer reopened on updated database. recent_messages cache is invalidated and all 3 messages appear.
        app2 = viewer.create_app(tmp_path, rescan=False)
        with app2.test_client() as client2:
            chats_resp = client2.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            assert chats[0]["id"] == "5550100"
            assert chats[0]["newest_ts"] == 1729000060000

            msgs_resp = client2.get("/api/messages?chat_id=5550100&chat_type=contact")
            assert msgs_resp.status_code == 200
            msgs = msgs_resp.get_json()
            assert len(msgs) == 3
            msg_texts = [m["text_body"] for m in msgs]
            assert "Increment photo" in msg_texts
            assert "Increment text via LID" in msg_texts
            assert "Base message" in msg_texts

            # Check media message classification
            photo_msg = next(m for m in msgs if m["msg_id"] == 102)
            assert photo_msg["media_type"] == "image"
            assert photo_msg["sender"] == "Alice"


class TestDeviceSuffixAndCoverageIndex:
    """Verify contact names resolve correctly when JIDs carry :device suffixes,

    and verify messages load using index on chat_row_id without full scans.
    """

    def test_contact_chat_with_device_suffix_displays_and_opens(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        arc_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '393401234567:0', '393401234567:0@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, NULL, 0, 1729000000000, 10)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (10, 1, 0, 1, 1729000000000, 'Hello contact', 0)"
        )
        wa_conn.commit()
        wa_conn.close()

        arc_conn = make_archive_db(arc_path)
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393401234567', '+393401234567', 'Marco')"
        )
        arc_conn.commit()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            # Canonical ID is stripped of :0 and display_name is matched to contact list
            assert chats[0]["id"] == "393401234567"
            assert chats[0]["display_name"] == "Marco"

            # Opening with clean canonical ID works immediately
            msgs_resp1 = client.get("/api/messages?chat_id=393401234567&chat_type=contact")
            assert msgs_resp1.status_code == 200
            msgs1 = msgs_resp1.get_json()
            assert len(msgs1) == 1
            assert msgs1[0]["sender"] == "Marco"

            # Opening with legacy dirty ID also works immediately without hanging
            msgs_resp2 = client.get("/api/messages?chat_id=393401234567:0&chat_type=contact")
            assert msgs_resp2.status_code == 200
            msgs2 = msgs_resp2.get_json()
            assert len(msgs2) == 1
            assert msgs2[0]["sender"] == "Marco"

    def test_group_chat_scroll_past_device_suffix_retains_contact_name(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        arc_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (100, 'group1', 'group1@g.us')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '393401234567', '393401234567@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (2, '393401234567:0', '393401234567:0@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (10, 100, 'Test Group', 0, 1729000020000, 12)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (11, 10, 0, 1, 1729000010000, 'Older message with clean JID', 0)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (12, 10, 0, 2, 1729000020000, 'Newer message with device suffix', 0)"
        )
        wa_conn.commit()
        wa_conn.close()

        arc_conn = make_archive_db(arc_path)
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393401234567', '+393401234567', 'Marco')"
        )
        arc_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        arc_conn.commit()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            # Both older and newer messages must resolve to Marco, never lost or replaced by WhatsApp ID
            for m in msgs:
                assert m["sender"] == "Marco"

    def test_android_chat_filter_query_plan_uses_index(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON message (chat_row_id, timestamp)"
        )
        wa_conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)"
        )
        wa_conn.commit()

        pred, params = viewer_sql_builder._android_chat_filter("393401234567", "contact")
        plan = wa_conn.execute(
            f"EXPLAIN QUERY PLAN SELECT m._id FROM message m WHERE {pred} ORDER BY m.timestamp DESC LIMIT 50",
            params,
        ).fetchall()
        wa_conn.close()

        plan_str = " ".join(" ".join(str(val) for val in tuple(row)) for row in plan)
        # Verifies the query plan uses index seek on chat_row_id rather than full SCAN on message
        assert "idx_msg_chat_ts" in plan_str or "SEARCH" in plan_str


class TestQuotedMessageResolution:
    def test_android_quoted_message_resolves_quoted_msg_id_and_ts(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY,
                user TEXT NOT NULL,
                server TEXT,
                raw_string TEXT NOT NULL
            );
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER NOT NULL,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                file_path TEXT,
                media_name TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                parent_message_chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                sender_jid_row_id INTEGER,
                key_id TEXT NOT NULL,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );

            INSERT INTO jid VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net');
            INSERT INTO chat VALUES (10, 1, NULL, 0, 1700000010000, 2);

            -- Message 1: Original message at ts 1700000000000
            INSERT INTO message VALUES (1, 10, 0, 'KEY_ORIG_1', 1, 1700000000000, 'Original text', 0);

            -- Message 2: Reply quoting Message 1, but with timestamp=0 in message_quoted
            INSERT INTO message VALUES (2, 10, 1, 'KEY_REPLY_2', 1, 1700000010000, 'Reply text', 0);
            INSERT INTO message_quoted VALUES (2, 10, 10, 0, 1, 'KEY_ORIG_1', 0, 0, 'Original text');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 2)
            assert reply_msg["quoted_msg_id"] == 1
            assert reply_msg["quoted_ts"] == 1700000000000
            assert reply_msg["quoted_text"] == "Original text"

    def test_ios_quoted_message_resolves_quoted_msg_id_and_media_label(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Bob');

            INSERT INTO ZWAMEDIAITEM VALUES (1, 'photo.jpg', 'photo.jpg');

            -- Message 1: Photo message without caption
            INSERT INTO ZWAMESSAGE VALUES (10, 1, 0, 700000000.0, 'STANZA_PHOTO', 1, 1, NULL, NULL, 'Bob', NULL, '15559876543@s.whatsapp.net');

            -- Message 2: Quoted reply pointing to Message 1
            INSERT INTO ZWAMESSAGE VALUES (11, 1, 1, 700000010.0, 'STANZA_REPLY', 0, NULL, NULL, 10, 'You', 'Great shot!', '15559876543@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 11)
            assert reply_msg["quoted_msg_id"] == 10
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)
            assert reply_msg["quoted_text"] == "📷 Photo"


class TestEditedMessages:
    def test_android_messages_with_message_edit_info(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("""
            CREATE TABLE message_edit_info (
                message_row_id INTEGER PRIMARY KEY,
                original_key_id TEXT,
                edited_timestamp INTEGER,
                sender_timestamp INTEGER
            );
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '12345678')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (101, 1, 0, 1700000000000, 'Original text', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (102, 1, 1, 1700000010000, 'Edited text', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp)
            VALUES (102, 'orig-key-102', 1700000025000, 1700000025000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=12345678&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert msgs[101]["is_edited"] == 0
            assert msgs[101]["edited_ts"] is None
            assert msgs[102]["is_edited"] == 1
            assert msgs[102]["edited_ts"] == 1700000025000

    def test_android_messages_fallback_message_add_on(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                timestamp INTEGER,
                message_add_on_type INTEGER
            );
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '12345678')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (201, 1, 0, 1700000000000, 'Regular message', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (202, 1, 1, 1700000010000, 'Add-on edited message', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_add_on (_id, parent_message_row_id, from_me, timestamp, message_add_on_type)
            VALUES (1, 202, 1, 1700000030000, 74)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=12345678&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert msgs[201]["is_edited"] == 0
            assert msgs[201]["edited_ts"] is None
            assert msgs[202]["is_edited"] == 1
            assert msgs[202]["edited_ts"] == 1700000030000

    def test_recent_messages_cache_preserves_edit_info(self, tmp_path):
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute("""
            INSERT INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body, is_edited, edited_ts)
            VALUES ('12345678', 'contact', 301, 1700000000000, 'Alice', 0, NULL, 'text', '', 'Cached edit', 1, 1700000045000)
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=12345678&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["msg_id"] == 301
            assert msgs[0]["is_edited"] == 1
            assert msgs[0]["edited_ts"] == 1700000045000

    def test_ios_messages_edit_defaults(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = sqlite3.connect(str(wa_path))
        archive_conn = make_archive_db(archive_path)
        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZFROMJID TEXT,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Partner')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) VALUES (1, 1, 0, 700000000.0, 0, 'iOS message')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_edited"] == 0
            assert msgs[0]["edited_ts"] is None

    def test_messages_at_endpoint_includes_edited(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("""
            CREATE TABLE message_edit_info (
                message_row_id INTEGER PRIMARY KEY,
                original_key_id TEXT,
                edited_timestamp INTEGER,
                sender_timestamp INTEGER
            );
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '99999999')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (401, 1, 0, 1700000000000, 'Original message', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp)
            VALUES (401, 'key-401', 1700000005000, 1700000005000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get(
                "/api/messages/at?chat_id=99999999&chat_type=contact&ts=1700000000000"
            )
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_edited"] == 1
            assert msgs[0]["edited_ts"] == 1700000005000

    def test_frontend_assets_include_msg_edited(self):
        js_content = _get_app_js()
        css_content = _get_app_css()
        assert "msg-edited" in js_content
        assert "msg-edited" in css_content


class TestTrack1Endpoints:
    def test_api_calls_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15551234', 'Alice Whitman', 'Alice (15551234)')"
            )
            arch_conn.commit()

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER);
                CREATE TABLE call_log (
                    _id INTEGER PRIMARY KEY,
                    call_id TEXT,
                    jid_row_id INTEGER,
                    from_me INTEGER,
                    timestamp INTEGER,
                    video_call INTEGER,
                    duration INTEGER,
                    call_result INTEGER,
                    group_jid_row_id INTEGER
                );
                INSERT INTO jid VALUES (1, '15551234', '15551234@s.whatsapp.net');
                INSERT INTO jid VALUES (2, '15559999', '15559999@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, NULL);
                INSERT INTO chat VALUES (20, 2, NULL);

                INSERT INTO call_log VALUES (1, 'call-101', 1, 0, 1793972400000, 1, 765, 1, NULL);
                INSERT INTO call_log VALUES (2, 'call-102', 1, 0, 1793973000000, 0, 0, 2, NULL);
                INSERT INTO call_log VALUES (3, 'call-103', 2, 1, 1793974000000, 0, 120, 1, NULL);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. All calls
            resp = client.get("/api/calls")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"] if isinstance(data, dict) and "calls" in data else data
            assert len(calls) == 3
            c1 = next(c for c in calls if c["id"] == "call-101")
            assert c1["caller_name"] == "Alice Whitman"
            assert c1["caller_phone"] == "+15551234"
            assert c1["is_video"] is True
            assert c1["is_missed"] is False
            assert c1["duration"] == 765

            # 2. Missed calls filter
            resp_missed = client.get("/api/calls?filter=missed")
            assert resp_missed.status_code == 200
            data_missed = resp_missed.get_json()
            calls_missed = (
                data_missed["calls"]
                if isinstance(data_missed, dict) and "calls" in data_missed
                else data_missed
            )
            assert len(calls_missed) == 1
            assert calls_missed[0]["id"] == "call-102"
            assert calls_missed[0]["is_missed"] is True

            # 3. Video calls filter
            resp_video = client.get("/api/calls?filter=video")
            assert resp_video.status_code == 200
            data_video = resp_video.get_json()
            calls_video = (
                data_video["calls"]
                if isinstance(data_video, dict) and "calls" in data_video
                else data_video
            )
            assert len(calls_video) == 1
            assert calls_video[0]["id"] == "call-101"
            assert calls_video[0]["is_video"] is True

    def test_api_calls_ios(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        call_path = tmp_path / "CallHistory.sqlite"

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
                CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
                CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE REAL);
                INSERT INTO ZWACHATSESSION VALUES (1, '15553333@s.whatsapp.net', 'Bob iOS');
            """)
            conn.commit()

        with sqlite3.connect(str(call_path)) as conn:
            conn.executescript("""
                CREATE TABLE ZWACDCALLEVENT (
                    Z_PK INTEGER PRIMARY KEY,
                    ZCALLIDSTRING TEXT,
                    ZDATE REAL,
                    ZDURATION INTEGER,
                    ZBYTESENT INTEGER,
                    ZBYTESRECEIVED INTEGER,
                    ZOUTCOME INTEGER,
                    ZAGGREGATECALLEVENT INTEGER,
                    Z12_AGGREGATECALLEVENT INTEGER
                );
                CREATE TABLE ZWAAGGREGATECALLEVENT (
                    Z_PK INTEGER PRIMARY KEY,
                    ZVIDEO INTEGER,
                    ZINCOMING INTEGER,
                    ZCALLRESULT INTEGER
                );
                CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                    Z_PK INTEGER PRIMARY KEY,
                    Z1PARTICIPANTS INTEGER,
                    ZJIDSTRING TEXT,
                    ZOUTCOME INTEGER
                );

                -- Call 1: Incoming connected video call (ZDATE=700000000 -> 2023)
                INSERT INTO ZWACDCALLEVENT VALUES (1, 'ios-call-1', 700000000.0, 300, 1000, 2000, 1, 1, 1);
                INSERT INTO ZWAAGGREGATECALLEVENT VALUES (1, 1, 1, 1);
                INSERT INTO ZWACDCALLEVENTPARTICIPANT VALUES (1, 1, '15553333@s.whatsapp.net', 1);

                -- Call 2: Incoming missed audio call
                INSERT INTO ZWACDCALLEVENT VALUES (2, 'ios-call-2', 700001000.0, 0, 0, 0, 2, 2, 2);
                INSERT INTO ZWAAGGREGATECALLEVENT VALUES (2, 0, 1, 2);
                INSERT INTO ZWACDCALLEVENTPARTICIPANT VALUES (2, 2, '15554444@s.whatsapp.net', 2);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"] if isinstance(data, dict) and "calls" in data else data
            assert len(calls) == 2

            c1 = next(c for c in calls if c["id"] == "ios-call-1")
            assert c1["caller_name"] == "Bob iOS"
            assert c1["caller_phone"] == "+15553333"
            assert c1["is_video"] is True
            assert c1["is_missed"] is False
            assert c1["duration"] == 300
            assert c1["from_me"] is False

            # Filter missed
            resp_missed = client.get("/api/calls?filter=missed")
            assert resp_missed.status_code == 200
            data_missed = resp_missed.get_json()
            calls_missed = (
                data_missed["calls"]
                if isinstance(data_missed, dict) and "calls" in data_missed
                else data_missed
            )
            assert len(calls_missed) == 1
            assert calls_missed[0]["id"] == "ios-call-2"

    def test_api_calls_with_preserved(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        pres_path = tmp_path / "preserved_messages.db"

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER);
                CREATE TABLE call_log (
                    _id INTEGER PRIMARY KEY, call_id TEXT, jid_row_id INTEGER,
                    from_me INTEGER, timestamp INTEGER, video_call INTEGER,
                    duration INTEGER, call_result INTEGER, group_jid_row_id INTEGER
                );
            """)
            conn.commit()

        with sqlite3.connect(str(pres_path)) as conn:
            conn.executescript("""
                CREATE TABLE preserved_calls (
                    call_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    caller_jid TEXT,
                    caller_name TEXT,
                    timestamp_ms INTEGER,
                    duration_sec INTEGER,
                    video_call INTEGER,
                    from_me INTEGER,
                    call_result INTEGER
                );
                INSERT INTO preserved_calls VALUES (
                    'pres-call-1', '99', '15557777@s.whatsapp.net', 'Ghost Caller',
                    1793979999000, 0, 0, 0, 2
                );
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"] if isinstance(data, dict) and "calls" in data else data
            assert len(calls) == 1
            assert calls[0]["id"] == "pres-call-1"
            assert calls[0]["caller_name"] == "Ghost Caller"
            assert calls[0]["is_missed"] is True

    def test_api_starred_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT,
                    starred INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_name TEXT
                );
                INSERT INTO jid VALUES (1, '15551111', '15551111@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, 'Project Alpha');
                INSERT INTO message VALUES (
                    100, 10, 0, 1, 1793970000000, 'Starred Important Note', 0, 'STANZA_STAR_1', 1
                );
                INSERT INTO message VALUES (
                    101, 10, 0, 1, 1793971000000, 'Regular Note', 0, 'STANZA_REG_1', 0
                );
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # Global starred
            resp = client.get("/api/starred")
            assert resp.status_code == 200
            data = resp.get_json()
            items = data["messages"] if isinstance(data, dict) and "messages" in data else data
            assert len(items) == 1
            assert items[0]["id"] == 100
            assert items[0]["text_body"] == "Starred Important Note"
            assert items[0]["chat_name"] == "Project Alpha"

            # Scoped chat starred
            resp_chat = client.get("/api/chat/10/starred")
            assert resp_chat.status_code == 200
            data_chat = resp_chat.get_json()
            chat_items = (
                data_chat["messages"]
                if isinstance(data_chat, dict) and "messages" in data_chat
                else data_chat
            )
            assert len(chat_items) == 1
            assert chat_items[0]["id"] == 100

            resp_empty = client.get("/api/chat/999/starred")
            assert resp_empty.status_code == 200
            data_empty = resp_empty.get_json()
            empty_items = (
                data_empty["messages"]
                if isinstance(data_empty, dict) and "messages" in data_empty
                else data_empty
            )
            assert len(empty_items) == 0

    def test_api_starred_android_contact_and_lid_chat_id_resolution(self, tmp_path):
        """Android /api/starred must emit canonical chat_id matching /api/chats for 1:1, LID, and group chats."""
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15551234', 'Alice Contact', 'Alice Contact (15551234)')"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15556789', 'Bob LID', 'Bob LID (15556789)')"
            )
            arch_conn.commit()

        with make_android_db(wa_path) as conn:
            conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
            conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
            conn.executescript("""
                INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net');
                INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '999000111222333', 'lid', '999000111222333@lid');
                INSERT INTO jid (_id, user, server, raw_string) VALUES (3, '15556789', 's.whatsapp.net', '15556789@s.whatsapp.net');
                INSERT INTO jid (_id, user, server, raw_string) VALUES (4, '120363000111', 'g.us', '120363000111@g.us');
                INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 3);

                INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (10, 1, NULL, 1793970000000, 0);
                INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (20, 2, NULL, 1793971000000, 0);
                INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (30, 4, 'Weekend Trip', 1793972000000, 0);

                -- 1:1 incoming starred message (sender_jid_row_id = 0, standard Android convention)
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (101, 10, 0, 0, 1793970000000, 'Alice starred note', 0, 'K1', 1);
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (102, 10, 1, 0, 1793970010000, 'Reply to Alice', 0, 'K2', 0);

                -- LID 1:1 outgoing starred message
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (201, 20, 1, 0, 1793971000000, 'Bob LID starred note', 0, 'K3', 1);

                -- Group starred message
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (301, 30, 0, 1, 1793972000000, 'Group starred note', 0, 'K4', 1);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            chats = client.get("/api/chats").get_json()
            chat_keys = {(c["id"], c["type"]): c for c in chats}

            resp = client.get("/api/starred")
            assert resp.status_code == 200
            items = resp.get_json()["messages"]
            assert len(items) == 3

            by_id = {m["id"]: m for m in items}

            # 1:1 standard contact
            m_alice = by_id[101]
            assert m_alice["chat_id"] == "15551234"
            assert m_alice["chat_type"] == "contact"
            assert m_alice["chat_name"] == "Alice Contact"
            assert m_alice["sender_name"] == "Alice Contact"
            assert (m_alice["chat_id"], m_alice["chat_type"]) in chat_keys

            # 1:1 LID contact
            m_bob = by_id[201]
            assert m_bob["chat_id"] == "15556789"
            assert m_bob["chat_type"] == "contact"
            assert m_bob["chat_name"] == "Bob LID"
            assert m_bob["sender_name"] == "You"
            assert (m_bob["chat_id"], m_bob["chat_type"]) in chat_keys

            # Group chat
            m_grp = by_id[301]
            assert m_grp["chat_id"] == "30"
            assert m_grp["chat_type"] == "group"
            assert m_grp["chat_name"] == "Weekend Trip"
            assert m_grp["sender_name"] == "Alice Contact"
            assert (m_grp["chat_id"], m_grp["chat_type"]) in chat_keys

            # Scoped /api/chat/<id>/starred works for canonical contact & LID IDs
            alice_scoped = client.get("/api/chat/15551234/starred").get_json()["messages"]
            assert [m["id"] for m in alice_scoped] == [101]
            bob_scoped = client.get("/api/chat/15556789/starred").get_json()["messages"]
            assert [m["id"] for m in bob_scoped] == [201]

            # Context jump endpoint resolves each starred message cleanly
            for item in items:
                ctx_resp = client.get(
                    f"/api/messages/at?chat_id={item['chat_id']}&chat_type={item['chat_type']}&msg_id={item['id']}&limit=50"
                )
                assert ctx_resp.status_code == 200
                ctx_msgs = ctx_resp.get_json()
                assert any(cm["msg_id"] == item["id"] for cm in ctx_msgs)

    def test_api_starred_ios_document_caption_and_sanitization(self, tmp_path):
        """iOS /api/starred projects captions over filenames, sanitizes tokens, and handles scoped chat resolution."""
        arch_path = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15551234', 'Alice Smith', 'Alice Smith (15551234)')"
            )
            arch_conn.commit()

        cs_path = tmp_path / "ChatStorage.sqlite"
        with make_ios_db(cs_path) as conn:
            conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZSTARRED INTEGER DEFAULT 0")
            conn.execute("""
                INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
                VALUES (1, '15551234@s.whatsapp.net', 'Alice Smith', 0)
            """)
            conn.execute("""
                INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZVCARDNAME)
                VALUES (10, 'Q3 Financial Report', 'Media/15551234@s.whatsapp.net/bef9135a.pdf', NULL)
            """)
            conn.execute("""
                INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZVCARDNAME)
                VALUES (11, NULL, NULL, 'Central Park\nNew York, NY')
            """)
            # Doc 1: Document with filename in ZTEXT and caption in ZTITLE
            conn.execute("""
                INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED, ZMEDIAITEM, ZPUSHNAME, ZFROMJID)
                VALUES (100, 1, 0, 720000000, 'bef9135a-843b-4519-9d49-9634f9da21bb.pdf', 8, 1, 10, 'CNDj+ZsG', '15551234@s.whatsapp.net')
            """)
            # Msg 2: Location with place name in ZVCARDNAME and base64 token in ZPUSHNAME
            conn.execute("""
                INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED, ZMEDIAITEM, ZPUSHNAME, ZFROMJID)
                VALUES (101, 1, 0, 720000010, NULL, 5, 1, 11, 'CKji9NUGIABIAZABAPABAtgCu5PzutWWlwM=', '15551234@s.whatsapp.net')
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        try:
            with app.test_client() as client:
                resp = client.get("/api/starred")
                assert resp.status_code == 200
                data = resp.get_json()
                items = data["messages"] if isinstance(data, dict) and "messages" in data else data
                assert len(items) == 2

                by_id = {m["id"]: m for m in items}
                doc_m = by_id[100]
                assert doc_m["media_type"] == "document"
                assert doc_m["text_body"] == "Q3 Financial Report"
                assert doc_m["file_name"] == "bef9135a-843b-4519-9d49-9634f9da21bb.pdf"
                # Sender name must be sanitized from CNDj+ZsG to contacts map name
                assert doc_m["sender_name"] == "Alice Smith"

                loc_m = by_id[101]
                assert loc_m["media_type"] == "location"
                assert loc_m["place_name"] == "Central Park\nNew York, NY"
                assert loc_m["sender_name"] == "Alice Smith"

                # Scoped chat query using phone number chat_id
                resp_scoped = client.get("/api/chat/15551234/starred")
                assert resp_scoped.status_code == 200
                scoped_data = resp_scoped.get_json()
                scoped_items = (
                    scoped_data["messages"]
                    if isinstance(scoped_data, dict) and "messages" in scoped_data
                    else scoped_data
                )
                assert len(scoped_items) == 2
                assert scoped_data.get("count") == 2

                # Scoped query using CoreData PK chat_id
                resp_pk = client.get("/api/chat/1/starred")
                assert resp_pk.status_code == 200
                assert len(resp_pk.get_json()["messages"]) == 2
        finally:
            ctx = app.config.get("VIEWER_CONTEXT")
            if ctx and hasattr(ctx, "close"):
                ctx.close()

    def test_api_pinned_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER,
                    expiry_timestamp INTEGER
                );
                CREATE TABLE message_add_on_pin_in_chat (
                    message_add_on_row_id INTEGER PRIMARY KEY,
                    pin_in_chat_state INTEGER,
                    sender_timestamp INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_duration INTEGER,
                    media_name TEXT
                );

                INSERT INTO jid VALUES (1, '15551111', '15551111@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, 'Pinned Chat');
                INSERT INTO message VALUES (
                    50, 10, 0, 1, 1793970000000, 'Pinned announcement message', 0, 'PIN_PARENT_1'
                );
                -- Add-on type 79 = Pin in Chat (Wire Tag 63)
                INSERT INTO message_add_on VALUES (
                    1, 50, 10, 0, 1, 'PIN_ADDON_1', 1793970500000, 79, 0, 9999999999000
                );
                INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned")
            assert resp.status_code == 200
            data = resp.get_json()
            items = (
                data["pinned_messages"]
                if isinstance(data, dict) and "pinned_messages" in data
                else data
            )
            assert len(items) == 1
            assert items[0]["id"] == 50
            assert items[0]["text_body"] == "Pinned announcement message"
            assert items[0]["is_pinned"] is True

    def test_api_chat_info_aggregates_calls(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_name TEXT
                );
                CREATE TABLE call_log (
                    _id INTEGER PRIMARY KEY,
                    call_id TEXT,
                    jid_row_id INTEGER,
                    from_me INTEGER,
                    timestamp INTEGER,
                    video_call INTEGER,
                    duration INTEGER,
                    call_result INTEGER,
                    group_jid_row_id INTEGER
                );
                CREATE TABLE jid_map (
                    lid_row_id INTEGER,
                    jid_row_id INTEGER
                );

                INSERT INTO jid VALUES (1, '15551234', '15551234@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, NULL);
                INSERT INTO message VALUES (1, 10, 0, 1, 1793970000000, 'Test msg', 0, 'M1');
                INSERT INTO call_log VALUES (1, 'call-1', 1, 0, 1793971000000, 0, 60, 1, NULL);
                INSERT INTO call_log VALUES (2, 'call-2', 1, 1, 1793972000000, 1, 120, 1, NULL);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=15551234&chat_type=contact")
            assert resp.status_code == 200
            info = resp.get_json()
            assert info["calls"] == 2


class TestReviewFixRegressions:
    """Regression suite covering critical fixes and optimizations from review."""

    def test_ios_pinned_cross_chat_scoping(self, tmp_path):
        """iOS /api/chat/<id>/pinned scopes ExtChatDatabase thread_messages and ZWAMESSAGE pins to the target chat."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '1110001@s.whatsapp.net', 'Alice', 0),
                   (2, '2220002@s.whatsapp.net', 'Bob', 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (10, 1, 0, 720000000, 'Alice pinned msg', 0, 'pin_chat_1'),
                   (20, 2, 0, 720000010, 'Bob ext pinned msg', 0, 'pin_chat_2'),
                   (21, 2, 0, 720000020, 'Bob legacy pinned msg', 0, 'pin_chat_2_legacy'),
                   (22, 2, 0, 720000030, NULL, 66, 'pin_event_chat_2');
            UPDATE ZWAMESSAGE SET ZPARENTMESSAGE = 21 WHERE Z_PK = 22;
        """)
        cs_conn.commit()
        cs_conn.close()

        ext_conn = sqlite3.connect(str(tmp_path / "ExtChatDatabase.sqlite"))
        ext_conn.executescript("""
            CREATE TABLE thread_messages (
                rowid INTEGER PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                key_id TEXT NOT NULL,
                pin_timestamp INTEGER,
                thread_type INTEGER
            );
            INSERT INTO thread_messages (rowid, chat_jid, key_id, pin_timestamp, thread_type)
            VALUES (1, '1110001@s.whatsapp.net', 'pin_chat_1', 1700000000, 0),
                   (2, '2220002@s.whatsapp.net', 'pin_chat_2', 1700000010, 0);
        """)
        ext_conn.commit()
        ext_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            r1 = client.get("/api/chat/1/pinned?chat_type=contact")
            assert r1.status_code == 200
            pins1 = r1.get_json()["pinned_messages"]
            assert [m["id"] for m in pins1] == [10]

            r2 = client.get("/api/chat/2/pinned?chat_type=contact")
            assert r2.status_code == 200
            pins2 = r2.get_json()["pinned_messages"]
            assert {m["id"] for m in pins2} == {20, 21}

    def test_api_chat_info_ios_legacy_call_schema(self, tmp_path):
        """iOS /api/chat-info supports legacy ZAGGREGATECALLEVENT/ZCALLEVENT/ZJID schemas and isolates group calls."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZCONTACTIDENTIFIER" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZCONTACTIDENTIFIER TEXT")
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZHIDDEN)
            VALUES (10, '15551112222@s.whatsapp.net', '9990001@lid', 'Alice', 0),
                   (20, '120363999@g.us', NULL, 'Team Group', 0);
            UPDATE ZWACHATSESSION SET ZGROUPINFO = 1 WHERE Z_PK = 20;
        """)
        cs_conn.commit()
        cs_conn.close()

        call_conn = sqlite3.connect(str(tmp_path / "CallHistory.sqlite"))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                ZAGGREGATECALLEVENT INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLEVENT INTEGER NOT NULL,
                ZJID TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (1, 0, 1), (2, 1, 0);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZGROUPJIDSTRING, ZDURATION, ZOUTCOME, ZDATE, ZAGGREGATECALLEVENT)
            VALUES (1, 'legacy_1to1', NULL, 42, 0, 720000000, 1),
                   (2, 'legacy_grp', '120363999@g.us', 120, 0, 720000100, 2);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, ZCALLEVENT, ZJID, ZOUTCOME)
            VALUES (1, 1, '9990001@lid', 0),
                   (2, 2, '9990001@lid', 0);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            c_info = client.get("/api/chat-info?chat_id=10&chat_type=contact").get_json()
            assert c_info["calls_count"] == 1
            g_info = client.get("/api/chat-info?chat_id=20&chat_type=group").get_json()
            assert g_info["calls_count"] == 1

    def test_api_chats_ios_without_zlastmessagedate_and_empty_chat(self, tmp_path):
        """iOS /api/chats works when ZWACHATSESSION lacks ZLASTMESSAGEDATE and yields newest_ts=0 for empty chats."""
        cs_conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        cs_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0,
                ZARCHIVED INTEGER DEFAULT 0,
                ZCONTACTABID INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT,
                ZXMPPTHUMBPATH TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT,
                ZMETADATA BLOB
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT,
                ZISACTIVE INTEGER DEFAULT 1
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER NOT NULL,
                ZISFROMME INTEGER NOT NULL DEFAULT 0,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZGROUPEVENTTYPE INTEGER DEFAULT 0,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZTOJID TEXT,
                ZRECEIPTINFO BLOB,
                ZMESSAGESTATUS INTEGER DEFAULT 0
            );
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZARCHIVED)
            VALUES (1, '15551110001@s.whatsapp.net', 'Has Msg', 0, 0),
                   (2, '15551110002@s.whatsapp.net', 'Empty Chat', 0, 1);
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE)
            VALUES (10, 1, 0, 720000000, 'Hello', 0);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = {c["id"]: c for c in resp.get_json()}
            assert chats["1"]["newest_ts"] > 0
            assert chats["2"]["newest_ts"] == 0

    def test_ios_calls_missed_fallback_and_filter_limit(self, tmp_path):
        """iOS /api/calls detects missed calls via ZOUTCOME=4 without ZMISSED and does not truncate filtered queries."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN) "
            "VALUES (1, '15557778888@s.whatsapp.net', NULL, 0)"
        )
        cs_conn.commit()
        cs_conn.close()

        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.commit()
        arch_conn.close()

        cv_conn = sqlite3.connect(str(tmp_path / "ContactsV2.sqlite"))
        cv_conn.execute(
            "CREATE TABLE ZWAADDRESSBOOKCONTACT ("
            "Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID TEXT, ZLID TEXT, ZFULLNAME TEXT, ZPHONESTATUS TEXT)"
        )
        cv_conn.execute(
            "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZWHATSAPPID, ZLID, ZFULLNAME, ZPHONESTATUS) "
            "VALUES (1, '15557778888@s.whatsapp.net', NULL, 'Saved Caller Name', NULL)"
        )
        cv_conn.commit()
        cv_conn.close()

        call_conn = sqlite3.connect(str(tmp_path / "CallHistory.sqlite"))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                Z1CALLEVENTS INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER NOT NULL,
                ZJIDSTRING TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
        """)
        # 55 newer connected audio outgoing calls + 5 older missed incoming + 2 older video calls
        for i in range(1, 56):
            call_conn.execute(
                "INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (?, 0, 0)",
                (i,),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS) "
                "VALUES (?, ?, 30, 0, ?, ?)",
                (i, f"conn_{i}", 720001000 + i, i),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME) "
                "VALUES (?, ?, '15557778888@s.whatsapp.net', 0)",
                (i, i),
            )
        for i in range(56, 61):
            call_conn.execute(
                "INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (?, 0, 1)",
                (i,),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS) "
                "VALUES (?, ?, 0, 4, ?, ?)",
                (i, f"missed_{i}", 720000000 + i, i),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME) "
                "VALUES (?, ?, '15557778888@s.whatsapp.net', 4)",
                (i, i),
            )
        for i in range(61, 63):
            call_conn.execute(
                "INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (?, 1, 0)",
                (i,),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS) "
                "VALUES (?, ?, 45, 0, ?, ?)",
                (i, f"vid_{i}", 720000100 + i, i),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME) "
                "VALUES (?, ?, '15557778888@s.whatsapp.net', 0)",
                (i, i),
            )
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            missed_resp = client.get("/api/calls?filter=missed&limit=10").get_json()
            assert len(missed_resp["calls"]) == 5
            assert all(c["call_type"] == "missed" for c in missed_resp["calls"])
            assert missed_resp["calls"][0]["caller_name"] == "Saved Caller Name"

            video_resp = client.get("/api/calls?filter=video&limit=10").get_json()
            assert len(video_resp["calls"]) == 2
            assert all(c["is_video"] is True for c in video_resp["calls"])

    def test_android_calls_group_preserves_subject(self, tmp_path):
        """Android /api/calls preserves group chat subject instead of overwriting with caller's contact name."""
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15551234', 'Alice (15551234)', 'Alice')"
        )
        arch_conn.commit()
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string)
            VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net'),
                   (2, '120363000111', 'g.us', '120363000111@g.us');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (20, 2, 'Weekend Crew', 1700000050000);
            INSERT INTO call_log VALUES (1, 'grp_call_1', 1, 0, 1700000040000, 1, 90, 1, 2);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            calls = client.get("/api/calls?limit=10").get_json()["calls"]
            assert len(calls) == 1
            assert calls[0]["chat_id"] == "20"
            assert calls[0]["caller_name"] == "Weekend Crew"

    def test_api_chat_starred_exact_total_and_pagination_beyond_100(self, tmp_path):
        """GET /api/chat/<id>/starred returns exact count regardless of limit and paginates past 100."""
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
        if "starred" not in msg_cols:
            wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
        wa_conn.executescript("""
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '15551234', '15551234@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000200000);
        """)
        for i in range(1, 106):
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred) "
                "VALUES (?, 10, 0, 1, ?, ?, 0, ?, 1)",
                (i, 1700000000000 + i * 1000, f"Starred {i}", f"STAR_{i}"),
            )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            r1 = client.get(
                "/api/chat/15551234/starred?chat_type=contact&limit=5&offset=0"
            ).get_json()
            assert r1["count"] == 105
            assert len(r1["messages"]) == 5

            r2 = client.get(
                "/api/chat/15551234/starred?chat_type=contact&limit=10&offset=100"
            ).get_json()
            assert r2["count"] == 105
            assert len(r2["messages"]) == 5

    def test_recent_messages_user_version_3_migration(self, tmp_path):
        """Upgrading archive DB from user_version=3 clears stale recent_messages and bumps user_version to 4."""
        arch_path = tmp_path / ".wa_media_archiver.db"
        arch_conn = make_archive_db(arch_path)
        arch_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN is_starred INTEGER NOT NULL DEFAULT 0"
        )
        arch_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0"
        )
        arch_conn.execute("""
            INSERT INTO recent_messages (
                chat_id, chat_type, msg_id, stanza_id, timestamp_ms, sender,
                from_me, media_type, text_body, is_starred, is_pinned
            ) VALUES (
                '15551234', 'contact', 1, 'M_STAR_PIN', 1700000010000, 'Alice',
                0, 'text', 'Important msg', 0, 0
            )
        """)
        arch_conn.execute("PRAGMA user_version = 3")
        arch_conn.commit()
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
        if "starred" not in msg_cols:
            wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            );
            CREATE TABLE message_add_on_pin_in_chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_add_on_row_id INTEGER NOT NULL,
                pin_in_chat_state INTEGER NOT NULL
            );
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '15551234', '15551234@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
            VALUES (1, 10, 0, 1, 1700000010000, 'Important msg', 0, 'M_STAR_PIN', 1);
            INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type) VALUES (1, 1, 79);
            INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id, pin_in_chat_state) VALUES (1, 1);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with sqlite3.connect(str(arch_path)) as ac:
            uv = ac.execute("PRAGMA user_version").fetchone()[0]
            assert uv == 7
            cnt = ac.execute("SELECT COUNT(*) FROM recent_messages").fetchone()[0]
            assert cnt == 0

        with app.test_client() as client:
            msgs = client.get(
                "/api/messages?chat_id=15551234&chat_type=contact&limit=10"
            ).get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_starred"] == 1
            assert msgs[0]["is_pinned"] == 1

    def test_messages_inclusive_pagination_and_preserved_pin_jump(self, tmp_path):
        """inclusive=1 includes boundary ties, api/messages/at keeps target with dense calls, and preserved pin jumps by -rowid."""
        from wab_archiver.preservation import PRESERVED_SCHEMA

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15556789', 's.whatsapp.net', '15556789@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000001000, 'Oldest', 0, 'T_OLD'),
                   (2, 10, 0, 1, 1700000005000, 'Tie 1', 0, 'T_1'),
                   (3, 10, 0, 1, 1700000005000, 'Tie 2', 0, 'T_2'),
                   (4, 10, 0, 1, 1700000005000, 'Tie 3', 0, 'T_3');
        """)
        for idx in range(1, 6):
            wa_conn.execute(
                "INSERT INTO call_log VALUES (?, ?, 1, 0, 1700000005000, 0, 10, 1, NULL)",
                (idx, f"tie_call_{idx}"),
            )
        wa_conn.commit()
        wa_conn.close()

        pres_conn = sqlite3.connect(str(tmp_path / "preserved_messages.db"))
        pres_conn.executescript(PRESERVED_SCHEMA)
        pres_conn.executescript("""
            INSERT INTO preserved_chats (chat_id, chat_type, subject, description, created_ts, oldest_ts, newest_ts, msg_count, members_json)
            VALUES ('15556789', 'contact', 'Alice', NULL, NULL, 1700000000000, 1700000009000, 1, NULL);
            INSERT INTO preserved_messages (
                rowid, chat_id, chat_type, msg_id, stanza_id, timestamp_ms,
                sender, from_me, media_type, text_body, is_pinned, preserved_at, preservation_reason, source_platform
            ) VALUES (
                42, '15556789', 'contact', '999', 'PRES_PIN_1', 1700000009000,
                'Alice', 0, 'text', 'Preserved Pin Target', 1, '2026-01-01T00:00:00Z', 'deleted', 'android'
            );
        """)
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. inclusive=1 includes tied timestamp_ms rows on upward pagination
            inc_page = client.get(
                "/api/messages?chat_id=15556789&chat_type=contact&limit=10&before=1700000005000&inclusive=1"
            ).get_json()
            inc_ids = [m["msg_id"] for m in inc_page]
            assert 2 in inc_ids and 3 in inc_ids and 4 in inc_ids and 1 in inc_ids

            # 2. api/messages/at keeps requested target even with dense same-timestamp calls
            at_tie = client.get(
                "/api/messages/at?chat_id=15556789&chat_type=contact&ts=1700000005000&msg_id=2&limit=4"
            ).get_json()
            assert any(m["msg_id"] == 2 for m in at_tie)

            # 3. Preserved pin uses negative -rowid and jumps accurately via api/messages/at
            pins = client.get("/api/chat/15556789/pinned?chat_type=contact").get_json()[
                "pinned_messages"
            ]
            assert any(p["id"] == -42 for p in pins)
            jump_pin = client.get(
                "/api/messages/at?chat_id=15556789&chat_type=contact&msg_id=-42&limit=10"
            ).get_json()
            assert any(
                m["msg_id"] == -42 and m["text_body"] == "Preserved Pin Target" for m in jump_pin
            )

    def test_multi_app_context_isolation(self, tmp_path):
        """Multiple create_app() instances retain independent ViewerContext and QuerySet state."""
        rich_dir = tmp_path / "android_rich"
        plain_dir = tmp_path / "android_plain"
        ios_dir = tmp_path / "ios_workspace"
        for d in (rich_dir, plain_dir, ios_dir):
            d.mkdir()

        # 1. Android workspace WITH message_edit_info
        make_archive_db(rich_dir / ".wa_media_archiver.db").close()
        rich_conn = make_android_db(rich_dir / "msgstore.db")
        rich_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_edit_info (
                message_row_id INTEGER PRIMARY KEY,
                original_key_id TEXT,
                edited_timestamp INTEGER DEFAULT 0,
                sender_timestamp INTEGER DEFAULT 0
            );
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15551110001', '15551110001@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (10, 1, 'Rich Chat', 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000010000, 'Edited Hello', 0, 'RICH_1');
            INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp)
            VALUES (1, 'RICH_1', 1700000015000, 1700000010000);
        """)
        rich_conn.commit()
        rich_conn.close()

        # 2. Android workspace WITHOUT message_edit_info
        make_archive_db(plain_dir / ".wa_media_archiver.db").close()
        plain_conn = make_android_db(plain_dir / "msgstore.db")
        plain_conn.executescript("""
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15551110002', '15551110002@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (10, 1, 'Plain Chat', 1700000020000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000020000, 'Plain Hello', 0, 'PLAIN_1');
        """)
        plain_conn.commit()
        plain_conn.close()

        # 3. iOS workspace
        make_archive_db(ios_dir / ".wa_media_archiver.db").close()
        ios_conn = make_ios_db(ios_dir / "ChatStorage.sqlite")
        ios_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZLASTMESSAGEDATE)
            VALUES (1, '15551110003@s.whatsapp.net', 'iOS Contact', 0, 720000000.0);
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT)
            VALUES (1, 1, 0, 720000000.0, 'IOS_1', 0, 'iOS Hello');
        """)
        ios_conn.commit()
        ios_conn.close()

        rich_app = viewer.create_app(rich_dir, rescan=False)
        plain_app = viewer.create_app(plain_dir, rescan=False)
        ios_app = viewer.create_app(ios_dir, rescan=False)
        for a in (rich_app, plain_app, ios_app):
            a.config["TESTING"] = True

        rich_ctx = rich_app.extensions["viewer"]
        plain_ctx = plain_app.extensions["viewer"]
        ios_ctx = ios_app.extensions["viewer"]

        assert rich_ctx.source_type == "android"
        assert rich_ctx.caps.has_android_edit_info is True
        assert plain_ctx.source_type == "android"
        assert plain_ctx.caps.has_android_edit_info is False
        assert ios_ctx.source_type == "ios"

        # Query all three apps interleaved in the same process after all apps are built
        with (
            rich_app.test_client() as rc,
            plain_app.test_client() as pc,
            ios_app.test_client() as ic,
        ):
            r_msgs = rc.get("/api/messages?chat_id=15551110001&chat_type=contact").get_json()
            p_msgs = pc.get("/api/messages?chat_id=15551110002&chat_type=contact").get_json()
            i_msgs = ic.get("/api/messages?chat_id=1&chat_type=contact").get_json()

            assert len(r_msgs) == 1
            assert r_msgs[0]["text_body"] == "Edited Hello"
            assert r_msgs[0]["is_edited"] == 1

            assert len(p_msgs) == 1
            assert p_msgs[0]["text_body"] == "Plain Hello"
            assert p_msgs[0]["is_edited"] == 0

            assert len(i_msgs) == 1
            assert i_msgs[0]["text_body"] == "iOS Hello"

    def test_multi_app_android_chat_row_cache_isolation(self, tmp_path):
        """Two Android apps sharing the same contact/group chat_id must not cross-contaminate row ID caches."""
        dir_a = tmp_path / "android_a"
        dir_b = tmp_path / "android_b"
        dir_a.mkdir()
        dir_b.mkdir()

        make_archive_db(dir_a / ".wa_media_archiver.db").close()
        conn_a = make_android_db(dir_a / "msgstore.db")
        conn_a.executescript("""
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15559990001', '15559990001@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (10, 1, NULL, 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000010000, 'From App A', 0, 'A_1');
        """)
        conn_a.commit()
        conn_a.close()

        make_archive_db(dir_b / ".wa_media_archiver.db").close()
        conn_b = make_android_db(dir_b / "msgstore.db")
        conn_b.executescript("""
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15559990001', '15559990001@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (99, 1, NULL, 1700000020000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 99, 0, 1, 1700000020000, 'From App B', 0, 'B_1');
        """)
        conn_b.commit()
        conn_b.close()

        app_a = viewer.create_app(dir_a, rescan=False)
        app_b = viewer.create_app(dir_b, rescan=False)
        app_a.config["TESTING"] = True
        app_b.config["TESTING"] = True

        with app_a.test_client() as ca, app_b.test_client() as cb:
            assert len(ca.get("/api/chats").get_json()) == 1
            msgs_a = ca.get("/api/messages?chat_id=15559990001&chat_type=contact").get_json()
            msgs_b = cb.get("/api/messages?chat_id=15559990001&chat_type=contact").get_json()

            assert len(msgs_a) == 1
            assert msgs_a[0]["text_body"] == "From App A"
            assert len(msgs_b) == 1
            assert msgs_b[0]["text_body"] == "From App B"

    def test_indexing_routes_not_duplicated_and_ios_location_chat_preview(self, tmp_path):
        """Indexing blueprint must register each route once and iOS location preview must not fail on sqlite3.Row."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        ios_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        ios_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZLASTMESSAGEDATE)
            VALUES (1, '15559990002@s.whatsapp.net', 'Location Contact', 0, 720000000.0);
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT)
            VALUES (1, 1, 0, 720000000.0, 'LOC_1', 5, 'Cafe Spot');
        """)
        ios_conn.commit()
        ios_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        rule_counts: dict[str, int] = {}
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith("/api/"):
                rule_counts[rule.rule] = rule_counts.get(rule.rule, 0) + 1
        assert rule_counts["/api/index-status"] == 1
        assert rule_counts["/api/preferences"] == 2  # GET and POST

        with app.test_client() as client:
            chats = client.get("/api/chats").get_json()
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "location"

    def test_long_message_collapse_assets_present(self, tmp_path):
        """app.css and app.js must include dual-theme Read More tokens, Pico resets, and collapse helpers."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            css = client.get("/static/app.css").get_data(as_text=True)
            js = client.get("/static/app.js").get_data(as_text=True)

        assert "--bubble-action:" in css
        assert "--bubble-action-sent:" in css
        assert ".msg-text .msg-read-more" in css
        assert ".msg-caption .msg-read-more" in css
        assert "COLLAPSE_CHAR_LIMIT = 750" in js
        assert "COLLAPSE_LINE_LIMIT = 15" in js
        assert "COLLAPSE_MIN_REMAINDER = 60" in js
        assert "function renderCollapsibleMessageText(" in js
        assert "expandedMsgIds.clear()" in js

    def test_grouped_media_caption_uses_first_non_empty_member(self, tmp_path):
        """Grouped media bubbles must render captions stored on any album member (e.g. iOS group[0])."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            js = client.get("/static/app.js").get_data(as_text=True)

        assert "const captionMsg = group.find(m => m.text_body && m.text_body.trim());" in js
        assert (
            "renderCollapsibleMessageText(cap, captionMsg.text_body, captionMsg.msg_id, captionMsg.mentions, isGroupChat);"
            in js
        )

    def test_ios_large_group_query_plan_and_pagination(self, tmp_path):
        """iOS /api/messages and /api/messages/at must use index scans without temp B-tree sort."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        ios_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        ios_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZGROUPINFO, ZHIDDEN, ZARCHIVED, ZLASTMESSAGEDATE)
            VALUES (1, '120363000000000001@g.us', 'Large Archived Group', 1, 0, 1, 720005000.0);
        """)
        rows = [(i, 1, 0, 720000000.0 + i, f"MSG_{i}", 0, f"Message {i}") for i in range(1, 2001)]
        ios_conn.executemany(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        ios_conn.commit()
        ios_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        ctx = app.extensions["viewer"]
        conn = ctx.get_wa()

        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_zwamessage_chat_ts" in indexes
        assert "idx_zwamessage_pin_lookup" in indexes
        assert "idx_zwamessage_stanza" in indexes

        plan_rows = conn.execute(
            f"{ctx.queries.ios_select} WHERE m.ZCHATSESSION = CAST(? AS INTEGER) "
            f"{ctx.queries.ios_filter} ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT ?",
            ["1", 50],
        ).fetchall()
        assert len(plan_rows) == 50

        eqp = conn.execute(
            f"EXPLAIN QUERY PLAN {ctx.queries.ios_select} WHERE m.ZCHATSESSION = CAST(? AS INTEGER) "
            f"{ctx.queries.ios_filter} ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT ?",
            ["1", 50],
        ).fetchall()
        plan_text = "\n".join(str(r[-1]) for r in eqp)
        assert "idx_zwamessage_chat_ts" in plan_text
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan_text
        assert "idx_zwamessage_pin_lookup" in plan_text

        with app.test_client() as client:
            latest = client.get("/api/messages?chat_id=1&chat_type=group&limit=50").get_json()
            assert len(latest) == 50
            assert latest[0]["text_body"] == "Message 2000"
            assert latest[-1]["text_body"] == "Message 1951"

            older = client.get(
                f"/api/messages?chat_id=1&chat_type=group&limit=50&before={latest[-1]['timestamp_ms']}"
            ).get_json()
            assert len(older) == 50
            assert older[0]["text_body"] == "Message 1950"

            around = client.get(
                f"/api/messages/at?chat_id=1&chat_type=group&ts={older[0]['timestamp_ms']}&limit=10"
            ).get_json()
            assert any(m["text_body"] == "Message 1950" for m in around)

    def test_collections_server_search_and_preview_richness(self, tmp_path):
        """Verify /api/calls and /api/starred server search (?q=) and preview metadata."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        pres_path = tmp_path / "preserved_messages.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT,
                jid_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            )
        """)
        wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER")
        wa_conn.execute("ALTER TABLE message_media ADD COLUMN media_duration INTEGER")

        # Insert live call and live starred message with rich preview columns
        wa_conn.execute(
            "INSERT INTO call_log (_id, call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result) "
            "VALUES (10, 'call_abc_1', 1, 0, 1700000000000, 1, 120, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, starred) "
            "VALUES (201, 10, 0, 1, 1700000000000, 'Project Roadmap', 9, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, media_duration) "
            "VALUES (201, 'Media/WhatsApp Documents/Roadmap.pdf', 'Roadmap.pdf', 0)"
        )
        wa_conn.commit()

        # Insert preserved call and preserved starred message
        with sqlite3.connect(str(pres_path)) as pres_conn:
            pres_conn.executescript("""
                CREATE TABLE preserved_calls (
                    call_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    caller_jid TEXT,
                    caller_name TEXT,
                    timestamp_ms INTEGER,
                    duration_seconds INTEGER,
                    is_video INTEGER,
                    from_me INTEGER,
                    call_result INTEGER
                );
                CREATE TABLE preserved_messages (
                    stanza_id TEXT,
                    chat_id TEXT,
                    chat_type TEXT,
                    sender_jid TEXT,
                    sender_name TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    media_type TEXT,
                    from_me INTEGER,
                    is_starred INTEGER,
                    media_duration INTEGER,
                    original_path TEXT
                );
            """)
            pres_conn.execute(
                "INSERT INTO preserved_calls (call_id, chat_id, caller_jid, caller_name, from_me, timestamp_ms, duration_seconds, is_video, call_result) "
                "VALUES ('pres_call_1', '15551234567', '15551234567@s.whatsapp.net', 'Bob Architect', 0, 1700000050000, 45, 0, 1)"
            )
            pres_conn.execute(
                "INSERT INTO preserved_messages (stanza_id, chat_id, chat_type, sender_jid, sender_name, timestamp_ms, text_body, media_type, from_me, is_starred, media_duration, original_path) "
                "VALUES ('pres_msg_1', '15551234567', 'contact', '15551234567@s.whatsapp.net', 'Bob Architect', 1700000050000, 'Audio note', 'audio', 0, 1, 65, 'Media/WhatsApp Voice/voice.opus')"
            )
            pres_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # 1. Test /api/calls server search (?q=)
        res_calls_all = client.get("/api/calls")
        assert res_calls_all.status_code == 200
        calls_data = res_calls_all.get_json()
        assert len(calls_data["calls"]) == 2
        # Check preserved and group flags
        pres_call_item = next(c for c in calls_data["calls"] if c["id"] == "pres_call_1")
        assert pres_call_item["is_preserved"] is True

        res_calls_q = client.get("/api/calls?q=Architect")
        assert res_calls_q.status_code == 200
        q_calls = res_calls_q.get_json()["calls"]
        assert len(q_calls) == 1
        assert q_calls[0]["id"] == "pres_call_1"

        # 2. Test /api/starred server search (?q=) and preview metadata
        res_starred_all = client.get("/api/starred")
        assert res_starred_all.status_code == 200
        starred_data = res_starred_all.get_json()
        assert len(starred_data["messages"]) == 2

        doc_msg = next(m for m in starred_data["messages"] if m["id"] == 201)
        assert doc_msg["media_type"] == "document"
        assert doc_msg["file_name"] == "Roadmap.pdf"

        audio_msg = next(m for m in starred_data["messages"] if m["id"] < 0)
        assert audio_msg["media_type"] == "audio"
        assert audio_msg["duration"] == 65

        res_starred_q = client.get("/api/starred?q=Roadmap")
        assert res_starred_q.status_code == 200
        q_starred = res_starred_q.get_json()["messages"]
        assert len(q_starred) == 1
        assert q_starred[0]["id"] == 201

    def test_api_messages_empty_chat_with_hd_associations_does_not_error(self, tmp_path):
        """Verify that an empty message result does not trigger an empty IN () SQL syntax error when HD association capability is present."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_association (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    child_message_row_id INTEGER,
                    association_type INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_name TEXT
                );
                CREATE TABLE message_quoted (
                    message_row_id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER NOT NULL,
                    parent_message_chat_row_id INTEGER NOT NULL,
                    from_me INTEGER NOT NULL,
                    sender_jid_row_id INTEGER,
                    key_id TEXT NOT NULL,
                    text_data TEXT,
                    timestamp INTEGER
                );
                INSERT INTO jid VALUES (1, '15559876543', '15559876543@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, NULL);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15559876543&chat_type=contact")
            assert resp.status_code == 200
            assert resp.get_json() == []
