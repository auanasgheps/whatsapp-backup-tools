"""Tests for wab_archiver.increment_merger."""

from __future__ import annotations

import io
import logging
import os
import sqlite3
import struct

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

logger = logging.getLogger(__name__)

from tests.fixtures.protobuf_factory import (
    build_field,
    build_history_sync_frame,
    build_image_wmi,
    build_text_wmi,
    encode_varint,
)
from tests.merger.conftest import (
    _build_image_wmi_with_media_data,
    _build_quoted_wmi,
    _build_wmi_with_receipts,
    _make_two_pass_test_db,
)
from wab_archiver import increment_merger

_encode_varint = encode_varint
_build_field = build_field
_build_text_wmi = build_text_wmi
_build_image_wmi = build_image_wmi
_build_history_sync_frame = build_history_sync_frame


def test_parse_varint():
    stream = io.BytesIO(b"\x00\x01\xac\x02")
    assert increment_merger.parse_varint(stream) == 0
    assert increment_merger.parse_varint(stream) == 1
    assert increment_merger.parse_varint(stream) == 300
    assert increment_merger.parse_varint(stream) is None


def test_parse_varint_truncated():
    stream = io.BytesIO(b"\x80")
    with pytest.raises(ValueError, match="Truncated varint"):
        increment_merger.parse_varint(stream)


def test_parse_wire_fields():
    data = (
        _build_field(1, 0, 42)
        + _build_field(2, 2, b"hello")
        + struct.pack("<BI", (3 << 3) | 5, 12345)
        + struct.pack("<BQ", (4 << 3) | 1, 999999)
    )
    fields = increment_merger.parse_wire_fields(data)
    assert fields[1][0] == (0, 42)
    assert fields[2][0] == (2, b"hello")
    assert struct.unpack("<I", fields[3][0][1])[0] == 12345
    assert struct.unpack("<Q", fields[4][0][1])[0] == 999999


def test_decode_wmi_text():
    wmi_bytes = _build_text_wmi(
        "5550100@s.whatsapp.net", "TEST_KEY_1", "Hello World", 1, 1729000000
    )
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["remote_jid"] == "5550100@s.whatsapp.net"
    assert decoded["key_id"] == "TEST_KEY_1"
    assert decoded["text_data"] == "Hello World"
    assert decoded["from_me"] == 1
    assert decoded["timestamp"] == 1729000000000
    assert decoded["message_type"] == 0
    assert decoded["media_info"] is None


def test_decode_wmi_image():
    sha = b"\x01\x02\x03\x04" * 8
    wmi_bytes = _build_image_wmi(
        "5550100@s.whatsapp.net",
        "IMG_KEY_1",
        "A photo caption",
        "image/jpeg",
        sha,
        54321,
        0,
        1729000050,
    )
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["remote_jid"] == "5550100@s.whatsapp.net"
    assert decoded["key_id"] == "IMG_KEY_1"
    assert decoded["text_data"] == "A photo caption"
    assert decoded["from_me"] == 0
    assert decoded["message_type"] == 1
    assert decoded["media_info"] is not None
    assert decoded["media_info"]["file_size"] == 54321
    assert decoded["media_info"]["file_sha256"] == sha
    assert decoded["media_info"]["mime_type"] == "image/jpeg"


def test_decode_wmi_view_once_container():
    sha = b"\xaa" * 32
    img_bytes = (
        _build_field(1, 2, b"https://example.com/vo.enc")
        + _build_field(2, 2, b"image/jpeg")
        + _build_field(3, 2, b"Secret View Once Photo")
        + _build_field(4, 2, sha)
        + _build_field(5, 0, 12345)
    )
    inner_msg = _build_field(3, 2, img_bytes)
    vo_payload = _build_field(1, 2, inner_msg)
    msg_bytes = _build_field(37, 2, vo_payload)  # tag 37: viewOnceMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"VO_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000100)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "VO_KEY_1"
    assert decoded["is_view_once"] == 1
    assert decoded["message_type"] == 42  # view once image
    assert decoded["text_data"] == "Secret View Once Photo"
    assert decoded["media_info"]["file_size"] == 12345


