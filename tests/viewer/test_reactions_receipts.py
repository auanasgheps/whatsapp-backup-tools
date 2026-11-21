"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import base64
import os
import sqlite3
from collections import Counter

from tests.conftest import (
    make_android_db,
    make_archive_db,
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
from wab_viewer import hydration as viewer_hydration
from wab_viewer import main as viewer
from wab_viewer import protobuf as viewer_protobuf

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


class TestAndroidReactions:
    """Tests for message reaction emoji from message_add_on tables."""

    def _setup(self, tmp_path):
        """Set up Android DB with message_add_on tables."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        # Create reaction tables
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id                   INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                from_me               INTEGER DEFAULT 0,
                sender_jid_row_id     INTEGER
            );
            CREATE TABLE message_add_on_reaction (
                _id                   INTEGER PRIMARY KEY,
                message_add_on_row_id INTEGER,
                reaction              TEXT,
                sender_timestamp      INTEGER
            );
        """)
        # Seed contact chat (chat 10, jid 1)
        seed_android_db(wa_conn, archive_conn)
        # Add a second message for edge case tests
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (2, 10, 1, NULL, 1700000001000, 'Another message', 0)"
        )
        # Add reaction to message 1: two thumbs up
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (1, 1, 1, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (1, '👍', 1700000000100)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (2, 1, 0, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (2, '👍', 1700000000200)"
        )
        # Add reaction to message 2: one heart
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (3, 2, 0, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (3, '❤️', 1700000000300)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        return viewer.create_app(tmp_path, rescan=False)

    def test_reactions_column_present(self, tmp_path):
        """Response includes a reactions key."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 2
        assert "reactions" in data[0]

    def test_reactions_aggregated_from_multiple(self, tmp_path):
        """Two 👍 reactions on the same message are aggregated."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        # Message 1 has two 👍 reactions
        msg1 = next(m for m in data if m["msg_id"] == 1)
        assert msg1["reactions"] == "👍,👍"

    def test_reactions_different_emoji(self, tmp_path):
        """Different emoji on the same message appear as comma-separated values."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        msg2 = next(m for m in data if m["msg_id"] == 2)
        assert msg2["reactions"] == "❤️"

    def test_no_reactions_returns_null(self, tmp_path):
        """Message in a chat without reactions has null reactions column."""
        # Use the existing setup (has reaction tables) but query a different chat
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        wa_path = tmp_path / "msgstore.db"
        # Add a second chat with a message (no reactions)
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '987654321')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (20, 2, NULL, 0, 1700000002000, 10)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (10, 20, 0, 2, 1700000002000, 'No reactions here', 0)"
        )
        wa_conn.commit()
        wa_conn.close()
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=987654321&chat_type=contact").get_json()
        assert len(data) == 1
        assert data[0]["reactions"] is None

    def test_reactions_null_without_tables(self, tmp_path):
        """When message_add_on tables don't exist, reactions column is NULL."""
        # Start with plain make_android_db (no message_add_on tables)
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 1
        assert data[0]["reactions"] is None

    def test_resolve_and_cache_reactions(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_add_on_reaction (
                message_add_on_row_id INTEGER PRIMARY KEY,
                reaction TEXT
            );
        """)
        wa_conn.execute("INSERT INTO message_add_on (_id, parent_message_row_id) VALUES (10, 100)")
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction) VALUES (10, '👍')"
        )
        wa_conn.commit()

        rows = [{"chat_id": "123", "msg_id": 100}]
        viewer_hydration._resolve_and_cache_reactions("android", wa_conn, arc_conn, rows)
        assert rows[0]["reactions"] == "👍"

        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE msg_id = 100"
        ).fetchone()
        assert cached is not None
        assert cached[0] == "👍"

        # Edge cases
        viewer_hydration._resolve_and_cache_reactions("ios", wa_conn, arc_conn, rows)
        viewer_hydration._resolve_and_cache_reactions("android", wa_conn, arc_conn, [])
        viewer_hydration._resolve_and_cache_reactions(
            "android", wa_conn, arc_conn, [{"chat_id": "123"}]
        )

        wa_conn.close()
        arc_conn.close()

    def test_recent_messages_self_healing_reactions(self, tmp_path):
        """When recent_messages has null reactions and reactions_cache is empty, fallback to wa_conn self-heals."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        archive_path = tmp_path / ".wa_media_archiver.db"

        # Prime recent_messages with null reactions and ensure reactions_cache is wiped
        arc_conn = sqlite3.connect(str(archive_path))
        arc_conn.execute("DELETE FROM reactions_cache")
        arc_conn.execute("DELETE FROM recent_messages")
        arc_conn.execute("""
            INSERT INTO recent_messages (
                chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, media_type, text_body, reactions
            ) VALUES ('123456789', 'contact', 1, 1700000000000, '+123456789', 0, 'text', 'Hello from cached', NULL)
        """)
        arc_conn.commit()
        arc_conn.close()

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            data = resp.get_json()

        assert len(data) == 1
        msg = data[0]
        assert msg["msg_id"] == 1
        # Must be self-healed from wa_conn (message 1 has two thumbs up in _setup)
        assert msg["reactions"] == "👍,👍"

        # Verify write-through persistence to reactions_cache and recent_messages
        arc_conn = sqlite3.connect(str(archive_path))
        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE msg_id = 1"
        ).fetchone()
        assert cached is not None
        assert cached[0] == "👍,👍"

        recent = arc_conn.execute(
            "SELECT reactions FROM recent_messages WHERE msg_id = 1"
        ).fetchone()
        assert recent is not None
        assert recent[0] == "👍,👍"
        arc_conn.close()


class _NoOpConn:
    """Minimal connection stub — no contacts to look up."""

    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        return None


class TestIosReceiptBlobParser:
    # Validated model: base_ts = top field 3; delivered = base + min(field-10 deltas)
    # in seconds (0 valid); read = base + field 5 (seconds) if present; field 4 ignored.

    def test_group_delivered_only(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 10, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == (base_ts + 10) * 1000
        assert members[0]["read_ts"] is None

    def test_group_delivered_and_read(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 20, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000  # delta 0 is a real delivery
        assert members[0]["read_ts"] == (base_ts + 20) * 1000

    def test_group_read_at_send_delta_zero(self):
        # read_delta == 0 means read at send time — must not collapse to None.
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 0, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] == base_ts * 1000

    def test_group_field4_is_ignored(self):
        # A large field-4 value (read receipts disabled) must not gate delivered/read.
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {
                    "lid": "1234567890123",
                    "delivered_delta": 17,
                    "read_delta": None,
                    "noise4": 1089263,
                },
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["delivered_ts"] == (base_ts + 17) * 1000
        assert members[0]["read_ts"] is None

    def test_group_lid_trailing_f_stripped(self):
        # Odd-length LID is padded with an 'f' nibble; parser must strip it.
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["jid"] == "1234567890123"

    def test_group_multiple_members(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 5, "noise4": None},
                {"lid": "2222222222222", "delivered_delta": 15, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 2
        assert members[0]["read_ts"] == (base_ts + 5) * 1000
        assert members[1]["read_ts"] is None
        assert members[1]["delivered_ts"] == (base_ts + 15) * 1000

    def test_group_regression_verified_deltas(self):
        # Mirrors two real ground-truth messages (synthetic LIDs, real validated deltas):
        #   read 5926s / 4708s / 1462s after send; a member with read receipts off (no field 5).
        base_ts = 1768325919
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 5926, "noise4": 2},
                {
                    "lid": "2222222222222",
                    "delivered_delta": 17,
                    "read_delta": None,
                    "noise4": 1089263,
                },
                {"lid": "3333333333333", "delivered_delta": 0, "read_delta": 0, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["read_ts"] == (base_ts + 5926) * 1000
        assert members[1]["read_ts"] is None  # read receipts disabled
        assert members[1]["delivered_ts"] == (base_ts + 17) * 1000
        assert members[2]["read_ts"] == base_ts * 1000  # read at send

    # --- 1-to-1 mode (is_group=False): collapse to a single member ---

    def test_1to1_delivered_and_read(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 120, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] == (base_ts + 120) * 1000

    def test_1to1_delivered_only(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] is None

    def test_1to1_multi_device_collapses_to_earliest(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 253, "noise4": None},
                {"lid": "2222222222222", "delivered_delta": 26, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000  # min delta = 0
        assert members[0]["read_ts"] == (base_ts + 253) * 1000

    # --- compact format: no base_ts, delivered from field 9, read blank ---

    def test_compact_group_delivered_only(self):
        msg_ts_s = 1784037821
        blob = _make_compact_receipt_blob(
            [
                {"lid": "1111111111111", "delivered_delta": 0},
                {"lid": "2222222222222", "delivered_delta": 60},
            ]
        )
        members = viewer_hydration._parse_ios_receipt_blob(
            blob, _NoOpConn(), is_group=True, msg_ts_s=msg_ts_s
        )
        assert len(members) == 2
        assert members[0]["delivered_ts"] == msg_ts_s * 1000
        assert members[0]["read_ts"] is None
        assert members[1]["delivered_ts"] == (msg_ts_s + 60) * 1000
        assert members[1]["read_ts"] is None

    def test_empty_blob_returns_empty_list(self):
        members = viewer_hydration._parse_ios_receipt_blob(b"", _NoOpConn())
        assert members == []

    def test_unknown_fixed64_wire_type_is_skipped(self):
        """A fixed64 field (wire=1) in an entry should be skipped, not abort parsing."""
        base_ts = 1700000000
        lid_bytes = _lid_field1_bytes("1234567890123")
        entry = _encode_varint((1 << 3) | 2) + _encode_varint(len(lid_bytes)) + lid_bytes
        entry += _encode_varint((8 << 3) | 1) + b"\x00" * 8  # unknown field, wire=1 (fixed64)
        entry += _encode_varint((5 << 3) | 0) + _encode_varint(10)  # read delta=10
        event = _encode_varint((1 << 3) | 0) + _encode_varint(0)
        entry += _encode_varint((10 << 3) | 2) + _encode_varint(len(event)) + event

        blob = _encode_varint((3 << 3) | 0) + _encode_varint(base_ts)
        blob += _encode_varint((2 << 3) | 2) + _encode_varint(len(entry)) + entry

        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["read_ts"] == (base_ts + 10) * 1000
        assert members[0]["delivered_ts"] == base_ts * 1000


class TestIosReadWithoutTimestamp:
    """iOS keeps the aggregate read flag (ZMESSAGESTATUS 8) for recent messages but
    no longer stores per-recipient read timestamps; the route surfaces that state."""

    def test_read_status_without_timestamp_is_flagged(self):
        assert viewer_hydration._ios_read_without_timestamp(8, None) is True

    def test_read_status_with_timestamp_is_not_flagged(self):
        # A stored timestamp already conveys the read state.
        assert viewer_hydration._ios_read_without_timestamp(8, 1700000000000) is False

    def test_delivered_status_is_not_flagged(self):
        # Status 6 = delivered, not read.
        assert viewer_hydration._ios_read_without_timestamp(6, None) is False

    def test_sent_status_is_not_flagged(self):
        assert viewer_hydration._ios_read_without_timestamp(1, None) is False


class TestIOSReactions:
    def test_parse_heart_reaction(self):
        blob = _make_zreceipt_blob_with_reactions([("334142384436", "❤️".encode())])
        result = viewer_protobuf._extract_ios_reactions(blob)
        assert len(result) == 1
        sender, emoji = result[0]
        assert sender == "334142384436"
        assert emoji == "❤️"

    def test_parse_multiple_reactions(self):
        blob = _make_zreceipt_blob_with_reactions(
            [
                ("334142384436", "❤️".encode()),
                ("334135434342", "😂".encode()),
            ]
        )
        result = viewer_protobuf._extract_ios_reactions(blob)
        assert len(result) == 2
        assert result[0][1] == "❤️"
        assert result[1][1] == "😂"

    def test_parse_various_emoji(self):
        blobs = []
        for emoji in ["😂", "😮", "😭", "👏"]:
            blobs.append(
                _make_zreceipt_blob_with_reactions([("334142384436", emoji.encode("utf-8"))])
            )
        for i, emoji in enumerate(["😂", "😮", "😭", "👏"]):
            result = viewer_protobuf._extract_ios_reactions(blobs[i])
            assert len(result) == 1, f"Failed for {emoji}"
            assert result[0][1] == emoji, f"Expected {emoji}, got {result[0][1]}"

    def test_parse_empty_blob(self):
        result = viewer_protobuf._extract_ios_reactions(b"")
        assert result == []

    def test_parse_no_field7(self):
        # Blob with a different field (e.g. field 1)
        blob = bytes([(1 << 3) | 2, 2, 0x41, 0x42])
        result = viewer_protobuf._extract_ios_reactions(blob)
        assert result == []

    def test_vs16_is_skipped_between_emoji(self):
        # VS16 (efb88f) appears BETWEEN two real emoji — it should not be a separate reaction
        blob = _make_zreceipt_blob_with_reactions(
            [
                ("334142384436", "❤️".encode() + "👏".encode()),
            ]
        )
        result = viewer_protobuf._extract_ios_reactions(blob)
        # Both emoji returned; VS16 is a presentation modifier, not a standalone emoji
        assert len(result) == 2
        emojis = sorted(r[1] for r in result)
        assert emojis == sorted(["❤️", "👏"])

    def test_many_reactions_large_field7(self):
        """Field 7 >= 128 bytes needs a multi-byte varint length; all reactors survive.

        The old single-byte length read truncated field 7 and dropped reactors.
        """
        reactors = [(f"33414238{i:04d}", "❤️".encode()) for i in range(11)]
        blob = _make_zreceipt_blob_with_reactions(reactors)
        assert len(blob) > 128  # forces a multi-byte varint length on field 7
        result = viewer_protobuf._extract_ios_reactions(blob)
        assert len(result) == 11
        assert all(emoji == "❤️" for _, emoji in result)

    def test_emoji_in_subfield_2(self):
        """Some reactor entries store the emoji in sub-field 2 (tag 0x12), not 3.

        Combined with a field 7 that needs a 2-byte varint length, the old parser's
        single-byte length read dropped the trailing byte and discarded this entry.
        """

        # Two entries with emoji in sub-field 3, plus a final entry with emoji in
        # sub-field 2. Pad the blob past 128 bytes so field 7 uses a 2-byte varint.
        def entry(field, emoji):
            sub1 = _encode_varint(0x0A) + _encode_varint(30) + (b"\x11" * 30)
            sub_emoji = _encode_varint((field << 3) | 2) + _encode_varint(len(emoji)) + emoji
            body = sub1 + sub_emoji
            return _encode_varint(0x0A) + _encode_varint(len(body)) + body

        laugh = "😂".encode()
        heart = "❤️".encode()
        field7 = entry(3, heart) + entry(3, laugh) + entry(3, laugh) + entry(2, laugh)
        blob = _encode_varint((7 << 3) | 2) + _encode_varint(len(field7)) + field7
        assert len(field7) > 128  # forces a 2-byte varint length on field 7
        result = viewer_protobuf._extract_ios_reactions(blob)
        counts = Counter(emoji for _, emoji in result)
        assert counts == Counter({"😂": 3, "❤️": 1})

    def test_protobuf_skips_deprecated_wire_types(self):
        # Build a blob with a deprecated wire type (6) embedded — should not crash
        # Wire type 6 = deprecated start_group; we'll embed it inside field 7
        entry_data = bytes([0x41, 0x42])  # field 8, wire 1 = deprecated
        inner = bytes([0x0A, len(entry_data) + 1]) + bytes([6]) + entry_data
        blob = bytes([(7 << 3) | 2, len(inner)]) + inner
        result = viewer_protobuf._extract_ios_reactions(blob)
        assert result == []

    def test_ios_reactions_me_detection_contact_chat(self, tmp_path):
        """In 1-to-1 chats ZGROUPMEMBER is NULL — _ios_reactions must fall back to ZFROMJID."""
        # Build minimal in-memory WA DB with the needed schema
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE)"
        )
        # ZWAGROUPMEMBER must exist (even if unused for contact chats)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)"
        )

        def encode_field7_reactor(phone_hex_str, emoji_bytes):
            phone_data = bytes.fromhex(phone_hex_str)
            entry = bytes([0x0A, len(phone_data)]) + phone_data
            entry += bytes([0x1A, len(emoji_bytes)]) + emoji_bytes
            field7 = bytes([0x0A, len(entry)]) + entry
            return bytes([0x3A, len(field7)]) + field7  # field 7, wire 2

        # Message from "me" (ZISFROMME=1) to identify my JID — no ZMESSAGEINFO needed
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '49123456789@s.whatsapp.net')"
        )
        # A message with reactions (ZISFROMME=0 = received)
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (encode_field7_reactor("34393132343536373839", "😂".encode()),),
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, '49876543210@s.whatsapp.net')"
        )
        conn.commit()

        result = viewer_hydration._ios_reactions(conn, "10", [2])
        assert 2 in result
        reactors = result[2]
        assert len(reactors) == 1
        sender, emoji, from_me = reactors[0]
        assert emoji == "😂"
        # Sender phone 34393132343536373839 ≠ my phone 49123456789 → from_me=0
        assert from_me == 0

    def test_ios_reactions_me_detection_null_jid(self, tmp_path):
        """System messages have NULL ZFROMJID — _ios_reactions must not crash."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)"
        )

        def encode_field7_reactor(phone_hex_str, emoji_bytes):
            phone_data = bytes.fromhex(phone_hex_str)
            entry = bytes([0x0A, len(phone_data)]) + phone_data
            entry += bytes([0x1A, len(emoji_bytes)]) + emoji_bytes
            field7 = bytes([0x0A, len(entry)]) + entry
            return bytes([0x3A, len(field7)]) + field7

        # Sent message with NULL ZFROMJID (system message) — "me" detection must not crash
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, NULL)"
        )
        # A message with reactions — reactor phone differs from "me" (which has NULL JID → my_phone=None)
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (encode_field7_reactor("34393132343536373839", "😂".encode()),),
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, '49876543210@s.whatsapp.net')"
        )
        conn.commit()

        # Must not raise AttributeError: 'NoneType' object has no attribute 'find'
        result = viewer_hydration._ios_reactions(conn, "10", [2])
        assert 2 in result
        sender, emoji, from_me = result[2][0]
        assert emoji == "😂"
        assert from_me == 0  # my_phone=None, reactor phone ≠ None → not from me


class TestReactionDetailsEndpoint:
    """Tests for /api/reaction_details/<message_id> — Android and iOS."""

    # ---- Android helpers ----

    def _setup_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id                   INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                from_me               INTEGER DEFAULT 0,
                sender_jid_row_id     INTEGER
            );
            CREATE TABLE message_add_on_reaction (
                _id                   INTEGER PRIMARY KEY,
                message_add_on_row_id INTEGER,
                reaction              TEXT,
                sender_timestamp      INTEGER
            );
        """)
        seed_android_db(wa_conn, archive_conn)
        # from_me reaction on message 1 (no sender_jid_row_id)
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (1, 1, 1, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (1, '👍', 1700000000100)"
        )
        # Alice's reaction on message 1 (sender_jid_row_id=1 → '123456789' → 'Alice')
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (2, 1, 0, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (2, '❤️', 1700000000200)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        return viewer.create_app(tmp_path, rescan=False)

    def test_android_total_and_reactors(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        assert data["available"] is True
        assert data["total"] == 2
        assert len(data["reactors"]) == 2

    def test_android_from_me_flag(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        from_me_reactors = [r for r in data["reactors"] if r["from_me"] == 1]
        assert len(from_me_reactors) == 1
        assert from_me_reactors[0]["emoji"] == "👍"

    def test_android_name_resolved_from_contacts(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        names = {r["name"] for r in data["reactors"]}
        assert "Alice" in names

    def test_android_empty_when_no_reactions(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/999").get_json()
        assert data["available"] is True
        assert data["total"] == 0
        assert data["reactors"] == []

    def test_android_unavailable_when_no_table(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        assert data["available"] is False

    # ---- iOS helpers ----

    def _setup_ios(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB
            );
            CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX
                ON ZWAMESSAGEINFO (ZMESSAGE);
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT
            );
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID) VALUES (10, 1, '99@g.us')"
        )
        # Message from me to provide my_phone derivation
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '15550001111@s.whatsapp.net')"
        )
        # Two reactors identified by phone JID (sub-field 2); sub-field 1 is an opaque token.
        blob = _make_ios_reaction_blob(
            [
                {"token": "3EB0903616E9C7C36F7B", "phone": "15550003333", "emoji": "👍".encode()},
                {"token": "3AB0BA8A4CA7122CA06A", "phone": "15550004444", "emoji": "❤️".encode()},
            ]
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)", (blob,)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, '15550002222@s.whatsapp.net')"
        )
        conn.commit()
        conn.close()
        archive_conn = make_archive_db(archive_path)
        archive_conn.close()
        return viewer.create_app(tmp_path, rescan=False)

    def test_ios_total_and_reactors(self, tmp_path):
        app = self._setup_ios(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["total"] == 2
        assert len(data["reactors"]) == 2

    def test_ios_emoji_preserved(self, tmp_path):
        app = self._setup_ios(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        emojis = {r["emoji"] for r in data["reactors"]}
        assert emojis == {"👍", "❤️"}

    def test_ios_unavailable_when_no_blob(self, tmp_path):
        app = self._setup_ios(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        # Message 1 has no ZMESSAGEINFO row → available: False
        assert data["available"] is False

    def test_ios_unavailable_when_no_table(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT
            );
        """)
        conn.commit()
        conn.close()
        archive_conn = make_archive_db(archive_path)
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        assert data["available"] is False

    def _ios_db_with_blob(self, tmp_path, contact_jid, blob):
        """Create a minimal iOS ChatStorage with one reacted message (Z_PK 2)."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB
            );
            CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE);
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT
            );
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID) VALUES (10, NULL, ?)",
            (contact_jid,),
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '15550001111@s.whatsapp.net')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)", (blob,)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, NULL)"
        )
        conn.commit()
        conn.close()

    def test_ios_phone_resolves_to_name(self, tmp_path):
        """Reactor identified by phone JID (sub-field 2) is resolved via arch.contacts."""
        phone = "41234567890"
        blob = _make_ios_reaction_blob(
            [
                {"token": "3EB0903616E9C7C36F7B", "phone": phone, "emoji": "👍".encode()},
            ]
        )
        self._ios_db_with_blob(tmp_path, "88@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES (?, 'Contacts', ?)",
            (phone, "Bob"),
        )
        archive_conn.commit()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["total"] == 1
        assert data["reactors"][0]["name"] == "Bob"
        assert data["reactors"][0]["emoji"] == "👍"

    def test_ios_no_identity_is_from_me(self, tmp_path):
        """Reactor entry with no phone and no @lid JID is the current user's own reaction."""
        blob = _make_ios_reaction_blob(
            [
                {"token": "3EB0903616E9C7C36F7B", "phone": None, "emoji": "👍".encode()},
            ]
        )
        self._ios_db_with_blob(tmp_path, "88@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["reactors"][0]["from_me"] == 1

    def test_ios_lid_resolves_via_pushname(self, tmp_path):
        """Group reactor identified by @lid JID resolves to a name via ZWAPROFILEPUSHNAME."""
        lid = "145500000000004"
        blob = _make_ios_reaction_blob(
            [
                {"token": "3A000000000000000001", "lid": lid, "emoji": "❤️".encode()},
            ]
        )
        self._ios_db_with_blob(tmp_path, "99@g.us", blob)
        # Insert a push-name row keyed by the @lid JID
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.execute(
            "INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) VALUES (1, ?, 'Eve Reactor')",
            (f"{lid}@lid",),
        )
        conn.commit()
        conn.close()
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["reactors"][0]["name"] == "Eve Reactor"
        assert data["reactors"][0]["emoji"] == "❤️"
        assert data["reactors"][0]["from_me"] == 0

    def test_ios_lid_unknown_when_no_name_source(self, tmp_path):
        """@lid reactor with no matching entry in any name table resolves to 'Unknown'."""
        blob = _make_ios_reaction_blob(
            [
                {"token": "3A42C9813B3F02D513AA", "lid": "999000111222333", "emoji": "👍".encode()},
            ]
        )
        self._ios_db_with_blob(tmp_path, "99@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["reactors"][0]["name"] == "Unknown"
        assert data["reactors"][0]["from_me"] == 0


class TestIosReceiptDeviceParser:
    def test_1to1_receipt_device_timestamps_converted_to_ms(self):
        class _MockConn:
            def execute(self, sql, params=None):
                class _Row:
                    def fetchone(self):
                        return None

                return _Row()

        infra_rows = [
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "device_id": 0,
                "delivered_timestamp": 1700000010,
                "read_timestamp": 1700000050,
                "played_timestamp": 1700000080,
            },
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "device_id": 1,
                "delivered_timestamp": 1700000015,
                "read_timestamp": None,
                "played_timestamp": None,
            },
        ]

        members = viewer_hydration._parse_ios_receipt_device_rows(
            infra_rows, _MockConn(), is_group=False
        )
        assert len(members) == 1
        entry = members[0]
        assert entry["delivered_ts"] == 1700000010 * 1000
        assert entry["read_ts"] == 1700000050 * 1000
        assert entry["played_ts"] == 1700000080 * 1000
        assert entry["read_known"] is True

    def test_group_receipt_device_resolves_names(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE _ios_contacts (jid TEXT, full_name TEXT)")
        conn.execute("INSERT INTO _ios_contacts VALUES ('15550002222@s.whatsapp.net', 'Alice')")
        conn.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")
        conn.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES ('15550003333@s.whatsapp.net', 'Bob')")

        infra_rows = [
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "device_id": 0,
                "delivered_timestamp": 1700000010,
                "read_timestamp": 1700000050,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550003333@s.whatsapp.net",
                "device_id": 0,
                "delivered_timestamp": 1700000012,
                "read_timestamp": None,
                "played_timestamp": None,
            },
        ]

        members = viewer_hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
        assert len(members) == 2
        m_map = {m["jid"]: m for m in members}
        assert m_map["15550002222"]["name"] == "Alice"
        assert m_map["15550002222"]["read_ts"] == 1700000050 * 1000
        assert m_map["15550003333"]["name"] == "Bob"
        assert m_map["15550003333"]["read_ts"] is None


