"""Unit tests for viewer integration with preserved_messages in wab_viewer.preserved.

Tests bidirectional quote resolution, negative ID spaces, live message precedence,
revocation re-hydration, and chat list merging with 100% synthetic data.
"""

from __future__ import annotations

import sqlite3

from wab_viewer import preserved


class TestResolveQuotedMsgIds:
    """Tests for _resolve_quoted_msg_ids across local, preserved, and live DBs."""

    def test_empty_rows_or_no_unresolved(self) -> None:
        rows: list[dict] = []
        preserved._resolve_quoted_msg_ids(rows, "android", None, None)
        assert rows == []

        rows = [{"msg_id": 1, "text_body": "hello"}]
        preserved._resolve_quoted_msg_ids(rows, "android", None, None)
        assert rows == [{"msg_id": 1, "text_body": "hello"}]

    def test_resolve_from_local_map(self) -> None:
        rows = [
            {
                "msg_id": 10,
                "stanza_id": "STZ_ORIGINAL",
                "timestamp_ms": 1729000000,
                "text_body": "Original local message",
                "sender": "Alice",
            },
            {
                "msg_id": 11,
                "stanza_id": "STZ_REPLY",
                "quoted_stanza_id": "STZ_ORIGINAL",
                "quoted_msg_id": None,
                "quoted_text": None,
                "quoted_sender": None,
                "quoted_ts": None,
            },
        ]
        preserved._resolve_quoted_msg_ids(rows, "android", None, None)

        reply = rows[1]
        assert reply["quoted_msg_id"] == 10
        assert reply["quoted_text"] == "Original local message"
        assert reply["quoted_sender"] == "Alice"
        assert reply["quoted_ts"] == 1729000000
        # Check quoted_stanza_id popped
        assert "quoted_stanza_id" not in reply

    def test_resolve_from_preserved_db(self, tmp_path) -> None:
        pres_db = tmp_path / "preserved.db"
        with sqlite3.connect(pres_db) as pconn:
            pconn.execute(
                "CREATE TABLE preserved_messages (stanza_id TEXT, timestamp_ms INTEGER, text_body TEXT, sender TEXT)"
            )
            pconn.execute(
                "INSERT INTO preserved_messages (stanza_id, timestamp_ms, text_body, sender) "
                "VALUES ('STZ_DELETED_ON_PHONE', 1729000500, 'I was deleted on phone', 'Bob')"
            )
            pconn.commit()

        pconn = sqlite3.connect(pres_db)
        pconn.row_factory = sqlite3.Row

        rows = [
            {
                "msg_id": 20,
                "quoted_stanza_id": "STZ_DELETED_ON_PHONE",
                "quoted_msg_id": None,
            }
        ]
        preserved._resolve_quoted_msg_ids(rows, "android", None, pconn)
        pconn.close()

        reply = rows[0]
        # Preserved IDs resolve to negative rowid (-1)
        assert reply["quoted_msg_id"] == -1
        assert reply["quoted_text"] == "I was deleted on phone"
        assert reply["quoted_sender"] == "Bob"
        assert reply["quoted_ts"] == 1729000500

    def test_resolve_from_android_live_db(self, tmp_path) -> None:
        wa_db = tmp_path / "msgstore.db"
        with sqlite3.connect(wa_db) as conn:
            conn.execute(
                "CREATE TABLE message (_id INTEGER PRIMARY KEY, key_id TEXT, timestamp INTEGER, message_type INTEGER)"
            )
            conn.execute("INSERT INTO message VALUES (42, 'LIVE_KEY_ANDROID', 1729000800, 1)")
            conn.commit()

        conn = sqlite3.connect(wa_db)
        conn.row_factory = sqlite3.Row

        rows = [
            {
                "msg_id": -5,  # Preserved message replying to live Android message
                "quoted_stanza_id": "LIVE_KEY_ANDROID",
                "quoted_msg_id": None,
            }
        ]
        preserved._resolve_quoted_msg_ids(rows, "android", conn, None)
        conn.close()

        reply = rows[0]
        assert reply["quoted_msg_id"] == 42
        assert reply["quoted_ts"] == 1729000800
        assert reply["quoted_has_thumb"] == 1

    def test_resolve_from_ios_live_db(self, tmp_path) -> None:
        wa_db = tmp_path / "ChatStorage.sqlite"
        with sqlite3.connect(wa_db) as conn:
            conn.executescript("""
                CREATE TABLE ZWAMESSAGE (
                    Z_PK INTEGER PRIMARY KEY,
                    ZSTANZAID TEXT,
                    ZMESSAGEDATE REAL,
                    ZMESSAGETYPE INTEGER,
                    ZTEXT TEXT,
                    ZISFROMME INTEGER,
                    ZPUSHNAME TEXT,
                    ZFROMJID TEXT,
                    ZMEDIAITEM INTEGER
                );
                CREATE TABLE ZWAMEDIAITEM (
                    Z_PK INTEGER PRIMARY KEY,
                    ZTITLE TEXT
                );
            """)
            # CoreData timestamp: seconds since Jan 1 2001 (offset 978307200)
            # 750000000 + 978307200 = 1728307200 epoch sec -> 1728307200000 ms
            conn.execute(
                "INSERT INTO ZWAMESSAGE VALUES (99, 'IOS_STANZA_LIVE', 750000000.0, 1, 'Photo caption', 0, 'Charlie', '5550300@s.whatsapp.net', 1)"
            )
            conn.execute("INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media title')")
            conn.commit()

        conn = sqlite3.connect(wa_db)
        conn.row_factory = sqlite3.Row

        rows = [
            {
                "msg_id": -10,
                "quoted_stanza_id": "IOS_STANZA_LIVE",
                "quoted_msg_id": None,
            }
        ]
        preserved._resolve_quoted_msg_ids(rows, "ios", conn, None)
        conn.close()

        reply = rows[0]
        assert reply["quoted_msg_id"] == 99
        assert reply["quoted_ts"] == (750000000 + 978307200) * 1000
        assert reply["quoted_has_thumb"] == 1
        assert reply["quoted_text"] == "Photo caption"
        assert reply["quoted_sender"] == "Charlie"