def test_decode_wmi_ephemeral_container():
    ext_bytes = _build_field(1, 2, b"Disappearing text message")
    inner_msg = _build_field(6, 2, ext_bytes)
    eph_payload = _build_field(1, 2, inner_msg)
    msg_bytes = _build_field(40, 2, eph_payload)  # tag 40: ephemeralMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"EPH_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000200)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "EPH_KEY_1"
    assert decoded["is_ephemeral"] == 1
    assert decoded["text_data"] == "Disappearing text message"


@pytest.mark.parametrize(
    "tag, inner_text, jid, from_me, key_id",
    [
        (67, b"Prompt to Meta AI", b"5550100@s.whatsapp.net", 1, "BOT_KEY_1"),
        (58, b"Corrected text via tag 58", b"5550100@s.whatsapp.net", 1, "EDIT_KEY_58"),
        (62, b"@group mention payload", b"123456@g.us", 0, "GM_KEY_62"),
    ],
)
def test_decode_wmi_future_proof_containers(tag, inner_text, jid, from_me, key_id):
    """Verify unwrap_message_container handles botInvokeMessage (67), editedMessage (58), and groupMentionedMessage (62)."""
    inner_conv = _build_field(1, 2, inner_text)
    payload = _build_field(1, 2, inner_conv)
    msg_bytes = _build_field(tag, 2, payload)
    key_bytes = (
        _build_field(1, 2, jid) + _build_field(2, 0, from_me) + _build_field(3, 2, key_id.encode())
    )
    wmi_bytes = _build_field(1, 2, key_bytes) + _build_field(2, 2, msg_bytes)
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == key_id
    assert decoded["text_data"] == inner_text.decode()


def test_decode_wmi_event_message():
    """Verify decode_wmi parses eventMessage (tag 75) into formatted text."""
    ev_payload = _build_field(1, 2, b"Project Review") + _build_field(2, 2, b"Q3 deliverables sync")
    msg_bytes = _build_field(75, 2, ev_payload)
    key_bytes = (
        _build_field(1, 2, b"123456@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"EVENT_KEY_1")
    )
    wmi_bytes = _build_field(1, 2, key_bytes) + _build_field(2, 2, msg_bytes)
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "EVENT_KEY_1"
    assert decoded["message_type"] == 0
    assert decoded["text_data"] == "📅 Project Review - Q3 deliverables sync"


def test_decode_wmi_ptv_video_note():
    sha = b"\xbb" * 32
    ptv_bytes = (
        _build_field(1, 2, b"https://example.com/ptv.enc")
        + _build_field(2, 2, b"video/mp4")
        + _build_field(3, 2, sha)
        + _build_field(4, 0, 88888)
    )
    msg_bytes = _build_field(66, 2, ptv_bytes)  # tag 66: ptvMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"PTV_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000700)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "PTV_KEY_1"
    assert decoded["message_type"] == 28
    assert decoded["media_info"] is not None
    assert decoded["media_info"]["file_size"] == 88888


def test_read_delimited_messages_bin(tmp_path):
    wmi1 = _build_text_wmi("5550100@s.whatsapp.net", "K1", "Msg 1", 0, 1729000010)
    wmi2 = _build_text_wmi("5550100@s.whatsapp.net", "K2", "Msg 2", 1, 1729000020)

    frame1 = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi1])
    frame2 = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi2])

    bin_path = os.path.join(tmp_path, "messages.bin")
    with open(bin_path, "wb") as f:
        f.write(frame1 + frame2)

    messages = increment_merger.read_delimited_messages_bin(bin_path)
    assert len(messages) == 2
    assert messages[0]["key_id"] == "K1"
    assert messages[0]["text_data"] == "Msg 1"
    assert messages[1]["key_id"] == "K2"
    assert messages[1]["text_data"] == "Msg 2"