class TestIosLidDatabaseResolution:
    def test_lid_db_resolves_unknown_lid_to_phone(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        lid_path = tmp_path / "LID.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # 1. ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (10, 1, '99@g.us', 'Test Group');
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 1, NULL, 1000, '15550001111@s.whatsapp.net', NULL);
        """)

        # Reaction from @lid user not in ContactsV2
        lid_val = "999888777666555"
        blob = _make_ios_reaction_blob(
            [
                {"token": "3AB0BA8A4CA7122CA06A", "lid": lid_val, "emoji": "🎉".encode()},
            ]
        )
        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (100, 2, ?)", (blob,))
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (2, 10, 0, 100, 2000, '15550002222@s.whatsapp.net', NULL)"
        )
        conn.commit()
        conn.close()

        # 2. LID.sqlite mapping lid_val to phone number
        lid_conn = sqlite3.connect(str(lid_path))
        lid_conn.executescript(f"""
            CREATE TABLE ZWAZACCOUNT (
                Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR
            );
            INSERT INTO ZWAZACCOUNT (Z_PK, ZIDENTIFIER, ZPHONENUMBER)
            VALUES (1, '{lid_val}@lid', '15553334444');
        """)
        lid_conn.commit()
        lid_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()

        assert data["available"] is True
        assert data["reactors"][0]["name"] == "+15553334444"


class TestIosMessageReceiptsApi:
    def test_message_receipts_from_messaging_infra_db(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        infra_path = tmp_path / "MessagingInfraDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE INTEGER, ZSTANZAID TEXT, ZMESSAGESTATUS INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);

            INSERT INTO ZWACHATSESSION VALUES (1, NULL, '15550002222@s.whatsapp.net', NULL);
            INSERT INTO ZWAMESSAGE VALUES (10, 1, 1, 1000, 'STANZA_123', 8);
        """)
        conn.commit()
        conn.close()

        # MessagingInfraDatabase
        infra_conn = sqlite3.connect(str(infra_path))
        infra_conn.executescript("""
            CREATE TABLE receipt_device (
                _id INTEGER PRIMARY KEY, stanza_id TEXT, chat_jid TEXT, user_jid TEXT,
                device_id INTEGER, send_timestamp LONG_INT, delivered_timestamp LONG_INT,
                read_timestamp LONG_INT, played_timestamp LONG_INT, device_version INTEGER
            );
            INSERT INTO receipt_device VALUES (
                1, 'STANZA_123', '15550002222@s.whatsapp.net', '15550002222@s.whatsapp.net',
                0, 1700000000, 1700000005, 1700000045, 1700000060, 1
            );
        """)
        infra_conn.commit()
        infra_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/10")
            data = resp.get_json()

        assert data["available"] is True
        assert data["delivered_ts"] == 1700000005 * 1000
        assert data["read_ts"] == 1700000045 * 1000
        assert data["played_ts"] == 1700000060 * 1000
        assert data["read_known"] is True

    def test_message_receipts_from_messaging_infra_db_in_whatsapp_databases(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        wa_path = db_dir / "ChatStorage.sqlite"
        infra_path = db_dir / "MessagingInfraDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE INTEGER, ZSTANZAID TEXT, ZMESSAGESTATUS INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);

            INSERT INTO ZWACHATSESSION VALUES (1, NULL, '15550002222@s.whatsapp.net', NULL);
            INSERT INTO ZWAMESSAGE VALUES (10, 1, 1, 1000, 'STANZA_123', 8);
        """)
        conn.commit()
        conn.close()

        # MessagingInfraDatabase
        infra_conn = sqlite3.connect(str(infra_path))
        infra_conn.executescript("""
            CREATE TABLE receipt_device (
                _id INTEGER PRIMARY KEY, stanza_id TEXT, chat_jid TEXT, user_jid TEXT,
                device_id INTEGER, send_timestamp LONG_INT, delivered_timestamp LONG_INT,
                read_timestamp LONG_INT, played_timestamp LONG_INT, device_version INTEGER
            );
            INSERT INTO receipt_device VALUES (
                1, 'STANZA_123', '15550002222@s.whatsapp.net', '15550002222@s.whatsapp.net',
                0, 1700000000, 1700000005, 1700000045, 1700000060, 1
            );
        """)
        infra_conn.commit()
        infra_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/10")
            data = resp.get_json()

        assert data["available"] is True
        assert data["delivered_ts"] == 1700000005 * 1000
        assert data["read_ts"] == 1700000045 * 1000
        assert data["played_ts"] == 1700000060 * 1000
        assert data["read_known"] is True


class TestMessageReceipts:
    def test_receipts_table_missing(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        seed_android_db(wa_conn, arc_conn)
        wa_conn.close()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/1")
            assert resp.status_code == 200
            assert resp.get_json() == {"available": False}

    def test_receipts_empty_and_populated(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        seed_android_db(wa_conn, arc_conn)
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS receipt_user (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                receipt_user_jid_row_id INTEGER,
                receipt_timestamp INTEGER,
                read_timestamp INTEGER,
                played_timestamp INTEGER
            );
        """)
        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/1")
            assert resp.status_code == 200
            assert resp.get_json() == {"available": True, "members": []}

            wa_conn.execute(
                "INSERT INTO jid (_id, user, raw_string) VALUES (2, '987654321', '987654321@s.whatsapp.net')"
            )
            wa_conn.execute(
                "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, receipt_timestamp, read_timestamp, played_timestamp) "
                "VALUES (1, 2, 1700000005000, 1700000010000, NULL)"
            )
            wa_conn.commit()

            resp = client.get("/api/message_receipts/1")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["available"] is True
            assert len(data["members"]) == 1
            member = data["members"][0]
            assert member["jid"] == "987654321@s.whatsapp.net"
            assert member["name"] == "+987654321"
            assert member["delivered_ts"] == 1700000005000
            assert member["read_ts"] == 1700000010000
            assert data["delivered_ts"] == 1700000005000
            assert data["read_ts"] == 1700000010000
        wa_conn.close()
        arc_conn.close()

    def test_ios_receipts_filtered_by_group_members(self, tmp_path):
        from unittest.mock import patch

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGESTATUS INTEGER,
                ZSTANZAID TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZRECEIPTINFO BLOB
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
        """)
        conn.execute("INSERT INTO ZWAGROUPINFO (Z_PK) VALUES (1)")
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, 'group1@g.us', 1, 'Group 1')"
        )
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZMEMBERJID, ZCONTACTNAME) VALUES (10, 'member1@s.whatsapp.net', 'Member One')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZMESSAGESTATUS, ZSTANZAID, ZGROUPMEMBER) VALUES (100, 1, 1, 1, 700000000.0, 3, 'stanza1', 10)"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (1, 100, X'0102')"
        )
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            with patch("wab_viewer.routes.messages._parse_ios_receipt_blob") as mock_parse:
                mock_parse.return_value = [
                    {"jid": "member1", "delivered_ts": 1000, "read_ts": 2000},
                    {"jid": "stranger", "delivered_ts": 1000, "read_ts": 2000},
                ]
                resp = client.get("/api/message_receipts/100")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data["available"] is True
                assert len(data["members"]) == 1
                assert data["members"][0]["jid"] == "member1"

    def test_ios_group_receipts_includes_silent_members_and_lid_bridging(self, tmp_path):
        from unittest.mock import patch

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGESTATUS INTEGER,
                ZSTANZAID TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZRECEIPTINFO BLOB
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
        """)
        conn.execute("INSERT INTO ZWAGROUPINFO (Z_PK) VALUES (1)")
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, 'group1@g.us', 1, 'Group 1')"
        )
        # member1 (sender), silent_reader (never sent a message, phone JID), lid_member (LID in ZRECEIPTINFO, mapped in _ios_contacts)
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (10, 1, 'member1@s.whatsapp.net', 'Member One')"
        )
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (11, 1, 'silent_reader@s.whatsapp.net', 'Silent Reader')"
        )
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (12, 1, '44112233@s.whatsapp.net', 'LID Member')"
        )
        # Only member1 has a row in ZWAMESSAGE; silent_reader and 44112233 have NO rows in ZWAMESSAGE
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZMESSAGESTATUS, ZSTANZAID, ZGROUPMEMBER) VALUES (100, 1, 1, 1, 700000000.0, 3, 'stanza1', 10)"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (1, 100, X'0102')"
        )
        conn.commit()
        conn.close()

        # Create LID.sqlite so _init_ios_contacts automatically bridges 999888777@lid <-> 44112233
        lid_conn = sqlite3.connect(str(tmp_path / "LID.sqlite"))
        lid_conn.executescript("""
            CREATE TABLE ZWAZACCOUNT (
                Z_PK INTEGER PRIMARY KEY,
                ZIDENTIFIER TEXT,
                ZPHONENUMBER TEXT
            );
            INSERT INTO ZWAZACCOUNT VALUES (1, '999888777@lid', '44112233');
        """)
        lid_conn.commit()
        lid_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            with patch("wab_viewer.routes.messages._parse_ios_receipt_blob") as mock_parse:
                mock_parse.return_value = [
                    {"jid": "member1", "delivered_ts": 1000, "read_ts": 2000},
                    {"jid": "silent_reader", "delivered_ts": 1100, "read_ts": 2100},
                    {"jid": "999888777", "delivered_ts": 1200, "read_ts": 2200},
                    {"jid": "outsider", "delivered_ts": 1300, "read_ts": 2300},
                ]
                resp = client.get("/api/message_receipts/100")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data["available"] is True
                returned_jids = [m["jid"] for m in data["members"]]
                assert "member1" in returned_jids
                assert "silent_reader" in returned_jids
                assert "999888777" in returned_jids
                assert "outsider" not in returned_jids