class TestMergeMessages:
    """Tests for _merge_messages deduplication, revocation rehydration, and ordering."""

    def test_empty_lists(self) -> None:
        assert preserved._merge_messages([], [], True, 50) == []

        live = [{"msg_id": 1, "text_body": "Live message"}]
        assert preserved._merge_messages(live, [], True, 50) == live

        pres = [{"msg_id": -1, "text_body": "Preserved message"}]
        assert preserved._merge_messages([], pres, True, 50) == pres

    def test_rehydrate_revoked_placeholders(self) -> None:
        live = [
            {
                "msg_id": 100,
                "stanza_id": "STZ_REVOKED",
                "media_type": "deleted",
                "from_me": 0,
                "timestamp_ms": 1729000100,
                "text_body": "This message was deleted",
            }
        ]
        pres = [
            {
                "msg_id": -1,
                "stanza_id": "STZ_REVOKED",
                "preservation_reason": "revoked_by_sender",
                "from_me": 0,
                "timestamp_ms": 1729000100,
                "text_body": "Secret message recovered before revocation",
            }
        ]
        merged = preserved._merge_messages(live, pres, False, 50)
        assert len(merged) == 1
        assert merged[0]["msg_id"] == -1
        assert merged[0]["text_body"] == "Secret message recovered before revocation"

    def test_live_non_deleted_takes_absolute_precedence(self) -> None:
        live = [
            {
                "msg_id": 101,
                "stanza_id": "STZ_ACTIVE",
                "media_type": "text",
                "from_me": 1,
                "timestamp_ms": 1729000200,
                "text_body": "Live active text",
            }
        ]
        pres = [
            {
                "msg_id": -2,
                "stanza_id": "STZ_ACTIVE",
                "preservation_reason": "deleted_on_phone",
                "from_me": 1,
                "timestamp_ms": 1729000200,
                "text_body": "Stale preserved copy",
            }
        ]
        merged = preserved._merge_messages(live, pres, False, 50)
        assert len(merged) == 1
        assert merged[0]["msg_id"] == 101
        assert merged[0]["text_body"] == "Live active text"

    def test_live_poll_deduplication(self) -> None:
        live = [
            {
                "msg_id": 200,
                "stanza_id": "STZ_LIVE_POLL",
                "media_type": "poll",
                "from_me": 0,
                "timestamp_ms": 1729000300,
                "text_body": "Lunch?",
            }
        ]
        pres = [
            {
                "msg_id": -3,
                "stanza_id": "STZ_PRES_POLL",
                "media_type": "poll",
                "from_me": 0,
                "timestamp_ms": 1729000301,  # within 2000ms
                "text_body": "Lunch?",
                "poll_data": '{"question": "Lunch?"}',
            }
        ]
        merged = preserved._merge_messages(live, pres, False, 50)
        # Preserved poll discarded because live poll exists within 2000ms
        assert len(merged) == 1
        assert merged[0]["msg_id"] == 200