def test_extract_messages_from_proto_frame_rejects_binary_junk():
    # JPEG header starts with \xff\xd8\xff\xe0 and contains 0x0A (wire type 2, field 1)
    fake_jpeg_header = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\x0a\x14Photoshop 3.0"
    )
    messages = increment_merger.extract_messages_from_proto_frame(fake_jpeg_header)
    assert len(messages) == 0

    # Arbitrary random noise
    random_noise = b"\x0a\x05hello\x12\x05world\x00\xff\xee"
    messages = increment_merger.extract_messages_from_proto_frame(random_noise)
    assert len(messages) == 0


def test_decode_wmi_extracts_push_name():
    # Build WMI payload with field 19 (push_name)
    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"KEY_WMI_PUSHNAME")
    )
    msg_bytes = _build_field(1, 2, b"Hello from push contact")
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000500)
        + _build_field(19, 2, b"Alice Wonderland")
    )
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded is not None
    assert decoded["key_id"] == "KEY_WMI_PUSHNAME"
    assert decoded["push_name"] == "Alice Wonderland"
    assert decoded["text_data"] == "Hello from push contact"


def test_decode_wmi_with_context_info_quoted():
    """Verify decode_wmi extracts quoted_info from extendedTextMessage contextInfo."""
    wmi_bytes = _build_quoted_wmi(
        "5550100@s.whatsapp.net",
        "REPLY_MSG_1",
        "I agree with that point",
        1,
        1729000500,
        "ORIG_ST_123",
        "Original question",
        "5550200@s.whatsapp.net",
    )
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "REPLY_MSG_1"
    assert decoded["text_data"] == "I agree with that point"
    assert decoded["quoted_info"] is not None
    assert decoded["quoted_info"]["stanza_id"] == "ORIG_ST_123"
    assert decoded["quoted_info"]["quoted_text"] == "Original question"
    assert decoded["quoted_info"]["participant"] == "5550200@s.whatsapp.net"


def test_decode_wmi_with_user_receipts():
    """Verify decode_wmi extracts status and repeated user receipts."""
    receipts_input = [
        {
            "user_jid": "5550101@s.whatsapp.net",
            "receipt_timestamp": 1729000600,
            "read_timestamp": 1729000620,
        },
        {
            "user_jid": "5550102@s.whatsapp.net",
            "receipt_timestamp": 1729000605,
            "read_timestamp": 0,
        },
    ]
    wmi_bytes = _build_wmi_with_receipts(
        "12036302@g.us",
        "GROUP_MSG_1",
        "Meeting at 3pm",
        1,
        1729000590,
        status=4,  # Protobuf Status READ -> mapped to 13 in Android
        user_receipts=receipts_input,
    )
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "GROUP_MSG_1"
    assert decoded["status"] == 13  # mapped to Android READ
    assert len(decoded["user_receipts"]) == 2
    assert decoded["user_receipts"][0]["user_jid"] == "5550101@s.whatsapp.net"
    assert decoded["user_receipts"][0]["receipt_timestamp"] == 1729000600000
    assert decoded["user_receipts"][0]["read_timestamp"] == 1729000620000
    assert decoded["user_receipts"][1]["user_jid"] == "5550102@s.whatsapp.net"
    assert decoded["user_receipts"][1]["receipt_timestamp"] == 1729000605000
    assert decoded["user_receipts"][1]["read_timestamp"] is None


