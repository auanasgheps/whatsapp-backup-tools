"""Unit tests for timeline windowing, navigation, and starred/pinned queries.

Tests wab_viewer.queries.starred_pinned, wab_viewer.queries.group_members,
and wab_viewer.routes.messages with 100% synthetic data.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

from tests.conftest import make_android_db
from wab_viewer import main as viewer
from wab_viewer.queries import group_members, starred_pinned
from wab_viewer.routes import messages as msg_routes

# ---------------------------------------------------------------------------
# Protobuf Helper
# ---------------------------------------------------------------------------


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val)
    return bytes(out)


def _encode_field(tag: int, wire: int, payload: bytes | int) -> bytes:
    key = (tag << 3) | wire
    head = _encode_varint(key)
    if wire == 0:
        return head + _encode_varint(int(payload))
    elif wire == 2:
        return head + _encode_varint(len(payload)) + bytes(payload)
    elif wire in (1, 5):
        return head + bytes(payload)
    return head


# ---------------------------------------------------------------------------
# 1. Tests for Token Parsing and Wire Helpers (starred_pinned.py)
# ---------------------------------------------------------------------------


class TestProtoAndTokenParsing:
    """Tests for stanza, base64, and protobuf wire parsing in starred_pinned."""

    def test_is_stanza_id_valid_and_invalid(self) -> None:
        assert starred_pinned._is_stanza_id(None) is False
        assert starred_pinned._is_stanza_id("") is False
        assert starred_pinned._is_stanza_id("abc") is False
        assert starred_pinned._is_stanza_id("Hello World!") is False

        # 3EB0 format
        assert starred_pinned._is_stanza_id("3EB0A1B2C3D4E5F6") is True
        assert starred_pinned._is_stanza_id("3EB01234567890ABCDEF") is True

        # Hex 16-64 chars
        assert starred_pinned._is_stanza_id("0123456789ABCDEF") is True
        assert starred_pinned._is_stanza_id("0123456789abcdef0123456789abcdef") is True

        # Timestamp-counter
        assert starred_pinned._is_stanza_id("172900000000000-1") is True

    def test_is_unreadable_token_detection(self) -> None:
        assert starred_pinned._is_unreadable_token(None) is False
        assert starred_pinned._is_unreadable_token("") is False
        assert starred_pinned._is_unreadable_token(12345) is False  # type: ignore

        # Readable text
        assert starred_pinned._is_unreadable_token("Hello world") is False
        assert starred_pinned._is_unreadable_token("Dinner tonight?") is False

        # Stanza ID
        assert starred_pinned._is_unreadable_token("3EB01234567890ABCDEF") is True

        # Base64 tokens
        assert starred_pinned._is_unreadable_token("AQIDBAUGBwgJCgsMDQ4PEA==") is True
        assert starred_pinned._is_unreadable_token("C1234567890abcdef==") is True

        # LID string
        assert starred_pinned._is_unreadable_token("999123456789@lid") is True

    def test_parse_proto_fields_wire_types(self) -> None:
        data = bytearray()
        data.extend(_encode_field(1, 0, 150))  # tag 1, varint 150
        data.extend(_encode_field(2, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"))  # tag 2, 64-bit
        data.extend(_encode_field(3, 2, b"hello"))  # tag 3, length-delimited
        data.extend(_encode_field(4, 5, b"\x0a\x0b\x0c\x0d"))  # tag 4, 32-bit

        fields = starred_pinned._parse_proto_fields(bytes(data))
        assert len(fields) == 4
        assert fields[0] == (1, 0, 150)
        assert fields[1] == (2, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08")
        assert fields[2] == (3, 2, b"hello")
        assert fields[3] == (4, 5, b"\x0a\x0b\x0c\x0d")

    def test_parse_ios_chat_session_pins_active_and_expired(self) -> None:
        assert starred_pinned._parse_ios_chat_session_pins(None) == []
        assert starred_pinned._parse_ios_chat_session_pins("") == []
        assert starred_pinned._parse_ios_chat_session_pins("not_base_64!!!") == []

        # Construct pin 1: active pin (status 1)
        sub1 = bytearray()
        sub1.extend(_encode_field(2, 2, b"chat_3EB0PIN1_0_sender"))
        meta1 = bytearray()
        meta1.extend(_encode_field(1, 0, 86400))  # duration
        meta1.extend(_encode_field(2, 0, 1))  # status 1 = active
        sub1.extend(_encode_field(4, 2, bytes(meta1)))
        sub1.extend(_encode_field(5, 0, 1729000000000))  # timestamp_ms

        # Construct pin 2: unpinned/expired (status 2)
        sub2 = bytearray()
        sub2.extend(_encode_field(2, 2, b"chat_3EB0PIN2_0_sender"))
        meta2 = bytearray()
        meta2.extend(_encode_field(1, 0, 86400))
        meta2.extend(_encode_field(2, 0, 2))  # status 2 = unpinned
        sub2.extend(_encode_field(4, 2, bytes(meta2)))

        # Construct pin 3: duplicate of pin 1
        sub3 = bytearray(sub1)

        root = bytearray()
        root.extend(_encode_field(31, 2, bytes(sub1)))
        root.extend(_encode_field(34, 2, bytes(sub2)))
        root.extend(_encode_field(31, 2, bytes(sub3)))

        b64 = base64.b64encode(bytes(root)).decode("ascii")
        pins = starred_pinned._parse_ios_chat_session_pins(b64)

        assert len(pins) == 1
        assert pins[0]["stanza_id"] == "3EB0PIN1"
        assert pins[0]["duration"] == 86400
        assert pins[0]["status"] == 1
        assert pins[0]["pin_ts_sec"] == 1729000000.0


# ---------------------------------------------------------------------------
# 2. Tests for iOS Starred and Pinned Queries (starred_pinned.py)
# ---------------------------------------------------------------------------


class TestIOSStarredAndPinnedQueries:
    """Tests for _query_ios_starred, _query_ios_pinned, and candidate resolution."""

    def _setup_ios_db(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER,
                ZLASTMESSAGETEXT TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZVCARDNAME TEXT,
                ZLATITUDE REAL,
                ZLONGITUDE REAL,
                ZMETADATA BLOB
            );
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
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZSTARRED INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TEMP TABLE _ios_contacts (
                jid TEXT PRIMARY KEY,
                full_name TEXT,
                phone_number TEXT
            );
        """)
        return conn

    def test_query_ios_starred_across_media_types_and_senders(self, tmp_path) -> None:
        db_path = tmp_path / "starred_ios.sqlite"
        conn = self._setup_ios_db(db_path)

        # Chat session 1
        conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15550001@s.whatsapp.net', 'Partner One', NULL, NULL)"
        )
        # Push name
        conn.execute(
            "INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550002@s.whatsapp.net', 'Push Sender')"
        )
        # Temporary contact in _ios_contacts
        conn.execute(
            "INSERT INTO _ios_contacts VALUES ('15550003@s.whatsapp.net', 'Contact Three', '15550003')"
        )

        # Media item 10: photo
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (10, 'Media/photo.jpg', 'Photo Caption', NULL, NULL, NULL, NULL)"
        )
        # Media item 20: location
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (20, NULL, 'Coffee Shop', NULL, 37.77, -122.41, NULL)"
        )
        # Media item 30: document
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (30, 'Media/doc.pdf', 'Document Caption', NULL, NULL, NULL, NULL)"
        )

        # Message 1: Starred Photo from me
        conn.execute("""
            INSERT INTO ZWAMESSAGE (
                Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZSTARRED
            ) VALUES (1, 1, 1, 750000000.0, 'STZ_1', 1, 10, 1)
        """)
        # Message 2: Starred Location from push sender
        conn.execute("""
            INSERT INTO ZWAMESSAGE (
                Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZFROMJID, ZSTARRED
            ) VALUES (2, 1, 0, 750001000.0, 'STZ_2', 5, 20, '15550002@s.whatsapp.net', 1)
        """)
        # Message 3: Starred Document with filename in ZTEXT and caption in ZTITLE
        conn.execute("""
            INSERT INTO ZWAMESSAGE (
                Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZTEXT, ZFROMJID, ZSTARRED
            ) VALUES (3, 1, 0, 750002000.0, 'STZ_3', 8, 30, 'annual_report.pdf', '15550003@s.whatsapp.net', 1)
        """)
        # Message 4: Starred Deleted message from other
        conn.execute("""
            INSERT INTO ZWAMESSAGE (
                Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZFROMJID, ZSTARRED
            ) VALUES (4, 1, 0, 750003000.0, 'STZ_4', 14, '15550004@s.whatsapp.net', 1)
        """)
        conn.commit()

        # Archive contacts map
        arc_path = tmp_path / "arch.db"
        with sqlite3.connect(arc_path) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            aconn.execute(
                "INSERT INTO contacts VALUES ('15550004', 'Archive Four', 'Contacts/Four')"
            )

        aconn = sqlite3.connect(arc_path)
        aconn.row_factory = sqlite3.Row

        results = starred_pinned._query_ios_starred(conn, "1", aconn, fetch_limit=10)
        conn.close()
        aconn.close()

        assert len(results) == 4
        by_mid = {r["id"]: r for r in results}

        # Message 1 (Photo from me)
        assert by_mid[1]["sender_name"] == "You"
        assert by_mid[1]["media_type"] == "photo"
        assert by_mid[1]["text"] == "Photo Caption"

        # Message 2 (Location from push sender)
        assert by_mid[2]["sender_name"] == "Push Sender"
        assert by_mid[2]["media_type"] == "location"
        assert by_mid[2]["place_name"] == "Coffee Shop"

        # Message 3 (Document from contact three)
        assert by_mid[3]["sender_name"] == "Contact Three"
        assert by_mid[3]["media_type"] == "document"
        assert by_mid[3]["text"] == "Document Caption"
        assert by_mid[3]["file_name"] == "annual_report.pdf"

        # Message 4 (Deleted from archive four)
        assert by_mid[4]["sender_name"] == "Archive Four"
        assert by_mid[4]["media_type"] == "deleted"
        assert by_mid[4]["text"] == "This message was deleted"

    def test_query_ios_pinned_purged_target_suppresses_raw_stanza(self, tmp_path) -> None:
        db_path = tmp_path / "pinned_ios.sqlite"
        conn = self._setup_ios_db(db_path)

        conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15550001@s.whatsapp.net', 'Partner One', NULL, NULL)"
        )

        # Message 10 is an iOS pin announcement (ZMESSAGETYPE = 66) pointing to purged stanza 3EB0PURGED
        conn.execute("""
            INSERT INTO ZWAMESSAGE (
                Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZGROUPEVENTTYPE, ZTEXT
            ) VALUES (10, 1, 0, 750000000.0, 'PIN_ANNOUNCE', 66, 0, '3EB0PURGED1234567890')
        """)
        conn.commit()

        # Query pinned messages
        pinned = starred_pinned._query_ios_pinned(conn, None, "1", None)
        conn.close()

        assert len(pinned) == 1
        # When target message is purged from DB, raw_text stanza must be suppressed
        # and fallback text "Pinned message" used instead of the raw stanza ID
        assert pinned[0]["text"] == "Pinned message"
        assert "3EB0PURGED" not in pinned[0]["text"]

    def test_query_ios_pinned_active_protobuf_in_chatsession(self, tmp_path) -> None:
        db_path = tmp_path / "pinned_proto.sqlite"
        conn = self._setup_ios_db(db_path)

        # Build active pin protobuf in ZLASTMESSAGETEXT
        sub = bytearray()
        sub.extend(_encode_field(2, 2, b"chat_3EB0TARGET_0_sender"))
        meta = bytearray()
        meta.extend(_encode_field(1, 0, 86400))
        meta.extend(_encode_field(2, 0, 1))  # active
        sub.extend(_encode_field(4, 2, bytes(meta)))
        root = bytearray()
        root.extend(_encode_field(31, 2, bytes(sub)))
        b64_pin = base64.b64encode(bytes(root)).decode("ascii")

        conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15550001@s.whatsapp.net', 'Partner One', NULL, ?)",
            (b64_pin,),
        )
        # Target message exists
        conn.execute("""
            INSERT INTO ZWAMESSAGE (
                Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT
            ) VALUES (50, 1, 1, 750000000.0, '3EB0TARGET', 0, 'Important Pinned Note')
        """)
        conn.commit()

        pinned = starred_pinned._query_ios_pinned(conn, None, "1", None)
        conn.close()

        assert len(pinned) == 1
        assert pinned[0]["id"] == 50
        assert pinned[0]["text"] == "Important Pinned Note"
        assert pinned[0]["sender_name"] == "You"