class TestMergePreservedChats:
    """Tests for _merge_preserved_chats into the viewer chat list."""

    def test_merge_preserved_chats_into_list(self, tmp_path) -> None:
        pres_db = tmp_path / "pres.db"
        with sqlite3.connect(pres_db) as pconn:
            pconn.executescript("""
                CREATE TABLE preserved_chats (chat_id TEXT, chat_type TEXT, newest_ts INTEGER, subject TEXT, description TEXT);
                CREATE TABLE preserved_messages (
                    chat_id TEXT,
                    chat_type TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    media_type TEXT,
                    from_me INTEGER,
                    media_name TEXT,
                    is_view_once INTEGER
                );
                INSERT INTO preserved_chats VALUES ('group_pres_1', 'group', 1729000900, 'Old Preserved Group', 'Old description');
                INSERT INTO preserved_chats VALUES ('5550999', 'contact', 1729000850, NULL, NULL);
                INSERT INTO preserved_messages VALUES ('group_pres_1', 'group', 1729000900, 'Last group msg', 'text', 0, NULL, 0);
                INSERT INTO preserved_messages VALUES ('5550999', 'contact', 1729000850, 'Last contact msg', 'text', 1, NULL, 0);
            """)

        arc_db = tmp_path / "archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute("CREATE TABLE groups (chat_row_id TEXT, subject TEXT, folder TEXT)")
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, jid TEXT, folder TEXT)"
            )
            aconn.execute(
                "INSERT INTO contacts VALUES ('5550999', 'Archived Contact', '5550999@s.whatsapp.net', 'Contacts/Archived Contact')"
            )
            aconn.commit()

        pconn = sqlite3.connect(pres_db)
        pconn.row_factory = sqlite3.Row
        aconn = sqlite3.connect(arc_db)
        aconn.row_factory = sqlite3.Row

        live_chats = [
            {
                "id": "active_chat_1",
                "type": "contact",
                "display_name": "Live Contact",
                "newest_ts": 1729001000,
            }
        ]
        merged_chats = preserved._merge_preserved_chats(live_chats, pconn, aconn)
        pconn.close()
        aconn.close()

        assert len(merged_chats) == 3
        # Live chat preserved and sorted first by newest_ts
        assert merged_chats[0]["id"] == "active_chat_1"
        assert merged_chats[0]["is_preserved_only"] is False

        # Preserved group
        grp = next(c for c in merged_chats if c["id"] == "group_pres_1")
        assert grp["display_name"] == "Old Preserved Group"
        assert grp["description"] == "Old description"
        assert grp["is_preserved_only"] is True

        # Preserved contact with name from contacts table
        ct = next(c for c in merged_chats if c["id"] == "5550999")
        assert ct["display_name"] == "Archived Contact"
        assert ct["is_preserved_only"] is True


class TestHasPreservedContent:
    """Tests for _has_preserved_content predicate."""

    def test_has_preserved_content_variations(self) -> None:
        assert preserved._has_preserved_content({"text_body": "Hello"}) is True
        assert preserved._has_preserved_content({"text_body": "   "}) is False
        assert preserved._has_preserved_content({"text_body": None}) is False

        assert preserved._has_preserved_content({"archive_path": "/path/to/media.jpg"}) is True
        assert preserved._has_preserved_content({"original_path": "/path/to/orig.mp4"}) is True
        assert preserved._has_preserved_content({"poll_data": '{"question": "Q"}'}) is True
        assert preserved._has_preserved_content({"poll": {"question": "Q"}}) is True
        assert preserved._has_preserved_content({"latitude": 37.7749}) is True
        assert preserved._has_preserved_content({"location_name": "San Francisco"}) is True
        assert preserved._has_preserved_content({"vcard_data": "BEGIN:VCARD..."}) is True
        assert preserved._has_preserved_content({"media_type": "image"}) is True
        assert preserved._has_preserved_content({"media_type": "video"}) is True
        assert preserved._has_preserved_content({}) is False