class TestProtobufParserEdgeCases:
    def test_read_varint(self):
        val, pos = viewer_protobuf._read_varint(b"\x96\x01", 0)
        assert val == 150
        assert pos == 2

    def test_extract_ios_group_description_invalid(self):
        assert viewer_protobuf._extract_ios_group_description("") is None
        assert viewer_protobuf._extract_ios_group_description("not-valid-base64!!!") is None

    def test_extract_ios_group_description_wire_types_and_subfields(self):

        sub = bytes([32, 5, 41]) + b"12345678" + bytes([53]) + b"1234" + bytes([26, 4]) + b"Desc"
        top = bytes([16, 2, 25]) + b"abcdefgh" + bytes([37]) + b"abcd" + bytes([10, len(sub)]) + sub
        b64 = "+" + base64.b64encode(top).decode("ascii")
        desc = viewer_protobuf._extract_ios_group_description(b64)
        assert desc == "Desc"


class TestProtobufWireTypesInReceiptBlob:
    def test_parse_ios_receipt_blob_wire1_wire5_fallback(self):
        # (imported/defined at module level)

        base_ts = 1700000000
        lid_bytes = _lid_field1_bytes("1555000123456")

        # Member entry with wire 1 (fixed64) and wire 5 (fixed32)
        entry = _encode_varint((1 << 3) | 2) + _encode_varint(len(lid_bytes)) + lid_bytes
        # Add wire 1 (tag: field 7, wire 1) + 8 dummy bytes
        entry += _encode_varint((7 << 3) | 1) + b"\x01\x02\x03\x04\x05\x06\x07\x08"
        # Add wire 5 (tag: field 8, wire 5) + 4 dummy bytes
        entry += _encode_varint((8 << 3) | 5) + b"\x01\x02\x03\x04"

        # Event delta (field 9: fallback delta) containing wire 1, 2, 5
        event_inner = _encode_varint((1 << 3) | 0) + _encode_varint(45)  # delta = 45s
        event_inner += (
            _encode_varint((2 << 3) | 2) + _encode_varint(3) + b"foo"
        )  # wire 2 inside event
        event_inner += (
            _encode_varint((3 << 3) | 1) + b"\x11\x22\x33\x44\x55\x66\x77\x88"
        )  # wire 1 inside event
        event_inner += _encode_varint((4 << 3) | 5) + b"\xaa\xbb\xcc\xdd"  # wire 5 inside event
        entry += _encode_varint((9 << 3) | 2) + _encode_varint(len(event_inner)) + event_inner

        # Top-level blob with wire 1 and wire 5
        blob = _encode_varint((3 << 3) | 0) + _encode_varint(base_ts)
        blob += _encode_varint((11 << 3) | 1) + b"\x00" * 8  # top-level wire 1
        blob += _encode_varint((12 << 3) | 5) + b"\x00" * 4  # top-level wire 5
        blob += _encode_varint((2 << 3) | 2) + _encode_varint(len(entry)) + entry

        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == (base_ts + 45) * 1000