def test_decode_wmi_group_icon_change_tag39_photo_change(mock_db, tmp_path):
    """Verify Tag 39 (PhotoChange) containing JPEG image bytes is not misparsed as a user receipt."""
    jpeg_bytes = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00"
    )
    photo_change = _build_field(1, 2, jpeg_bytes) + _build_field(3, 0, 1729000600)
    key_bytes = (
        _build_field(1, 2, b"12036302@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"STUB_GROUP_ICON_1")
    )
    # WMI with stubType 22 (GROUP_CHANGE_ICON) and tag 39 PhotoChange
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(3, 0, 1729000600)
        + _build_field(4, 0, 1)
        + _build_field(24, 0, 22)  # GROUP_CHANGE_ICON stub type
        + _build_field(39, 2, photo_change)  # Tag 39 is PhotoChange, NOT UserReceipt
    )
    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "STUB_GROUP_ICON_1"
    # User receipts must be empty; JPEG bytes must never pollute user_receipts
    assert decoded["user_receipts"] == []

    # Ingesting into database must succeed without crashing on invalid JID
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS receipt_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            receipt_user_jid_row_id INTEGER NOT NULL,
            receipt_timestamp INTEGER,
            read_timestamp INTEGER,
            played_timestamp INTEGER
        );
    """)
    bin_path = str(tmp_path / "messages.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_bytes)) + wmi_bytes)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()
    assert inserted == 1
    cur = conn.cursor()
    ru_count = cur.execute("SELECT COUNT(*) FROM receipt_user").fetchone()[0]
    assert ru_count == 0
    conn.close()


def test_decode_wmi_stub_type():
    # Test wire Tag 24 (messageStubType) and Tag 25 (messageStubParameters)
    key_bytes = (
        _build_field(1, 2, b"390000000010@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"STUB_KEY_123")
    )
    # Tag 24: stub_type = 67 (E2E_IDENTITY_CHANGED)
    # Tag 25: repeated stub_param = "param1"
    wmi = (
        _build_field(1, 2, key_bytes)
        + _build_field(3, 0, 1729000000)
        + _build_field(24, 0, 67)
        + _build_field(25, 2, b"param1")
    )
    decoded = increment_merger.decode_wmi(wmi)
    assert decoded["key_id"] == "STUB_KEY_123"
    assert decoded["message_type"] == 7
    assert decoded["text_data"] is None
    assert decoded["stub_type"] == 67
    assert decoded["stub_params"] == ["param1"]
    assert decoded["has_content"] is True


def test_decode_wmi_empty_frame():
    # Frame with only key and timestamp, no message payload and no stub
    key_bytes = (
        _build_field(1, 2, b"390000000010@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"EMPTY_KEY_123")
    )
    wmi = _build_field(1, 2, key_bytes) + _build_field(3, 0, 1729000000)
    decoded = increment_merger.decode_wmi(wmi)
    assert decoded["key_id"] == "EMPTY_KEY_123"
    assert decoded["message_type"] == 0
    assert decoded["text_data"] is None
    assert decoded["stub_type"] is None
    assert decoded["has_content"] is False


def test_decode_wmi_stub_revoke():
    # Protobuf StubType.REVOKE (1) must decode to message_type = 15 (deleted), not system message (7)
    key_bytes = (
        _build_field(1, 2, b"390000000010@s.whatsapp.net")
        + _build_field(2, 0, 1)  # from_me = 1
        + _build_field(3, 2, b"REVOKE_KEY_999")
    )
    wmi = (
        _build_field(1, 2, key_bytes)
        + _build_field(3, 0, 1729000888)
        + _build_field(24, 0, 1)  # REVOKE = 1
    )
    decoded = increment_merger.decode_wmi(wmi)
    assert decoded["key_id"] == "REVOKE_KEY_999"
    assert decoded["message_type"] == 15
    assert decoded["text_data"] is None
    assert decoded["stub_type"] is None
    assert decoded["has_content"] is True


def test_decode_wmi_stub_translation():
    # Group stub types in Protobuf must translate to Android SystemActionType
    key_bytes = (
        _build_field(1, 2, b"123456-789@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"STUB_TRANS_KEY")
    )
    # 21 = GROUP_CHANGE_SUBJECT in Protobuf -> 1 in Android (SUBJECT_CHANGE)
    wmi_sub = _build_field(1, 2, key_bytes) + _build_field(3, 0, 100) + _build_field(24, 0, 21)
    dec_sub = increment_merger.decode_wmi(wmi_sub)
    assert dec_sub["message_type"] == 7
    assert dec_sub["stub_type"] == 1

    # 22 = GROUP_CHANGE_ICON in Protobuf -> 6 in Android (ICON_CHANGED)
    wmi_ico = _build_field(1, 2, key_bytes) + _build_field(3, 0, 100) + _build_field(24, 0, 22)
    dec_ico = increment_merger.decode_wmi(wmi_ico)
    assert dec_ico["message_type"] == 7
    assert dec_ico["stub_type"] == 6

    # 20 = GROUP_CREATE in Protobuf -> 11 in Android (GROUP_CREATED)
    wmi_cr = _build_field(1, 2, key_bytes) + _build_field(3, 0, 100) + _build_field(24, 0, 20)
    dec_cr = increment_merger.decode_wmi(wmi_cr)
    assert dec_cr["message_type"] == 7
    assert dec_cr["stub_type"] == 11


def test_decode_wmi_and_apply_receipt_only_frame(mock_db, tmp_path):
    """Verify decode_wmi has_content recognizes user receipts and attaches them without blank messages (F-04)."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS receipt_user (
            message_row_id INTEGER NOT NULL,
            receipt_user_jid_row_id INTEGER NOT NULL,
            receipt_timestamp INTEGER,
            read_timestamp INTEGER,
            played_timestamp INTEGER
        );
    """)

    cur = conn.cursor()
    cur.execute("SELECT _id FROM chat LIMIT 1")
    chat_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO message (chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) "
        "VALUES (?, 1, 'RECEIPT_TARGET_KEY', 1729000000, 0, 'Ping', 4)",
        (chat_id,),
    )
    target_msg_id = cur.lastrowid
    conn.commit()

    # Build a receipt-only WMI frame
    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"RECEIPT_TARGET_KEY")
    )
    ur_bytes = (
        _build_field(1, 2, b"5550200@s.whatsapp.net")
        + _build_field(2, 0, 1729000010)  # receipt_timestamp
        + _build_field(3, 0, 1729000020)  # read_timestamp
    )
    wmi_receipt = (
        _build_field(1, 2, key_bytes)
        + _build_field(40, 2, ur_bytes)
        + _build_field(3, 0, 1729000020)
    )

    decoded = increment_merger.decode_wmi(wmi_receipt)
    assert decoded["has_content"] is True
    assert len(decoded["user_receipts"]) == 1

    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_receipt])
    bin_file = tmp_path / "receipt_only.bin"
    bin_file.write_bytes(frame)

    increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()

    # Verify no new message was created (1 initial from mock_db + 1 target message)
    msg_count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    assert msg_count == 2

    # Verify receipt was attached to target_msg_id
    rcpt = conn.execute(
        "SELECT message_row_id, read_timestamp FROM receipt_user WHERE message_row_id = ?",
        (target_msg_id,),
    ).fetchone()
    assert rcpt is not None
    assert rcpt[1] == 1729000020000
    conn.close()


def test_decode_wmi_and_apply_messages_bin_quoted_media_thumbnail(mock_db, tmp_path):
    """Verify decode_wmi and apply_messages_bin handle quoted media thumbnails and message_type."""
    fake_thumb = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
    )
    img_inner = _build_field(3, 2, b"Sunset over the lake") + _build_field(16, 2, fake_thumb)
    quoted_msg = _build_field(3, 2, img_inner)
    ctx_bytes = (
        _build_field(1, 2, b"ORIG_PHOTO_99")
        + _build_field(2, 2, b"5550200@s.whatsapp.net")
        + _build_field(3, 2, quoted_msg)
    )
    ext_bytes = _build_field(1, 2, b"Amazing sunset!") + _build_field(17, 2, ctx_bytes)
    msg_bytes = _build_field(6, 2, ext_bytes)
    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"REPLY_IMG_KEY")
    )
    wmi_data = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000950)
    )

    decoded = increment_merger.decode_wmi(wmi_data)
    assert decoded["key_id"] == "REPLY_IMG_KEY"
    qi = decoded["quoted_info"]
    assert qi is not None
    assert qi["stanza_id"] == "ORIG_PHOTO_99"
    assert qi["quoted_text"] == "Sunset over the lake"
    assert qi["message_type"] == 1
    assert qi["thumbnail"] == fake_thumb

    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_quoted (
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
        CREATE TABLE IF NOT EXISTS message_quoted_media (
            message_row_id INTEGER PRIMARY KEY,
            thumbnail BLOB
        );
    """)

    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_data])
    bin_file = tmp_path / "messages_quoted_thumb.bin"
    bin_file.write_bytes(frame)

    inserted = increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()
    assert inserted == 1

    q_row = conn.execute(
        "SELECT message_row_id, key_id, text_data, message_type FROM message_quoted WHERE key_id = 'ORIG_PHOTO_99'"
    ).fetchone()
    assert q_row is not None
    assert q_row[1] == "ORIG_PHOTO_99"
    assert q_row[2] == "Sunset over the lake"
    assert q_row[3] == 1

    mqm_row = conn.execute(
        "SELECT message_row_id, thumbnail FROM message_quoted_media WHERE message_row_id = ?",
        (q_row[0],),
    ).fetchone()
    assert mqm_row is not None
    assert mqm_row[1] == fake_thumb
    conn.close()