# ---------------------------------------------------------------------------
# 3. Tests for Android Starred and Preserved Queries (starred_pinned.py)
# ---------------------------------------------------------------------------


class TestAndroidStarredAndPreservedQueries:
    """Tests for _query_android_starred and preserved starred/pinned queries."""

    def test_query_android_starred_missing_tables_returns_empty(self) -> None:
        conn = sqlite3.connect(":memory:")
        assert starred_pinned._query_android_starred(conn, "1", None, None, {}) == []

        conn.execute("CREATE TABLE message (_id INT)")
        assert starred_pinned._query_android_starred(conn, "1", None, None, {}) == []
        conn.close()

    def test_query_android_starred_full_flow(self, tmp_path) -> None:
        db_path = tmp_path / "msgstore_starred.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                starred INTEGER,
                message_type INTEGER,
                text_data TEXT,
                key_id TEXT
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
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            CREATE TEMP TABLE _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
            CREATE TEMP TABLE _lid_map_resolved (lid_row_id INTEGER PRIMARY KEY, phone_jid TEXT);
            CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '5550100', '5550100@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL);
            INSERT INTO message VALUES (100, 1, 0, 1, 1729000000000, 1, 5, 'Meet here', 'STZ_AND_1');
            INSERT INTO message_location VALUES (100, 40.7128, -74.0060, 'New York Hall', 'Manhattan');
            INSERT INTO _contacts_resolved VALUES ('5550100', 'Bob NY', 'Contacts/Bob');
        """)
        conn.commit()

        res = starred_pinned._query_android_starred(conn, "1", None, None, {})
        conn.close()

        assert len(res) == 1
        assert res[0]["id"] == 100
        assert res[0]["media_type"] == "location"
        assert res[0]["latitude"] == 40.7128
        assert res[0]["sender_name"] == "Bob NY"

    def test_query_android_pinned_active_and_revoked(self, tmp_path) -> None:
        db_path = tmp_path / "msgstore_pinned.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                message_add_on_type INTEGER,
                status INTEGER,
                expiry_timestamp INTEGER
            );
            CREATE TABLE message_add_on_pin_in_chat (
                message_add_on_row_id INTEGER PRIMARY KEY,
                pin_in_chat_state INTEGER
            );

            CREATE TEMP TABLE _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
            CREATE TEMP TABLE _lid_map_resolved (lid_row_id INTEGER PRIMARY KEY, phone_jid TEXT);
            CREATE TEMP TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '5550101', '5550101@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL);
            -- Message 10 is pinned
            INSERT INTO message VALUES (10, 1, 1, 1, 1729000000000, 0, 'Pinned android note');
            -- Add-on 1: message_add_on_type = 79 (pin), pin_in_chat_state = 1 (active)
            INSERT INTO message_add_on VALUES (1, 10, 79, 0, 9999999999999);
            INSERT INTO message_add_on_pin_in_chat VALUES (1, 1);
        """)
        conn.commit()

        pins = starred_pinned._query_android_pinned(conn, "1", "contact", None, {})
        conn.close()

        assert len(pins) == 1
        assert pins[0]["id"] == 10
        assert pins[0]["text"] == "Pinned android note"

    def test_query_preserved_starred_and_pinned(self, tmp_path) -> None:
        db_path = tmp_path / "preserved.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE preserved_messages (
                rowid INTEGER PRIMARY KEY,
                msg_id INTEGER,
                chat_id TEXT,
                chat_type TEXT,
                timestamp_ms INTEGER,
                sender TEXT,
                from_me INTEGER,
                text_body TEXT,
                media_type TEXT,
                media_name TEXT,
                is_starred INTEGER,
                is_pinned INTEGER,
                pin_expiry_ts INTEGER,
                latitude REAL,
                longitude REAL,
                location_name TEXT,
                original_path TEXT,
                media_duration INTEGER
            );
            INSERT INTO preserved_messages VALUES (
                1, 100, 'c1', 'contact', 1729000000000, 'Preserved Alice', 0, 'Star msg', 'text', NULL, 1, 0, NULL, NULL, NULL, NULL, NULL, 0
            );
            INSERT INTO preserved_messages VALUES (
                2, 200, 'c1', 'contact', 1729000050000, 'Preserved Bob', 1, 'Pin msg', 'location', NULL, 0, 1, NULL, 51.5074, -0.1278, 'London', NULL, 0
            );
        """)
        conn.commit()

        # Starred query
        starred = starred_pinned._query_preserved_starred(conn, "c1", fetch_limit=10)
        assert len(starred) == 1
        assert starred[0]["id"] == -1
        assert starred[0]["text"] == "Star msg"
        assert starred[0]["sender_name"] == "Preserved Alice"

        # Pinned query
        pinned = starred_pinned._query_preserved_pinned(conn, "c1")
        assert len(pinned) == 1
        assert pinned[0]["id"] == -2
        assert pinned[0]["media_type"] == "location"
        assert pinned[0]["place_name"] == "London"

        # Count starred
        count = starred_pinned._count_chat_starred(None, conn, "android", "c1", {})
        assert count == 1
        conn.close()


# ---------------------------------------------------------------------------
# 4. Tests for Group Members Roster (group_members.py)
# ---------------------------------------------------------------------------


class TestGroupMembersRosterQueries:
    """Tests for group_members roster resolution and fallbacks."""

    def test_android_group_members_gpu_and_fallback(self, tmp_path) -> None:
        db_path = tmp_path / "msgstore_roster.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
            CREATE TABLE group_participant_user (
                group_jid_row_id INTEGER,
                user_jid_row_id INTEGER
            );
            CREATE TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, 'group1', 'group1@g.us', 'g.us');
            INSERT INTO jid VALUES (2, '5550200', '5550200@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO chat VALUES (10, 1);
            INSERT INTO group_participant_user VALUES (1, 2);
        """)

        arc_path = tmp_path / "arc_roster.db"
        with sqlite3.connect(arc_path) as aconn:
            aconn.execute("CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT)")
            aconn.execute("INSERT INTO contacts VALUES ('5550200', 'Roster Member 2')")

        conn.execute(f"ATTACH DATABASE '{arc_path}' AS arch")
        roster = group_members._group_members_android(conn, "10")
        assert len(roster) == 1
        assert roster[0]["name"] == "Roster Member 2"
        assert roster[0]["number"] == "5550200"
        conn.close()

    def test_android_group_members_message_history_fallback(self, tmp_path) -> None:
        db_path = tmp_path / "msgstore_hist.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # No group_participant_user, fallback to message history
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT, server TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                sender_jid_row_id INTEGER,
                from_me INTEGER
            );
            CREATE TABLE _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '5550300', '5550300@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO chat VALUES (20, 1, NULL);
            INSERT INTO message VALUES (1, 20, 1, 0);
        """)

        arc_path = tmp_path / "arc_hist.db"
        with sqlite3.connect(arc_path) as aconn:
            aconn.execute("CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT)")
            aconn.execute("INSERT INTO contacts VALUES ('5550300', 'History Sender')")

        conn.execute(f"ATTACH DATABASE '{arc_path}' AS arch")
        roster = group_members._group_members_android(conn, "20")
        assert len(roster) == 1
        assert roster[0]["name"] == "History Sender"
        assert roster[0]["number"] == "5550300"
        conn.close()

    def test_ios_group_members_zwagroupmember_and_history_fallback(self, tmp_path) -> None:
        db_path = tmp_path / "chatstorage_roster.sqlite"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT,
                ZISACTIVE INTEGER DEFAULT 1
            );
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (5, 'group5@g.us', 'Group 5');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 5, '15550500@s.whatsapp.net', 'Alice GM', 1);
            INSERT INTO _ios_contacts VALUES ('15550500@s.whatsapp.net', 'Alice GM', '15550500');
        """)

        arc_path = tmp_path / "arc_ios_roster.db"
        with sqlite3.connect(arc_path) as aconn:
            aconn.execute("CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT)")

        conn.execute(f"ATTACH DATABASE '{arc_path}' AS arch")
        roster = group_members._group_members_ios(conn, "5")
        assert len(roster) == 1
        assert roster[0]["name"] == "Alice GM"
        assert roster[0]["number"] == "15550500"
        conn.close()

    def test_ios_group_members_inactive_and_history_fallbacks(self, tmp_path) -> None:
        # 1. Inactive member fallback (ZISACTIVE = 0)
        db_path = tmp_path / "chatstorage_inactive.sqlite"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER DEFAULT 0
            );
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (6, 'group6@g.us', 'Group 6');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 6, '15550600@s.whatsapp.net', 0);
            INSERT INTO _ios_contacts VALUES ('15550600@s.whatsapp.net', 'Bob Inactive', '15550600');
        """)
        arc_path = tmp_path / "arc_dummy.db"
        with sqlite3.connect(arc_path) as aconn:
            aconn.execute("CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT)")
        conn.execute(f"ATTACH DATABASE '{arc_path}' AS arch")
        roster = group_members._group_members_ios(conn, "6")
        assert len(roster) == 1
        assert roster[0]["name"] == "Bob Inactive"
        conn.close()

        # 2. History fallback via ZFROMJID (no ZWAGROUPMEMBER table)
        db_path_hist = tmp_path / "chatstorage_fromjid.sqlite"
        conn2 = sqlite3.connect(db_path_hist)
        conn2.row_factory = sqlite3.Row
        conn2.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZFROMJID TEXT
            );
            CREATE TABLE _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (7, 'group7@g.us', 'Group 7');
            INSERT INTO ZWAMESSAGE VALUES (1, 7, '15550700@s.whatsapp.net');
            INSERT INTO _ios_contacts VALUES ('15550700@s.whatsapp.net', 'Charlie Hist', '15550700');
        """)
        conn2.execute(f"ATTACH DATABASE '{arc_path}' AS arch")
        roster2 = group_members._group_members_ios(conn2, "7")
        assert len(roster2) == 1
        assert roster2[0]["name"] == "Charlie Hist"
        assert roster2[0]["number"] == "15550700"
        conn2.close()