class TestDetectPreservedDb:
    """Tests for _detect_preserved_db location detection."""

    def test_detect_in_wa_databases_dir(self, tmp_path) -> None:
        wa_dir = tmp_path / "Whatsapp Databases"
        wa_dir.mkdir()
        db_file = wa_dir / "preserved_messages.db"
        db_file.write_text("dummy")

        detected = preserved._detect_preserved_db(tmp_path)
        assert detected == db_file

    def test_detect_in_root_dir(self, tmp_path) -> None:
        db_file = tmp_path / "preserved_messages.db"
        db_file.write_text("dummy")

        detected = preserved._detect_preserved_db(tmp_path)
        assert detected == db_file

    def test_detect_missing_returns_none(self, tmp_path) -> None:
        assert preserved._detect_preserved_db(tmp_path) is None


class TestQueryPreservedMessages:
    """Tests for _query_preserved_messages filtering, projection, and media synthesis."""

    def _setup_db(self, db_path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE preserved_messages (
                chat_id TEXT,
                chat_type TEXT,
                timestamp_ms INTEGER,
                text_body TEXT,
                media_type TEXT,
                media_name TEXT,
                archive_path TEXT,
                original_path TEXT,
                is_view_once INTEGER,
                is_edited INTEGER,
                is_starred INTEGER,
                is_pinned INTEGER,
                live_location_share_duration INTEGER,
                poll_data TEXT,
                service_metadata TEXT
            )
        """)
        return conn

    def test_before_and_after_filtering(self, tmp_path) -> None:
        db_path = tmp_path / "query_pres.db"
        conn = self._setup_db(db_path)
        for ts in [1000, 2000, 3000, 4000]:
            conn.execute(
                "INSERT INTO preserved_messages (chat_id, chat_type, timestamp_ms, text_body) VALUES ('c1', 'contact', ?, 'msg')",
                (ts,),
            )
        conn.commit()

        # before inclusive & non-inclusive
        res_b_inc = preserved._query_preserved_messages(
            conn,
            "c1",
            "contact",
            before=3000,
            after=None,
            before_inclusive=True,
            after_inclusive=False,
            limit=10,
        )
        assert [r["timestamp_ms"] for r in res_b_inc] == [3000, 2000, 1000]

        res_b_exc = preserved._query_preserved_messages(
            conn,
            "c1",
            "contact",
            before=3000,
            after=None,
            before_inclusive=False,
            after_inclusive=False,
            limit=10,
        )
        assert [r["timestamp_ms"] for r in res_b_exc] == [2000, 1000]

        # after inclusive & non-inclusive
        res_a_inc = preserved._query_preserved_messages(
            conn,
            "c1",
            "contact",
            before=None,
            after=2000,
            before_inclusive=False,
            after_inclusive=True,
            limit=10,
        )
        assert [r["timestamp_ms"] for r in res_a_inc] == [2000, 3000, 4000]

        res_a_exc = preserved._query_preserved_messages(
            conn,
            "c1",
            "contact",
            before=None,
            after=2000,
            before_inclusive=False,
            after_inclusive=False,
            limit=10,
        )
        assert [r["timestamp_ms"] for r in res_a_exc] == [3000, 4000]

        # chat_type None
        res_no_type = preserved._query_preserved_messages(
            conn,
            "c1",
            None,
            before=None,
            after=None,
            before_inclusive=False,
            after_inclusive=False,
            limit=10,
        )
        assert len(res_no_type) == 4
        conn.close()

    def test_media_synthesis_audio_location_poll_and_link(self, tmp_path) -> None:
        db_path = tmp_path / "media_pres.db"
        conn = self._setup_db(db_path)
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, archive_path, is_view_once
            ) VALUES ('c1', 'contact', 1000, 'audio', 'Voice Notes/AUD-1.opus', 0)
        """)
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, media_name, archive_path
            ) VALUES ('c1', 'contact', 2000, 'audio', 'track.opus', 'Audio/track.mp3')
        """)
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, live_location_share_duration
            ) VALUES ('c1', 'contact', 3000, 'location', 1800)
        """)
        # Poll with selectable_options_count == 0
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, poll_data
            ) VALUES ('c1', 'contact', 4000, 'poll', '{"question":"Fav color?","selectable_options_count":0,"options":[{"name":"Blue","votes":2}]}')
        """)
        # Poll with invalid JSON -> fallback poll
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, text_body, poll_data
            ) VALUES ('c1', 'contact', 5000, 'poll', 'Corrupted Poll', 'bad json')
        """)
        # Link with service_metadata and thumbnail
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, service_metadata
            ) VALUES ('c1', 'contact', 6000, 'link', '{"link_preview":{"title":"WA Web","url":"https://web.whatsapp.com","thumbnail_b64":"AQID"}}')
        """)
        # Link regex fallback from text_body
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, text_body
            ) VALUES ('c1', 'contact', 7000, 'link', 'Check out www.example.com/docs!')
        """)
        # Video note
        conn.execute("""
            INSERT INTO preserved_messages (
                chat_id, chat_type, timestamp_ms, media_type, original_path
            ) VALUES ('c1', 'contact', 8000, 'video', '/sdcard/WhatsApp/Media/Video Notes/ptv-123.mp4')
        """)
        conn.commit()

        results = preserved._query_preserved_messages(
            conn,
            "c1",
            "contact",
            before=None,
            after=None,
            before_inclusive=False,
            after_inclusive=False,
            limit=20,
        )
        conn.close()

        res_by_ts = {r["timestamp_ms"]: r for r in results}

        # 1000: voice note
        assert res_by_ts[1000]["is_voice_note"] == 1
        assert res_by_ts[1000]["media_name"] == "Voice message"

        # 2000: audio opus
        assert res_by_ts[2000]["media_name"] == "Audio"

        # 3000: live location
        assert res_by_ts[3000]["media_type"] == "live_location"

        # 4000: poll with multiple_answers True from selectable_options_count == 0
        assert res_by_ts[4000]["poll"]["multiple_answers"] is True
        assert res_by_ts[4000]["poll"]["options"][0]["name"] == "Blue"

        # 5000: corrupted poll fallback
        assert res_by_ts[5000]["poll"]["question"] == "Corrupted Poll"
        assert res_by_ts[5000]["poll"]["total_votes"] == 0

        # 6000: link with service metadata and thumbnail
        assert res_by_ts[6000]["link_title"] == "WA Web"
        assert res_by_ts[6000]["has_thumbnail"] == 1

        # 7000: link url regex fallback
        assert res_by_ts[7000]["link_url"] == "https://www.example.com/docs"

        # 8000: video note
        assert res_by_ts[8000]["is_video_note"] == 1
        assert res_by_ts[8000]["raw_msg_type"] == 28


