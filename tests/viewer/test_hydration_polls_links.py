"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import base64
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
)
from wab_viewer import main as viewer
from wab_viewer import protobuf as viewer_protobuf

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


class TestPollSupport:
    """Test suite for poll bubbles, hydration, vote details, and FTS search."""

    @staticmethod
    def _make_poll_blob(
        question: str, options: list[str], multiple: bool, votes: list[dict]
    ) -> bytes:
        def encode_varint(n: int) -> bytes:
            res = bytearray()
            while True:
                b = n & 0x7F
                n >>= 7
                if n:
                    res.append(b | 0x80)
                else:
                    res.append(b)
                    break
            return bytes(res)

        def encode_field(tag: int, wire: int, val: bytes) -> bytes:
            header = encode_varint((tag << 3) | wire)
            if wire == 0:
                return header + val
            elif wire == 2:
                return header + encode_varint(len(val)) + val
            raise ValueError(f"Unsupported wire {wire}")

        f8 = bytearray()
        f8.extend(encode_field(2, 2, question.encode("utf-8")))
        for opt in options:
            opt_b = encode_field(1, 2, opt.encode("utf-8"))
            f8.extend(encode_field(3, 2, opt_b))
        mode_val = 0 if multiple else 1
        f8.extend(encode_field(4, 0, encode_varint(mode_val)))
        for v in votes:
            vp = bytearray()
            packed = bytearray()
            for idx in v.get("option_indices", []):
                packed.extend(encode_varint(idx))
            if packed:
                vp.extend(encode_field(1, 2, bytes(packed)))
            if "client_ts" in v:
                vp.extend(encode_field(2, 0, encode_varint(v["client_ts"])))
            if "stanza_id" in v:
                vp.extend(encode_field(3, 2, v["stanza_id"].encode("utf-8")))
            if "voter_jid" in v:
                vp.extend(encode_field(4, 2, v["voter_jid"].encode("utf-8")))
            if "server_ts" in v:
                vp.extend(encode_field(5, 0, encode_varint(v["server_ts"])))
            f8.extend(encode_field(5, 2, bytes(vp)))

        return encode_field(8, 2, bytes(f8))

    def test_parse_ios_poll_blob(self):
        votes = [
            {
                "option_indices": [0],
                "client_ts": 1700000000000,
                "voter_jid": "15551112222@s.whatsapp.net",
            },
            {
                "option_indices": [0, 1],
                "client_ts": 1700000001000,
                "voter_jid": "15553334444@s.whatsapp.net",
            },
        ]
        blob = self._make_poll_blob("Weekend plan?", ["Option A", "Option B"], True, votes)
        parsed = viewer_protobuf._parse_ios_poll_blob(blob)
        assert parsed is not None
        assert parsed["question"] == "Weekend plan?"
        assert parsed["options"] == ["Option A", "Option B"]
        assert parsed["multiple_answers"] is True
        assert len(parsed["votes"]) == 2
        assert parsed["votes"][0]["voter_jid"] == "15551112222@s.whatsapp.net"
        assert parsed["votes"][1]["option_indices"] == [0, 1]

        # Empty/corrupt blob
        assert viewer_protobuf._parse_ios_poll_blob(b"") is None
        assert viewer_protobuf._parse_ios_poll_blob(b"random bytes") is None

    def test_android_poll_messages_and_details(self, tmp_path):
        """Test Android poll hydration in /api/messages and /api/poll_details."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
            CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, message_add_on_type INTEGER, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_add_on_poll_vote_selected_option (message_add_on_row_id INTEGER, message_poll_option_id INTEGER);
        """)

        # Chat
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '15552222222')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Group Chat', 0, 1700000000000, 10)"
        )

        # Poll message
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (10, 1, 0, 2, 1700000000000, 'Lunch spot?', 66)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (10, 0)"
        )  # Multi-choice
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (101, 10, 'Tacos', 2)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (102, 10, 'Pizza', 1)"
        )

        # Votes: Bob (jid 2) voted Tacos. Device user (from_me 1) voted Tacos and Pizza.
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type, from_me, sender_jid_row_id, timestamp) VALUES (201, 10, 67, 0, 2, 1700000001000)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type, from_me, sender_jid_row_id, timestamp) VALUES (202, 10, 67, 1, NULL, 1700000002000)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (201, 101)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (202, 101)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (202, 102)"
        )
        wa_conn.commit()

        # Contacts
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15552222222', 'Bob (15552222222)', 'Bob')"
        )
        archive_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # Test /api/messages
        res = client.get("/api/messages?chat_id=1&chat_type=group")
        assert res.status_code == 200
        msgs = res.get_json()
        assert len(msgs) == 1
        poll_msg = msgs[0]
        assert poll_msg["media_type"] == "poll"
        assert poll_msg["poll"]["question"] == "Lunch spot?"
        assert poll_msg["poll"]["multiple_answers"] is True
        assert poll_msg["poll"]["total_votes"] == 3
        assert poll_msg["poll"]["total_voters"] == 2
        opts = poll_msg["poll"]["options"]
        assert len(opts) == 2
        assert opts[0]["name"] == "Tacos"
        assert opts[0]["vote_count"] == 2
        assert opts[0]["user_selected"] is True
        assert opts[1]["name"] == "Pizza"
        assert opts[1]["vote_count"] == 1
        assert opts[1]["user_selected"] is True

        # Test /api/poll_details/<msg_id>
        res_det = client.get("/api/poll_details/10")
        assert res_det.status_code == 200
        det = res_det.get_json()
        assert det["available"] is True
        assert det["question"] == "Lunch spot?"
        assert det["total_voters"] == 2
        opt_map = {o["id"]: o for o in det["options"]}
        assert len(opt_map[101]["voters"]) == 2
        voter_names = [v["name"] for v in opt_map[101]["voters"]]
        assert "Bob" in voter_names
        assert "You" in voter_names
        assert len(opt_map[102]["voters"]) == 1
        assert opt_map[102]["voters"][0]["name"] == "You"

        # Test non-existent poll details
        res_none = client.get("/api/poll_details/9999")
        assert res_none.status_code == 200
        assert res_none.get_json()["available"] is True
        assert len(res_none.get_json()["options"]) == 0

    def test_live_poll_not_misidentified_as_preserved(self, tmp_path):
        """Verify that a live poll present in msgstore.db is NEVER misidentified as preserved

        or duplicated even if preserved_messages.db has an entry, and results are never lost.
        """
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, key_id TEXT, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
            CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, message_add_on_type INTEGER, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_add_on_poll_vote_selected_option (message_add_on_row_id INTEGER, message_poll_option_id INTEGER);
        """)

        # Group Chat
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '15552222222')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Group Chat', 0, 1700000000000, 10)"
        )

        # Live poll in database with key_id
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, key_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (10, 1, 'POLL_STANZA_1', 0, 2, 1700000000000, 'Lunch spot?', 66)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (10, 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (101, 10, 'Tacos', 2)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (102, 10, 'Pizza', 1)"
        )
        wa_conn.commit()

        # Contacts
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15552222222', 'Bob (15552222222)', 'Bob')"
        )
        archive_conn.commit()

        # Now simulate a preserved_messages.db that also contains an entry for this poll
        pres_path = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_path))
        pres_conn.executescript("""
            CREATE TABLE preserved_messages (
                msg_id INTEGER,
                stanza_id TEXT,
                chat_id TEXT,
                chat_type TEXT,
                chat_jid TEXT,
                timestamp_ms INTEGER,
                sender TEXT,
                sender_jid TEXT,
                sender_phone TEXT,
                from_me INTEGER,
                text_body TEXT,
                media_type TEXT,
                media_name TEXT,
                archive_path TEXT,
                original_path TEXT,
                vcard_data TEXT,
                media_width INTEGER,
                media_height INTEGER,
                media_duration INTEGER,
                media_size INTEGER,
                is_view_once INTEGER,
                latitude REAL,
                longitude REAL,
                location_name TEXT,
                location_address TEXT,
                location_url TEXT,
                live_location_share_duration INTEGER,
                quoted_text TEXT,
                quoted_sender TEXT,
                quoted_ts INTEGER,
                quoted_stanza_id TEXT,
                quoted_parent_type TEXT,
                is_edited INTEGER,
                edited_ts INTEGER,
                is_starred INTEGER,
                action_type INTEGER,
                participant_name TEXT,
                preserved_at TEXT,
                preservation_reason TEXT,
                source_platform TEXT,
                reactions TEXT,
                poll_data TEXT,
                is_pinned INTEGER,
                pin_expiry_ts INTEGER,
                service_metadata TEXT
            );
        """)
        pres_conn.execute(
            """INSERT INTO preserved_messages (
                msg_id, stanza_id, chat_id, chat_type, timestamp_ms, sender, from_me,
                text_body, media_type, preservation_reason, poll_data
            ) VALUES (
                10, 'POLL_STANZA_1', '1', 'group', 1700000000000, 'Bob', 0,
                'Lunch spot?', 'poll', 'deleted_on_phone', '{"options": [{"name": "Tacos", "votes": 0}], "total_votes": 0}'
            )"""
        )
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # Initial fetch from live database (and backfill to recent_messages)
        res1 = client.get("/api/messages?chat_id=1&chat_type=group")
        assert res1.status_code == 200
        msgs1 = res1.get_json()
        assert len(msgs1) == 1, "Must NOT duplicate live poll with preserved copy"
        p1 = msgs1[0]
        assert p1["msg_id"] == 10
        assert p1.get("is_preserved") is None or p1.get("is_preserved") == 0
        assert p1["media_type"] == "poll"
        assert p1["poll"]["total_votes"] == 3
        assert len(p1["poll"]["options"]) == 2

        # Second fetch: populate from recent_messages cache directly
        # Wait for background thread backfill if running
        time.sleep(0.1)
        res2 = client.get("/api/messages?chat_id=1&chat_type=group")
        assert res2.status_code == 200
        msgs2 = res2.get_json()
        assert len(msgs2) == 1, "Recent messages cache must NOT leak preserved duplicate"
        p2 = msgs2[0]
        assert p2["msg_id"] == 10
        assert p2.get("is_preserved") is None or p2.get("is_preserved") == 0
        assert p2["poll"]["total_votes"] == 3
        assert len(p2["poll"]["options"]) == 2

        # Test negative ID lookup for poll details (preserved poll details fallback)
        res_neg = client.get("/api/poll_details/-1")
        assert res_neg.status_code == 200
        neg_data = res_neg.get_json()
        assert neg_data["available"] is True
        # Since POLL_STANZA_1 exists in live msgstore.db, it resolved to live details!
        assert len(neg_data["options"]) == 2

    def test_ios_poll_messages_and_details(self, tmp_path):
        """Test iOS poll hydration in /api/messages and /api/poll_details."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        wa_path = tmp_path / "ChatStorage.sqlite"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZHIDDEN INTEGER DEFAULT 0);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER, ZMESSAGETYPE INTEGER,
                ZTEXT TEXT, ZMEDIAITEM INTEGER, ZPARENTMESSAGE INTEGER, ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZMEDIALOCALPATH TEXT);
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
        """)

        # Contacts in ContactsV2
        cv2_path = tmp_path / "ContactsV2.sqlite"
        cv2_conn = sqlite3.connect(str(cv2_path))
        cv2_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID TEXT, ZFULLNAME TEXT);
        """)
        cv2_conn.execute(
            "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZWHATSAPPID, ZFULLNAME) VALUES (1, '15553333333@s.whatsapp.net', 'Charlie')"
        )
        cv2_conn.commit()
        cv2_conn.close()

        # Chat
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID) VALUES (5, 1, '15550009999-12345@g.us')"
        )

        # Synthetic poll protobuf: question "Meeting day?", options ["Monday", "Friday"], single choice
        votes = [
            {
                "option_indices": [0],
                "client_ts": 1700000000000,
                "voter_jid": "15553333333@s.whatsapp.net",
            },
            {"option_indices": [1], "client_ts": 1700000001000, "voter_jid": "You"},
        ]
        blob = self._make_poll_blob("Meeting day?", ["Monday", "Friday"], False, votes)

        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) "
            "VALUES (50, 5, 0, 1000, 46, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (1, 50, ?)", (blob,)
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # Test /api/messages
        res = client.get("/api/messages?chat_id=5&chat_type=group")
        assert res.status_code == 200
        msgs = res.get_json()
        assert len(msgs) == 1
        poll_msg = msgs[0]
        assert poll_msg["media_type"] == "poll"
        assert poll_msg["text_body"] == "Meeting day?"
        assert poll_msg["poll"]["question"] == "Meeting day?"
        assert poll_msg["poll"]["multiple_answers"] is False
        assert poll_msg["poll"]["total_votes"] == 2
        assert poll_msg["poll"]["total_voters"] == 2
        opts = poll_msg["poll"]["options"]
        assert len(opts) == 2
        assert opts[0]["name"] == "Monday"
        assert opts[0]["vote_count"] == 1
        assert opts[1]["name"] == "Friday"
        assert opts[1]["vote_count"] == 1
        assert opts[1]["user_selected"] is True

        # Test /api/poll_details/50
        res_det = client.get("/api/poll_details/50")
        assert res_det.status_code == 200
        det = res_det.get_json()
        assert det["available"] is True
        assert det["question"] == "Meeting day?"
        assert det["total_voters"] == 2
        opt_map = {o["id"]: o for o in det["options"]}
        assert len(opt_map[0]["voters"]) == 1
        assert opt_map[0]["voters"][0]["name"] == "Charlie"
        assert len(opt_map[1]["voters"]) == 1
        assert opt_map[1]["voters"][0]["name"] == "You"

    def test_poll_chat_preview(self, tmp_path):
        """Test that polls format properly in /api/chats sidebar previews."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Event Planning', 0, 1700000000000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (1, 1, 0, 1, 1700000000000, 'Picnic or Hike?', 66)"
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        res = client.get("/api/chats")
        assert res.status_code == 200
        chats = res.get_json()
        assert len(chats) == 1
        assert chats[0]["last_msg_type"] == "poll"
        assert chats[0]["last_msg_preview"] == "Picnic or Hike?"

    def test_poll_search_indexing(self, tmp_path):
        """Test FTS search indexing matches poll questions and options."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Game Night', 0, 1700000000000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (1, 1, 0, 1, 1700000000000, 'Boardgames?', 66)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (1, 1, 'Catan', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (2, 1, 'Carcassonne', 1)"
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        # Trigger indexing
        client.get("/api/messages?chat_id=1&chat_type=group")
        for _ in range(25):
            time.sleep(0.1)
            q_res = client.get("/api/search?q=Boardgames")
            if q_res.status_code == 200 and len(q_res.get_json()["results"]) == 1:
                break

        # Match question
        q_res = client.get("/api/search?q=Boardgames")
        assert q_res.status_code == 200
        assert len(q_res.get_json()["results"]) == 1

        # Match option
        opt_res = client.get("/api/search?q=Carcassonne")
        assert opt_res.status_code == 200
        assert len(opt_res.get_json()["results"]) == 1

    def test_poll_frontend_badge_wording_and_star(self):
        """Verify poll badge uses descriptive wording without action verbs and winner star is defined."""
        js_content = _get_app_js()
        css_content = _get_app_css()

        # Badge wording uses i18n keys (verified in en.json as "Multiple choice" / "Single choice")
        assert "msg.poll_multiple" in js_content
        assert "msg.poll_single" in js_content
        assert "Select one or more" not in js_content
        assert "Select one" not in js_content

        # Star styles and tokens
        assert "--star-color" in css_content
        assert "msg-poll-winner-star" in css_content
        assert "msg-poll-winner-star" in js_content
        assert "msg-poll-details-option-hdr" in css_content
        assert "msg-poll-details-option-hdr" in js_content

    def test_missing_media_thumbnail_and_dimensions(self, tmp_path):
        """Verify media dimensions and thumbnail streaming for Android missing media."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        wa_conn = sqlite3.connect(tmp_path / "msgstore.db")
        wa_conn.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT)")
        wa_conn.execute(
            "CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER)"
        )
        wa_conn.execute("""
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                media_duration INTEGER,
                file_hash TEXT,
                media_name TEXT
            )
        """)
        wa_conn.execute(
            "CREATE TABLE message_thumbnail (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB)"
        )
        wa_conn.execute(
            "CREATE TABLE media_hash_thumbnail (media_hash TEXT PRIMARY KEY, thumbnail BLOB)"
        )
        wa_conn.execute(
            "CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, from_me INTEGER, text_data TEXT, sender_jid_row_id INTEGER, timestamp INTEGER)"
        )

        wa_conn.execute("INSERT INTO jid VALUES (1, '15551234567')")
        wa_conn.execute("INSERT INTO chat VALUES (1, 1, 'Test Chat', 0)")

        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
        # msg 1: has thumbnail in message_thumbnail
        wa_conn.execute("INSERT INTO message VALUES (1, 1, 0, 1, 1700000000000, 'caption 1', 1)")
        wa_conn.execute(
            "INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/missing1.jpg', 12345, 800, 600, 0, 'hash1', 'missing1.jpg')"
        )
        wa_conn.execute("INSERT INTO message_thumbnail VALUES (1, ?)", (fake_jpeg,))

        # msg 2: has thumbnail in media_hash_thumbnail
        wa_conn.execute("INSERT INTO message VALUES (2, 1, 0, 1, 1700000001000, 'caption 2', 1)")
        wa_conn.execute(
            "INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/missing2.jpg', 67890, 1920, 1080, 0, 'hash2', 'missing2.jpg')"
        )
        wa_conn.execute("INSERT INTO media_hash_thumbnail VALUES ('hash2', ?)", (fake_jpeg,))

        # msg 3: no thumbnail
        wa_conn.execute("INSERT INTO message VALUES (3, 1, 0, 1, 1700000002000, 'caption 3', 3)")
        wa_conn.execute(
            "INSERT INTO message_media VALUES (3, 'Media/WhatsApp Video/missing3.mp4', 99999, 1280, 720, 45, 'hash3', 'missing3.mp4')"
        )

        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=15551234567&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}

        # Check msg 1
        m1 = msgs[1]
        assert m1["has_thumbnail"] == 1
        assert m1["media_width"] == 800
        assert m1["media_height"] == 600
        assert m1["media_size"] == 12345
        assert m1["aspect_ratio"] == round(800 / 600, 3)

        # Check msg 2
        m2 = msgs[2]
        assert m2["has_thumbnail"] == 1
        assert m2["media_width"] == 1920
        assert m2["media_height"] == 1080
        assert m2["media_size"] == 67890

        # Check msg 3
        m3 = msgs[3]
        assert m3["has_thumbnail"] == 0
        assert m3["media_width"] == 1280
        assert m3["media_height"] == 720
        assert m3["media_duration"] == 45

        # Check thumbnail endpoint
        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/jpeg"
        assert t1.data == fake_jpeg
        assert "public, max-age=86400" in t1.headers.get("Cache-Control", "")

        t2 = client.get("/api/thumbnail/2")
        assert t2.status_code == 200
        assert t2.data == fake_jpeg

        t3 = client.get("/api/thumbnail/3")
        assert t3.status_code == 404

    def test_missing_media_ios_thumbnail_and_dimensions(self, tmp_path):
        """Verify media dimensions and thumbnail streaming for iOS missing media."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        # Create a fake thumbnail file on disk for iOS thumb path
        thumb_dir = tmp_path / "Media" / "15551234567@s.whatsapp.net" / "a" / "b"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_file = thumb_dir / "test.thumb"
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10"
        thumb_file.write_bytes(fake_png)

        wa_conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
        wa_conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZFILESIZE INTEGER,
                ZASPECTRATIO FLOAT,
                ZMOVIEDURATION INTEGER,
                ZXMPPTHUMBPATH TEXT,
                ZTHUMBNAILLOCALPATH TEXT,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE FLOAT,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            )
        """)

        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15551234567@s.whatsapp.net', 'Alice', NULL)"
        )
        # msg 1: image with thumb on disk
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 54321, 1.333, 0, 'Media/15551234567@s.whatsapp.net/a/b/test.thumb', NULL, 'nonexistent.jpg', 'img.jpg')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 700000000.0, 38, 'photo caption', '15551234567@s.whatsapp.net', NULL, NULL, 'Alice')"
        )

        # msg 2: video without thumb
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (2, 98765, 1.778, 30, NULL, NULL, 'nonexistent.mp4', 'vid.mp4')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (2, 1, 2, 0, 700000010.0, 39, 'video caption', '15551234567@s.whatsapp.net', NULL, NULL, 'Alice')"
        )

        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}

        m1 = msgs[1]
        assert m1["has_thumbnail"] == 1
        assert m1["media_size"] == 54321
        assert m1["aspect_ratio"] == 1.333

        m2 = msgs[2]
        assert m2["has_thumbnail"] == 0
        assert m2["media_size"] == 98765
        assert m2["media_duration"] == 30
        assert m2["aspect_ratio"] == 1.778

        # Test iOS thumbnail endpoint
        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/png"
        assert t1.data == fake_png

        t2 = client.get("/api/thumbnail/2")
        assert t2.status_code == 404

    def test_missing_media_quoted_thumbnail_resolution(self, tmp_path):
        """Verify quoted media thumbnails are linked to original missing media messages."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        wa_conn = sqlite3.connect(tmp_path / "msgstore.db")
        wa_conn.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT)")
        wa_conn.execute(
            "CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER)"
        )
        wa_conn.execute("""
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                key_id TEXT,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                media_duration INTEGER,
                file_hash TEXT,
                media_name TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                key_id TEXT,
                parent_message_chat_row_id INTEGER,
                from_me INTEGER,
                text_data TEXT,
                sender_jid_row_id INTEGER,
                timestamp INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_quoted_media (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            )
        """)

        wa_conn.execute("INSERT INTO jid VALUES (1, '15551234567')")
        wa_conn.execute("INSERT INTO chat VALUES (1, 1, 'Test Chat', 0)")

        fake_quote_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xfe\x00\x05QUOTE\xff\xd9"
        # msg 10: original photo message with missing file on disk
        wa_conn.execute(
            "INSERT INTO message VALUES (10, 1, 0, 'PHOTO_ORIG_KEY', 1, 1700000010000, 'orig photo', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media VALUES (10, 'Media/WhatsApp Images/missing10.jpg', 45000, 1200, 900, 0, 'hash10', 'missing10.jpg')"
        )

        # msg 11: reply message quoting msg 10
        wa_conn.execute(
            "INSERT INTO message VALUES (11, 1, 1, 'REPLY_KEY_11', 1, 1700000020000, 'nice picture!', 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_quoted VALUES (11, 'PHOTO_ORIG_KEY', 1, 0, 'orig photo', 1, 1700000010000)"
        )
        wa_conn.execute("INSERT INTO message_quoted_media VALUES (11, ?)", (fake_quote_jpeg,))

        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}

        # Original missing message 10 must have has_thumbnail = 1 bridged from quote
        assert msgs[10]["has_thumbnail"] == 1

        # Fetch thumbnail for message 10
        t10 = client.get("/api/thumbnail/10")
        assert t10.status_code == 200
        assert t10.content_type == "image/jpeg"
        assert t10.data == fake_quote_jpeg
        assert "public, max-age=86400, immutable" in t10.headers.get("Cache-Control", "")

    def test_missing_media_ios_zmetadata_fallback_thumbnail(self, tmp_path):
        """Verify iOS missing media falls back to embedded JPEG in ZWAMEDIAITEM.ZMETADATA."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        wa_conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
        wa_conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZFILESIZE INTEGER,
                ZASPECTRATIO FLOAT,
                ZMOVIEDURATION INTEGER,
                ZXMPPTHUMBPATH TEXT,
                ZTHUMBNAILLOCALPATH TEXT,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE FLOAT,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            )
        """)

        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15551234567@s.whatsapp.net', 'Alice', NULL)"
        )
        fake_embedded_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xfe\x00\x04EMB\xff\xd9"
        proto_blob = b"\x08\x01\x12\x20" + b"\x00" * 32 + fake_embedded_jpeg + b"\x18\x02"

        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 30000, 1.0, 0, NULL, NULL, 'missing_ios.jpg', 'img.jpg', ?)",
            (proto_blob,),
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 700000000.0, 1, 'embedded thumb test', '15551234567@s.whatsapp.net', NULL, NULL, 'Alice')"
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}
        assert msgs[1]["has_thumbnail"] == 1

        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/jpeg"
        assert t1.data == fake_embedded_jpeg

    def test_missing_media_ios_extracted_thumbnails_dir_resolution(self, tmp_path):
        """Verify viewer resolves iOS thumbnails extracted to Whatsapp Databases/.thumbnails/."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.execute(
            "INSERT INTO archive_copies VALUES ('Message/Media/123@s.whatsapp.net/uuid123.thumb', 'Whatsapp Databases/.thumbnails/uuid123.thumb')"
        )
        arch_conn.commit()

        # Place the thumbnail in Whatsapp Databases/.thumbnails/
        thumb_dir = tmp_path / "Whatsapp Databases" / ".thumbnails"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\xff\xd9"
        (thumb_dir / "uuid123.thumb").write_bytes(fake_jpeg)

        wa_conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
        wa_conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZXMPPTHUMBPATH TEXT,
                ZMEDIALOCALPATH TEXT,
                ZFILESIZE INTEGER,
                ZASPECTRATIO FLOAT,
                ZMOVIEDURATION INTEGER,
                ZTITLE TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE FLOAT,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZSTANZAID TEXT,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            )
        """)
        wa_conn.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")

        wa_conn.execute("INSERT INTO ZWACHATSESSION VALUES (1, '123@s.whatsapp.net', 'Bob', NULL)")
        # Missing media item with thumb path
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/123@s.whatsapp.net/uuid123.thumb', 'Media/123@s.whatsapp.net/missing.jpg', 1234, 1.0, 0, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 1000.0, 1, NULL, '123@s.whatsapp.net', 'S1', 'Bob', NULL, NULL)"
        )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}
        assert msgs[1]["has_thumbnail"] == 1

        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/jpeg"
        assert t1.data == fake_jpeg
        assert "immutable" in t1.headers.get("Cache-Control", "")

    def test_thumbnail_404_cache_isolated_between_contexts(self, tmp_path):
        """Verify thumbnail 404 cache on one viewer context does not pollute another viewer context."""
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        # dir1: empty DB where thumbnail 1 returns 404
        wa_conn1 = sqlite3.connect(dir1 / "ChatStorage.sqlite")
        wa_conn1.execute(
            "CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER)"
        )
        wa_conn1.execute(
            "CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZXMPPTHUMBPATH TEXT, ZMEDIALOCALPATH TEXT, ZFILESIZE INTEGER, ZASPECTRATIO FLOAT, ZMOVIEDURATION INTEGER, ZTITLE TEXT)"
        )
        wa_conn1.execute(
            "CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE FLOAT, ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZFROMJID TEXT, ZSTANZAID TEXT, ZPUSHNAME TEXT, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER)"
        )
        wa_conn1.execute("CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)")
        wa_conn1.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")
        wa_conn1.commit()
        wa_conn1.close()

        app1 = viewer.create_app(dir1, rescan=True)
        client1 = app1.test_client()
        res1 = client1.get("/api/thumbnail/1")
        assert res1.status_code == 404

        # dir2: DB where thumbnail 1 actually exists
        arch_conn2 = sqlite3.connect(dir2 / ".wa_media_archiver.db")
        arch_conn2.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn2.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn2.commit()

        thumb_dir = dir2 / "Media" / "123@s.whatsapp.net" / "a"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10"
        (thumb_dir / "t.thumb").write_bytes(fake_png)

        wa_conn2 = sqlite3.connect(dir2 / "ChatStorage.sqlite")
        wa_conn2.execute(
            "CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER)"
        )
        wa_conn2.execute(
            "CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZXMPPTHUMBPATH TEXT, ZMEDIALOCALPATH TEXT, ZFILESIZE INTEGER, ZASPECTRATIO FLOAT, ZMOVIEDURATION INTEGER, ZTITLE TEXT)"
        )
        wa_conn2.execute(
            "CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE FLOAT, ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZFROMJID TEXT, ZSTANZAID TEXT, ZPUSHNAME TEXT, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER)"
        )
        wa_conn2.execute("CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)")
        wa_conn2.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")
        wa_conn2.execute("INSERT INTO ZWACHATSESSION VALUES (1, '123@s.whatsapp.net', 'Bob', NULL)")
        wa_conn2.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/123@s.whatsapp.net/a/t.thumb', 'Media/123@s.whatsapp.net/a/missing.jpg', 1234, 1.0, 0, NULL)"
        )
        wa_conn2.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 1000.0, 1, NULL, '123@s.whatsapp.net', 'S1', 'Bob', NULL, NULL)"
        )
        wa_conn2.commit()
        wa_conn2.close()

        app2 = viewer.create_app(dir2, rescan=True)
        client2 = app2.test_client()
        res2 = client2.get("/api/thumbnail/1")
        assert res2.status_code == 200
        assert res2.data == fake_png

    def test_link_thumbnail_fast_fail_android(self, tmp_path):
        """Link messages on Android fast-fail with 404 without cascading to media/reply joins."""
        out_dir = tmp_path / "android_fast_fail"
        out_dir.mkdir()
        db_dir = out_dir / "Whatsapp Databases"
        db_dir.mkdir()
        conn = sqlite3.connect(db_dir / "msgstore.db")
        conn.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT)")
        conn.execute("CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER)")
        conn.execute(
            "CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT, key_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE message_thumbnail (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB)"
        )
        conn.execute(
            "CREATE TABLE message_quoted_text (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB)"
        )
        conn.execute("INSERT INTO jid VALUES (1, 'user', 'user@s.whatsapp.net')")
        conn.execute("INSERT INTO chat VALUES (1, 1)")
        # Message 1: text/link message (message_type = 0) with no thumbnail
        conn.execute("INSERT INTO message VALUES (1, 1, 0, 1000, 0, 'https://example.com', 'K1')")
        # Message 2: text/link message with thumbnail in message_quoted_text
        fake_jpeg = b"\xff\xd8\xff" + b"\x00" * 20 + b"\xff\xd9"
        conn.execute("INSERT INTO message VALUES (2, 1, 0, 2000, 0, 'https://example.org', 'K2')")
        conn.execute("INSERT INTO message_quoted_text VALUES (2, ?)", (fake_jpeg,))
        conn.commit()
        conn.close()

        app = viewer.create_app(out_dir, rescan=True)
        client = app.test_client()
        res1 = client.get("/api/thumbnail/1")
        assert res1.status_code == 404
        res2 = client.get("/api/thumbnail/2")
        assert res2.status_code == 200
        assert res2.data == fake_jpeg

    def test_link_thumbnail_fast_fail_ios(self, tmp_path):
        """Link messages on iOS fast-fail with 404 without scanning the entire chat session for replies."""
        out_dir = tmp_path / "ios_fast_fail"
        out_dir.mkdir()
        conn = sqlite3.connect(out_dir / "ChatStorage.sqlite")
        conn.execute(
            "CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZGROUPINFO INTEGER)"
        )
        conn.execute(
            "CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZXMPPTHUMBPATH TEXT, ZMEDIALOCALPATH TEXT, ZFILESIZE INTEGER, ZASPECTRATIO FLOAT, ZMOVIEDURATION INTEGER, ZTITLE TEXT, ZMETADATA BLOB)"
        )
        conn.execute(
            "CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEDIAITEM INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE FLOAT, ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZFROMJID TEXT, ZSTANZAID TEXT, ZPUSHNAME TEXT, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER)"
        )
        conn.execute("CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)")
        conn.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")
        conn.execute("INSERT INTO ZWACHATSESSION VALUES (1, '123@s.whatsapp.net', 'Alice', NULL)")
        # Message 1: link message (ZMESSAGETYPE = 7) with no thumbnail
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, NULL, 0, 1000.0, 7, 'https://example.com', '123@s.whatsapp.net', 'S1', 'Alice', NULL, NULL)"
        )
        # Message 2: link message with metadata thumbnail
        fake_jpeg = b"\xff\xd8\xff" + b"\x00" * 30 + b"\xff\xd9"
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (2, NULL, NULL, 0, 1.0, 0, NULL, ?)", (fake_jpeg,)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (2, 1, 2, 0, 2000.0, 7, 'https://example.org', '123@s.whatsapp.net', 'S2', 'Alice', NULL, NULL)"
        )
        conn.commit()
        conn.close()

        app = viewer.create_app(out_dir, rescan=True)
        client = app.test_client()
        res1 = client.get("/api/thumbnail/1")
        assert res1.status_code == 404
        res2 = client.get("/api/thumbnail/2")
        assert res2.status_code == 200
        assert res2.data == fake_jpeg