# ---------------------------------------------------------------------------
# 5. Tests for Timeline Windowing & Jump (routes/messages.py)
# ---------------------------------------------------------------------------


class TestPreservedTimelineJumpAndDetails:
    """Tests for /api/messages/at, reaction_details, and poll_details."""

    def test_ios_apple_bound_math(self) -> None:
        # Boundary helper for Apple CoreData float timestamps
        op, bound = msg_routes._ios_apple_bound(1729000000000, inclusive=True, is_upper=True)
        assert op == "<"
        assert bound > 0

        op2, bound2 = msg_routes._ios_apple_bound(1729000000000, inclusive=False, is_upper=False)
        assert op2 == ">="
        assert bound2 > 0

    def test_messages_at_missing_ts_and_msg_id_returns_400(self, tmp_path) -> None:
        # Create minimal viewer app
        make_android_db(tmp_path / "msgstore.db")
        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        resp = client.get("/api/messages/at?chat_id=c1")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_messages_at_preserved_only_mode(self, tmp_path) -> None:
        make_android_db(tmp_path / "msgstore.db")
        wa_dir = tmp_path / "Whatsapp Databases"
        wa_dir.mkdir()
        pres_db = wa_dir / "preserved_messages.db"
        with sqlite3.connect(pres_db) as pconn:
            pconn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY,
                    chat_id TEXT,
                    chat_type TEXT,
                    timestamp_ms INTEGER,
                    sender TEXT,
                    from_me INTEGER,
                    text_body TEXT,
                    media_type TEXT
                );
                INSERT INTO preserved_messages VALUES (1, 'c1', 'contact', 1000, 'Alice', 0, 'Before 1', 'text');
                INSERT INTO preserved_messages VALUES (2, 'c1', 'contact', 2000, 'Alice', 0, 'Target Msg', 'text');
                INSERT INTO preserved_messages VALUES (3, 'c1', 'contact', 3000, 'Alice', 0, 'After 1', 'text');
            """)

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        # Jump by timestamp 2000
        resp = client.get("/api/messages/at?chat_id=c1&ts=2000&limit=10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        m_texts = [m["text_body"] for m in data]
        assert "Target Msg" in m_texts

    def test_messages_at_jump_by_android_msg_id(self, tmp_path) -> None:
        db_file = tmp_path / "msgstore.db"
        conn = make_android_db(db_file)
        conn.executescript("""
            INSERT INTO jid VALUES (1, '5550001', '5550001@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL, 0, 0, NULL);
            INSERT INTO message VALUES (101, 1, 0, 1, 10000, 'Earlier msg', 0, 'K1');
            INSERT INTO message VALUES (102, 1, 1, 1, 20000, 'Target msg 102', 0, 'K2');
            INSERT INTO message VALUES (103, 1, 0, 1, 30000, 'Later msg', 0, 'K3');
        """)
        conn.close()

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        # Jump directly by msg_id=102 without passing ts
        resp = client.get("/api/messages/at?msg_id=102&limit=10")
        assert resp.status_code == 200
        data = resp.get_json()
        ids = [m["msg_id"] for m in data]
        assert 102 in ids

    def test_api_messages_window_pagination(self, tmp_path) -> None:
        db_file = tmp_path / "msgstore.db"
        conn = make_android_db(db_file)
        conn.executescript("""
            INSERT INTO jid VALUES (1, '5550002', '5550002@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL, 0, 0, NULL);
            INSERT INTO message VALUES (201, 1, 0, 1, 100000, 'Window msg 1', 0, 'WK1');
            INSERT INTO message VALUES (202, 1, 0, 1, 200000, 'Window msg 2', 0, 'WK2');
            INSERT INTO message VALUES (203, 1, 1, 1, 300000, 'Window msg 3', 0, 'WK3');
        """)
        conn.close()

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        # Query messages before timestamp 250000
        resp = client.get(
            "/api/messages?chat_id=5550002&chat_type=contact&before=250000&limit=50&inclusive=1"
        )
        assert resp.status_code == 200
        data = resp.get_json()
        ids = [m["msg_id"] for m in data]
        assert 201 in ids
        assert 202 in ids
        assert 203 not in ids

    def test_reaction_details_preserved_negative_id(self, tmp_path) -> None:
        make_android_db(tmp_path / "msgstore.db")
        wa_dir = tmp_path / "Whatsapp Databases"
        wa_dir.mkdir()
        pres_db = wa_dir / "preserved_messages.db"
        with sqlite3.connect(pres_db) as pconn:
            pconn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY,
                    chat_id TEXT,
                    chat_type TEXT,
                    timestamp_ms INTEGER,
                    sender TEXT,
                    from_me INTEGER,
                    reactions TEXT
                );
                INSERT INTO preserved_messages VALUES (10, 'c1', 'contact', 1000, 'Bob Preserved', 0, '👍,❤️');
            """)

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        # Preserved message ID is negative (-rowid = -10)
        resp = client.get("/api/reaction_details/-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert data["total"] == 2
        assert data["reactors"][0]["emoji"] == "👍"
        assert data["reactors"][0]["name"] == "Bob Preserved"

        # Invalid non-integer ID -> 400
        resp_bad = client.get("/api/reaction_details/invalid_id")
        assert resp_bad.status_code == 400

    def test_poll_details_preserved_negative_id(self, tmp_path) -> None:
        make_android_db(tmp_path / "msgstore.db")
        wa_dir = tmp_path / "Whatsapp Databases"
        wa_dir.mkdir()
        pres_db = wa_dir / "preserved_messages.db"
        with sqlite3.connect(pres_db) as pconn:
            pconn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY,
                    chat_id TEXT,
                    chat_type TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    poll_data TEXT
                );
                INSERT INTO preserved_messages VALUES (
                    20, 'c1', 'contact', 1000, 'Best Framework?',
                    '{"question":"Best Framework?","multiple_answers":false,"total_votes":15,"options":[{"id":1,"name":"Flask","vote_count":10},{"id":2,"name":"Django","vote_count":5}]}'
                );
            """)

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        # Query poll details for -20
        resp = client.get("/api/poll_details/-20")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert data["question"] == "Best Framework?"
        assert data["multiple_answers"] is False
        assert len(data["options"]) == 2
        assert data["options"][0]["name"] == "Flask"
        assert data["options"][0]["vote_count"] == 10

    def test_poll_details_android_live(self, tmp_path) -> None:
        db_file = tmp_path / "msgstore.db"
        conn = make_android_db(db_file)
        conn.executescript("""
            CREATE TABLE message_poll (
                message_row_id INTEGER PRIMARY KEY,
                selectable_options_count INTEGER
            );
            CREATE TABLE message_poll_option (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                option_name TEXT,
                vote_total INTEGER
            );
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                sender_jid_row_id INTEGER
            );
            CREATE TABLE message_add_on_poll_vote_selected_option (
                message_add_on_row_id INTEGER,
                poll_option_id INTEGER
            );

            INSERT INTO jid VALUES (10, '5550999', '5550999@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 10, NULL, 0, 0, NULL);
            INSERT INTO message VALUES (500, 1, 0, 10, 1000, 'Favorite Editor?', 0, 'PK1');
            INSERT INTO message_poll VALUES (500, 1);
            INSERT INTO message_poll_option VALUES (1, 500, 'VS Code', 1);
            INSERT INTO message_poll_option VALUES (2, 500, 'Vim', 0);
            INSERT INTO message_add_on VALUES (1, 500, 10);
            INSERT INTO message_add_on_poll_vote_selected_option VALUES (1, 1);
        """)
        conn.close()

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        resp = client.get("/api/poll_details/500")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert data["question"] == "Favorite Editor?"
        assert len(data["options"]) == 2
        assert data["options"][0]["name"] == "VS Code"

    def test_api_message_receipts_android_fallback_to_status(self, tmp_path) -> None:
        db_file = tmp_path / "msgstore.db"
        conn = make_android_db(db_file)
        conn.executescript("""
            CREATE TABLE receipt_user (
                message_row_id INTEGER,
                receipt_user_jid_row_id INTEGER,
                receipt_timestamp INTEGER,
                read_timestamp INTEGER,
                played_timestamp INTEGER
            );
            INSERT INTO jid VALUES (1, '5550001', '5550001@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL, 0, 0, NULL);
            -- Message 600 from me with status = 13 (Read)
            INSERT INTO message VALUES (600, 1, 1, 1, 500000, 'Check read status', 0, 'RK1');
        """)
        # Add status column to message table
        conn.execute("ALTER TABLE message ADD COLUMN status INTEGER DEFAULT 13")
        conn.commit()
        conn.close()

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        resp = client.get("/api/message_receipts/600")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert len(data["members"]) == 1
        assert data["members"][0]["delivered_ts"] == 500000

    def test_api_reaction_details_android_missing_add_on(self, tmp_path) -> None:
        db_file = tmp_path / "msgstore.db"
        conn = make_android_db(db_file)
        conn.close()

        arc_db = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(arc_db) as aconn:
            aconn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
            )
            aconn.execute("PRAGMA user_version = 7")

        app = viewer.create_app(tmp_path, rescan=False)
        client = app.test_client()

        resp = client.get("/api/reaction_details/10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is False