class TestResolveQuotedMsgIdsEdgeCases:
    """Tests for edge cases in _resolve_quoted_msg_ids."""

    def test_ios_live_from_me_and_operational_error(self, tmp_path) -> None:
        wa_db = tmp_path / "live_ios.sqlite"
        with sqlite3.connect(wa_db) as conn:
            conn.executescript("""
                CREATE TABLE ZWAMESSAGE (
                    Z_PK INTEGER PRIMARY KEY,
                    ZSTANZAID TEXT,
                    ZMESSAGEDATE REAL,
                    ZMESSAGETYPE INTEGER,
                    ZISFROMME INTEGER,
                    ZTEXT TEXT,
                    ZMEDIAITEM INTEGER,
                    ZGROUPEVENTTYPE INTEGER,
                    ZPUSHNAME TEXT,
                    ZFROMJID TEXT
                );
                CREATE TABLE ZWAMEDIAITEM (
                    Z_PK INTEGER PRIMARY KEY,
                    ZTITLE TEXT
                );
                -- from_me = 1
                INSERT INTO ZWAMESSAGE (Z_PK, ZSTANZAID, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZTEXT)
                VALUES (1, 'STZ_FROM_ME', 750000000.0, 0, 1, 'My live message');
            """)

        rows = [
            {
                "msg_id": -10,
                "stanza_id": "REPLY_STZ",
                "quoted_stanza_id": "STZ_FROM_ME",
                "quoted_msg_id": None,
                "quoted_text": None,
                "quoted_sender": None,
                "from_me": 0,
                "sender": "Carol",
            }
        ]
        with sqlite3.connect(wa_db) as wconn:
            wconn.row_factory = sqlite3.Row
            preserved._resolve_quoted_msg_ids(rows, "ios", wconn, None)

        assert rows[0]["quoted_msg_id"] == 1
        assert rows[0]["quoted_sender"] == "You"
        assert rows[0]["quoted_text"] == "My live message"

    def test_preserved_conn_operational_error_graceful_handling(self) -> None:
        # pres_conn without preserved_messages table raises OperationalError, should pass
        bad_pres_conn = sqlite3.connect(":memory:")
        rows = [
            {
                "msg_id": 1,
                "stanza_id": "R1",
                "quoted_stanza_id": "Q1",
                "quoted_msg_id": None,
            }
        ]
        preserved._resolve_quoted_msg_ids(rows, "android", None, bad_pres_conn)
        assert rows[0]["quoted_msg_id"] is None
        bad_pres_conn.close()