@pytest.mark.parametrize(
    "key_id, raw_path, expected_path",
    [
        (
            "KEY_IMG_38",
            "Media/WhatsApp Images/IMG-20261016-WA0005.jpg",
            "Media/WhatsApp Images/IMG-20261016-WA0005.jpg",
        ),
        (
            "KEY_IMG_NORM",
            ".\\Media\\WhatsApp Images\\IMG-20261016-WA0006.jpg",
            "Media/WhatsApp Images/IMG-20261016-WA0006.jpg",
        ),
    ],
)
def test_decode_wmi_extracts_and_normalizes_tag38_local_path(key_id, raw_path, expected_path):
    wmi_bytes = _build_image_wmi_with_media_data(
        "123456789@s.whatsapp.net",
        key_id,
        "Test caption",
        "image/jpeg",
        b"\x01" * 32,
        45000,
        0,
        1729000000000,
        raw_path,
    )
    msg = increment_merger.decode_wmi(wmi_bytes)
    assert msg["local_path"] == expected_path
    assert msg["media_info"] is not None
    assert msg["media_info"]["file_path"] == expected_path


def test_malformed_wire_jid_safe_fallback(tmp_path):
    """Verify that corrupt JID wire strings do not abort incremental merging."""
    conn = _make_two_pass_test_db(tmp_path / "test_malformed_jid.db")
    bin_path = str(tmp_path / "messages.bin")

    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "PARENT_JID_SAFE", "Text message", 0, 1729000100
    )

    # Embedded reaction with invalid participant wire bytes
    rx_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"RX_CORRUPT_KEY")
    )
    corrupt_participant = b"\xff\xfe\x00\x01\x80\x00\x12"
    rx_bytes = (
        _build_field(1, 2, rx_key)
        + _build_field(2, 2, "👍".encode())
        + _build_field(3, 2, corrupt_participant)
        + _build_field(4, 0, 1729000100000)
    )
    wmi_with_corrupt_rx = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 1)
            + _build_field(3, 2, b"MSG_PARENT_WITH_RX"),
        )
        + _build_field(2, 2, _build_field(1, 2, b"Parent with corrupt reaction"))
        + _build_field(3, 0, 1729000100)
        + _build_field(41, 2, rx_bytes)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)
        f.write(_encode_varint(len(wmi_with_corrupt_rx)) + wmi_with_corrupt_rx)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted >= 1
    cur = conn.cursor()
    # Reaction was safely recorded with fallback sender_jid_row_id = -1
    rx_row = cur.execute(
        "SELECT ao.sender_jid_row_id, r.reaction "
        "FROM message_add_on ao "
        "JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id "
        "WHERE ao.key_id = 'RX_CORRUPT_KEY'"
    ).fetchone()
    assert rx_row is not None
    assert rx_row[0] == -1
    assert rx_row[1] == "👍"
    conn.close()