class TestRichLinkPreviews:
    """Verify rich link previews decoding, caching, and serving across endpoints."""

    def test_android_rich_link_previews(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_text (
                message_row_id INTEGER PRIMARY KEY,
                description TEXT,
                page_title TEXT,
                url TEXT,
                matched_text TEXT,
                canonical_url TEXT,
                preview_type INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
        """)
        seed_android_db(wa_conn, make_archive_db(archive_path))
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type)
            VALUES (2, 10, 0, 1, 1700000001000, 'Check this https://github.com', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_text (message_row_id, description, page_title, url, matched_text, canonical_url, preview_type)
            VALUES (2, 'GitHub: Let’s build from here', 'GitHub', 'https://github.com', 'https://github.com', 'https://github.com', 1)
        """)
        wa_conn.execute("""
            INSERT INTO message_thumbnail (message_row_id, thumbnail)
            VALUES (2, X'FFD8FFE000104A464946')
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            link_msg = next(m for m in msgs if m["msg_id"] == 2)
            assert link_msg["link_title"] == "GitHub"
            assert link_msg["link_desc"] == "GitHub: Let’s build from here"
            assert link_msg["link_url"] == "https://github.com"
            assert link_msg["link_preview_type"] == 1
            assert bool(link_msg["has_thumbnail"]) is True

            # Also verify /api/media/links endpoint
            resp_links = client.get("/api/media/links?chat_id=10&chat_type=contact")
            assert resp_links.status_code == 200
            links = resp_links.get_json()
            assert len(links) >= 1
            assert any(item["link_title"] == "GitHub" for item in links)

    def test_ios_rich_link_previews(self, tmp_path):
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
                ZTITLE TEXT,
                ZMEDIAURL TEXT,
                ZMETADATA BLOB
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
        """)
        # Wire tag 3: (3 << 3) | 2 = 26 (0x1A), len 14, b"Python website"
        desc_bytes = b"Python website"
        metadata_blob = b"\x1a" + bytes([len(desc_bytes)]) + desc_bytes
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA) VALUES (1, NULL, 'Welcome to Python.org', ?)",
            (metadata_blob,),
        )
        # Message type 7 is link preview on iOS!
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (10, 1, 0, 700000000.0, 'STANZA_LINK_7', 7, 1, NULL, NULL, 'Bob', 'https://www.python.org', '15559876543@s.whatsapp.net')
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
            assert len(msgs) == 1
            link_msg = msgs[0]
            assert link_msg["msg_id"] == 10
            assert link_msg["media_type"] == "link"
            assert link_msg["link_title"] == "Welcome to Python.org"
            assert link_msg["link_desc"] == "Python website"
            assert link_msg["link_url"] == "https://www.python.org"

            # Also check /api/media/links endpoint
            resp_links = client.get("/api/media/links?chat_id=1&chat_type=contact")
            assert resp_links.status_code == 200
            links = resp_links.get_json()
            assert len(links) == 1
            assert links[0]["link_title"] == "Welcome to Python.org"

    def test_ios_link_preview_with_message_data_item(self, tmp_path):
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
                ZTITLE TEXT,
                ZMEDIAURL TEXT,
                ZMETADATA BLOB
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
            CREATE TABLE ZWAMESSAGEDATAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZINDEX INTEGER,
                ZMESSAGE INTEGER,
                ZTITLE TEXT,
                ZSUMMARY TEXT,
                ZMATCHEDTEXT TEXT,
                ZTHUMBNAILPATH TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Bob');
        """)
        # Message 10: dedicated link (type 7) with ZWAMESSAGEDATAITEM thumbnail
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA) VALUES (1, NULL, 'Python Main Site', NULL)"
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (10, 1, 0, 700000000.0, 'STANZA_MDI_1', 7, 1, NULL, NULL, 'Bob', 'https://www.python.org', '15559876543@s.whatsapp.net')
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGEDATAITEM (Z_PK, ZINDEX, ZMESSAGE, ZTITLE, ZSUMMARY, ZMATCHEDTEXT, ZTHUMBNAILPATH)
            VALUES (1, 0, 10, 'Python MDI Title', 'Python description from summary', 'https://www.python.org', 'Media/thumb_sample.thumb')
        """)

        # Message 11: plain text (type 0) with inline link extracted into ZWAMESSAGEDATAITEM
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (11, 1, 0, 700000100.0, 'STANZA_MDI_2', 0, NULL, NULL, NULL, 'Bob', 'Visit wa.me/12345 to chat', '15559876543@s.whatsapp.net')
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGEDATAITEM (Z_PK, ZINDEX, ZMESSAGE, ZTITLE, ZSUMMARY, ZMATCHEDTEXT, ZTHUMBNAILPATH)
            VALUES (2, 0, 11, 'WhatsApp Chat', 'Direct chat link', 'https://wa.me/12345', NULL)
        """)
        conn.commit()
        conn.close()

        # Write sample thumbnail file on disk
        media_dir = tmp_path / "Media"
        media_dir.mkdir(parents=True, exist_ok=True)
        fake_jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
            + b"\x00" * 32
            + b"\xff\xd9"
        )
        (media_dir / "thumb_sample.thumb").write_bytes(fake_jpeg)

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2

            m10 = next(m for m in msgs if m["msg_id"] == 10)
            assert m10["media_type"] == "link"
            assert m10["link_title"] == "Python Main Site"
            assert m10["link_desc"] == "Python description from summary"
            assert m10["link_url"] == "https://www.python.org"
            assert m10["has_thumbnail"] == 1

            m11 = next(m for m in msgs if m["msg_id"] == 11)
            assert m11["media_type"] == "link"
            assert m11["link_title"] == "WhatsApp Chat"
            assert m11["link_desc"] == "Direct chat link"
            assert m11["link_url"] == "https://wa.me/12345"

            # Check thumbnail endpoint
            thumb_resp = client.get("/api/thumbnail/10")
            assert thumb_resp.status_code == 200
            assert thumb_resp.mimetype == "image/jpeg"
            assert thumb_resp.data == fake_jpeg

    def test_recent_messages_caches_link_preview_info(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_text (
                message_row_id INTEGER PRIMARY KEY,
                description TEXT,
                page_title TEXT,
                url TEXT,
                matched_text TEXT,
                canonical_url TEXT,
                preview_type INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
        """)
        seed_android_db(wa_conn, make_archive_db(archive_path))
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type)
            VALUES (2, 10, 0, 1, 1700000001000, 'Check this https://github.com', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_text (message_row_id, description, page_title, url, matched_text, canonical_url, preview_type)
            VALUES (2, 'GitHub Desc', 'GitHub Title', 'https://github.com', 'https://github.com', 'https://github.com', 1)
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=True)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200

        archive_conn = sqlite3.connect(str(archive_path))
        archive_conn.row_factory = sqlite3.Row
        row = None
        for _ in range(20):
            row = archive_conn.execute("SELECT * FROM recent_messages WHERE msg_id = 2").fetchone()
            if row is not None:
                break
            time.sleep(0.1)
        assert row is not None
        assert row["link_title"] == "GitHub Title"
        assert row["link_desc"] == "GitHub Desc"
        assert row["link_url"] == "https://github.com"
        assert row["link_preview_type"] == 1
        archive_conn.close()

    def test_ios_photo_and_document_captions(self, tmp_path):
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
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
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

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Alice');
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL)
            VALUES
                (1, 'Media/photo.jpg', 'Beautiful Sunset', NULL, 'https://mmg.whatsapp.net/mms/photo.jpg'),
                (2, 'Media/reqs.docx', 'Please review this doc', NULL, 'https://mmg.whatsapp.net/mms/reqs.docx');

            -- Photo with caption (ZMESSAGETYPE=1, ZTEXT is NULL, ZTITLE is caption)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (20, 1, 0, 700000000.0, 'STANZA_PHOTO_20', 1, 1, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Document with caption (ZMESSAGETYPE=8, ZTEXT is filename, ZTITLE is caption)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (21, 1, 0, 700000010.0, 'STANZA_DOC_21', 8, 2, NULL, NULL, 'Alice', 'Requirements_2026.docx', '15559876543@s.whatsapp.net');
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
            photo_msg = next(m for m in msgs if m["msg_id"] == 20)
            doc_msg = next(m for m in msgs if m["msg_id"] == 21)

            assert photo_msg["text_body"] == "Beautiful Sunset"
            assert photo_msg["media_type"] == "image"
            assert photo_msg["link_url"] is None

            assert doc_msg["text_body"] == "Please review this doc"
            assert doc_msg["media_name"] == "Requirements_2026.docx"
            assert doc_msg["media_type"] == "document"
            assert doc_msg["link_url"] is None

    def test_ios_video_audio_classification_and_captions(self, tmp_path):
        """ZMESSAGETYPE=2 is Video with caption in ZTITLE, ZMESSAGETYPE=3 is Audio, ZMESSAGETYPE=54 is Video note."""
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
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
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

            INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID, ZPARTNERNAME)
            VALUES (1, NULL, '15559876543@s.whatsapp.net', 'Alice');

            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL)
            VALUES
                (1, 'Media/video.mp4', 'Goal celebration!', NULL, 'https://mmg.whatsapp.net/mms/video.mp4'),
                (2, 'Media/voice_note.opus', NULL, NULL, 'https://mmg.whatsapp.net/mms/voice.opus'),
                (3, 'Media/ptv_note.mp4', NULL, NULL, 'https://mmg.whatsapp.net/mms/ptv.mp4');

            -- Message 30: Video with caption (ZMESSAGETYPE=2, ZTEXT is NULL, ZTITLE is caption)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (30, 1, 0, 700000000.0, 'STANZA_VID_30', 2, 1, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Message 31: Audio voice note (ZMESSAGETYPE=3, ZTEXT is NULL, ZTITLE is NULL)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (31, 1, 0, 700000010.0, 'STANZA_AUD_31', 3, 2, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Message 32: Push-to-video circular note (ZMESSAGETYPE=54)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (32, 1, 0, 700000020.0, 'STANZA_PTV_32', 54, 3, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Message 33: Reply quoting the video (ZPARENTMESSAGE=30)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (33, 1, 1, 700000030.0, 'STANZA_REPLY_33', 0, NULL, NULL, 30, 'Me', 'Awesome video!', NULL);

            -- Message 34: Reply quoting the audio (ZPARENTMESSAGE=31)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (34, 1, 1, 700000040.0, 'STANZA_REPLY_34', 0, NULL, NULL, 31, 'Me', 'Heard your message', NULL);
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
            assert len(msgs) == 5
            vid_msg = next(m for m in msgs if m["msg_id"] == 30)
            aud_msg = next(m for m in msgs if m["msg_id"] == 31)
            ptv_msg = next(m for m in msgs if m["msg_id"] == 32)
            reply_vid = next(m for m in msgs if m["msg_id"] == 33)
            reply_aud = next(m for m in msgs if m["msg_id"] == 34)

            # Video asserts: type is video and caption is preserved
            assert vid_msg["media_type"] == "video"
            assert vid_msg["text_body"] == "Goal celebration!"

            # Audio asserts: type is audio
            assert aud_msg["media_type"] == "audio"

            # Video note assert: type is video
            assert ptv_msg["media_type"] == "video"

            # Quoted reply previews
            assert "🎥 Video" in reply_vid["quoted_text"]
            assert "🎵 Audio" in reply_aud["quoted_text"]

    def test_ios_quoted_media_reply_no_url_leak(self, tmp_path):
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
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
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

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Alice');
        """)

        def encode_varint(v):
            res = bytearray()
            while v > 0x7F:
                res.append((v & 0x7F) | 0x80)
                v >>= 7
            res.append(v & 0x7F)
            return bytes(res)

        stanza_bytes = b"STANZA_ORIG_100"
        sender_bytes = b"15559876543@s.whatsapp.net"
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

        im_payload = encode_varint((16 << 3) | 2) + encode_varint(len(fake_jpeg)) + fake_jpeg
        t19_sub3 = encode_varint((3 << 3) | 2) + encode_varint(len(im_payload)) + im_payload

        metadata_blob = (
            encode_varint((5 << 3) | 2)
            + encode_varint(len(stanza_bytes))
            + stanza_bytes
            + encode_varint((6 << 3) | 2)
            + encode_varint(len(sender_bytes))
            + sender_bytes
            + encode_varint((19 << 3) | 2)
            + encode_varint(len(t19_sub3))
            + t19_sub3
        )

        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (10, 'Media/moon.jpg', 'Look at moon', NULL, 'https://mmg.whatsapp.net/mms/moon.jpg')"
        )
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (11, NULL, NULL, ?, 'https://mmg.whatsapp.net/mms/dummy_url')",
            (metadata_blob,),
        )

        # Message 100: Photo
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (100, 1, 0, 700000000.0, 'STANZA_ORIG_100', 1, 10, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net')
        """)

        # Message 101: Reply to photo (ZMESSAGETYPE=0, ZTEXT='With your phone?', ZMEDIAITEM=11)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (101, 1, 1, 700000010.0, 'STANZA_REPLY_101', 0, 11, NULL, NULL, 'Me', 'With your phone?', '15551234567@s.whatsapp.net')
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
            reply_msg = next(m for m in msgs if m["msg_id"] == 101)

            # Quoted metadata should be extracted and linked
            assert reply_msg["quoted_msg_id"] == 100
            assert reply_msg["quoted_sender"] == "Alice"
            assert reply_msg["quoted_has_thumb"] == 1
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)

            # WhatsApp MMS CDN URL must NOT leak as link preview!
            assert reply_msg["link_url"] is None
            assert reply_msg["link_title"] is None
            assert reply_msg["has_thumbnail"] == 0

            # Jump to timestamp via /api/messages/at
            ts_ms = reply_msg["quoted_ts"]
            resp_at = client.get(f"/api/messages/at?chat_id=1&ts={ts_ms}")
            assert resp_at.status_code == 200
            at_msgs = resp_at.get_json()
            assert any(m["msg_id"] == 100 for m in at_msgs)

            # Thumbnail fallback recovery for message 100 from reply quote
            resp_thumb = client.get("/api/thumbnail/100")
            assert resp_thumb.status_code == 200
            assert resp_thumb.data == fake_jpeg

    def test_ios_quoted_photo_reply_older_jump_and_missing_media_redirection(self, tmp_path):
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
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
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

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Alice');
        """)

        def encode_varint(v):
            res = bytearray()
            while v > 0x7F:
                res.append((v & 0x7F) | 0x80)
                v >>= 7
            res.append(v & 0x7F)
            return bytes(res)

        stanza_bytes = b"STANZA_OLDER_PHOTO_50"
        sender_bytes = b"15559876543@s.whatsapp.net"
        caption_bytes = b"Stunning sunset in the mountains"
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

        # Protobuf submessage for image (field 3) containing thumbnail and caption (subfield 3)
        t3_caption = encode_varint((3 << 3) | 2) + encode_varint(len(caption_bytes)) + caption_bytes
        im_payload = (
            encode_varint((16 << 3) | 2) + encode_varint(len(fake_jpeg)) + fake_jpeg + t3_caption
        )
        t19_sub3 = encode_varint((3 << 3) | 2) + encode_varint(len(im_payload)) + im_payload

        reply_metadata = (
            encode_varint((5 << 3) | 2)
            + encode_varint(len(stanza_bytes))
            + stanza_bytes
            + encode_varint((6 << 3) | 2)
            + encode_varint(len(sender_bytes))
            + sender_bytes
            + encode_varint((19 << 3) | 2)
            + encode_varint(len(t19_sub3))
            + t19_sub3
        )

        # Message 50: Photo with caption whose local media file is missing on device (ZMEDIALOCALPATH is NULL)
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (50, NULL, 'Stunning sunset in the mountains', NULL, 'https://mmg.whatsapp.net/mms/photo50.enc')"
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (50, 1, 0, 700000000.0, 'STANZA_OLDER_PHOTO_50', 1, 50, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net')
        """)

        # Add 60 intervening text messages so Message 50 is older than the default 50-message window
        for i in range(1, 61):
            conn.execute(f"""
                INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
                VALUES ({100 + i}, 1, 0, {700000000.0 + i * 10}, 'STANZA_{100 + i}', 0, NULL, NULL, NULL, 'Alice', 'Intervening message {i}', '15559876543@s.whatsapp.net')
            """)

        # Message 200: Recent reply quoting the older photo (Message 50)
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (200, NULL, NULL, ?, 'https://mmg.whatsapp.net/mms/reply.enc')",
            (reply_metadata,),
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (200, 1, 1, 700001000.0, 'STANZA_REPLY_200', 0, 200, NULL, NULL, 'Me', 'Where was this taken?', '15551234567@s.whatsapp.net')
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Fetch recent messages (Message 50 is older and thus not in the initial 50 window)
            resp = client.get("/api/messages?chat_id=1&chat_type=contact&limit=50")
            assert resp.status_code == 200
            recent_msgs = resp.get_json()
            assert len(recent_msgs) == 50
            assert not any(m["msg_id"] == 50 for m in recent_msgs)

            reply_msg = next(m for m in recent_msgs if m["msg_id"] == 200)
            assert reply_msg["quoted_msg_id"] == 50
            assert reply_msg["quoted_has_thumb"] == 1
            assert "Stunning sunset in the mountains" in reply_msg["quoted_text"]
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)

            # 2. Redirect/jump via /api/messages/at at the quoted message timestamp
            target_ts = reply_msg["quoted_ts"]
            resp_at = client.get(f"/api/messages/at?chat_id=1&chat_type=contact&ts={target_ts}")
            assert resp_at.status_code == 200
            at_msgs = resp_at.get_json()

            # Message 50 must NOT be dropped by _BASE_IOS_FILTER despite ZMEDIALOCALPATH being NULL
            target_msg = next((m for m in at_msgs if m["msg_id"] == 50), None)
            assert target_msg is not None
            assert target_msg["media_type"] == "image"
            assert target_msg["text_body"] == "Stunning sunset in the mountains"
            assert target_msg["archive_path"] is None

    def test_android_quoted_photo_reply_thumbnail_and_caption_jump(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                jid_row_id INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0,
                subject TEXT
            );
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                server TEXT NOT NULL,
                raw_string TEXT
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
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
            CREATE TABLE message_quoted_media (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                file_path TEXT,
                file_size INTEGER,
                media_name TEXT
            );
        """)
        conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO chat (_id, jid_row_id, hidden, subject) VALUES (1, 1, 0, NULL)")

        # Original photo from contact with caption
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (10, 1, 0, 'PHOTO_STANZA_10', 1, 1000000, 1, 'Beautiful lake view')
        """)

        # Generate 55 subsequent messages to push message 10 out of initial 50-message paging window
        for i in range(11, 66):
            conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
                "VALUES (?, 1, 0, ?, 1, ?, 0, ?)",
                (i, f"STANZA_{i}", 1000000 + i * 1000, f"Chat message {i}"),
            )

        # Replying message quoting the photo
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 'STANZA_REPLY_100', NULL, 2000000, 0, 'Where was this lake?')
        """)
        conn.execute("""
            INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, sender_jid_row_id, key_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 0, 1, 'PHOTO_STANZA_10', 1000000, 1, NULL)
        """)
        conn.execute(
            "INSERT INTO message_quoted_media (message_row_id, thumbnail) VALUES (100, ?)",
            (fake_jpeg,),
        )
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Fetch recent messages
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact&limit=50")
            assert resp.status_code == 200
            recent_msgs = resp.get_json()
            assert not any(m["msg_id"] == 10 for m in recent_msgs)

            reply_msg = next(m for m in recent_msgs if m["msg_id"] == 100)
            assert reply_msg["quoted_msg_id"] == 10
            assert reply_msg["quoted_has_thumb"] == 1
            assert "Beautiful lake view" in reply_msg["quoted_text"]
            assert reply_msg["quoted_ts"] == 1000000

            # 2. Thumbnail endpoint verification
            # Direct hit on replying message holding message_quoted_media
            thumb_reply = client.get("/api/thumbnail/100")
            assert thumb_reply.status_code == 200
            assert thumb_reply.data == fake_jpeg

            # Hit on quoted message bridged via reply
            thumb_orig = client.get("/api/thumbnail/10")
            assert thumb_orig.status_code == 200
            assert thumb_orig.data == fake_jpeg

            # 3. Jump to target timestamp via /api/messages/at
            resp_at = client.get(
                "/api/messages/at?chat_id=15551234567&chat_type=contact&ts=1000000"
            )
            assert resp_at.status_code == 200
            at_msgs = resp_at.get_json()
            target_msg = next((m for m in at_msgs if m["msg_id"] == 10), None)
            assert target_msg is not None
            assert target_msg["media_type"] == "image"
            assert target_msg["text_body"] == "Beautiful lake view"

    def test_android_quoted_photo_reply_thumbnail_on_orig_and_cache_self_heal(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                jid_row_id INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0,
                subject TEXT
            );
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                server TEXT NOT NULL,
                raw_string TEXT
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
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
            CREATE TABLE message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                file_path TEXT,
                file_size INTEGER,
                media_name TEXT
            );
        """)
        conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO chat (_id, jid_row_id, hidden, subject) VALUES (1, 1, 0, NULL)")

        # Original photo message with thumbnail in message_thumbnail
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (10, 1, 0, 'PHOTO_STANZA_10', 1, 1000000, 1, 'Mountain Peak')
        """)
        conn.execute(
            "INSERT INTO message_thumbnail (message_row_id, thumbnail) VALUES (10, ?)",
            (fake_jpeg,),
        )

        # Replying text message quoting the photo; message_quoted_media does NOT exist
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 'STANZA_REPLY_100', NULL, 2000000, 0, 'Awesome mountain!')
        """)
        conn.execute("""
            INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, sender_jid_row_id, key_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 0, 1, 'PHOTO_STANZA_10', 1000000, 1, 'Mountain Peak')
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        # Pre-seed recent_messages with quoted_has_thumb = 0 and 📷 prefix to test self-heal
        archive_conn.execute("""
            INSERT INTO recent_messages (
                chat_id, chat_type, msg_id, timestamp_ms, sender, from_me,
                media_type, text_body, quoted_msg_id, quoted_text,
                quoted_sender, quoted_ts, quoted_has_thumb
            ) VALUES (
                '15551234567', 'contact', 100, 2000000, 'me', 1,
                'text', 'Awesome mountain!', 10, '📷 Mountain Peak',
                'Contact', 1000000, 0
            )
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact&limit=50")
            assert resp.status_code == 200
            msgs = resp.get_json()
            reply = next(m for m in msgs if m["msg_id"] == 100)
            assert reply["quoted_msg_id"] == 10
            assert reply["quoted_has_thumb"] == 1

            # Thumbnail endpoint: both quoted_msg_id and reply_msg_id must resolve
            thumb_quoted = client.get("/api/thumbnail/10")
            assert thumb_quoted.status_code == 200
            assert thumb_quoted.data == fake_jpeg

            thumb_reply = client.get("/api/thumbnail/100")
            assert thumb_reply.status_code == 200
            assert thumb_reply.data == fake_jpeg


class TestPinnedMessagesRichPreviews:
    """Tests for pinned message resolution, anti-stanza-ID protection, and preview richness."""

    def test_ios_pinned_resolves_type_66_stanza_id_when_parent_is_null(self, tmp_path):
        """iOS ZMESSAGETYPE=66 with ZPARENTMESSAGE=NULL resolves to target message via ZSTANZAID."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            -- Target message: text
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (10, 1, 0, 720000000, 'Important meeting agenda', 0, '3EB0TARGETSTANZAPIN1');

            -- Announcement message (type 66): ZTEXT contains target stanza ID, ZPARENTMESSAGE is NULL
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPARENTMESSAGE)
            VALUES (11, 1, 0, 720000050, '3EB0TARGETSTANZAPIN1', 66, '3EB0ANNOUNCESTANZAPIN1', NULL);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            # Must resolve to target message PK 10, NOT the announcement PK 11
            assert pin["id"] == 10
            # Must show target text, NOT the raw stanza ID
            assert pin["text_body"] == "Important meeting agenda"
            assert pin["is_pinned"] is True

    def test_ios_pinned_media_richness(self, tmp_path):
        """iOS pinned media message projects media_type, caption, and filename via ZWAMEDIAITEM."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
        if "ZMOVIEDURATION" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZMOVIEDURATION INTEGER")
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZMOVIEDURATION)
            VALUES (101, 'Sunset at the lake', 'Media/Photos/sunset.jpg', 0),
                   (102, 'Sprint Demo Recording', 'Media/Videos/demo.mp4', 125);

            -- Photo message with caption in media item
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZMEDIAITEM)
            VALUES (20, 1, 0, 720000100, NULL, 1, '3EB0PHOTOSUNSET1', 101);

            -- Video message with caption and duration
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZMEDIAITEM)
            VALUES (21, 1, 0, 720000200, NULL, 2, '3EB0VIDEODEMO1', 102);

            -- Pin announcements pointing to the media messages
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPARENTMESSAGE)
            VALUES (22, 1, 0, 720000150, '3EB0PHOTOSUNSET1', 66, '3EB0PINANNOUNCE1', NULL),
                   (23, 1, 0, 720000250, '3EB0VIDEODEMO1', 66, '3EB0PINANNOUNCE2', NULL);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 2

            video_pin = next(p for p in pins if p["id"] == 21)
            assert video_pin["media_type"] == "video"
            assert video_pin["text_body"] == "Sprint Demo Recording"
            assert video_pin["duration"] == 125
            assert video_pin["file_name"] == "demo.mp4"

            photo_pin = next(p for p in pins if p["id"] == 20)
            assert photo_pin["media_type"] == "photo"
            assert photo_pin["text_body"] == "Sunset at the lake"
            assert photo_pin["file_name"] == "sunset.jpg"

    def test_ios_pinned_unresolved_never_exposes_stanza_id(self, tmp_path):
        """When an iOS type 66 pin targets a missing message, raw stanza ID is suppressed."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            -- Announcement pointing to a purged message
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPARENTMESSAGE)
            VALUES (30, 1, 0, 720000050, '3EB0DELETEDSTANZAPIN999', 66, '3EB0ANNOUNCE30', NULL);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            # Raw stanza ID must NOT be leaked
            assert "3EB0" not in pin["text_body"]
            assert pin["text_body"] == "Pinned message"

    def test_android_pinned_media_richness(self, tmp_path):
        """Android pinned messages project media_type, duration, file_name, and place_name."""
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
                CREATE TABLE message_location (
                    message_row_id INTEGER PRIMARY KEY,
                    latitude REAL,
                    longitude REAL,
                    place_name TEXT,
                    place_address TEXT
                );

                INSERT INTO jid VALUES (1, '15551111', '15551111@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, 'Pinned Chat');

                -- Message 50: Photo with file_path and text caption
                INSERT INTO message VALUES (50, 10, 0, 1, 1793970000000, 'Mountain peak', 1, 'KEY_PHOTO_50');
                INSERT INTO message_media VALUES (50, '/storage/emulated/0/WhatsApp/Media/peak.jpg', 'image/jpeg', 0, 'peak.jpg');
                INSERT INTO message_add_on VALUES (1, 50, 10, 0, 1, 'ADDON_PIN_50', 1793970500000, 79, 0, 9999999999000);
                INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);

                -- Message 51: Audio with media_duration
                INSERT INTO message VALUES (51, 10, 0, 1, 1793970100000, '', 3, 'KEY_AUDIO_51');
                INSERT INTO message_media VALUES (51, '/storage/emulated/0/WhatsApp/Media/voice.opus', 'audio/ogg', 82, 'voice.opus');
                INSERT INTO message_add_on VALUES (2, 51, 10, 0, 1, 'ADDON_PIN_51', 1793970600000, 79, 0, 9999999999000);
                INSERT INTO message_add_on_pin_in_chat VALUES (2, 1, 1793970600000);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 2

            photo_pin = next(p for p in pins if p["id"] == 50)
            assert photo_pin["media_type"] == "photo"
            assert photo_pin["text_body"] == "Mountain peak"
            assert photo_pin["file_name"] == "peak.jpg"

            audio_pin = next(p for p in pins if p["id"] == 51)
            assert audio_pin["media_type"] == "audio"
            assert audio_pin["duration"] == 82

    def test_preserved_pinned_media_richness(self, tmp_path):
        """Preserved pinned messages project media_type, duration, file_name, and place_name."""
        make_ios_db(tmp_path / "ChatStorage.sqlite")
        pres_db = tmp_path / "preserved_messages.db"
        with sqlite3.connect(str(pres_db)) as conn:
            conn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER,
                    chat_id TEXT,
                    chat_type TEXT,
                    from_me INTEGER,
                    sender_name TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    media_type TEXT,
                    media_duration INTEGER,
                    original_path TEXT,
                    place_name TEXT,
                    is_pinned INTEGER DEFAULT 0,
                    pin_expiry_ts INTEGER
                );
                INSERT INTO preserved_messages (
                    msg_id, chat_id, chat_type, from_me, sender_name, timestamp_ms,
                    text_body, media_type, media_duration, original_path, place_name, is_pinned, pin_expiry_ts
                ) VALUES (
                    88, '1001', 'contact', 0, 'Alice', 1700000000000,
                    'Check out this document', 'document', 0, 'Media/Documents/Specs.pdf', '', 1, 9999999999000
                );
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1001/pinned")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            doc_pin = pins[0]
            assert doc_pin["media_type"] == "document"
            assert doc_pin["file_name"] == "Specs.pdf"
            assert doc_pin["text_body"] == "Check out this document"
            assert doc_pin["is_pinned"] is True

    def test_frontend_message_preview_contracts(self, tmp_path):
        """app.js contains _formatMessagePreview, stanza ID sanitization, and shared formatting."""
        make_ios_db(tmp_path / "ChatStorage.sqlite")
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/static/app.js")
            assert resp.status_code == 200
            js = resp.get_data(as_text=True)
            assert "function _formatMessagePreview(m)" in js
            assert "_isRawStanzaId" in js
            assert "_isUnreadableToken" in js
            assert "snippetEl.textContent = _formatMessagePreview(pin);" in js

    def test_ios_pinned_album_container_never_treated_as_pin(self, tmp_path):
        """iOS type 66 media album containers with ZMEDIAITEM must NEVER be treated as pins."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH)
            VALUES (101, NULL, NULL);

            -- Media album container (type 66 with media item and base64 push name)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZMEDIAITEM, ZPUSHNAME)
            VALUES (50, 1, 0, 720000000, NULL, 66, '3A86D5BC08A513A4AEE7', 101, 'CKji9NUGIABIAZABAPABAtgCu5PzutWWlwM=');

            -- Normal message in same chat
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (51, 1, 0, 720000010, 'Hello there', 0, '3EB0NORMALMSG51');
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 0
            assert resp.get_json()["pinned_message"] is None

            # Verify GET /api/messages does not mark messages as is_pinned = 1
            resp_m = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp_m.status_code == 200
            for m in resp_m.get_json():
                assert m.get("is_pinned", 0) == 0

    def test_ios_pinned_deleted_in_extchat_thread_id_excluded(self, tmp_path):
        """In ExtChatDatabase.sqlite, thread_id with deleted = 1 is excluded from pins."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (60, 1, 0, 720000000, 'Unpinned message', 0, '3EB0UNPINNED60');
        """)
        cs_conn.commit()
        cs_conn.close()

        ext_db = tmp_path / "ExtChatDatabase.sqlite"
        ext_conn = sqlite3.connect(str(ext_db))
        ext_conn.executescript("""
            CREATE TABLE thread_id (
                id INTEGER PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                key_id TEXT NOT NULL,
                pin_timestamp INTEGER,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE thread_messages (
                id INTEGER PRIMARY KEY,
                stanza_id TEXT NOT NULL,
                thread_key_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL
            );
            -- An unpinned message (deleted = 1)
            INSERT INTO thread_id (id, chat_jid, key_id, pin_timestamp, deleted)
            VALUES (1, '12345678@s.whatsapp.net', '3EB0UNPINNED60', 1700000000, 1);
            INSERT INTO thread_messages (id, stanza_id, thread_key_id, chat_jid)
            VALUES (1, '3EB0UNPINNED60', '3EB0UNPINNED60', '12345678@s.whatsapp.net');
        """)
        ext_conn.commit()
        ext_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 0

            resp_m = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp_m.status_code == 200
            msgs = resp_m.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_pinned"] == 0

    def test_ios_pinned_unreadable_token_push_name_sanitized(self, tmp_path):
        """Base64 push name or LID token is sanitized and not returned as sender_name."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            -- Target message with base64 binary push name
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPUSHNAME, ZFROMJID)
            VALUES (70, 1, 0, 720000000, 'Important pinned note', 0, '3EB0VALIDPIN70', 'CKji9NUGIABIAZABAPABAtgCu5PzutWWlwM=', '393481234567@s.whatsapp.net');

            -- Valid pin announcement targeting message 70
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (71, 1, 0, 720000050, '3EB0VALIDPIN70', 66, '3EB0PINANNOUNCE71');
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            # Must NOT expose base64 token
            assert "CKji9NUG" not in pin["sender_name"]
            # Must resolve to phone number or Unknown
            assert pin["sender_name"] in ("+393481234567", "393481234567", "Unknown")

    def test_ios_pinned_chat_session_metadata_resolution(self, tmp_path):
        """Modern iOS pin resolution via ZWACHATSESSION.ZLASTMESSAGETEXT protobuf."""

        def encode_varint(v: int) -> bytes:
            b = bytearray()
            while True:
                part = v & 0x7F
                v >>= 7
                if v:
                    b.append(part | 0x80)
                else:
                    b.append(part)
                    break
            return bytes(b)

        # Build submessage tag 4 (duration and status)
        sub4 = bytearray()
        sub4.extend(encode_varint((1 << 3) | 0))  # tag 1: duration
        sub4.extend(encode_varint(2592000))
        sub4.extend(encode_varint((2 << 3) | 0))  # tag 2: status (1 = active)
        sub4.extend(encode_varint(1))

        # Build tag 31 submessage
        target_key = b"120363000000000005@g.us_3EB0TARGETPIN10_0_145500000000005@lid"
        sub31 = bytearray()
        sub31.extend(encode_varint((1 << 3) | 2))  # tag 1: action stanza ID
        sub31.extend(encode_varint(len(b"3EB0PINACT10")))
        sub31.extend(b"3EB0PINACT10")
        sub31.extend(encode_varint((2 << 3) | 2))  # tag 2: target key
        sub31.extend(encode_varint(len(target_key)))
        sub31.extend(target_key)
        sub31.extend(encode_varint((3 << 3) | 2))  # tag 3: pinner LID
        sub31.extend(encode_varint(len(b"145500000000005@lid")))
        sub31.extend(b"145500000000005@lid")
        sub31.extend(encode_varint((4 << 3) | 2))  # tag 4: meta
        sub31.extend(encode_varint(len(sub4)))
        sub31.extend(sub4)
        sub31.extend(encode_varint((5 << 3) | 0))  # tag 5: pin ts
        sub31.extend(encode_varint(1728307200000))

        # Root message
        root = bytearray()
        root.extend(encode_varint((31 << 3) | 2))
        root.extend(encode_varint(len(sub31)))
        root.extend(sub31)
        b64_payload = base64.b64encode(bytes(root)).decode("ascii")

        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZLASTMESSAGETEXT TEXT")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER,
                Z_OPT INTEGER,
                ZCONTACTABID INTEGER,
                ZISACTIVE INTEGER,
                ZISADMIN INTEGER,
                ZSENDERKEYSENT INTEGER,
                ZCHATSESSION INTEGER,
                ZRECENTGROUPCHAT INTEGER,
                ZCONTACTIDENTIFIER TEXT,
                ZCONTACTNAME TEXT,
                ZFIRSTNAME TEXT,
                ZMEMBERJID TEXT
            );

            CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER,
                Z_OPT INTEGER,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );

            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '120363000000000005@g.us', 'Project Team', 0);

            INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID)
            VALUES (100, 1, '145500000000005@lid');

            INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME)
            VALUES (200, '145500000000005@lid', 'Alice Smith');

            -- Target message
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZGROUPMEMBER, ZFROMJID)
            VALUES (10, 1, 0, 750000000, 'Meeting notes and roadmap', 0, '3EB0TARGETPIN10', 100, '120363000000000005@g.us');
        """)
        cs_conn.execute(
            "UPDATE ZWACHATSESSION SET ZLASTMESSAGETEXT = ? WHERE Z_PK = 1",
            (b64_payload,),
        )
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            assert pin["id"] == 10
            assert pin["sender_name"] == "Alice Smith"
            assert pin["text"] == "Meeting notes and roadmap"
            assert pin["is_pinned"] is True
            assert pin["text"] == "Meeting notes and roadmap"
            assert pin["is_pinned"] is True

    def test_android_and_ios_video_note_contracts(self, tmp_path):
        """Android (message_type=28) and iOS (ZMESSAGETYPE=54) video notes expose is_video_note and raw_msg_type."""
        android_dir = tmp_path / "android"
        android_dir.mkdir()
        wa_conn = make_android_db(android_dir / "msgstore.db")
        arc_conn = make_archive_db(android_dir / ".wa_media_archiver.db")

        wa_conn.execute("INSERT INTO jid VALUES (1, '15551234567', '15551234567@s.whatsapp.net')")
        wa_conn.execute("INSERT INTO chat VALUES (10, 1, 'Alice Chat', 0, 1700000000000, 102)")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
            "VALUES (101, 10, 0, 1, 1700000000000, '', 28, 'STANZA_VN_ANDROID')"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
            "VALUES (102, 10, 0, 1, 1700000001000, '', 81, 'STANZA_VN_ANDROID_81')"
        )
        wa_conn.execute(
            "INSERT INTO message_media (_id, message_row_id, chat_row_id, file_path, media_name, mime_type, message_url) "
            "VALUES (101, 101, 10, 'Media/WhatsApp Video Notes/202601/VID-101.mp4', 'VID-101.mp4', 'video/mp4', '')"
        )
        wa_conn.execute(
            "INSERT INTO message_media (_id, message_row_id, chat_row_id, file_path, media_name, mime_type, message_url) "
            "VALUES (102, 102, 10, 'Media/WhatsApp Video Notes/202501/PTV-20250101-WA0010.mp4', 'PTV-20250101-WA0010.mp4', 'video/mp4', '')"
        )
        wa_conn.commit()

        arc_conn.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Video Notes/202601/VID-101.mp4', x'01020304')"
        )
        arc_conn.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Video Notes/202501/PTV-20250101-WA0010.mp4', x'05060708')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/WhatsApp Video Notes/202601/VID-101.mp4', 'Contacts/Alice/2026/Received/VID-101.mp4')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/WhatsApp Video Notes/202501/PTV-20250101-WA0010.mp4', 'Contacts/Alice/2025/Received/PTV-20250101-WA0010.mp4')"
        )
        arc_conn.commit()

        app_android = viewer.create_app(android_dir, rescan=False)
        app_android.config["TESTING"] = True
        with app_android.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 2
            by_id = {m["msg_id"]: m for m in data}
            assert 101 in by_id
            assert 102 in by_id
            assert by_id[101]["is_video_note"] == 1
            assert by_id[101]["media_type"] == "video"
            assert by_id[101]["archive_path"] == "Contacts/Alice/2026/Received/VID-101.mp4"
            assert by_id[102]["is_video_note"] == 1
            assert by_id[102]["media_type"] == "video"
            assert (
                by_id[102]["archive_path"] == "Contacts/Alice/2025/Received/PTV-20250101-WA0010.mp4"
            )

            # Check chat snippet
            chat_resp = client.get("/api/chats")
            assert chat_resp.status_code == 200
            chats = chat_resp.get_json()
            assert len(chats) >= 1
            c = next(x for x in chats if str(x["id"]) in ("15551234567", "10"))
            assert c["last_msg_type"] == "video"

        # Now verify iOS ZMESSAGETYPE = 54
        ios_dir = tmp_path / "ios"
        ios_dir.mkdir()
        ios_conn = make_ios_db(ios_dir / "ChatStorage.sqlite")
        ios_arc = make_archive_db(ios_dir / ".wa_media_archiver.db")

        ios_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME) VALUES (1, '15559876543@s.whatsapp.net', 'Bob')"
        )
        ios_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH) VALUES (501, 'Media/VideoNotes/vn_501.mp4')"
        )
        ios_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZFROMJID, ZTOJID, ZMESSAGEDATE, ZMESSAGETYPE, ZMEDIAITEM, ZISFROMME, ZSTANZAID) "
            "VALUES (501, 1, '15559876543@s.whatsapp.net', 'me@s.whatsapp.net', 700000000, 54, 501, 0, 'STANZA_VN_IOS')"
        )
        ios_conn.commit()

        ios_arc.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Message/Media/VideoNotes/vn_501.mp4', x'05060708')"
        )
        ios_arc.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Message/Media/VideoNotes/vn_501.mp4', 'Contacts/Bob/2026/Received/vn_501.mp4')"
        )
        ios_arc.commit()

        app_ios = viewer.create_app(ios_dir, rescan=False)
        app_ios.config["TESTING"] = True
        with app_ios.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            msg = data[0]
            assert msg["is_video_note"] == 1
            assert msg["media_type"] == "video"
            assert msg["archive_path"] == "Contacts/Bob/2026/Received/vn_501.mp4"