class TestMergePreservedChatsEdgeCases:
    """Tests for edge cases in _merge_preserved_chats."""

    def test_operational_error_on_tables(self) -> None:
        pconn = sqlite3.connect(":memory:")
        pconn.row_factory = sqlite3.Row
        aconn = sqlite3.connect(":memory:")
        aconn.row_factory = sqlite3.Row

        live_chats = [{"id": "c1", "type": "contact", "newest_ts": 100}]
        merged = preserved._merge_preserved_chats(live_chats, pconn, aconn)
        assert len(merged) == 1
        assert merged[0]["id"] == "c1"
        pconn.close()
        aconn.close()

    def test_group_and_contact_name_and_description_fallbacks(self, tmp_path) -> None:
        pres_db = tmp_path / "pres_chats.db"
        with sqlite3.connect(pres_db) as pconn:
            pconn.executescript("""
                CREATE TABLE preserved_chats (
                    chat_id TEXT PRIMARY KEY,
                    chat_type TEXT,
                    newest_ts INTEGER,
                    subject TEXT,
                    description TEXT
                );
                CREATE TABLE preserved_messages (
                    chat_id TEXT,
                    chat_type TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    media_type TEXT,
                    from_me INTEGER,
                    media_name TEXT,
                    is_view_once INTEGER
                );
                -- Group with folder fallback in archive
                INSERT INTO preserved_chats VALUES ('grp_folder', 'group', 1000, NULL, NULL);
                INSERT INTO preserved_messages VALUES ('grp_folder', 'group', 1000, 'Hey', 'text', 0, NULL, 1);

                -- Group with description from archive groups table
                INSERT INTO preserved_chats VALUES ('grp_desc', 'group', 2000, 'Group Title', NULL);
                INSERT INTO preserved_messages VALUES ('grp_desc', 'group', 2000, 'Hello', 'text', 0, NULL, 0);

                -- Contact with cid = '0' -> 'WhatsApp'
                INSERT INTO preserved_chats VALUES ('0', 'contact', 3000, NULL, NULL);
                INSERT INTO preserved_messages VALUES ('0', 'contact', 3000, 'Notice', 'text', 0, NULL, 0);

                -- Contact with folder fallback
                INSERT INTO preserved_chats VALUES ('5551111', 'contact', 4000, NULL, NULL);
                INSERT INTO preserved_messages VALUES ('5551111', 'contact', 4000, 'Hi', 'text', 0, NULL, 0);

                -- Contact with numeric fallback (+5552222)
                INSERT INTO preserved_chats VALUES ('5552222', 'contact', 5000, NULL, NULL);
                INSERT INTO preserved_messages VALUES ('5552222', 'contact', 5000, 'Yo', 'text', 0, NULL, 0);
            """)

        arc_db = tmp_path / "arc_chats.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.executescript("""
                CREATE TABLE groups (chat_row_id TEXT, subject TEXT, folder TEXT, description TEXT);
                CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, jid TEXT, folder TEXT);
                INSERT INTO groups VALUES ('grp_folder', NULL, 'Groups/FolderGroup', NULL);
                INSERT INTO groups VALUES ('grp_desc', 'Group Title', 'Groups/FolderGroup2', 'Archived description text');
                INSERT INTO contacts VALUES ('5551111', NULL, '5551111@s.whatsapp.net', 'Contacts/FolderContact');
            """)

        pconn = sqlite3.connect(pres_db)
        pconn.row_factory = sqlite3.Row
        aconn = sqlite3.connect(arc_db)
        aconn.row_factory = sqlite3.Row

        merged = preserved._merge_preserved_chats([], pconn, aconn)
        pconn.close()
        aconn.close()

        chats_by_id = {c["id"]: c for c in merged}

        # grp_folder uses folder
        assert chats_by_id["grp_folder"]["display_name"] == "Groups/FolderGroup"
        assert chats_by_id["grp_folder"]["is_view_once"] == 1

        # grp_desc uses description from groups table
        assert chats_by_id["grp_desc"]["display_name"] == "Group Title"
        assert chats_by_id["grp_desc"]["description"] == "Archived description text"

        # cid == 0 resolves to WhatsApp
        assert chats_by_id["0"]["display_name"] == "WhatsApp"

        # 5551111 uses folder
        assert chats_by_id["5551111"]["display_name"] == "Contacts/FolderContact"

        # 5552222 falls back to +5552222
        assert chats_by_id["5552222"]["display_name"] == "+5552222"
