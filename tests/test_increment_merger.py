import base64
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import struct
import zipfile

import pytest
from wab_archiver import increment_merger

logger = logging.getLogger("test_increment_merger")


# ---------------------------------------------------------------------------
# Protobuf Helper Functions for Tests
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    buf = bytearray()
    while True:
        towrite = value & 0x7F
        value >>= 7
        if value:
            buf.append(towrite | 0x80)
        else:
            buf.append(towrite)
            break
    return bytes(buf)


def _build_field(field_num: int, wire_type: int, payload: bytes | int) -> bytes:
    tag = (field_num << 3) | wire_type
    tag_bytes = _encode_varint(tag)
    if wire_type == 0:
        return tag_bytes + _encode_varint(payload)
    elif wire_type == 1:
        return tag_bytes + payload
    elif wire_type == 2:
        return tag_bytes + _encode_varint(len(payload)) + payload
    elif wire_type == 5:
        return tag_bytes + payload
    raise ValueError(f"Unsupported wire type {wire_type}")


def _build_text_wmi(remote_jid: str, key_id: str, text: str, from_me: int, timestamp: int) -> bytes:
    key_bytes = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, from_me)
        + _build_field(3, 2, key_id.encode("utf-8"))
    )
    msg_bytes = _build_field(1, 2, text.encode("utf-8"))
    return (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, timestamp)
    )


def _build_image_wmi(
    remote_jid: str,
    key_id: str,
    caption: str,
    mimetype: str,
    file_sha256: bytes,
    file_length: int,
    from_me: int,
    timestamp: int,
) -> bytes:
    key_bytes = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, from_me)
        + _build_field(3, 2, key_id.encode("utf-8"))
    )
    img_bytes = (
        _build_field(1, 2, b"https://example.com/media.enc")
        + _build_field(2, 2, mimetype.encode("utf-8"))
        + _build_field(3, 2, caption.encode("utf-8"))
        + _build_field(4, 2, file_sha256)
        + _build_field(5, 0, file_length)
    )
    msg_bytes = _build_field(3, 2, img_bytes)
    return (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, timestamp)
    )


def _build_history_sync_frame(remote_jid: str, wmi_bytes_list: list[bytes]) -> bytes:
    hsm_bytes = b"".join(_build_field(1, 2, wmi) for wmi in wmi_bytes_list)
    conv_bytes = _build_field(1, 2, remote_jid.encode("utf-8")) + _build_field(2, 2, hsm_bytes)
    hs_bytes = _build_field(2, 2, conv_bytes)
    return _encode_varint(len(hs_bytes)) + hs_bytes


# ---------------------------------------------------------------------------
# Unit Tests: Protobuf Parser
# ---------------------------------------------------------------------------


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


def test_decode_wmi_reaction_tag46():
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"PARENT_MSG_KEY_123")
    )
    reaction_bytes = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, "🔥".encode())
        + _build_field(4, 0, 1729000300000)
    )
    msg_bytes = _build_field(46, 2, reaction_bytes)  # tag 46: reactionMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"REACT_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000300)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "REACT_KEY_1"
    assert decoded["message_type"] == 25
    assert decoded["text_data"] == "🔥"
    assert decoded["reaction_info"] is not None
    assert decoded["reaction_info"]["target_key_id"] == "PARENT_MSG_KEY_123"
    assert decoded["reaction_info"]["emoji"] == "🔥"
    assert decoded["reaction_info"]["sender_ts"] == 1729000300000


def test_decode_wmi_location():
    loc_bytes = (
        _build_field(1, 1, struct.pack("<d", 37.7749))
        + _build_field(2, 1, struct.pack("<d", -122.4194))
        + _build_field(3, 2, b"Market Street")
        + _build_field(4, 2, b"San Francisco, CA")
    )
    msg_bytes = _build_field(5, 2, loc_bytes)  # tag 5: locationMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"LOC_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000400)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "LOC_KEY_1"
    assert decoded["message_type"] == 5
    assert decoded["text_data"] == "Market Street"
    assert decoded["location_info"] is not None
    assert round(decoded["location_info"]["latitude"], 4) == 37.7749
    assert round(decoded["location_info"]["longitude"], 4) == -122.4194
    assert decoded["location_info"]["place_address"] == "San Francisco, CA"


def test_decode_wmi_poll_creation():
    opt1 = _build_field(1, 2, b"Option A")
    opt2 = _build_field(1, 2, b"Option B")
    poll_bytes = (
        _build_field(2, 2, b"What is your choice?")
        + _build_field(3, 2, opt1)
        + _build_field(3, 2, opt2)
        + _build_field(4, 0, 1)  # single choice
    )
    msg_bytes = _build_field(49, 2, poll_bytes)  # tag 49: pollCreationMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"POLL_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000500)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "POLL_KEY_1"
    assert decoded["message_type"] == 66
    assert decoded["text_data"] == "What is your choice?"
    assert decoded["poll_info"] is not None
    assert decoded["poll_info"]["question"] == "What is your choice?"
    assert decoded["poll_info"]["options"] == ["Option A", "Option B"]
    assert decoded["poll_info"]["selectable_count"] == 1


def test_decode_wmi_pin_in_chat():
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"TARGET_PIN_KEY")
    )
    pin_bytes = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, 1)  # pin
        + _build_field(3, 0, 1729000600000)
    )
    msg_bytes = _build_field(63, 2, pin_bytes)  # tag 63: pinInChatMessage

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"PIN_KEY_1")
    )
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000600)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "PIN_KEY_1"
    assert decoded["pin_info"] is not None
    assert decoded["pin_info"]["target_key_id"] == "TARGET_PIN_KEY"
    assert decoded["pin_info"]["pin_state"] == 1


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


def test_decode_wmi_protocol_revoke_and_edit():
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"TARGET_TO_REVOKE")
    )
    revoke_proto = _build_field(1, 2, target_key) + _build_field(2, 0, 0)
    wmi_revoke = (
        _build_field(1, 2, _build_field(3, 2, b"PROTO_REVOKE"))
        + _build_field(2, 2, _build_field(12, 2, revoke_proto))
        + _build_field(3, 0, 1729000800)
    )
    decoded_rev = increment_merger.decode_wmi(wmi_revoke)
    assert decoded_rev["message_type"] == 15
    assert decoded_rev["revoke_info"]["target_key_id"] == "TARGET_TO_REVOKE"

    edit_inner = _build_field(1, 2, b"Updated Edited Text")
    edit_proto = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, 14)  # edit
        + _build_field(10, 0, 1729000850000)
        + _build_field(14, 2, edit_inner)
    )
    wmi_edit = (
        _build_field(1, 2, _build_field(3, 2, b"PROTO_EDIT"))
        + _build_field(2, 2, _build_field(12, 2, edit_proto))
        + _build_field(3, 0, 1729000850)
    )
    decoded_edit = increment_merger.decode_wmi(wmi_edit)
    assert decoded_edit["text_data"] == "Updated Edited Text"
    assert decoded_edit["edit_info"]["target_key_id"] == "TARGET_TO_REVOKE"
    assert decoded_edit["edit_info"]["new_text"] == "Updated Edited Text"
    assert decoded_edit["edit_info"]["edited_ts"] == 1729000850000


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


# ---------------------------------------------------------------------------
# Unit Tests: Increment Discovery & Sorting
# ---------------------------------------------------------------------------


def test_find_increment_files(tmp_path):
    f3 = tmp_path / "msgstore-increment-3-2026-10-16.db.crypt15"
    f1 = tmp_path / "msgstore-increment-1-2026-10-14.db.crypt15"
    f2 = tmp_path / "msgstore-increment-2-2026-10-15.db.crypt15"
    f_tmp = tmp_path / "msgstore-increment-4.tmp"
    f_base = tmp_path / "msgstore.db.crypt15"

    for p in (f3, f1, f2, f_tmp, f_base):
        p.write_bytes(b"data")

    found = increment_merger.find_increment_files(str(tmp_path))
    assert len(found) == 3
    assert [os.path.basename(p) for p in found] == [
        "msgstore-increment-1-2026-10-14.db.crypt15",
        "msgstore-increment-2-2026-10-15.db.crypt15",
        "msgstore-increment-3-2026-10-16.db.crypt15",
    ]


# ---------------------------------------------------------------------------
# Unit Tests: SQLite Delta Replay
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test_msgstore.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE jid (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            server TEXT NOT NULL,
            raw_string TEXT
        );
        CREATE TABLE chat (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            jid_row_id INTEGER NOT NULL,
            subject TEXT,
            created_timestamp INTEGER
        );
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            message_type INTEGER NOT NULL,
            text_data TEXT,
            starred INTEGER DEFAULT 0,
            status INTEGER DEFAULT 0
        );
        CREATE TABLE message_media (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            chat_row_id INTEGER NOT NULL,
            file_path TEXT,
            file_size INTEGER,
            media_name TEXT,
            message_url TEXT,
            mime_type TEXT,
            file_hash TEXT,
            transferred INTEGER DEFAULT 1
        );
        CREATE TABLE props (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT
        );

        INSERT INTO jid (user, server, raw_string) VALUES ('5550100', 's.whatsapp.net', '5550100@s.whatsapp.net');
        INSERT INTO chat (jid_row_id, subject) VALUES (1, NULL);
        INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
        VALUES (100, 1, 0, 'INITIAL_KEY_1', 1, 1729000000000, 0, 'Original message');
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def test_apply_entity_modifications(mock_db, tmp_path):
    mod_json = tmp_path / "props_modified_1.json"
    mod_json.write_text(
        json.dumps(
            [{"key": "backup_version", "value": "2.0"}, {"key": "feature_flag", "value": "1"}]
        )
    )

    conn = sqlite3.connect(mock_db)
    count = increment_merger.apply_entity_modifications(conn, "props", str(mod_json), logger)
    conn.commit()
    assert count == 2

    cur = conn.cursor()
    val = cur.execute("SELECT value FROM props WHERE key = 'backup_version'").fetchone()[0]
    assert val == "2.0"
    conn.close()


def test_apply_entity_deletions(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute("INSERT INTO props (key, value) VALUES ('old_prop', 'temp')")
    conn.commit()

    del_json = tmp_path / "props_deleted_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [1]}))

    count = increment_merger.apply_entity_deletions(conn, "props", str(del_json), logger)
    conn.commit()
    assert count == 1
    conn.close()


def test_apply_message_deletions(mock_db, tmp_path):
    del_json = tmp_path / "deletedMessages_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [100]}))

    conn = sqlite3.connect(mock_db)
    count = increment_merger.apply_message_deletions(conn, str(del_json), logger, None, None, None)
    conn.commit()
    assert count == 1

    remaining = conn.execute("SELECT COUNT(*) FROM message WHERE _id = 100").fetchone()[0]
    assert remaining == 0
    conn.close()


def test_apply_messages_bin(mock_db, tmp_path):
    wmi_text = _build_text_wmi(
        "5550100@s.whatsapp.net", "INC_MSG_1", "Incremental text", 1, 1729000100
    )
    wmi_img = _build_image_wmi(
        "5550100@s.whatsapp.net",
        "INC_IMG_1",
        "Incremental image",
        "image/jpeg",
        b"\xaa" * 32,
        45678,
        0,
        1729000200,
    )
    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_text, wmi_img])

    bin_path = tmp_path / "messages.bin"
    bin_path.write_bytes(frame)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, str(bin_path), logger, None, None, None)
    conn.commit()
    assert inserted == 2

    cur = conn.cursor()
    msgs = cur.execute(
        "SELECT key_id, text_data, message_type FROM message ORDER BY _id"
    ).fetchall()
    assert len(msgs) == 3
    assert msgs[1] == ("INC_MSG_1", "Incremental text", 0)
    assert msgs[2] == ("INC_IMG_1", "Incremental image", 1)

    media = cur.execute("SELECT file_size, mime_type FROM message_media").fetchall()
    assert len(media) == 1
    assert media[0] == (45678, "image/jpeg")
    conn.close()


def test_apply_messages_bin_view_once_media(mock_db, tmp_path):
    """Verify apply_messages_bin sets message_type=42 and inserts into message_view_once_media."""
    img_bytes = (
        _build_field(1, 2, b"https://example.com/vo.enc")
        + _build_field(2, 2, b"image/jpeg")
        + _build_field(3, 2, b"VO photo")
        + _build_field(4, 2, b"\xbb" * 32)
        + _build_field(5, 0, 12345)
    )
    inner_msg = _build_field(3, 2, img_bytes)
    vo_payload = _build_field(1, 2, inner_msg)
    msg_bytes_vo = _build_field(55, 2, vo_payload)  # tag 55: viewOnceMessageV2

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"VO_KEY_1")
    )
    wmi_vo = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes_vo)
        + _build_field(3, 0, 1729000300)
    )
    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_vo])
    bin_path = tmp_path / "messages.bin"
    bin_path.write_bytes(frame)

    conn = sqlite3.connect(mock_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS message_view_once_media (message_row_id INTEGER PRIMARY KEY, state INTEGER NOT NULL)"
    )
    conn.commit()

    inserted = increment_merger.apply_messages_bin(conn, str(bin_path), logger, None, None, None)
    conn.commit()
    assert inserted == 1

    cur = conn.cursor()
    row = cur.execute(
        "SELECT _id, key_id, message_type FROM message WHERE key_id = 'VO_KEY_1'"
    ).fetchone()
    assert row is not None
    msg_id, key_id, msg_type = row
    assert msg_type == 42

    vom = cur.execute(
        "SELECT message_row_id, state FROM message_view_once_media WHERE message_row_id = ?",
        (msg_id,),
    ).fetchone()
    assert vom is not None
    assert vom[0] == msg_id
    assert vom[1] == 1
    conn.close()


def test_apply_sequences(mock_db, tmp_path):
    seq_json = tmp_path / "sequences.json"
    seq_json.write_text(
        json.dumps([{"name": "message", "value": 500}, {"name": "chat", "value": 10}])
    )

    conn = sqlite3.connect(mock_db)
    count = increment_merger.apply_sequences(conn, str(seq_json), logger)
    conn.commit()
    assert count == 2

    cur = conn.cursor()
    msg_seq = cur.execute("SELECT seq FROM SQLITE_SEQUENCE WHERE name = 'message'").fetchone()[0]
    chat_seq = cur.execute("SELECT seq FROM SQLITE_SEQUENCE WHERE name = 'chat'").fetchone()[0]
    assert msg_seq == 500
    assert chat_seq == 10
    conn.close()


# ---------------------------------------------------------------------------
# Integration Tests: Full Incremental Merge Pipeline
# ---------------------------------------------------------------------------


def test_merge_increments_end_to_end(mock_db, tmp_path):
    # Create increment 1: deletes msg 100, inserts msg 101
    inc1_dir = tmp_path / "inc1_contents"
    inc1_dir.mkdir()
    (inc1_dir / "header.json").write_text(
        json.dumps(
            {
                "format": "protobuf",
                "deleted_message_ids_files": ["deletedMessages_1.json"],
                "sequences": "sequences.json",
                "entities": ["props"],
            }
        )
    )
    (inc1_dir / "deletedMessages_1.json").write_text(json.dumps({"deleted_ids": [100]}))
    (inc1_dir / "sequences.json").write_text(json.dumps([{"name": "message", "value": 101}]))
    (inc1_dir / "props_modified_1.json").write_text(
        json.dumps([{"key": "app_version", "value": "1.0"}])
    )

    wmi101 = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_101", "Day 1 new message", 0, 1729000100
    )
    (inc1_dir / "messages.bin").write_bytes(
        _build_history_sync_frame("5550100@s.whatsapp.net", [wmi101])
    )

    inc1_zip = tmp_path / "msgstore-increment-1-2026-10-15.db.crypt15"
    with zipfile.ZipFile(str(inc1_zip), "w") as z:
        for f in inc1_dir.iterdir():
            z.write(str(f), arcname=f.name)

    # Create increment 2: inserts msg 102
    inc2_dir = tmp_path / "inc2_contents"
    inc2_dir.mkdir()
    (inc2_dir / "header.json").write_text(
        json.dumps(
            {
                "format": "protobuf",
                "deleted_message_ids_files": [],
                "sequences": "sequences.json",
            }
        )
    )
    (inc2_dir / "sequences.json").write_text(json.dumps([{"name": "message", "value": 102}]))
    wmi102 = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_102", "Day 2 new message", 1, 1729000200
    )
    (inc2_dir / "messages.bin").write_bytes(
        _build_history_sync_frame("5550100@s.whatsapp.net", [wmi102])
    )

    inc2_zip = tmp_path / "msgstore-increment-2-2026-10-16.db.crypt15"
    with zipfile.ZipFile(str(inc2_zip), "w") as z:
        for f in inc2_dir.iterdir():
            z.write(str(f), arcname=f.name)

    # Run merge_increments
    output_db = str(tmp_path / "merged_msgstore.db")
    staging_dir = str(tmp_path / "staging")

    total_inserted = increment_merger.merge_increments(
        mock_db,
        [str(inc1_zip), str(inc2_zip)],
        "mock_key",
        output_db,
        staging_dir,
        logger,
    )

    assert total_inserted == 2

    # Verify final database content
    conn = sqlite3.connect(output_db)
    cur = conn.cursor()

    # Msg 100 must be deleted
    old_msg = cur.execute("SELECT COUNT(*) FROM message WHERE _id = 100").fetchone()[0]
    assert old_msg == 0

    # Msgs 101 and 102 must exist
    rows = cur.execute("SELECT key_id, text_data FROM message ORDER BY _id").fetchall()
    assert len(rows) == 2
    assert rows[0] == ("KEY_101", "Day 1 new message")
    assert rows[1] == ("KEY_102", "Day 2 new message")

    # Props updated
    prop = cur.execute("SELECT value FROM props WHERE key = 'app_version'").fetchone()[0]
    assert prop == "1.0"

    # Sequences advanced
    seq = cur.execute("SELECT seq FROM SQLITE_SEQUENCE WHERE name = 'message'").fetchone()[0]
    assert seq == 102

    conn.close()


def test_main_decrypt_with_increments_cleanup_on_success(mock_db, tmp_path, monkeypatch):
    import argparse

    from wab_archiver import main

    output_dir = tmp_path / "output"
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    # Create increment
    inc1_dir = tmp_path / "inc1_contents"
    inc1_dir.mkdir()
    (inc1_dir / "header.json").write_text(
        json.dumps({"format": "protobuf", "deleted_message_ids_files": []})
    )
    wmi = _build_text_wmi("5550100@s.whatsapp.net", "KEY_STAGED", "Staged msg", 0, 1729000100)
    (inc1_dir / "messages.bin").write_bytes(
        _build_history_sync_frame("5550100@s.whatsapp.net", [wmi])
    )

    inc_file = staging_dir / "msgstore-increment-1-2026-10-15.db.crypt15"
    with zipfile.ZipFile(str(inc_file), "w") as z:
        for f in inc1_dir.iterdir():
            z.write(str(f), arcname=f.name)

    base_db = staging_dir / "msgstore.db.crypt15"
    base_db.write_bytes(b"dummy_encrypted")

    # Mock decrypt_msgstore to simply copy mock_db
    monkeypatch.setattr(
        main, "decrypt_msgstore", lambda src, key, dst, log: shutil.copy2(mock_db, dst)
    )

    args = argparse.Namespace(
        msgstore=str(base_db),
        output=str(output_dir),
        e2e_key="a" * 64,
        preserve_deleted=False,
        _staged_increments=[str(inc_file)],
    )

    main._decrypt_msgstore_if_needed(args, logger)

    # 1. Output database exists and contains merged message
    final_db = output_dir / "Whatsapp Databases" / "msgstore.db"
    assert final_db.exists()
    conn = sqlite3.connect(str(final_db))
    msg_exists = conn.execute(
        "SELECT COUNT(*) FROM message WHERE key_id = 'KEY_STAGED'"
    ).fetchone()[0]
    assert msg_exists == 1
    conn.close()

    # 2. Staged increment was cleaned up upon successful merge and attribute cleared
    assert not inc_file.exists()
    assert args._staged_increments == []

    # 3. Subsequent invocation is an idempotent no-op and does not raise FileNotFoundError
    main._decrypt_msgstore_if_needed(args, logger)
    assert args.msgstore == str(final_db)


def test_main_decrypt_with_increments_retained_on_failure(mock_db, tmp_path, monkeypatch):
    import argparse

    from wab_archiver import main

    output_dir = tmp_path / "output"
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    # Corrupt increment file that cannot be unpacked
    inc_file = staging_dir / "msgstore-increment-1-2026-10-15.db.crypt15"
    inc_file.write_bytes(b"corrupt_non_zip_data")

    base_db = staging_dir / "msgstore.db.crypt15"
    base_db.write_bytes(b"dummy_encrypted")

    monkeypatch.setattr(
        main, "decrypt_msgstore", lambda src, key, dst, log: shutil.copy2(mock_db, dst)
    )

    args = argparse.Namespace(
        msgstore=str(base_db),
        output=str(output_dir),
        e2e_key="a" * 64,
        preserve_deleted=False,
        _staged_increments=[str(inc_file)],
    )

    with pytest.raises(Exception):
        main._decrypt_msgstore_if_needed(args, logger)

    # Staged increment MUST be retained when an error occurs so data is not lost
    assert inc_file.exists()


def test_adb_enumerate_and_pull(tmp_path, monkeypatch):
    from wab_archiver import adb_extractor

    remote_ls_output = """
msgstore-increment-2-2026-10-15.db.crypt15
msgstore.db.crypt15
msgstore-increment-1-2026-10-14.db.crypt15
msgstore-2026-10-10.1.db.crypt15
other_file.txt
"""

    class MockCompletedProcess:
        def __init__(self, stdout):
            self.stdout = stdout.encode("utf-8")
            self.returncode = 0

    def mock_run_adb(cmd, log):
        return MockCompletedProcess(remote_ls_output)

    monkeypatch.setattr(adb_extractor, "_run_adb", mock_run_adb)

    base_remote, inc_remotes = adb_extractor.enumerate_remote_backup_files(False, logger)
    assert base_remote is not None
    assert base_remote.endswith("msgstore.db.crypt15")
    assert len(inc_remotes) == 2
    assert inc_remotes[0].endswith("msgstore-increment-1-2026-10-14.db.crypt15")
    assert inc_remotes[1].endswith("msgstore-increment-2-2026-10-15.db.crypt15")

    pulled_cmds = []

    def mock_subprocess_run(cmd, check, stderr):
        pulled_cmds.append(cmd)
        # Create destination file
        dest = cmd[3]
        with open(dest, "w") as f:
            f.write("pulled")

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    staging_dir = str(tmp_path / "adb_staging")
    local_base, local_incs = adb_extractor.pull_msgstore_and_increments(staging_dir, False, logger)

    assert os.path.basename(local_base) == "msgstore.db.crypt15"
    assert len(local_incs) == 2
    assert os.path.basename(local_incs[0]) == "msgstore-increment-1-2026-10-14.db.crypt15"
    assert os.path.basename(local_incs[1]) == "msgstore-increment-2-2026-10-15.db.crypt15"


def test_apply_messages_bin_auxiliary_entities(tmp_path):
    db_path = tmp_path / "test_aux.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, server TEXT NOT NULL, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY AUTOINCREMENT, jid_row_id INTEGER NOT NULL UNIQUE, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER);
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            message_type INTEGER NOT NULL,
            text_data TEXT,
            starred INTEGER DEFAULT 0,
            status INTEGER DEFAULT 0
        );
        CREATE TABLE message_media (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            chat_row_id INTEGER NOT NULL,
            file_path TEXT,
            file_size INTEGER,
            media_name TEXT,
            message_url TEXT,
            mime_type TEXT,
            file_hash TEXT,
            transferred INTEGER DEFAULT 1
        );
        CREATE TABLE message_add_on (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            parent_message_row_id INTEGER NOT NULL,
            status INTEGER DEFAULT 0,
            message_add_on_type INTEGER NOT NULL,
            timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_reaction (
            message_add_on_row_id INTEGER PRIMARY KEY,
            reaction TEXT NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_pin_in_chat (
            message_add_on_row_id INTEGER PRIMARY KEY,
            pin_in_chat_state INTEGER NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_poll (
            message_row_id INTEGER PRIMARY KEY,
            selectable_options_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE message_poll_option (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            option_name TEXT NOT NULL,
            vote_total INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE message_location (
            message_row_id INTEGER PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            place_name TEXT,
            place_address TEXT,
            url TEXT,
            live_location_share_duration INTEGER DEFAULT 0,
            live_location_sequence_number INTEGER DEFAULT 0
        );
        CREATE TABLE message_edit_info (
            message_row_id INTEGER PRIMARY KEY,
            original_key_id TEXT NOT NULL,
            edited_timestamp INTEGER NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_poll_vote (
            message_add_on_row_id INTEGER PRIMARY KEY,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_poll_vote_selected_option (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_add_on_row_id INTEGER NOT NULL,
            message_poll_option_id INTEGER NOT NULL
        );

        INSERT INTO jid (user, server, raw_string) VALUES ('5550100', 's.whatsapp.net', '5550100@s.whatsapp.net');
        INSERT INTO chat (jid_row_id) VALUES (1);
        INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
        VALUES (1, 1, 0, 'PARENT_KEY_1', 1, 1729000000000, 0, 'Original text before edit');
    """)
    conn.commit()

    # 1. Build a reaction message targeting PARENT_KEY_1
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"PARENT_KEY_1")
    )
    reaction_bytes = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, "🚀".encode())
        + _build_field(4, 0, 1729000100000)
    )
    wmi_react = (
        _build_field(
            1, 2, _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(3, 2, b"REACT_1")
        )
        + _build_field(2, 2, _build_field(46, 2, reaction_bytes))
        + _build_field(3, 0, 1729000100)
    )

    # 2. Build a poll message
    poll_bytes = (
        _build_field(2, 2, b"Which framework?")
        + _build_field(3, 2, _build_field(1, 2, b"React"))
        + _build_field(3, 2, _build_field(1, 2, b"Vue"))
        + _build_field(4, 0, 1)
    )
    wmi_poll = (
        _build_field(
            1, 2, _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(3, 2, b"POLL_1")
        )
        + _build_field(2, 2, _build_field(49, 2, poll_bytes))
        + _build_field(3, 0, 1729000200)
    )

    # 3. Build a pin message targeting PARENT_KEY_1
    pin_bytes = (
        _build_field(1, 2, target_key) + _build_field(2, 0, 1) + _build_field(3, 0, 1729000300000)
    )
    wmi_pin = (
        _build_field(
            1, 2, _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(3, 2, b"PIN_1")
        )
        + _build_field(2, 2, _build_field(63, 2, pin_bytes))
        + _build_field(3, 0, 1729000300)
    )

    # 4. Build a location message
    loc_bytes = (
        _build_field(1, 1, struct.pack("<d", 48.8584))
        + _build_field(2, 1, struct.pack("<d", 2.2945))
        + _build_field(3, 2, b"Eiffel Tower")
        + _build_field(4, 2, b"Champ de Mars, Paris")
    )
    wmi_loc = (
        _build_field(
            1, 2, _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(3, 2, b"LOC_1")
        )
        + _build_field(2, 2, _build_field(5, 2, loc_bytes))
        + _build_field(3, 0, 1729000400)
    )

    # 5. Build an edit message updating PARENT_KEY_1
    edit_proto = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, 14)
        + _build_field(10, 0, 1729000500000)
        + _build_field(14, 2, _build_field(1, 2, b"Updated text via edit"))
    )
    wmi_edit = (
        _build_field(
            1, 2, _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(3, 2, b"EDIT_1")
        )
        + _build_field(2, 2, _build_field(12, 2, edit_proto))
        + _build_field(3, 0, 1729000500)
    )

    # Write all to messages.bin
    bin_path = str(tmp_path / "messages.bin")
    frames = [wmi_react, wmi_poll, wmi_pin, wmi_loc, wmi_edit]
    with open(bin_path, "wb") as f:
        for wmi in frames:
            f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 5

    # Verify reaction in message_add_on & message_add_on_reaction
    react_row = conn.execute(
        "SELECT a.parent_message_row_id, r.reaction FROM message_add_on a "
        "JOIN message_add_on_reaction r ON a._id = r.message_add_on_row_id "
        "WHERE a.message_add_on_type = 1"
    ).fetchone()
    assert react_row is not None
    assert react_row[0] == 1
    assert react_row[1] == "🚀"

    # Verify poll creation
    poll_row = conn.execute(
        "SELECT m.text_data, p.selectable_options_count FROM message m "
        "JOIN message_poll p ON m._id = p.message_row_id WHERE m.key_id = 'POLL_1'"
    ).fetchone()
    assert poll_row is not None
    assert poll_row[0] == "Which framework?"
    assert poll_row[1] == 1

    options = [
        r[0]
        for r in conn.execute(
            "SELECT option_name FROM message_poll_option WHERE message_row_id = (SELECT _id FROM message WHERE key_id = 'POLL_1') ORDER BY _id"
        ).fetchall()
    ]
    assert options == ["React", "Vue"]

    # Verify pin
    pin_row = conn.execute(
        "SELECT a.parent_message_row_id, p.pin_in_chat_state FROM message_add_on a "
        "JOIN message_add_on_pin_in_chat p ON a._id = p.message_add_on_row_id "
        "WHERE a.message_add_on_type = 79"
    ).fetchone()
    assert pin_row is not None
    assert pin_row[0] == 1
    assert pin_row[1] == 1

    # Verify location
    loc_row = conn.execute(
        "SELECT l.place_name, l.place_address FROM message_location l "
        "JOIN message m ON l.message_row_id = m._id WHERE m.key_id = 'LOC_1'"
    ).fetchone()
    assert loc_row is not None
    assert loc_row[0] == "Eiffel Tower"
    assert loc_row[1] == "Champ de Mars, Paris"

    # Verify edited message
    edited_msg = conn.execute("SELECT text_data FROM message WHERE _id = 1").fetchone()
    assert edited_msg[0] == "Updated text via edit"
    edit_info_row = conn.execute(
        "SELECT original_key_id FROM message_edit_info WHERE message_row_id = 1"
    ).fetchone()
    assert edit_info_row is not None
    assert edit_info_row[0] == "PARENT_KEY_1"

    conn.close()


# ---------------------------------------------------------------------------
# Integration & E2E Tests: Increment Merging, Quoted Messages, Receipts & Viewer
# ---------------------------------------------------------------------------


def _build_quoted_wmi(
    remote_jid: str,
    key_id: str,
    text: str,
    from_me: int,
    timestamp: int,
    quoted_stanza_id: str,
    quoted_text: str,
    quoted_participant: str,
) -> bytes:
    """Build a WebMessageInfo with an extendedTextMessage containing ContextInfo quoted message."""
    # ContextInfo: field 1 = stanzaId, field 2 = participant, field 3 = quotedMessage (conversation = 1)
    q_msg_bytes = _build_field(1, 2, quoted_text.encode("utf-8"))
    ctx_bytes = (
        _build_field(1, 2, quoted_stanza_id.encode("utf-8"))
        + _build_field(2, 2, quoted_participant.encode("utf-8"))
        + _build_field(3, 2, q_msg_bytes)
    )
    # extendedTextMessage: field 1 = text, field 17 = contextInfo
    ext_bytes = _build_field(1, 2, text.encode("utf-8")) + _build_field(17, 2, ctx_bytes)
    msg_bytes = _build_field(6, 2, ext_bytes)

    key_bytes = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, from_me)
        + _build_field(3, 2, key_id.encode("utf-8"))
    )
    return (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, timestamp)
    )


def _build_wmi_with_receipts(
    remote_jid: str,
    key_id: str,
    text: str,
    from_me: int,
    timestamp: int,
    status: int,
    user_receipts: list[dict],
) -> bytes:
    """Build a WebMessageInfo with status and repeated UserReceipt records."""
    key_bytes = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, from_me)
        + _build_field(3, 2, key_id.encode("utf-8"))
    )
    msg_bytes = _build_field(1, 2, text.encode("utf-8"))

    ur_bytes_list = []
    for ur in user_receipts:
        b = _build_field(1, 2, ur["user_jid"].encode("utf-8"))
        if "receipt_timestamp" in ur and ur["receipt_timestamp"]:
            b += _build_field(2, 0, ur["receipt_timestamp"])
        if "read_timestamp" in ur and ur["read_timestamp"]:
            b += _build_field(3, 0, ur["read_timestamp"])
        if "played_timestamp" in ur and ur["played_timestamp"]:
            b += _build_field(4, 0, ur["played_timestamp"])
        ur_bytes_list.append(_build_field(40, 2, b))

    wmi_data = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, timestamp)
        + _build_field(4, 0, status)
        + b"".join(ur_bytes_list)
    )
    return wmi_data


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


def test_apply_messages_bin_populates_quoted_and_receipts(mock_db, tmp_path):
    """Verify apply_messages_bin inserts into message_quoted and receipt_user tables."""
    conn = sqlite3.connect(mock_db)
    # Ensure message_quoted and receipt_user tables exist in mock db
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_quoted (
            message_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            parent_message_chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            sender_jid_row_id INTEGER,
            key_id TEXT NOT NULL,
            timestamp INTEGER,
            message_type INTEGER,
            text_data TEXT
        );
        CREATE TABLE IF NOT EXISTS receipt_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            receipt_user_jid_row_id INTEGER NOT NULL,
            receipt_timestamp INTEGER,
            read_timestamp INTEGER,
            played_timestamp INTEGER
        );
    """)

    wmi_quoted = _build_quoted_wmi(
        "5550100@s.whatsapp.net",
        "REPLY_KEY_10",
        "Replying here",
        1,
        1729000700,
        "STANZA_99",
        "Quoted question text",
        "5550100@s.whatsapp.net",
    )
    wmi_receipted = _build_wmi_with_receipts(
        "5550100@s.whatsapp.net",
        "MSG_REC_11",
        "Read message",
        1,
        1729000800,
        status=4,
        user_receipts=[
            {
                "user_jid": "5550100@s.whatsapp.net",
                "receipt_timestamp": 1729000810,
                "read_timestamp": 1729000820,
            }
        ],
    )
    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_quoted, wmi_receipted])
    bin_file = tmp_path / "messages_quoted_rec.bin"
    bin_file.write_bytes(frame)

    inserted = increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()
    assert inserted == 2

    # Verify message_quoted
    quoted_rows = conn.execute(
        "SELECT message_row_id, chat_row_id, parent_message_chat_row_id, key_id, text_data, from_me FROM message_quoted"
    ).fetchall()
    assert len(quoted_rows) == 1
    assert quoted_rows[0][1] == 1
    assert quoted_rows[0][2] == 1
    assert quoted_rows[0][3] == "STANZA_99"
    assert quoted_rows[0][4] == "Quoted question text"
    assert quoted_rows[0][5] == 0

    # Verify receipt_user
    rec_rows = conn.execute(
        "SELECT message_row_id, receipt_timestamp, read_timestamp FROM receipt_user"
    ).fetchall()
    assert len(rec_rows) == 1
    assert rec_rows[0][1] == 1729000810000
    assert rec_rows[0][2] == 1729000820000

    conn.close()


def test_increment_merge_viewer_integration(tmp_path):
    """
    End-to-end integration test verifying that:
    1. Base database is merged with staged incremental backups.
    2. Invalid chat.display_message_row_id pointing to another chat is safely isolated.
    3. Viewer /api/chats displays the correct preview for each chat.
    4. Viewer /api/messages correctly returns merged messages and quoted text.
    5. Viewer /api/message_receipts accurately returns receipt info for increment messages.
    """
    from wab_viewer import main as viewer

    db_path = tmp_path / "msgstore.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jid (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            server TEXT NOT NULL,
            raw_string TEXT
        );
        CREATE TABLE chat (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            jid_row_id INTEGER NOT NULL,
            subject TEXT,
            hidden INTEGER DEFAULT 0,
            sort_timestamp INTEGER,
            display_message_row_id INTEGER
        );
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL DEFAULT 0,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            message_type INTEGER NOT NULL DEFAULT 0,
            text_data TEXT,
            starred INTEGER DEFAULT 0,
            status INTEGER DEFAULT 0
        );
        CREATE TABLE message_media (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            chat_row_id INTEGER NOT NULL,
            file_path TEXT,
            file_size INTEGER,
            media_name TEXT,
            message_url TEXT,
            mime_type TEXT,
            file_hash TEXT,
            transferred INTEGER DEFAULT 1
        );
        CREATE TABLE message_quoted (
            message_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            parent_message_chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            sender_jid_row_id INTEGER,
            key_id TEXT NOT NULL,
            timestamp INTEGER,
            message_type INTEGER,
            text_data TEXT
        );
        CREATE TABLE receipt_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            receipt_user_jid_row_id INTEGER NOT NULL,
            receipt_timestamp INTEGER,
            read_timestamp INTEGER,
            played_timestamp INTEGER
        );

        INSERT INTO jid (user, server, raw_string) VALUES ('5550001', 's.whatsapp.net', '5550001@s.whatsapp.net');
        INSERT INTO jid (user, server, raw_string) VALUES ('5550002', 's.whatsapp.net', '5550002@s.whatsapp.net');

        -- Chat 1: Chat with 5550001
        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, display_message_row_id)
        VALUES (1, 1, NULL, 1729000000000, 10);
        INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data, status)
        VALUES (10, 1, 0, 'MSG_CHAT1_OLD', 1, 1729000000000, 0, 'Old message in chat 1', 0);

        -- Chat 2: Chat with 5550002
        INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, display_message_row_id)
        VALUES (2, 2, NULL, 1729000050000, 20);
        INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data, status)
        VALUES (20, 2, 0, 'MSG_CHAT2_OLD', 2, 1729000050000, 0, 'Old message in chat 2', 0);
    """)
    conn.commit()
    conn.close()

    # Create staging increment
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    inc_work = tmp_path / "inc_work"
    inc_work.mkdir()

    # Mismatched display_message_row_id: Phone sets chat 1's display_message_row_id to 20 (which belongs to chat 2!)
    chat_mod = [{"_id": 1, "sort_timestamp": 1729000999000, "display_message_row_id": 20}]
    (inc_work / "chat_modified_1.json").write_text(json.dumps(chat_mod))
    (inc_work / "header.json").write_text(
        json.dumps({"format": "protobuf", "deleted_message_ids_files": []})
    )

    # Add new messages in increment:
    # 1. New message in chat 1 replying to old message with quoted text
    wmi_c1 = _build_quoted_wmi(
        "5550001@s.whatsapp.net",
        "INC_KEY_C1_NEW",
        "Fresh reply in chat 1",
        1,
        1729000999,
        "MSG_CHAT1_OLD",
        "Old message in chat 1",
        "5550001@s.whatsapp.net",
    )
    # 2. Sent message in chat 2 with status READ (4 -> 13) and user receipt
    wmi_c2 = _build_wmi_with_receipts(
        "5550002@s.whatsapp.net",
        "INC_KEY_C2_NEW",
        "Fresh update in chat 2",
        1,
        1729000888,
        status=4,
        user_receipts=[
            {
                "user_jid": "5550002@s.whatsapp.net",
                "receipt_timestamp": 1729000890,
                "read_timestamp": 1729000895,
            }
        ],
    )
    frame_c1 = _build_history_sync_frame("5550001@s.whatsapp.net", [wmi_c1])
    frame_c2 = _build_history_sync_frame("5550002@s.whatsapp.net", [wmi_c2])
    (inc_work / "messages.bin").write_bytes(frame_c1 + frame_c2)

    inc_zip = staging_dir / "msgstore-increment-1-2026-10-18.db.crypt15"
    with zipfile.ZipFile(str(inc_zip), "w") as z:
        for f in inc_work.iterdir():
            z.write(str(f), arcname=f.name)

    # Execute merge_increments
    out_dir = tmp_path / "merged_output"
    out_dir.mkdir()
    merged_db = str(out_dir / "msgstore_merged.db")
    total_ins = increment_merger.merge_increments(
        str(db_path),
        [str(inc_zip)],
        None,
        merged_db,
        str(staging_dir / "temp_unpack"),
        logger,
    )
    assert total_ins == 2
    assert os.path.exists(merged_db)

    # Initialize viewer with merged database in output directory
    archive_root = tmp_path / "archive_output"
    archive_root.mkdir()
    # Move or copy merged msgstore.db as msgstore.db in archive_root
    shutil.copy2(merged_db, str(archive_root / "msgstore.db"))

    archive_db_path = archive_root / ".wa_media_archiver.db"
    arch_conn = sqlite3.connect(str(archive_db_path))
    arch_conn.executescript("""
        CREATE TABLE contacts (
            number TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE groups (
            chat_row_id TEXT PRIMARY KEY,
            folder TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO contacts (number, folder, display_name) VALUES ('5550001', 'Bob (5550001)', 'Bob');
        INSERT INTO contacts (number, folder, display_name) VALUES ('5550002', 'Charlie (5550002)', 'Charlie');
    """)
    arch_conn.commit()
    arch_conn.close()

    app = viewer.create_app(archive_root, rescan=False)
    app.config["TESTING"] = True
    client = app.test_client()

    # 1. Test /api/chats - Preview isolation
    resp = client.get("/api/chats")
    assert resp.status_code == 200
    chats = resp.get_json()
    assert len(chats) == 2

    chat_map = {c["id"]: c for c in chats}
    assert "5550001" in chat_map
    assert "5550002" in chat_map

    # Chat 1 preview MUST NOT be from Chat 2 even though display_message_row_id had mismatched id 20!
    assert "Fresh reply in chat 1" in chat_map["5550001"]["last_msg_preview"]
    assert "Fresh update in chat 2" in chat_map["5550002"]["last_msg_preview"]

    # 2. Test /api/messages - Merged messages and quoted text
    resp_msgs_c1 = client.get("/api/messages?chat_id=5550001&chat_type=contact&limit=10")
    assert resp_msgs_c1.status_code == 200
    msgs_c1 = resp_msgs_c1.get_json()
    assert len(msgs_c1) == 2
    new_c1_msg = [m for m in msgs_c1 if m["text_body"] == "Fresh reply in chat 1"][0]
    assert new_c1_msg["quoted_text"] == "Old message in chat 1"

    # 3. Test /api/message_receipts - Receipt information
    resp_msgs_c2 = client.get("/api/messages?chat_id=5550002&chat_type=contact&limit=10")
    assert resp_msgs_c2.status_code == 200
    msgs_c2 = resp_msgs_c2.get_json()
    new_c2_msg = [m for m in msgs_c2 if m["text_body"] == "Fresh update in chat 2"][0]

    resp_receipt = client.get(f"/api/message_receipts/{new_c2_msg['msg_id']}")
    assert resp_receipt.status_code == 200
    receipt_data = resp_receipt.get_json()
    assert receipt_data["read_known"] is True
    assert receipt_data["read_ts"] is not None


def test_filter_active_increments_multi_generation():
    paths = [
        "msgstore-increment-1-2026-10-09.1.db.crypt15",
        "msgstore-increment-2-2026-10-09.1.db.crypt15",
        "msgstore-increment-1-2026-10-16.1.db.crypt15",
        "msgstore-increment-2-2026-10-16.1.db.crypt15",
        "msgstore-increment-3-2026-10-16.1.db.crypt15",
        "msgstore-increment-1.db.crypt15",
    ]
    # Undated active generation supersedes historical rotated dates
    filtered = increment_merger.filter_active_increments(paths, None)
    assert filtered == [
        "msgstore-increment-1.db.crypt15",
    ]

    # Explicit base date tag matches its own historical generation
    filtered_rotated = increment_merger.filter_active_increments(
        paths, "msgstore-2026-10-16.1.db.crypt15"
    )
    assert filtered_rotated == [
        "msgstore-increment-1-2026-10-16.1.db.crypt15",
        "msgstore-increment-2-2026-10-16.1.db.crypt15",
        "msgstore-increment-3-2026-10-16.1.db.crypt15",
    ]

    # Multiple dated generations without untagged files selects latest dated tag
    dated_only = [p for p in paths if p != "msgstore-increment-1.db.crypt15"]
    filtered_dated = increment_merger.filter_active_increments(dated_only, None)
    assert filtered_dated == [
        "msgstore-increment-1-2026-10-16.1.db.crypt15",
        "msgstore-increment-2-2026-10-16.1.db.crypt15",
        "msgstore-increment-3-2026-10-16.1.db.crypt15",
    ]


def test_filter_active_increments_with_base_tag():
    paths = [
        "/remote/msgstore-increment-1-2026-10-09.1.db.crypt15",
        "/remote/msgstore-increment-2-2026-10-09.1.db.crypt15",
        "/remote/msgstore-increment-1-2026-10-16.1.db.crypt15",
    ]
    filtered = increment_merger.filter_active_increments(paths, "msgstore-2026-10-09.1.db.crypt15")
    assert filtered == [
        "/remote/msgstore-increment-1-2026-10-09.1.db.crypt15",
        "/remote/msgstore-increment-2-2026-10-09.1.db.crypt15",
    ]


def test_apply_entity_modifications_props_unique_collision(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO props (_id, key, value) VALUES (1, 'k1', 'v1')")
    cur.execute("INSERT INTO props (_id, key, value) VALUES (2, 'k2', 'v2')")
    conn.commit()

    # Modify k2 where the increment carries _id=1 (matching k1's _id on the base DB)
    mod_json = tmp_path / "props_modified_collision.json"
    mod_json.write_text(json.dumps([{"_id": 1, "key": "k2", "value": "updated_v2"}]))

    count = increment_merger.apply_entity_modifications(conn, "props", str(mod_json), logger)
    conn.commit()
    assert count == 1

    # Verify k2 was updated and k1 was preserved without UNIQUE constraint failure
    rows = cur.execute("SELECT _id, key, value FROM props ORDER BY _id").fetchall()
    assert rows == [(1, "k1", "v1"), (2, "k2", "updated_v2")]

    # Insert a new key that carries an already-used _id
    mod_json_new = tmp_path / "props_modified_new.json"
    mod_json_new.write_text(json.dumps([{"_id": 1, "key": "k3", "value": "new_v3"}]))
    count = increment_merger.apply_entity_modifications(conn, "props", str(mod_json_new), logger)
    conn.commit()
    assert count == 1

    val = cur.execute("SELECT value FROM props WHERE key = 'k3'").fetchone()[0]
    assert val == "new_v3"
    conn.close()


def test_apply_entity_deletions_props_by_key_string(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute("INSERT INTO props (key, value) VALUES ('str_prop', 'temp')")
    conn.commit()

    del_json = tmp_path / "props_deleted_str.json"
    del_json.write_text(json.dumps({"deleted_ids": ["str_prop"]}))

    count = increment_merger.apply_entity_deletions(conn, "props", str(del_json), logger)
    conn.commit()
    assert count == 1
    assert conn.execute("SELECT 1 FROM props WHERE key = 'str_prop'").fetchone() is None
    conn.close()


def test_apply_entity_modifications_chat_unique_jid(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (10, 100, 'Subject 10')")
    cur.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (20, 200, 'Subject 20')")
    conn.commit()

    # Modify chat for jid_row_id=200 where incoming JSON carries _id=10
    mod_json = tmp_path / "chat_modified_collision.json"
    mod_json.write_text(
        json.dumps([{"_id": 10, "jid_row_id": 200, "subject": "Subject 20 Updated"}])
    )

    count = increment_merger.apply_entity_modifications(conn, "chat", str(mod_json), logger)
    conn.commit()
    assert count == 1

    chat_20 = cur.execute("SELECT _id, subject FROM chat WHERE jid_row_id = 200").fetchone()
    assert chat_20 == (20, "Subject 20 Updated")
    chat_10 = cur.execute("SELECT _id, subject FROM chat WHERE jid_row_id = 100").fetchone()
    assert chat_10 == (10, "Subject 10")

    # Now insert a new chat with jid_row_id=300 where incoming JSON carries conflicting _id=10
    mod_json_new = tmp_path / "chat_modified_new.json"
    mod_json_new.write_text(
        json.dumps([{"_id": 10, "jid_row_id": 300, "subject": "Subject 30 New"}])
    )
    count_new = increment_merger.apply_entity_modifications(conn, "chat", str(mod_json_new), logger)
    conn.commit()
    assert count_new == 1

    chat_30 = cur.execute("SELECT subject FROM chat WHERE jid_row_id = 300").fetchone()
    assert chat_30 == ("Subject 30 New",)
    conn.close()


def test_adb_enumerate_multi_generation(monkeypatch):
    from wab_archiver import adb_extractor

    remote_ls_output = """
msgstore-increment-1-2026-10-09.1.db.crypt15
msgstore-increment-2-2026-10-09.1.db.crypt15
msgstore-increment-1-2026-10-16.1.db.crypt15
msgstore-increment-2-2026-10-16.1.db.crypt15
msgstore-increment-3-2026-10-16.1.db.crypt15
msgstore-increment-1.db.crypt15
msgstore.db.crypt15
msgstore-2026-10-09.1.db.crypt15
msgstore-2026-10-16.1.db.crypt15
"""

    class MockCompletedProcess:
        def __init__(self, stdout):
            self.stdout = stdout.encode("utf-8")
            self.returncode = 0

    def mock_run_adb(cmd, log):
        return MockCompletedProcess(remote_ls_output)

    monkeypatch.setattr(adb_extractor, "_run_adb", mock_run_adb)

    base_remote, inc_remotes = adb_extractor.enumerate_remote_backup_files(False, logger)
    assert base_remote is not None
    assert base_remote.endswith("msgstore.db.crypt15")
    # Undated msgstore.db.crypt15 matches active untagged increment
    assert len(inc_remotes) == 1
    assert [os.path.basename(p) for p in inc_remotes] == [
        "msgstore-increment-1.db.crypt15",
    ]


def test_adb_enumerate_rotated_base_fallback(monkeypatch):
    from wab_archiver import adb_extractor

    remote_ls_output = """
msgstore-increment-1-2026-10-09.1.db.crypt15
msgstore-increment-2-2026-10-09.1.db.crypt15
msgstore-increment-1-2026-10-16.1.db.crypt15
msgstore-2026-10-09.1.db.crypt15
"""

    class MockCompletedProcess:
        def __init__(self, stdout):
            self.stdout = stdout.encode("utf-8")
            self.returncode = 0

    def mock_run_adb(cmd, log):
        return MockCompletedProcess(remote_ls_output)

    monkeypatch.setattr(adb_extractor, "_run_adb", mock_run_adb)

    base_remote, inc_remotes = adb_extractor.enumerate_remote_backup_files(False, logger)
    assert base_remote is not None
    assert base_remote.endswith("msgstore-2026-10-09.1.db.crypt15")
    assert len(inc_remotes) == 2
    assert [os.path.basename(p) for p in inc_remotes] == [
        "msgstore-increment-1-2026-10-09.1.db.crypt15",
        "msgstore-increment-2-2026-10-09.1.db.crypt15",
    ]


def test_resolve_or_create_chat_populates_hidden_zero(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.commit()

    chat_id = increment_merger.resolve_or_create_chat(conn, 999, logger, None)
    conn.commit()
    row = conn.execute("SELECT _id, hidden FROM chat WHERE _id = ?", (chat_id,)).fetchone()
    assert row[1] == 0
    conn.close()


def test_apply_messages_bin_unhides_chat(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.commit()

    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('5559999', 's.whatsapp.net', '5559999@s.whatsapp.net')"
    )
    jid_id = cur.lastrowid
    cur.execute("INSERT INTO chat (jid_row_id, hidden) VALUES (?, 1)", (jid_id,))
    chat_id = cur.lastrowid
    conn.commit()

    wmi = _build_text_wmi("5559999@s.whatsapp.net", "UNHIDE_MSG_1", "Hello there", 0, 1729001000)
    frame = _build_history_sync_frame("5559999@s.whatsapp.net", [wmi])
    bin_file = tmp_path / "unhide_msg.bin"
    bin_file.write_bytes(frame)

    inserted = increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()
    assert inserted == 1

    hidden_val = conn.execute("SELECT hidden FROM chat WHERE _id = ?", (chat_id,)).fetchone()[0]
    assert hidden_val == 0
    conn.close()


def test_resolve_or_create_jid_strips_device_suffix_and_reuses_primary(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    jid_cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in jid_cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")
        conn.commit()
    cur.execute(
        "INSERT INTO jid (user, server, raw_string, device) VALUES ('5551234', 's.whatsapp.net', '5551234@s.whatsapp.net', 0)"
    )
    primary_id = cur.lastrowid
    conn.commit()

    # Resolving wire JID with device suffix :0 should reuse canonical primary row
    res_id = increment_merger.resolve_or_create_jid(conn, "5551234:0@s.whatsapp.net", logger, None)
    assert res_id == primary_id

    # Resolving new contact with device suffix :2 should insert canonical row with device = 0
    res_new_id = increment_merger.resolve_or_create_jid(
        conn, "5559876:2@s.whatsapp.net", logger, None
    )
    row = conn.execute(
        "SELECT user, server, device, raw_string FROM jid WHERE _id = ?", (res_new_id,)
    ).fetchone()
    assert row[0] == "5559876"
    assert row[1] == "s.whatsapp.net"
    assert row[2] == 0
    assert row[3] == "5559876@s.whatsapp.net"
    conn.close()


def test_apply_entity_modifications_jid_strips_device_suffix_and_deduplicates(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    jid_cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in jid_cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")
        conn.commit()
    cur.execute(
        "INSERT INTO jid (user, server, raw_string, device) VALUES ('5551234', 's.whatsapp.net', '5551234@s.whatsapp.net', 0)"
    )
    canonical_id = cur.lastrowid
    conn.commit()

    delta_file = tmp_path / "jid_modified_1.json"
    delta_file.write_text(
        json.dumps([{"_id": 999, "user": "5551234:0", "server": "s.whatsapp.net", "device": 0}]),
        encoding="utf-8",
    )

    increment_merger.apply_entity_modifications(conn, "jid", str(delta_file), logger)
    conn.commit()

    # Row 999 should not have been inserted; canonical row should be preserved
    rows = conn.execute("SELECT _id, user FROM jid WHERE user = '5551234'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == canonical_id
    assert conn.execute("SELECT 1 FROM jid WHERE _id = 999").fetchone() is None
    conn.close()


def test_resolve_or_create_chat_reuses_active_phone_chat_from_jid_map(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.commit()
    jid_cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in jid_cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")
        conn.commit()

    cur.execute(
        "INSERT INTO jid (user, server, raw_string, device) VALUES ('5551111', 's.whatsapp.net', '5551111@s.whatsapp.net', 0)"
    )
    phone_jid_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, raw_string, device) VALUES ('100200300', 'lid', '100200300@lid', 0)"
    )
    lid_jid_id = cur.lastrowid
    conn.execute("CREATE TABLE IF NOT EXISTS jid_map (lid_row_id INTEGER, jid_row_id INTEGER)")
    conn.commit()
    cur.execute(
        "INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (?, ?)", (lid_jid_id, phone_jid_id)
    )
    cur.execute("INSERT INTO chat (jid_row_id, hidden) VALUES (?, 0)", (phone_jid_id,))
    existing_chat_id = cur.lastrowid
    conn.commit()

    # Resolving chat for LID should reuse the active phone chat from jid_map
    resolved_chat_id = increment_merger.resolve_or_create_chat(conn, lid_jid_id, logger, None)
    assert resolved_chat_id == existing_chat_id
    conn.close()


def test_resolve_or_create_chat_reuses_hidden_phone_chat_and_unhides(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.commit()

    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('5552222', 's.whatsapp.net', '5552222@s.whatsapp.net')"
    )
    phone_jid_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('100200400', 'lid', '100200400@lid')"
    )
    lid_jid_id = cur.lastrowid
    conn.execute("CREATE TABLE IF NOT EXISTS jid_map (lid_row_id INTEGER, jid_row_id INTEGER)")
    conn.commit()
    cur.execute(
        "INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (?, ?)", (lid_jid_id, phone_jid_id)
    )
    # Legacy phone chat marked hidden = 1 by WhatsApp
    cur.execute("INSERT INTO chat (jid_row_id, hidden) VALUES (?, 1)", (phone_jid_id,))
    existing_chat_id = cur.lastrowid
    conn.commit()

    resolved_chat_id = increment_merger.resolve_or_create_chat(conn, lid_jid_id, logger, None)
    assert resolved_chat_id == existing_chat_id
    # Chat must have been unhidden
    hidden_val = conn.execute(
        "SELECT hidden FROM chat WHERE _id = ?", (existing_chat_id,)
    ).fetchone()[0]
    assert hidden_val == 0
    conn.close()


def test_resolve_or_create_chat_reuses_via_account_jid_row_id(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "account_jid_row_id" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN account_jid_row_id INTEGER")
        conn.commit()

    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('5553333', 's.whatsapp.net', '5553333@s.whatsapp.net')"
    )
    phone_jid_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('100200500', 'lid', '100200500@lid')"
    )
    lid_jid_id = cur.lastrowid

    # Existing chat has phone_jid_id as jid_row_id and lid_jid_id as account_jid_row_id
    cur.execute(
        "INSERT INTO chat (jid_row_id, account_jid_row_id) VALUES (?, ?)",
        (phone_jid_id, lid_jid_id),
    )
    existing_chat_id = cur.lastrowid
    conn.commit()

    # Empty jid_map: resolution must succeed via account_jid_row_id
    resolved_chat_id = increment_merger.resolve_or_create_chat(conn, lid_jid_id, logger, None)
    assert resolved_chat_id == existing_chat_id
    conn.close()


def test_resolve_or_create_chat_reuses_via_lid_display_name(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT, username TEXT)"
    )
    conn.commit()

    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('393401234567', 's.whatsapp.net', '393401234567@s.whatsapp.net')"
    )
    phone_jid_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('100200600', 'lid', '100200600@lid')"
    )
    lid_jid_id = cur.lastrowid

    cur.execute(
        "INSERT INTO lid_display_name (lid_row_id, display_name) VALUES (?, '+39 340 123 4567')",
        (lid_jid_id,),
    )
    cur.execute("INSERT INTO chat (jid_row_id) VALUES (?)", (phone_jid_id,))
    existing_chat_id = cur.lastrowid
    conn.commit()

    # No jid_map entry: resolution must match phone JID via lid_display_name phone string
    resolved_chat_id = increment_merger.resolve_or_create_chat(conn, lid_jid_id, logger, None)
    assert resolved_chat_id == existing_chat_id
    conn.close()


def test_apply_messages_bin_preserves_existing_media_file_path(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (1, 0, 'EXISTING_IMG_1', 1, 1729000100, 'Old text', 1)"
    )
    old_msg_id = cur.lastrowid
    cur.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path, file_size, mime_type) "
        "VALUES (?, 1, 'WhatsApp/Media/WhatsApp Images/IMG_001.jpg', 12345, 'image/jpeg')",
        (old_msg_id,),
    )
    conn.commit()
    conn.close()

    # Replay messages.bin with updated WMI record for EXISTING_IMG_1
    wmi_img = _build_image_wmi(
        "5550100@s.whatsapp.net",
        "EXISTING_IMG_1",
        "Updated caption",
        "image/jpeg",
        b"\xbb" * 32,
        12345,
        0,
        1729000200,
    )
    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_img])
    bin_path = tmp_path / "messages.bin"
    bin_path.write_bytes(frame)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, str(bin_path), logger, None, None, None)
    conn.commit()
    assert inserted == 1

    cur = conn.cursor()
    msg_row = cur.execute(
        "SELECT m._id, m.key_id, m.text_data, mm.file_path, mm.file_size "
        "FROM message m JOIN message_media mm ON mm.message_row_id = m._id "
        "WHERE m.key_id = 'EXISTING_IMG_1'"
    ).fetchone()
    assert msg_row is not None
    assert msg_row[1] == "EXISTING_IMG_1"
    assert msg_row[2] == "Updated caption"
    # Local file_path must be preserved from the deleted old row
    assert msg_row[3] == "WhatsApp/Media/WhatsApp Images/IMG_001.jpg"
    conn.close()


def test_is_valid_jid_string():
    # Valid JIDs
    assert increment_merger.is_valid_jid_string("5550100@s.whatsapp.net")
    assert increment_merger.is_valid_jid_string("120363028374928192@g.us")
    assert increment_merger.is_valid_jid_string("108394829384729@lid")
    assert increment_merger.is_valid_jid_string("status@broadcast")
    assert increment_merger.is_valid_jid_string("0@s.whatsapp.net")
    assert increment_merger.is_valid_jid_string("12345:1@s.whatsapp.net")

    # Invalid JIDs
    assert not increment_merger.is_valid_jid_string(None)
    assert not increment_merger.is_valid_jid_string("")
    assert not increment_merger.is_valid_jid_string("not_a_jid")
    assert not increment_merger.is_valid_jid_string("@s.whatsapp.net")
    assert not increment_merger.is_valid_jid_string("5550100@")
    assert not increment_merger.is_valid_jid_string("5550100@evil.com")
    assert not increment_merger.is_valid_jid_string("555\x000100@s.whatsapp.net")
    assert not increment_merger.is_valid_jid_string("\xff\xd8\xff\xe0@s.whatsapp.net")
    assert not increment_merger.is_valid_jid_string("JFIF\x00\x01@s.whatsapp.net")


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


def test_apply_increment_folder_dag_order(mock_db, tmp_path):
    # Test that apply_increment_folder processes jid before chat and resolves jid_id_map
    inc_dir = tmp_path / "increment_1"
    inc_dir.mkdir()

    # jid_modified_1 introduces JID _id=50 (phone 5550200)
    jid_data = [
        {
            "_id": 50,
            "user": "5550200",
            "server": "s.whatsapp.net",
            "raw_string": "5550200@s.whatsapp.net",
        }
    ]
    (inc_dir / "jid_modified_1.json").write_text(json.dumps(jid_data), encoding="utf-8")

    # chat_modified_1 introduces chat with foreign jid_row_id=50
    chat_data = [{"_id": 200, "jid_row_id": 50, "subject": "Test DAG Chat"}]
    (inc_dir / "chat_modified_1.json").write_text(json.dumps(chat_data), encoding="utf-8")

    conn = sqlite3.connect(mock_db)
    inserted_msgs, receipts, reactions, pins, mod_counts = increment_merger.apply_increment_folder(
        conn, str(inc_dir), logger, None, None, None
    )
    conn.commit()

    cur = conn.cursor()
    # Check that jid was inserted
    jid_row = cur.execute("SELECT _id, user FROM jid WHERE user = '5550200'").fetchone()
    assert jid_row is not None
    local_jid_id = jid_row[0]

    # Check that chat references the local jid _id, NOT foreign 50
    chat_row = cur.execute(
        "SELECT _id, jid_row_id, subject FROM chat WHERE subject = 'Test DAG Chat'"
    ).fetchone()
    assert chat_row is not None
    assert chat_row[1] == local_jid_id
    conn.close()


def test_apply_entity_modifications_remaps_jid_row_id(mock_db, tmp_path):
    # chat_modified with foreign jid_row_id=999
    mod_json = tmp_path / "chat_modified_1.json"
    mod_json.write_text(
        json.dumps([{"_id": 300, "jid_row_id": 999, "subject": "Remapped Chat"}]),
        encoding="utf-8",
    )

    jid_id_map = {999: 1}  # Map foreign 999 to existing mock_db JID 1
    conn = sqlite3.connect(mock_db)
    count = increment_merger.apply_entity_modifications(
        conn, "chat", str(mod_json), logger, jid_id_map=jid_id_map
    )
    conn.commit()
    assert count == 1

    cur = conn.cursor()
    chat_row = cur.execute(
        "SELECT jid_row_id, subject FROM chat WHERE subject = 'Remapped Chat'"
    ).fetchone()
    assert chat_row is not None
    assert chat_row[0] == 1  # Successfully remapped
    conn.close()


# ---------------------------------------------------------------------------
# Two-Pass Add-On Staging Engine Tests
# ---------------------------------------------------------------------------


def _make_two_pass_test_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, server TEXT NOT NULL, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY AUTOINCREMENT, jid_row_id INTEGER NOT NULL UNIQUE, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER);
        CREATE TABLE message (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            message_type INTEGER NOT NULL,
            text_data TEXT,
            starred INTEGER DEFAULT 0,
            status INTEGER DEFAULT 0
        );
        CREATE TABLE message_add_on (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            parent_message_row_id INTEGER NOT NULL,
            status INTEGER DEFAULT 0,
            message_add_on_type INTEGER NOT NULL,
            timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_reaction (
            message_add_on_row_id INTEGER PRIMARY KEY,
            reaction TEXT NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_pin_in_chat (
            message_add_on_row_id INTEGER PRIMARY KEY,
            pin_in_chat_state INTEGER NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_edit_info (
            message_row_id INTEGER PRIMARY KEY,
            original_key_id TEXT NOT NULL,
            edited_timestamp INTEGER NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_poll_vote (
            message_add_on_row_id INTEGER PRIMARY KEY,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE message_add_on_poll_vote_selected_option (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_add_on_row_id INTEGER NOT NULL,
            message_poll_option_id INTEGER NOT NULL
        );
        CREATE TABLE message_poll_option (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            option_name TEXT NOT NULL,
            option_sha256 TEXT,
            vote_total INTEGER DEFAULT 0
        );
        CREATE TABLE message_secret (
            message_row_id INTEGER PRIMARY KEY,
            message_secret BLOB
        );
        INSERT INTO jid (user, server, raw_string) VALUES ('5550100', 's.whatsapp.net', '5550100@s.whatsapp.net');
        INSERT INTO chat (jid_row_id) VALUES (1);
    """)
    conn.commit()
    return conn


def _build_reaction_wmi(
    remote_jid: str, react_key_id: str, target_key_id: str, emoji: str, timestamp: int
) -> bytes:
    target_key = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, 0)
        + _build_field(3, 2, target_key_id.encode("utf-8"))
    )
    reaction_bytes = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, emoji.encode("utf-8"))
        + _build_field(4, 0, timestamp * 1000)
    )
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(3, 2, react_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(46, 2, reaction_bytes))
        + _build_field(3, 0, timestamp)
    )


def _build_pin_wmi(
    remote_jid: str, pin_key_id: str, target_key_id: str, pin_state: int, timestamp: int
) -> bytes:
    target_key = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, 0)
        + _build_field(3, 2, target_key_id.encode("utf-8"))
    )
    pin_bytes = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, pin_state)
        + _build_field(3, 0, timestamp * 1000)
    )
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(3, 2, pin_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(63, 2, pin_bytes))
        + _build_field(3, 0, timestamp)
    )


def _build_edit_wmi(
    remote_jid: str, edit_key_id: str, target_key_id: str, new_text: str, timestamp: int
) -> bytes:
    target_key = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, 0)
        + _build_field(3, 2, target_key_id.encode("utf-8"))
    )
    edit_proto = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, 14)
        + _build_field(10, 0, timestamp * 1000)
        + _build_field(14, 2, _build_field(1, 2, new_text.encode("utf-8")))
    )
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(3, 2, edit_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(12, 2, edit_proto))
        + _build_field(3, 0, timestamp)
    )


def _build_poll_creation_wmi(
    remote_jid: str, poll_key_id: str, question: str, options: list[str], timestamp: int
) -> bytes:
    opts_bytes = b"".join(
        _build_field(3, 2, _build_field(1, 2, opt.encode("utf-8"))) for opt in options
    )
    poll_bytes = _build_field(2, 2, question.encode("utf-8")) + opts_bytes + _build_field(4, 0, 1)
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(2, 0, 1)
            + _build_field(3, 2, poll_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(49, 2, poll_bytes))
        + _build_field(3, 0, timestamp)
    )


def _build_poll_vote_wmi(
    remote_jid: str,
    vote_key_id: str,
    poll_key_id: str,
    timestamp: int,
    sender_ts_ms: int | None = None,
) -> bytes:
    target_key = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, 1)
        + _build_field(3, 2, poll_key_id.encode("utf-8"))
    )
    vote_bytes = _build_field(1, 2, target_key) + _build_field(
        3, 0, sender_ts_ms if sender_ts_ms is not None else timestamp * 1000
    )
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(2, 0, 0)
            + _build_field(3, 2, vote_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(50, 2, vote_bytes))
        + _build_field(3, 0, timestamp)
    )


def test_two_pass_reaction_arrives_before_parent_message(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_react.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Reaction targeting KEY_PARENT_1
    wmi_react = _build_reaction_wmi(
        "5550100@s.whatsapp.net", "REACT_OUT_OF_ORDER", "KEY_PARENT_1", "🔥", 1729000200
    )
    # Frame 1: Parent message KEY_PARENT_1
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_PARENT_1", "Hello parent message", 0, 1729000100
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_react)) + wmi_react)
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 2

    # Verify that reaction was resolved in Pass 2 and linked to KEY_PARENT_1
    cur = conn.cursor()
    parent_id = cur.execute("SELECT _id FROM message WHERE key_id = 'KEY_PARENT_1'").fetchone()[0]
    react_row = cur.execute(
        "SELECT a.parent_message_row_id, r.reaction, a.message_add_on_type "
        "FROM message_add_on a JOIN message_add_on_reaction r ON a._id = r.message_add_on_row_id "
        "WHERE a.parent_message_row_id = ?",
        (parent_id,),
    ).fetchone()
    assert react_row is not None
    assert react_row[0] == parent_id
    assert react_row[1] == "🔥"
    assert react_row[2] == 1
    conn.close()


def test_two_pass_edit_arrives_before_parent_message(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_edit.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Edit targeting KEY_PARENT_2 with updated text
    wmi_edit = _build_edit_wmi(
        "5550100@s.whatsapp.net",
        "EDIT_OUT_OF_ORDER",
        "KEY_PARENT_2",
        "Edited text from future",
        1729000300,
    )
    # Frame 1: Parent message KEY_PARENT_2 with original text
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_PARENT_2", "Original initial text", 0, 1729000100
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_edit)) + wmi_edit)
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 2

    cur = conn.cursor()
    # Parent message text must be updated to the edited text
    msg_row = cur.execute(
        "SELECT _id, text_data FROM message WHERE key_id = 'KEY_PARENT_2'"
    ).fetchone()
    assert msg_row is not None
    parent_id, text = msg_row
    assert text == "Edited text from future"

    # message_edit_info must be populated
    edit_row = cur.execute(
        "SELECT message_row_id, original_key_id FROM message_edit_info WHERE message_row_id = ?",
        (parent_id,),
    ).fetchone()
    assert edit_row is not None
    assert edit_row[0] == parent_id
    assert edit_row[1] == "KEY_PARENT_2"

    # message_add_on must have an entry of type 74
    addon_row = cur.execute(
        "SELECT message_add_on_type FROM message_add_on WHERE parent_message_row_id = ?",
        (parent_id,),
    ).fetchone()
    assert addon_row is not None
    assert addon_row[0] == 74
    conn.close()


def test_two_pass_pin_arrives_before_parent_message(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_pin.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Pin targeting KEY_PARENT_3
    wmi_pin = _build_pin_wmi(
        "5550100@s.whatsapp.net", "PIN_OUT_OF_ORDER", "KEY_PARENT_3", 1, 1729000200
    )
    # Frame 1: Parent message KEY_PARENT_3
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_PARENT_3", "Message to pin", 0, 1729000100
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_pin)) + wmi_pin)
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 2

    cur = conn.cursor()
    parent_id = cur.execute("SELECT _id FROM message WHERE key_id = 'KEY_PARENT_3'").fetchone()[0]
    pin_row = cur.execute(
        "SELECT a.parent_message_row_id, p.pin_in_chat_state, a.message_add_on_type "
        "FROM message_add_on a JOIN message_add_on_pin_in_chat p ON a._id = p.message_add_on_row_id "
        "WHERE a.parent_message_row_id = ?",
        (parent_id,),
    ).fetchone()
    assert pin_row is not None
    assert pin_row[0] == parent_id
    assert pin_row[1] == 1
    assert pin_row[2] == 79
    conn.close()


def test_two_pass_multiple_chronological_reactions_out_of_order(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_chrono.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Newer reaction (timestamp 1729000300, emoji '❤️')
    wmi_react_newer = _build_reaction_wmi(
        "5550100@s.whatsapp.net", "REACT_NEW", "KEY_PARENT_4", "❤️", 1729000300
    )
    # Frame 1: Older reaction (timestamp 1729000200, emoji '👍')
    wmi_react_older = _build_reaction_wmi(
        "5550100@s.whatsapp.net", "REACT_OLD", "KEY_PARENT_4", "👍", 1729000200
    )
    # Frame 2: Parent message (timestamp 1729000100)
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_PARENT_4", "React message", 0, 1729000100
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_react_newer)) + wmi_react_newer)
        f.write(_encode_varint(len(wmi_react_older)) + wmi_react_older)
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 3

    cur = conn.cursor()
    parent_id = cur.execute("SELECT _id FROM message WHERE key_id = 'KEY_PARENT_4'").fetchone()[0]
    # Because Pass 2 sorts by timestamp, REACT_OLD ('👍') was applied first, then REACT_NEW ('❤️') replaced it
    react_rows = cur.execute(
        "SELECT r.reaction FROM message_add_on a "
        "JOIN message_add_on_reaction r ON a._id = r.message_add_on_row_id "
        "WHERE a.parent_message_row_id = ?",
        (parent_id,),
    ).fetchall()
    assert len(react_rows) == 1
    assert react_rows[0][0] == "❤️"
    conn.close()


def test_two_pass_true_orphan_add_on_fails_gracefully(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_orphan.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Reaction targeting a message that never exists anywhere
    wmi_orphan_react = _build_reaction_wmi(
        "5550100@s.whatsapp.net", "REACT_ORPHAN", "NEVER_EXISTING_KEY", "🚀", 1729000200
    )
    # Frame 1: Valid text message
    wmi_valid = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_VALID_5", "Normal message", 0, 1729000100
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_orphan_react)) + wmi_orphan_react)
        f.write(_encode_varint(len(wmi_valid)) + wmi_valid)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    # Replay must succeed without crashing
    assert inserted == 2

    cur = conn.cursor()
    # No message_add_on row should exist since parent was never found
    addon_count = cur.execute("SELECT COUNT(*) FROM message_add_on").fetchone()[0]
    assert addon_count == 0

    # Normal message must exist
    valid_row = cur.execute("SELECT _id FROM message WHERE key_id = 'KEY_VALID_5'").fetchone()
    assert valid_row is not None
    conn.close()


def test_apply_message_deletions_gate1_time_paradox(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Insert message 200 with future timestamp 1729005000000
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (200, 1, 0, 'KEY_FUTURE', 1729005000000, 0, 'Future message')"
    )
    conn.commit()

    del_json = tmp_path / "deletedMessages_paradox.json"
    del_json.write_text(json.dumps({"deleted_ids": [200]}))

    # Backup timestamp is 1729000000000 (5000s earlier, beyond 3600s tolerance)
    count = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, None, 1729000000000, 500
    )
    conn.commit()

    assert count == 0
    remaining = conn.execute("SELECT COUNT(*) FROM message WHERE _id = 200").fetchone()[0]
    assert remaining == 1
    conn.close()


def test_apply_message_deletions_gate2_sequence_overflow(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (300, 1, 0, 'KEY_OVERFLOW', 1729000000000, 0, 'Overflow message')"
    )
    conn.commit()

    del_json = tmp_path / "deletedMessages_overflow.json"
    del_json.write_text(json.dumps({"deleted_ids": [300]}))

    # Max sequence is 250 (300 exceeds 250)
    count = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, None, 1729000100000, 250
    )
    conn.commit()

    assert count == 0
    remaining = conn.execute("SELECT COUNT(*) FROM message WHERE _id = 300").fetchone()[0]
    assert remaining == 1
    conn.close()


def test_apply_message_deletions_gate3_invalid_stanza_key(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (400, 1, 0, '', 1729000000000, 0, 'Blank key message')"
    )
    conn.commit()

    del_json = tmp_path / "deletedMessages_invalid_key.json"
    del_json.write_text(json.dumps({"deleted_ids": [400]}))

    count = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, None, 1729000100000, 500
    )
    conn.commit()

    assert count == 0
    remaining = conn.execute("SELECT COUNT(*) FROM message WHERE _id = 400").fetchone()[0]
    assert remaining == 1
    conn.close()


def test_apply_message_deletions_cascades_strictly_for_verified_only(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS message_add_on (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER,
            parent_message_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            status INTEGER DEFAULT 0,
            message_add_on_type INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_add_on_reaction (
            message_add_on_row_id INTEGER PRIMARY KEY,
            reaction TEXT NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_add_on_pin_in_chat (
            message_add_on_row_id INTEGER PRIMARY KEY,
            pin_in_chat_state INTEGER NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_poll (
            message_row_id INTEGER PRIMARY KEY,
            selectable_options_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS message_poll_option (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            option_name TEXT NOT NULL,
            vote_total INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS message_location (
            message_row_id INTEGER PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            place_name TEXT
        );
        CREATE TABLE IF NOT EXISTS message_edit_info (
            message_row_id INTEGER PRIMARY KEY,
            original_key_id TEXT NOT NULL,
            edited_timestamp INTEGER NOT NULL,
            sender_timestamp INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_quoted (
            message_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            parent_message_chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            timestamp INTEGER,
            text_data TEXT
        );
        CREATE TABLE IF NOT EXISTS receipt_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            receipt_user_jid_row_id INTEGER NOT NULL,
            receipt_timestamp INTEGER
        );
    """)
    # Message 501: Valid message within sequence ceiling and causality
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (501, 1, 0, 'KEY_VERIFIED_501', 1729000000000, 0, 'Valid for deletion')"
    )
    # Message 502: Invalid message violating causality (future paradox)
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (502, 1, 0, 'KEY_REJECTED_502', 1729050000000, 0, 'Future message')"
    )

    # Add auxiliary records for verified message 501 across all child tables
    cur.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path) VALUES (501, 1, 'path/to/501.jpg')"
    )
    cur.execute(
        "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, key_id, timestamp, message_add_on_type) VALUES (10, 501, 0, 'ADDON_501', 1729000010000, 1)"
    )
    cur.execute(
        "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) VALUES (10, '👍', 1729000010000)"
    )
    cur.execute(
        "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, key_id, timestamp, message_add_on_type) VALUES (11, 501, 0, 'PIN_501', 1729000011000, 79)"
    )
    cur.execute(
        "INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id, pin_in_chat_state, sender_timestamp) VALUES (11, 1, 1729000011000)"
    )
    cur.execute(
        "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (501, 1)"
    )
    cur.execute(
        "INSERT INTO message_poll_option (message_row_id, option_name) VALUES (501, 'Option A')"
    )
    cur.execute(
        "INSERT INTO message_location (message_row_id, latitude, longitude, place_name) VALUES (501, 48.8584, 2.2945, 'Eiffel Tower')"
    )
    cur.execute(
        "INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp) VALUES (501, 'KEY_VERIFIED_501', 1729000020000, 1729000020000)"
    )
    cur.execute(
        "INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, key_id, timestamp, text_data) VALUES (501, 1, 1, 0, 'STANZA_BASE', 1729000000000, 'Quoted msg')"
    )
    cur.execute(
        "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, receipt_timestamp) VALUES (501, 1, 1729000010000)"
    )

    # Add auxiliary records for rejected future message 502 across child tables
    cur.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path) VALUES (502, 1, 'path/to/502.jpg')"
    )
    cur.execute(
        "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, key_id, timestamp, message_add_on_type) VALUES (12, 502, 0, 'ADDON_502', 1729050010000, 1)"
    )
    cur.execute(
        "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) VALUES (12, '❤️', 1729050010000)"
    )
    cur.execute(
        "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (502, 1)"
    )
    cur.execute(
        "INSERT INTO message_poll_option (message_row_id, option_name) VALUES (502, 'Option B')"
    )
    cur.execute(
        "INSERT INTO message_location (message_row_id, latitude, longitude, place_name) VALUES (502, 40.7128, -74.0060, 'NYC')"
    )
    cur.execute(
        "INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp) VALUES (502, 'KEY_REJECTED_502', 1729050020000, 1729050020000)"
    )
    cur.execute(
        "INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, key_id, timestamp, text_data) VALUES (502, 1, 1, 0, 'STANZA_FUTURE', 1729050000000, 'Future quoted')"
    )
    cur.execute(
        "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, receipt_timestamp) VALUES (502, 1, 1729050010000)"
    )
    conn.commit()

    del_json = tmp_path / "deletedMessages_mixed.json"
    del_json.write_text(json.dumps({"deleted_ids": [501, 502]}))

    schema_cache = {}
    count = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, schema_cache, 1729000500000, 600
    )
    conn.commit()

    assert count == 1
    # 501 was verified and cascaded across all 10 auxiliary tables
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 501").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM message_media WHERE message_row_id = 501").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_add_on WHERE parent_message_row_id = 501"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_add_on_reaction WHERE message_add_on_row_id = 10"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_add_on_pin_in_chat WHERE message_add_on_row_id = 11"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_poll WHERE message_row_id = 501").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_poll_option WHERE message_row_id = 501"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_location WHERE message_row_id = 501").fetchone()[
            0
        ]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_edit_info WHERE message_row_id = 501"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_quoted WHERE message_row_id = 501").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM receipt_user WHERE message_row_id = 501").fetchone()[0]
        == 0
    )

    # 502 was rejected and protected, along with its records across all child tables!
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 502").fetchone()[0] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM message_media WHERE message_row_id = 502").fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_add_on WHERE parent_message_row_id = 502"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_add_on_reaction WHERE message_add_on_row_id = 12"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_poll WHERE message_row_id = 502").fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_poll_option WHERE message_row_id = 502"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_location WHERE message_row_id = 502").fetchone()[
            0
        ]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_edit_info WHERE message_row_id = 502"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_quoted WHERE message_row_id = 502").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM receipt_user WHERE message_row_id = 502").fetchone()[0]
        == 1
    )
    conn.close()


def test_apply_increment_folder_nested_header_and_sequences(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Message 601: within sequence ceiling (601 <= 650) and before backup time
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (601, 1, 0, 'KEY_INC_601', 1729000000000, 0, 'To be deleted')"
    )
    # Message 700: exceeds sequence ceiling (700 > 650)
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (700, 1, 0, 'KEY_INC_700', 1729000000000, 0, 'Must be protected')"
    )
    conn.commit()

    inc_dir = tmp_path / "inc_nested"
    inc_dir.mkdir()

    # Real WhatsApp nested header format
    header_content = {
        "format": "protobuf",
        "header": {"creation_date": 1729001000000},
        "deleted_messages": {"deleted_message_ids_files": ["deletedMessages_1.json"]},
        "sequences": "sequences.json",
    }
    (inc_dir / "header.json").write_text(json.dumps(header_content))

    # sequences.json capping message sequence at 650
    sequences_content = {"sequences": [{"name": "message", "value": 650}]}
    (inc_dir / "sequences.json").write_text(json.dumps(sequences_content))

    # deletedMessages_1.json requesting deletion of 601 and 700
    (inc_dir / "deletedMessages_1.json").write_text(json.dumps({"deleted_ids": [601, 700]}))

    stats = increment_merger.apply_increment_folder(conn, str(inc_dir), logger, None, None, None)

    # Only 601 was deleted; 700 was rejected due to sequence ceiling
    assert stats["messages_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 601").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 700").fetchone()[0] == 1
    conn.close()


def _build_image_wmi_with_media_data(
    remote_jid: str,
    key_id: str,
    caption: str,
    mimetype: str,
    file_sha256: bytes,
    file_length: int,
    from_me: int,
    timestamp: int,
    local_path: str,
) -> bytes:
    base = _build_image_wmi(
        remote_jid,
        key_id,
        caption,
        mimetype,
        file_sha256,
        file_length,
        from_me,
        timestamp,
    )
    media_data = _build_field(1, 2, local_path.encode("utf-8"))
    return base + _build_field(38, 2, media_data)


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


def test_apply_messages_bin_populates_media_file_path_from_tag38(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    wmi_bytes = _build_image_wmi_with_media_data(
        "123456789@s.whatsapp.net",
        "KEY_MEDIA_PATH",
        "Sunset photo",
        "image/jpeg",
        b"\x03" * 32,
        89000,
        0,
        1729000000000,
        "Media/WhatsApp Images/IMG-20261016-WA0007.jpg",
    )
    frame = _build_history_sync_frame("123456789@s.whatsapp.net", [wmi_bytes])
    bin_file = tmp_path / "messages.bin"
    bin_file.write_bytes(frame)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(
        conn, str(bin_file), logger, None, None, schema_cache
    )
    assert inserted == 1

    cur = conn.cursor()
    row = cur.execute(
        "SELECT mm.file_path, mm.mime_type, mm.file_size FROM message_media mm "
        "JOIN message m ON m._id = mm.message_row_id WHERE m.key_id = 'KEY_MEDIA_PATH'"
    ).fetchone()
    assert row is not None
    assert row[0] == "Media/WhatsApp Images/IMG-20261016-WA0007.jpg"
    assert row[1] == "image/jpeg"
    assert row[2] == 89000
    conn.close()


def test_apply_messages_bin_prefers_existing_file_path_over_tag38(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Pre-seed an existing message with a relocated local path
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type) "
        "VALUES (800, 1, 0, 'KEY_RELOCATED', 1728900000000, 1)"
    )
    cur.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path, mime_type, file_size) "
        "VALUES (800, 1, 'Media/Custom/Relocated.jpg', 'image/jpeg', 89000)"
    )
    conn.commit()

    # Replay increment message with identical key_id carrying a different Tag 38 path
    wmi_bytes = _build_image_wmi_with_media_data(
        "123456789@s.whatsapp.net",
        "KEY_RELOCATED",
        "Updated caption",
        "image/jpeg",
        b"\x04" * 32,
        89000,
        0,
        1729000000000,
        "Media/WhatsApp Images/Original_From_Phone.jpg",
    )
    frame = _build_history_sync_frame("123456789@s.whatsapp.net", [wmi_bytes])
    bin_file = tmp_path / "messages.bin"
    bin_file.write_bytes(frame)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, schema_cache)

    row = cur.execute(
        "SELECT mm.file_path FROM message_media mm "
        "JOIN message m ON m._id = mm.message_row_id WHERE m.key_id = 'KEY_RELOCATED'"
    ).fetchone()
    assert row is not None
    # Existing locally preserved path must take precedence
    assert row[0] == "Media/Custom/Relocated.jpg"
    conn.close()


def test_apply_entity_modifications_skips_unknown_tables(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    # Ensure a dummy user_device table exists
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_device (_id INTEGER PRIMARY KEY, user_jid_row_id INTEGER)"
    )
    conn.commit()

    mod_json = tmp_path / "user_device_modified_1.json"
    mod_json.write_text(
        json.dumps([{"_id": 1, "user_jid_row_id": 999}]),
        encoding="utf-8",
    )

    count = increment_merger.apply_entity_modifications(
        conn, "user_device", str(mod_json), logger, None
    )
    assert count == 0
    # Table must not have been modified
    assert conn.execute("SELECT COUNT(*) FROM user_device").fetchone()[0] == 0
    conn.close()


def test_apply_entity_deletions_skips_unknown_tables(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_device (_id INTEGER PRIMARY KEY, user_jid_row_id INTEGER)"
    )
    conn.execute("INSERT INTO user_device (_id, user_jid_row_id) VALUES (1, 100)")
    conn.commit()

    del_json = tmp_path / "user_device_deleted_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [1]}), encoding="utf-8")

    count = increment_merger.apply_entity_deletions(conn, "user_device", str(del_json), logger)
    assert count == 0
    # Row must remain intact
    assert conn.execute("SELECT COUNT(*) FROM user_device WHERE _id = 1").fetchone()[0] == 1
    conn.close()


def test_apply_increment_folder_safely_skips_vendor_ancillary_tables(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    inc_dir = tmp_path / "inc_ancillary"
    inc_dir.mkdir()

    # Header with standard metadata
    (inc_dir / "header.json").write_text(json.dumps({"creation_date": 1729000000000}))

    # Supported table: props
    (inc_dir / "props_modified_1.json").write_text(
        json.dumps([{"key": "test_supported_key", "value": "active"}]),
        encoding="utf-8",
    )

    # Unsupported auxiliary tables that appear in real WhatsApp backups
    (inc_dir / "receipt_device_modified_1.json").write_text(
        json.dumps([{"_id": 99, "device_id": 5}]),
        encoding="utf-8",
    )
    (inc_dir / "user_device_deleted_1.json").write_text(
        json.dumps({"deleted_ids": [55]}),
        encoding="utf-8",
    )

    schema_cache = {}
    stats = increment_merger.apply_increment_folder(
        conn, str(inc_dir), logger, None, None, schema_cache
    )

    # props was applied, receipt_device was skipped cleanly
    assert stats["entity_modified"] == 1
    assert stats["entity_deleted"] == 0
    assert (
        conn.execute("SELECT value FROM props WHERE key = 'test_supported_key'").fetchone()[0]
        == "active"
    )
    conn.close()


def test_apply_entity_modifications_group_past_participant_user(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_past_participant_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_jid_row_id INTEGER NOT NULL,
            user_jid_row_id INTEGER NOT NULL,
            is_leave INTEGER NOT NULL,
            timestamp INTEGER NOT NULL
        )
        """
    )
    conn.commit()

    jid_id_map = {9001: 10, 9002: 20}
    mod_json = tmp_path / "group_past_participant_user_modified_1.json"
    mod_json.write_text(
        json.dumps(
            [
                {
                    "_id": 999,
                    "group_jid_row_id": 9001,
                    "user_jid_row_id": 9002,
                    "is_leave": 1,
                    "timestamp": 1791238958000,
                }
            ]
        ),
        encoding="utf-8",
    )

    count = increment_merger.apply_entity_modifications(
        conn, "group_past_participant_user", str(mod_json), logger, jid_id_map
    )
    assert count == 1

    row = conn.execute(
        "SELECT group_jid_row_id, user_jid_row_id, is_leave, timestamp FROM group_past_participant_user"
    ).fetchone()
    assert row == (10, 20, 1, 1791238958000)

    # Re-apply update with modified status for same natural composite key
    mod_json2 = tmp_path / "group_past_participant_user_modified_2.json"
    mod_json2.write_text(
        json.dumps(
            [
                {
                    "_id": 9999,
                    "group_jid_row_id": 9001,
                    "user_jid_row_id": 9002,
                    "is_leave": 0,
                    "timestamp": 1791239999000,
                }
            ]
        ),
        encoding="utf-8",
    )
    count2 = increment_merger.apply_entity_modifications(
        conn, "group_past_participant_user", str(mod_json2), logger, jid_id_map
    )
    assert count2 == 1

    # Verify updated in place without duplicate row
    rows = conn.execute(
        "SELECT group_jid_row_id, user_jid_row_id, is_leave, timestamp FROM group_past_participant_user"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == (10, 20, 0, 1791239999000)
    conn.close()


def test_apply_entity_modifications_call_log(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_log (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT UNIQUE NOT NULL,
            jid_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            video_call INTEGER NOT NULL,
            duration INTEGER NOT NULL,
            call_result INTEGER NOT NULL,
            bytes_transferred INTEGER NOT NULL,
            group_jid_row_id INTEGER,
            call_creator_device_jid_row_id INTEGER
        )
        """
    )
    conn.commit()

    jid_id_map = {8888: 15, 9999: 25}
    mod_json = tmp_path / "call_log_modified_1.json"
    mod_json.write_text(
        json.dumps(
            [
                {
                    "_id": 500,
                    "call_id": "call:TEST_CALL_1",
                    "jid_row_id": 8888,
                    "group_jid_row_id": 9999,
                    "from_me": 0,
                    "timestamp": 1791708599000,
                    "video_call": 1,
                    "duration": 120,
                    "call_result": 5,
                    "bytes_transferred": 50000,
                }
            ]
        ),
        encoding="utf-8",
    )

    count = increment_merger.apply_entity_modifications(
        conn, "call_log", str(mod_json), logger, jid_id_map
    )
    assert count == 1

    row = conn.execute(
        "SELECT call_id, jid_row_id, group_jid_row_id, video_call, duration FROM call_log"
    ).fetchone()
    assert row == ("call:TEST_CALL_1", 15, 25, 1, 120)

    # Re-apply modification with updated duration matching same call_id
    mod_json2 = tmp_path / "call_log_modified_2.json"
    mod_json2.write_text(
        json.dumps(
            [
                {
                    "_id": 501,
                    "call_id": "call:TEST_CALL_1",
                    "jid_row_id": 8888,
                    "group_jid_row_id": 9999,
                    "from_me": 0,
                    "timestamp": 1791708599000,
                    "video_call": 1,
                    "duration": 185,
                    "call_result": 5,
                    "bytes_transferred": 75000,
                }
            ]
        ),
        encoding="utf-8",
    )
    count2 = increment_merger.apply_entity_modifications(
        conn, "call_log", str(mod_json2), logger, jid_id_map
    )
    assert count2 == 1

    rows = conn.execute(
        "SELECT call_id, jid_row_id, duration, bytes_transferred FROM call_log"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("call:TEST_CALL_1", 15, 185, 75000)
    conn.close()


def test_apply_entity_deletions_call_log_by_call_id(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_log (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT UNIQUE NOT NULL,
            jid_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            video_call INTEGER NOT NULL,
            duration INTEGER NOT NULL,
            call_result INTEGER NOT NULL,
            bytes_transferred INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO call_log (call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result, bytes_transferred) VALUES ('call:DELETE_ME', 1, 0, 1000, 0, 10, 5, 0)"
    )
    conn.commit()

    del_json = tmp_path / "call_log_deleted_1.json"
    del_json.write_text(json.dumps({"deleted_ids": ["call:DELETE_ME"]}), encoding="utf-8")

    count = increment_merger.apply_entity_deletions(conn, "call_log", str(del_json), logger)
    assert count == 1
    assert conn.execute("SELECT COUNT(*) FROM call_log").fetchone()[0] == 0
    conn.close()


def test_apply_entity_deletions_group_past_participant_user_by_composite(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_past_participant_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_jid_row_id INTEGER NOT NULL,
            user_jid_row_id INTEGER NOT NULL,
            is_leave INTEGER NOT NULL,
            timestamp INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO group_past_participant_user (group_jid_row_id, user_jid_row_id, is_leave, timestamp) VALUES (10, 20, 1, 1000)"
    )
    conn.commit()

    del_json = tmp_path / "group_past_participant_user_deleted_1.json"
    del_json.write_text(
        json.dumps({"deleted_ids": [{"group_jid_row_id": 10, "user_jid_row_id": 20}]}),
        encoding="utf-8",
    )

    count = increment_merger.apply_entity_deletions(
        conn, "group_past_participant_user", str(del_json), logger
    )
    assert count == 1
    assert conn.execute("SELECT COUNT(*) FROM group_past_participant_user").fetchone()[0] == 0
    conn.close()


def test_apply_poll_vote_in_order(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_poll_in_order.db")
    bin_path = str(tmp_path / "messages.bin")

    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_KEY_1", "Favorite color?", ["Red", "Blue"], 1729000100
    )
    wmi_vote = _build_poll_vote_wmi(
        "5550100@s.whatsapp.net", "VOTE_KEY_1", "POLL_KEY_1", 1729000200, 1729000200000
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_vote)) + wmi_vote)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 2

    cur = conn.cursor()
    poll_id = cur.execute(
        "SELECT _id, message_type FROM message WHERE key_id = 'POLL_KEY_1'"
    ).fetchone()[0]
    vote_row = cur.execute(
        "SELECT a._id, a.parent_message_row_id, a.message_add_on_type, a.key_id, v.sender_timestamp "
        "FROM message_add_on a JOIN message_add_on_poll_vote v ON a._id = v.message_add_on_row_id "
        "WHERE a.parent_message_row_id = ?",
        (poll_id,),
    ).fetchone()
    assert vote_row is not None
    assert vote_row[1] == poll_id
    assert vote_row[2] == 67
    assert vote_row[3] == "VOTE_KEY_1"
    assert vote_row[4] == 1729000200000
    conn.close()


def test_two_pass_poll_vote_arrives_before_poll_creation(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_poll.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Vote arrives first (out of order sync)
    wmi_vote = _build_poll_vote_wmi(
        "5550100@s.whatsapp.net", "VOTE_KEY_2", "POLL_KEY_2", 1729000300, 1729000300000
    )
    # Frame 1: Poll creation arrives second
    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_KEY_2", "Lunch place?", ["Pizza", "Sushi"], 1729000100
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_vote)) + wmi_vote)
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 2

    cur = conn.cursor()
    poll_id = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_KEY_2'").fetchone()[0]
    vote_row = cur.execute(
        "SELECT a.parent_message_row_id, a.message_add_on_type, a.key_id, v.sender_timestamp "
        "FROM message_add_on a JOIN message_add_on_poll_vote v ON a._id = v.message_add_on_row_id "
        "WHERE a.parent_message_row_id = ?",
        (poll_id,),
    ).fetchone()
    assert vote_row is not None
    assert vote_row[0] == poll_id
    assert vote_row[1] == 67
    assert vote_row[2] == "VOTE_KEY_2"
    assert vote_row[3] == 1729000300000
    conn.close()


def test_apply_poll_vote_supersedes_prior_vote(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_poll_supersede.db")
    bin_path = str(tmp_path / "messages.bin")

    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_KEY_3", "Weekend plan?", ["Hike", "Beach"], 1729000100
    )
    wmi_vote_1 = _build_poll_vote_wmi(
        "5550100@s.whatsapp.net", "VOTE_KEY_3A", "POLL_KEY_3", 1729000200, 1729000200000
    )
    wmi_vote_2 = _build_poll_vote_wmi(
        "5550100@s.whatsapp.net", "VOTE_KEY_3B", "POLL_KEY_3", 1729000300, 1729000300000
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_vote_1)) + wmi_vote_1)
        f.write(_encode_varint(len(wmi_vote_2)) + wmi_vote_2)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 3

    cur = conn.cursor()
    poll_id = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_KEY_3'").fetchone()[0]

    # Only one vote add-on should exist for this sender on this poll
    add_ons = cur.execute(
        "SELECT key_id, message_add_on_type FROM message_add_on WHERE parent_message_row_id = ?",
        (poll_id,),
    ).fetchall()
    assert len(add_ons) == 1
    assert add_ons[0][0] == "VOTE_KEY_3B"
    assert add_ons[0][1] == 67

    poll_votes = cur.execute(
        "SELECT v.sender_timestamp FROM message_add_on_poll_vote v "
        "JOIN message_add_on a ON a._id = v.message_add_on_row_id "
        "WHERE a.parent_message_row_id = ?",
        (poll_id,),
    ).fetchall()
    assert len(poll_votes) == 1
    assert poll_votes[0][0] == 1729000300000
    conn.close()


def test_apply_message_deletions_cascades_poll_votes(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_poll_delete_cascade.db")
    bin_path = str(tmp_path / "messages.bin")

    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_KEY_4", "Dinner?", ["Burger", "Salad"], 1729000100
    )
    wmi_vote = _build_poll_vote_wmi(
        "5550100@s.whatsapp.net", "VOTE_KEY_4", "POLL_KEY_4", 1729000200, 1729000200000
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_vote)) + wmi_vote)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    poll_id = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_KEY_4'").fetchone()[0]
    assert (
        cur.execute("SELECT COUNT(*) FROM message WHERE key_id = 'POLL_KEY_4'").fetchone()[0] == 1
    )
    assert (
        cur.execute("SELECT COUNT(*) FROM message_add_on WHERE key_id = 'VOTE_KEY_4'").fetchone()[0]
        == 1
    )
    assert cur.execute("SELECT COUNT(*) FROM message_add_on_poll_vote").fetchone()[0] == 1

    del_json = tmp_path / "deletedMessages_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [poll_id]}), encoding="utf-8")

    # Now delete the poll message
    deleted = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, schema_cache, None, None
    )
    conn.commit()

    assert deleted == 1
    assert (
        cur.execute("SELECT COUNT(*) FROM message WHERE key_id = 'POLL_KEY_4'").fetchone()[0] == 0
    )
    assert (
        cur.execute("SELECT COUNT(*) FROM message_add_on WHERE key_id = 'VOTE_KEY_4'").fetchone()[0]
        == 0
    )
    assert cur.execute("SELECT COUNT(*) FROM message_add_on_poll_vote").fetchone()[0] == 0
    conn.close()


def test_sanitize_jid_string():
    # Companion device and agent token stripping
    assert (
        increment_merger.sanitize_jid_string("5550100.0:40@s.whatsapp.net")
        == "5550100@s.whatsapp.net"
    )
    assert (
        increment_merger.sanitize_jid_string("5550100:40@s.whatsapp.net")
        == "5550100@s.whatsapp.net"
    )
    assert increment_merger.sanitize_jid_string("100200300.1:4@lid") == "100200300@lid"
    # Groups and broadcasts preserved
    assert (
        increment_merger.sanitize_jid_string("5550100-1600000000@g.us") == "5550100-1600000000@g.us"
    )
    assert increment_merger.sanitize_jid_string("status@broadcast") == "status@broadcast"
    # Bare string
    assert increment_merger.sanitize_jid_string("5550100.0:40") == "5550100"


def test_resolve_or_create_jid_strips_devices_and_prefers_primary(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")

    # Insert primary JID (device=0) and companion device (device=40) for 5550888
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550888', 's.whatsapp.net', 0, '5550888@s.whatsapp.net')"
    )
    primary_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550888', 's.whatsapp.net', 40, '5550888.0:40@s.whatsapp.net')"
    )
    _companion_id = cur.lastrowid
    conn.commit()

    cache = {}
    # Resolving companion wire string must resolve to primary JID
    resolved = increment_merger.resolve_or_create_jid(
        conn, "5550888.0:40@s.whatsapp.net", logger, cache
    )
    assert resolved == primary_id
    # Resolving clean string must resolve to primary JID
    resolved_clean = increment_merger.resolve_or_create_jid(
        conn, "5550888@s.whatsapp.net", logger, cache
    )
    assert resolved_clean == primary_id
    conn.close()


def test_apply_messages_bin_sender_invariants_1on1_and_group(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    schema_cache = {}

    # 1-on-1 incoming message
    wmi_1on1_in = _build_text_wmi(
        "5550100@s.whatsapp.net",
        "KEY_1ON1_IN",
        "Hello from peer",
        from_me=0,
        timestamp=1729000100,
    )
    # 1-on-1 outgoing message
    wmi_1on1_out = _build_text_wmi(
        "5550100@s.whatsapp.net",
        "KEY_1ON1_OUT",
        "Hello back",
        from_me=1,
        timestamp=1729000200,
    )
    # Group incoming message with participant Wire Tag 5
    key_grp = (
        _build_field(1, 2, b"5550000-123456@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"KEY_GRP_IN")
    )
    wmi_grp_in = (
        _build_field(1, 2, key_grp)
        + _build_field(2, 2, _build_field(1, 2, b"Group message"))
        + _build_field(3, 0, 1729000300)
        + _build_field(5, 2, b"5550200@s.whatsapp.net")
    )
    # Group outgoing message
    wmi_grp_out = _build_text_wmi(
        "5550000-123456@g.us",
        "KEY_GRP_OUT",
        "My group reply",
        from_me=1,
        timestamp=1729000400,
    )

    frame = _build_history_sync_frame(
        "5550100@s.whatsapp.net",
        [wmi_1on1_in, wmi_1on1_out, wmi_grp_in, wmi_grp_out],
    )
    bin_path = str(tmp_path / "messages.bin")
    (tmp_path / "messages.bin").write_bytes(frame)

    jid_cache = {}
    chat_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, jid_cache, chat_cache, schema_cache)
    conn.commit()

    cur = conn.cursor()
    # Check 1-on-1 invariant: sender_jid_row_id == 0 for both in and out
    r_1on1_in = cur.execute(
        "SELECT sender_jid_row_id, from_me FROM message WHERE key_id = 'KEY_1ON1_IN'"
    ).fetchone()
    assert r_1on1_in[0] == 0

    r_1on1_out = cur.execute(
        "SELECT sender_jid_row_id, from_me FROM message WHERE key_id = 'KEY_1ON1_OUT'"
    ).fetchone()
    assert r_1on1_out[0] == 0

    # Check Group invariant: sender_jid_row_id == participant_jid_id for incoming, 0 for outgoing
    part_jid_id = cur.execute("SELECT _id FROM jid WHERE user = '5550200'").fetchone()[0]
    r_grp_in = cur.execute(
        "SELECT sender_jid_row_id, from_me FROM message WHERE key_id = 'KEY_GRP_IN'"
    ).fetchone()
    assert r_grp_in[0] == part_jid_id

    r_grp_out = cur.execute(
        "SELECT sender_jid_row_id, from_me FROM message WHERE key_id = 'KEY_GRP_OUT'"
    ).fetchone()
    assert r_grp_out[0] == 0
    conn.close()


def test_migrate_restored_backup_to_lid_consolidates_split_chats(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
    if "account_jid_row_id" not in cols:
        conn.execute("ALTER TABLE chat ADD COLUMN account_jid_row_id INTEGER")
    if "sort_timestamp" not in cols:
        conn.execute("ALTER TABLE chat ADD COLUMN sort_timestamp INTEGER")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS jid_map (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )

    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('5550999', 's.whatsapp.net', '5550999@s.whatsapp.net')"
    )
    phone_jid_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('999000111', 'lid', '999000111@lid')"
    )
    lid_jid_id = cur.lastrowid

    cur.execute(
        "INSERT INTO jid_map (jid_row_id, lid_row_id) VALUES (?, ?)",
        (phone_jid_id, lid_jid_id),
    )

    cur.execute(
        "INSERT INTO chat (jid_row_id, hidden, sort_timestamp) VALUES (?, 0, 1000)",
        (phone_jid_id,),
    )
    phone_chat_id = cur.lastrowid
    cur.execute(
        "INSERT INTO chat (jid_row_id, hidden, sort_timestamp) VALUES (?, 0, 2000)",
        (lid_jid_id,),
    )
    lid_chat_id = cur.lastrowid

    for i in range(5):
        cur.execute(
            "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (?, 0, ?, 0, ?, 'msg', 0)",
            (phone_chat_id, f"PHONE_MSG_{i}", 1000 + i),
        )
    for i in range(2):
        cur.execute(
            "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (?, 0, ?, 0, ?, 'msg', 0)",
            (lid_chat_id, f"LID_MSG_{i}", 2000 + i),
        )
    conn.commit()

    increment_merger.migrate_restored_backup_to_lid(conn, logger)

    phone_chat_row = cur.execute(
        "SELECT hidden, account_jid_row_id, sort_timestamp FROM chat WHERE _id = ?",
        (phone_chat_id,),
    ).fetchone()
    assert phone_chat_row[0] == 0
    assert phone_chat_row[1] == lid_jid_id
    assert phone_chat_row[2] >= 2000

    lid_chat_row = cur.execute("SELECT hidden FROM chat WHERE _id = ?", (lid_chat_id,)).fetchone()
    assert lid_chat_row[0] == 1

    phone_msg_count = cur.execute(
        "SELECT COUNT(*) FROM message WHERE chat_row_id = ?", (phone_chat_id,)
    ).fetchone()[0]
    assert phone_msg_count == 7

    lid_msg_count = cur.execute(
        "SELECT COUNT(*) FROM message WHERE chat_row_id = ?", (lid_chat_id,)
    ).fetchone()[0]
    assert lid_msg_count == 0

    conn.close()


def test_resolve_or_create_chat_deduplicates_by_natural_identity(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")

    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550777', 's.whatsapp.net', 0, '5550777@s.whatsapp.net')"
    )
    primary_jid_id = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550777', 's.whatsapp.net', 40, '5550777.0:40@s.whatsapp.net')"
    )
    companion_jid_id = cur.lastrowid

    cur.execute(
        "INSERT INTO chat (jid_row_id, hidden) VALUES (?, 0)",
        (primary_jid_id,),
    )
    existing_chat_id = cur.lastrowid
    conn.commit()

    cache = {}
    resolved_chat_id = increment_merger.resolve_or_create_chat(
        conn, companion_jid_id, logger, cache
    )
    assert resolved_chat_id == existing_chat_id

    # Verify no new duplicate chat was inserted for user 5550777
    user_chats = cur.execute(
        "SELECT COUNT(*) FROM chat WHERE jid_row_id IN (?, ?)",
        (primary_jid_id, companion_jid_id),
    ).fetchone()[0]
    assert user_chats == 1
    conn.close()


def test_migrate_restored_backup_to_lid_consolidates_natural_duplicate_chats(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
    if "sort_timestamp" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN sort_timestamp INTEGER")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jid_map (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )

    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('5550333', 's.whatsapp.net', '5550333@s.whatsapp.net')"
    )
    primary_jid = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, raw_string) VALUES ('5550333', 's.whatsapp.net', '5550333.0:0@s.whatsapp.net')"
    )
    companion_jid = cur.lastrowid

    cur.execute(
        "INSERT INTO chat (jid_row_id, hidden, sort_timestamp) VALUES (?, 0, 1000)",
        (primary_jid,),
    )
    chat_primary = cur.lastrowid
    cur.execute(
        "INSERT INTO chat (jid_row_id, hidden, sort_timestamp) VALUES (?, 0, 2000)",
        (companion_jid,),
    )
    chat_dup = cur.lastrowid

    for i in range(3):
        cur.execute(
            "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (?, 0, ?, 0, ?, 'base', 0)",
            (chat_primary, f"BASE_{i}", 1000 + i),
        )
    for i in range(2):
        cur.execute(
            "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (?, 0, ?, 0, ?, 'inc', 0)",
            (chat_dup, f"INC_{i}", 2000 + i),
        )
    conn.commit()

    increment_merger.migrate_restored_backup_to_lid(conn, logger)

    c1_count = cur.execute(
        "SELECT COUNT(*) FROM message WHERE chat_row_id = ?", (chat_primary,)
    ).fetchone()[0]
    assert c1_count == 5

    c2_count = cur.execute(
        "SELECT COUNT(*) FROM message WHERE chat_row_id = ?", (chat_dup,)
    ).fetchone()[0]
    assert c2_count == 0

    c2_hidden = cur.execute("SELECT hidden FROM chat WHERE _id = ?", (chat_dup,)).fetchone()[0]
    assert c2_hidden == 1
    conn.close()


def test_migrate_restored_backup_to_lid_repoints_companion_senders(mock_db):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jid_map (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_add_on (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER,
            parent_message_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            status INTEGER DEFAULT 0,
            message_add_on_type INTEGER NOT NULL
        )
    """)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")

    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('999000333', 'lid', 0, '999000333@lid')"
    )
    primary_lid = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('999000333', 'lid', 0, '999000333.1:0@lid')"
    )
    companion_lid = cur.lastrowid

    cur.execute("INSERT INTO chat (jid_row_id) VALUES (1)")
    c_id = cur.lastrowid

    cur.execute(
        "INSERT INTO message (chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (?, 0, 'MSG_COMP_SENDER', ?, 1700000000, 'test', 0)",
        (c_id, companion_lid),
    )
    conn.commit()

    increment_merger.migrate_restored_backup_to_lid(conn, logger)

    repointed_sender = cur.execute(
        "SELECT sender_jid_row_id FROM message WHERE key_id = 'MSG_COMP_SENDER'"
    ).fetchone()[0]
    assert repointed_sender == primary_lid
    conn.close()


def test_ensure_jid_map_resolved_bridges_companion_lids(mock_db):
    from wab_viewer.main import _ensure_jid_map_resolved

    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jid_map (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_add_on (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER,
            parent_message_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            status INTEGER DEFAULT 0,
            message_add_on_type INTEGER NOT NULL
        )
    """)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")

    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550555', 's.whatsapp.net', 0, '5550555@s.whatsapp.net')"
    )
    phone_jid = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('999000555', 'lid', 0, '999000555@lid')"
    )
    primary_lid = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('999000555', 'lid', 0, '999000555.1:0@lid')"
    )
    companion_lid = cur.lastrowid

    cur.execute(
        "INSERT INTO jid_map (jid_row_id, lid_row_id) VALUES (?, ?)", (phone_jid, primary_lid)
    )
    conn.commit()

    _ensure_jid_map_resolved(conn, None)

    # Companion LID must bridge to the phone JID of its primary LID
    row = cur.execute(
        "SELECT jid_row_id FROM _jid_map_resolved WHERE lid_row_id = ?", (companion_lid,)
    ).fetchone()
    assert row is not None
    assert row[0] == phone_jid
    conn.close()


def test_decode_wmi_poll_with_embedded_votes():
    secret = b"\xaa" * 32
    opt1_hash = hashlib.sha256(b"Option A").digest()
    opt2_hash = hashlib.sha256(b"Option B").digest()

    poll_bytes = (
        _build_field(2, 2, b"Poll Question?")
        + _build_field(3, 2, _build_field(1, 2, b"Option A"))
        + _build_field(3, 2, _build_field(1, 2, b"Option B"))
        + _build_field(4, 0, 1)
    )
    ctx_bytes = _build_field(3, 2, secret)
    msg_bytes = _build_field(64, 2, poll_bytes) + _build_field(35, 2, ctx_bytes)

    # Tag 45 vote entry
    v_key = (
        _build_field(1, 2, b"12345@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"VOTE_100")
        + _build_field(4, 2, b"user1@lid")
    )
    vote_sub = _build_field(1, 2, opt1_hash) + _build_field(1, 2, opt2_hash)
    tag45_entry = (
        _build_field(1, 2, v_key) + _build_field(2, 2, vote_sub) + _build_field(3, 0, 1729000500000)
    )

    wmi_bytes = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"12345@g.us")
            + _build_field(2, 0, 0)
            + _build_field(3, 2, b"POLL_100"),
        )
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000100)
        + _build_field(45, 2, tag45_entry)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "POLL_100"
    assert decoded["message_type"] == 66
    assert decoded["poll_info"] is not None
    assert decoded["poll_info"]["question"] == "Poll Question?"
    assert decoded["poll_info"]["options"] == ["Option A", "Option B"]
    assert decoded["poll_info"]["message_secret"] == secret
    assert len(decoded["poll_info"]["embedded_votes"]) == 1
    v = decoded["poll_info"]["embedded_votes"][0]
    assert v["key_id"] == "VOTE_100"
    assert v["from_me"] == 0
    assert v["participant"] == "user1@lid"
    assert v["sender_ts"] == 1729000500000
    assert v["selected_option_hashes"] == [opt1_hash, opt2_hash]


def test_apply_poll_creation_with_embedded_votes(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_poll_embedded.db")
    bin_path = str(tmp_path / "messages.bin")

    secret = b"\xbb" * 32
    opt_a_hash = hashlib.sha256(b"Alpha").digest()
    opt_b_hash = hashlib.sha256(b"Beta").digest()

    poll_bytes = (
        _build_field(2, 2, b"Test Poll?")
        + _build_field(3, 2, _build_field(1, 2, b"Alpha"))
        + _build_field(3, 2, _build_field(1, 2, b"Beta"))
        + _build_field(4, 0, 1)
    )
    ctx_bytes = _build_field(3, 2, secret)
    msg_bytes = _build_field(64, 2, poll_bytes) + _build_field(35, 2, ctx_bytes)

    # Vote 1: voter 1 voted for Alpha in group 12345@g.us
    v_key1 = (
        _build_field(1, 2, b"12345@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"VOTE_EMB_1")
        + _build_field(4, 2, b"voter1@lid")
    )
    vote_sub1 = _build_field(1, 2, opt_a_hash)
    tag45_1 = (
        _build_field(1, 2, v_key1)
        + _build_field(2, 2, vote_sub1)
        + _build_field(3, 0, 1729000200000)
    )

    # Vote 2: voter 2 voted for Beta in group 12345@g.us
    v_key2 = (
        _build_field(1, 2, b"12345@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"VOTE_EMB_2")
        + _build_field(4, 2, b"voter2@lid")
    )
    vote_sub2 = _build_field(1, 2, opt_b_hash)
    tag45_2 = (
        _build_field(1, 2, v_key2)
        + _build_field(2, 2, vote_sub2)
        + _build_field(3, 0, 1729000300000)
    )

    wmi_poll = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"12345@g.us")
            + _build_field(2, 0, 0)
            + _build_field(3, 2, b"POLL_EMB_1"),
        )
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000100)
        + _build_field(45, 2, tag45_1)
        + _build_field(45, 2, tag45_2)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)

    schema_cache = {}
    logger = logging.getLogger("test")
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    poll_row = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_EMB_1'").fetchone()
    assert poll_row is not None
    poll_id = poll_row[0]

    # Verify secret table
    sec_row = cur.execute(
        "SELECT message_secret FROM message_secret WHERE message_row_id = ?", (poll_id,)
    ).fetchone()
    assert sec_row is not None
    assert sec_row[0] == secret

    # Verify poll options
    opts = cur.execute(
        "SELECT _id, option_name, option_sha256, vote_total FROM message_poll_option WHERE message_row_id = ? ORDER BY _id ASC",
        (poll_id,),
    ).fetchall()
    assert len(opts) == 2
    assert opts[0][1] == "Alpha"
    assert opts[0][2] == base64.b64encode(opt_a_hash).decode("ascii")
    assert opts[0][3] == 1
    assert opts[1][1] == "Beta"
    assert opts[1][2] == base64.b64encode(opt_b_hash).decode("ascii")
    assert opts[1][3] == 1

    # Verify message_add_on and message_add_on_poll_vote_selected_option
    add_ons = cur.execute(
        "SELECT a._id, a.key_id, vso.message_poll_option_id "
        "FROM message_add_on a "
        "JOIN message_add_on_poll_vote_selected_option vso ON vso.message_add_on_row_id = a._id "
        "WHERE a.parent_message_row_id = ? ORDER BY a._id ASC",
        (poll_id,),
    ).fetchall()
    assert len(add_ons) == 2
    assert add_ons[0][1] == "VOTE_EMB_1"
    assert add_ons[0][2] == opts[0][0]
    assert add_ons[1][1] == "VOTE_EMB_2"
    assert add_ons[1][2] == opts[1][0]
    conn.close()


def test_apply_poll_vote_with_option_hashes(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_poll_opt_hash.db")
    bin_path = str(tmp_path / "messages.bin")

    opt_red_hash = hashlib.sha256(b"Red").digest()
    opt_blue_hash = hashlib.sha256(b"Blue").digest()

    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_KEY_HASH", "Color?", ["Red", "Blue"], 1729000100
    )

    # Standalone Tag 50 with option hash in vote field (subtag 1)
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"POLL_KEY_HASH")
    )
    vote_val = _build_field(1, 2, opt_blue_hash)
    vote_payload = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, vote_val)
        + _build_field(3, 0, 1729000200000)
    )

    wmi_vote = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 0)
            + _build_field(3, 2, b"VOTE_KEY_HASH"),
        )
        + _build_field(2, 2, _build_field(50, 2, vote_payload))
        + _build_field(3, 0, 1729000200)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_vote)) + wmi_vote)

    schema_cache = {}
    logger = logging.getLogger("test")
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    poll_id = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_KEY_HASH'").fetchone()[0]
    opts = cur.execute(
        "SELECT _id, option_name, vote_total FROM message_poll_option WHERE message_row_id = ? ORDER BY _id ASC",
        (poll_id,),
    ).fetchall()
    assert opts[0][1] == "Red"
    assert opts[0][2] == 0
    assert opts[1][1] == "Blue"
    assert opts[1][2] == 1

    conn.close()


def test_hydrate_poll_rows_resilience():
    from wab_viewer.main import _hydrate_poll_rows

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
    CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY AUTOINCREMENT, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
    CREATE TABLE message_add_on (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_message_row_id INTEGER,
        from_me INTEGER,
        message_add_on_type INTEGER
    );
    CREATE TABLE message_add_on_poll_vote_selected_option (
        _id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_add_on_row_id INTEGER,
        message_poll_option_id INTEGER
    );
    """)

    conn.execute("INSERT INTO message_poll VALUES (10, 0)")
    conn.execute("INSERT INTO message_poll_option VALUES (101, 10, 'Yes', 0)")
    conn.execute("INSERT INTO message_poll_option VALUES (102, 10, 'No', 0)")

    conn.execute("INSERT INTO message_add_on VALUES (1, 10, 0, 67)")
    conn.execute("INSERT INTO message_add_on VALUES (2, 10, 0, 67)")
    conn.execute("INSERT INTO message_add_on VALUES (3, 10, 1, 67)")
    conn.execute("INSERT INTO message_add_on VALUES (4, 10, 0, 67)")

    conn.execute("INSERT INTO message_add_on_poll_vote_selected_option VALUES (1, 1, 101)")
    conn.execute("INSERT INTO message_add_on_poll_vote_selected_option VALUES (2, 2, 101)")
    conn.execute("INSERT INTO message_add_on_poll_vote_selected_option VALUES (3, 3, 101)")
    conn.execute("INSERT INTO message_add_on_poll_vote_selected_option VALUES (4, 4, 102)")
    conn.commit()

    rows = [
        {
            "msg_id": 10,
            "media_type": "poll",
            "text_body": "Do you agree?",
        }
    ]
    _hydrate_poll_rows(rows, "android", conn)

    poll_data = rows[0]["poll"]
    assert poll_data is not None
    assert poll_data["question"] == "Do you agree?"
    assert poll_data["total_voters"] == 4
    assert poll_data["total_votes"] == 4
    opts = poll_data["options"]
    assert opts[0]["name"] == "Yes"
    assert opts[0]["vote_count"] == 3
    assert opts[0]["user_selected"] is True
    assert opts[1]["name"] == "No"
    assert opts[1]["vote_count"] == 1
    assert opts[1]["user_selected"] is False
    conn.close()


def test_apply_poll_vote_encrypted_payload(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    conn = _make_two_pass_test_db(tmp_path / "test_poll_enc.db")
    bin_path = str(tmp_path / "messages.bin")

    secret = b"\xcc" * 32
    opt_cat_hash = hashlib.sha256(b"Cat").digest()
    opt_dog_hash = hashlib.sha256(b"Dog").digest()

    poll_bytes = (
        _build_field(2, 2, b"Pet?")
        + _build_field(3, 2, _build_field(1, 2, b"Cat"))
        + _build_field(3, 2, _build_field(1, 2, b"Dog"))
        + _build_field(4, 0, 1)
    )
    ctx_bytes = _build_field(3, 2, secret)
    msg_bytes = _build_field(64, 2, poll_bytes) + _build_field(35, 2, ctx_bytes)

    wmi_poll = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"12345@g.us")
            + _build_field(2, 0, 0)
            + _build_field(3, 2, b"POLL_ENC_1"),
        )
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000100)
    )

    # Derive secret key and encrypt vote payload (voting for Dog)
    voter_jid = "voter_dog@lid"
    orig_clean = "12345@g.us"
    mod_clean = voter_jid
    use_case_secret = (
        b"POLL_ENC_1" + orig_clean.encode("utf-8") + mod_clean.encode("utf-8") + b"Poll Vote"
    )
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=use_case_secret,
    )
    derived_key = hkdf.derive(secret)
    iv = b"\x07" * 12
    aad = f"POLL_ENC_1\x00{mod_clean}".encode()
    plaintext_vote = _build_field(1, 2, opt_dog_hash)
    aesgcm = AESGCM(derived_key)
    enc_payload = aesgcm.encrypt(iv, plaintext_vote, aad)

    # Standalone Tag 50 with encPayload (tag 1) and encIv (tag 2) inside vote (tag 2)
    target_key = (
        _build_field(1, 2, b"12345@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"POLL_ENC_1")
    )
    enc_vote_fields = _build_field(1, 2, enc_payload) + _build_field(2, 2, iv)
    vote_payload = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, enc_vote_fields)
        + _build_field(3, 0, 1729000200000)
    )

    wmi_vote = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"12345@g.us")
            + _build_field(2, 0, 0)
            + _build_field(3, 2, b"VOTE_ENC_1")
            + _build_field(4, 2, voter_jid.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(50, 2, vote_payload))
        + _build_field(3, 0, 1729000200)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_vote)) + wmi_vote)

    schema_cache = {}
    logger = logging.getLogger("test")
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    poll_id = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_ENC_1'").fetchone()[0]
    opts = cur.execute(
        "SELECT _id, option_name, vote_total FROM message_poll_option WHERE message_row_id = ? ORDER BY _id ASC",
        (poll_id,),
    ).fetchall()
    assert opts[0][1] == "Cat"
    assert opts[0][2] == 0
    assert opts[1][1] == "Dog"
    assert opts[1][2] == 1

    conn.close()


def test_apply_messages_bin_resolves_quoted_timestamp_immediate_and_deferred(mock_db, tmp_path):
    """Verify apply_messages_bin resolves quoted message timestamp immediately and deferred in Pass 2."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_quoted (
            message_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER NOT NULL,
            parent_message_chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            sender_jid_row_id INTEGER,
            key_id TEXT NOT NULL,
            timestamp INTEGER,
            message_type INTEGER,
            text_data TEXT
        );
    """)

    # 1. Base message already in DB (immediate resolution via DB lookup)
    conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
        "VALUES (50, 1, 0, 'PRE_EXISTING_KEY', 1, 1729000050000, 0, 'Pre-existing message')"
    )
    conn.commit()

    # Case A: Reply quotes PRE_EXISTING_KEY (resolved immediately via DB)
    wmi_reply_pre = _build_quoted_wmi(
        "5550100@s.whatsapp.net",
        "REPLY_TO_PRE",
        "Replying to pre-existing",
        1,
        1729000100,
        "PRE_EXISTING_KEY",
        "Pre-existing message",
        "5550100@s.whatsapp.net",
    )

    # Case B: Out-of-order pair where Reply arrives BEFORE target message
    # Message 2 in stream quotes LATER_KEY (which is Message 3 in stream)
    wmi_reply_later = _build_quoted_wmi(
        "5550100@s.whatsapp.net",
        "REPLY_TO_LATER",
        "Replying to later message",
        1,
        1729000200,
        "LATER_KEY",
        "Later message text",
        "5550100@s.whatsapp.net",
    )

    # Message 3 in stream is LATER_KEY with timestamp 1729000150000
    key_bytes_later = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"LATER_KEY")
    )
    msg_bytes_later = _build_field(1, 2, b"Later message text")
    wmi_later = (
        _build_field(1, 2, key_bytes_later)
        + _build_field(2, 2, msg_bytes_later)
        + _build_field(3, 0, 1729000150)
    )

    bin_path = str(tmp_path / "messages_quoted_resolved.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_reply_pre)) + wmi_reply_pre)
        f.write(_encode_varint(len(wmi_reply_later)) + wmi_reply_later)
        f.write(_encode_varint(len(wmi_later)) + wmi_later)

    schema_cache = {}
    logger = logging.getLogger("test")
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()
    assert inserted == 3

    # Check timestamps in message_quoted
    cur = conn.cursor()
    # Case A: pre-existing key resolved to 1729000050000
    row_a = cur.execute(
        "SELECT mq.timestamp FROM message_quoted mq "
        "JOIN message m ON m._id = mq.message_row_id "
        "WHERE m.key_id = 'REPLY_TO_PRE'"
    ).fetchone()
    assert row_a is not None
    assert row_a[0] == 1729000050000

    # Case B: later key resolved via Pass 2 to 1729000150000 (1729000150 * 1000)
    row_b = cur.execute(
        "SELECT mq.timestamp FROM message_quoted mq "
        "JOIN message m ON m._id = mq.message_row_id "
        "WHERE m.key_id = 'REPLY_TO_LATER'"
    ).fetchone()
    assert row_b is not None
    assert row_b[0] == 1729000150000

    conn.close()


def test_apply_messages_bin_link_preview(mock_db, tmp_path):
    """Verify decode_wmi and apply_messages_bin extract link previews into message_text and message_thumbnail."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
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

    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"LINK_MSG_KEY")
    )
    ext_bytes = (
        _build_field(1, 2, b"Check https://example.com")
        + _build_field(2, 2, b"https://example.com")
        + _build_field(4, 2, b"https://example.com")
        + _build_field(5, 2, b"Example domain website")
        + _build_field(6, 2, b"Example Domain")
        + _build_field(10, 0, 1)
        + _build_field(16, 2, b"\xff\xd8\xff\xe0\x00")
    )
    msg_bytes = _build_field(6, 2, ext_bytes)
    wmi = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000100)
    )

    decoded = increment_merger.decode_wmi(wmi)
    assert decoded["link_preview_info"] is not None
    assert decoded["link_preview_info"]["title"] == "Example Domain"
    assert decoded["link_preview_info"]["description"] == "Example domain website"
    assert decoded["link_preview_info"]["url"] == "https://example.com"
    assert decoded["link_preview_info"]["preview_type"] == 1
    assert decoded["link_preview_info"]["thumbnail"] == b"\xff\xd8\xff\xe0\x00"

    bin_path = str(tmp_path / "messages_link.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()
    assert inserted == 1

    cur = conn.cursor()
    row = cur.execute("""
        SELECT mt.page_title, mt.description, mt.url, mt.preview_type, mth.thumbnail
        FROM message m
        JOIN message_text mt ON mt.message_row_id = m._id
        LEFT JOIN message_thumbnail mth ON mth.message_row_id = m._id
        WHERE m.key_id = 'LINK_MSG_KEY'
    """).fetchone()

    assert row is not None
    assert row[0] == "Example Domain"
    assert row[1] == "Example domain website"
    assert row[2] == "https://example.com"
    assert row[3] == 1
    assert row[4] == b"\xff\xd8\xff\xe0\x00"
    conn.close()


def test_decode_wmi_poll_creation_v4_v5_future_proof():
    # Test Tag 93 (V4) wrapped in FutureProofMessage (field 1 is inner message)
    opt1 = _build_field(1, 2, b"Option Alpha")
    opt2 = _build_field(1, 2, b"Option Beta")
    # Pattern B: 1=name, 2=options, 3=selectableCount
    poll_inner = (
        _build_field(1, 2, b"Which release?")
        + _build_field(2, 2, opt1)
        + _build_field(2, 2, opt2)
        + _build_field(3, 0, 1)
    )
    # FutureProof wrapper: field 1 is the inner message (or poll payload)
    fp_wrapper = _build_field(1, 2, poll_inner)
    msg_bytes_v4 = _build_field(93, 2, fp_wrapper)

    key_bytes_v4 = (
        _build_field(1, 2, b"123456789@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"POLL_V4_KEY")
    )
    wmi_v4 = (
        _build_field(1, 2, key_bytes_v4)
        + _build_field(2, 2, msg_bytes_v4)
        + _build_field(3, 0, 1729000500)
    )

    decoded_v4 = increment_merger.decode_wmi(wmi_v4)
    assert decoded_v4["key_id"] == "POLL_V4_KEY"
    assert decoded_v4["message_type"] == 66
    assert decoded_v4["text_data"] == "Which release?"
    assert decoded_v4["poll_info"] is not None
    assert decoded_v4["poll_info"]["question"] == "Which release?"
    assert decoded_v4["poll_info"]["options"] == ["Option Alpha", "Option Beta"]
    assert decoded_v4["poll_info"]["selectable_count"] == 1

    # Test Tag 111 (V5) direct Pattern B
    msg_bytes_v5 = _build_field(111, 2, poll_inner)
    key_bytes_v5 = (
        _build_field(1, 2, b"123456789@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"POLL_V5_KEY")
    )
    wmi_v5 = (
        _build_field(1, 2, key_bytes_v5)
        + _build_field(2, 2, msg_bytes_v5)
        + _build_field(3, 0, 1729000501)
    )

    decoded_v5 = increment_merger.decode_wmi(wmi_v5)
    assert decoded_v5["key_id"] == "POLL_V5_KEY"
    assert decoded_v5["message_type"] == 66
    assert decoded_v5["text_data"] == "Which release?"
    assert decoded_v5["poll_info"]["options"] == ["Option Alpha", "Option Beta"]


def test_apply_messages_bin_surrogate_churn_poll(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_churn.db")
    cur = conn.cursor()
    logger = logging.getLogger("test")

    # Add last_message_row_id column to chat table and set initial value
    cur.execute("ALTER TABLE chat ADD COLUMN last_message_row_id INTEGER")
    cur.execute("UPDATE chat SET last_message_row_id = 100 WHERE _id = 1")
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data, status) "
        "VALUES (100, 1, 0, 'CHURN_POLL_KEY', 1, 1729000000000, 0, 'Old Text', 0)"
    )
    # Add an existing vote add-on pointing to parent 100
    cur.execute(
        "INSERT INTO message_add_on (_id, chat_row_id, from_me, key_id, sender_jid_row_id, parent_message_row_id, timestamp, status, message_add_on_type) "
        "VALUES (500, 1, 0, 'VOTE_ADDON_1', 1, 100, 1729000100000, 0, 67)"
    )
    conn.commit()

    # Replay poll message via messages.bin with Tag 93 (pollCreationMessageV4)
    opt1 = _build_field(1, 2, b"Yes")
    opt2 = _build_field(1, 2, b"No")
    poll_payload = (
        _build_field(1, 2, b"Do you agree?")
        + _build_field(2, 2, opt1)
        + _build_field(2, 2, opt2)
        + _build_field(3, 0, 1)
    )
    msg_bytes = _build_field(93, 2, poll_payload)
    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"CHURN_POLL_KEY")
    )
    wmi = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000000)
    )

    bin_path = str(tmp_path / "messages_churn.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()
    assert inserted == 1

    # Verify the new message replaced the old one
    new_msg = cur.execute(
        "SELECT _id, message_type, text_data FROM message WHERE key_id = 'CHURN_POLL_KEY'"
    ).fetchone()
    assert new_msg is not None
    new_id = new_msg[0]
    assert new_id != 100  # auto-increment produced a new surrogate ID
    assert new_msg[1] == 66
    assert new_msg[2] == "Do you agree?"

    # Verify message_add_on was re-pointed from old ID (100) to new ID
    addon = cur.execute(
        "SELECT parent_message_row_id FROM message_add_on WHERE _id = 500"
    ).fetchone()
    assert addon[0] == new_id

    # Verify message_poll_option has the options under the new ID
    options = cur.execute(
        "SELECT option_name FROM message_poll_option WHERE message_row_id = ? ORDER BY _id ASC",
        (new_id,),
    ).fetchall()
    assert [o[0] for o in options] == ["Yes", "No"]

    # Verify chat.last_message_row_id was updated to new_id
    chat_row = cur.execute("SELECT last_message_row_id FROM chat WHERE _id = 1").fetchone()
    assert chat_row[0] == new_id
    conn.close()


def test_decode_wmi_stub_type():
    # Test wire Tag 24 (messageStubType) and Tag 25 (messageStubParameters)
    key_bytes = (
        _build_field(1, 2, b"393926739354@s.whatsapp.net")
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
        _build_field(1, 2, b"393926739354@s.whatsapp.net")
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


def test_apply_messages_bin_skips_empty_control_frames(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_empty_skip.db")
    cur = conn.cursor()
    logger = logging.getLogger("test")

    # Add empty frame to messages.bin
    key_bytes = (
        _build_field(1, 2, b"393926739354@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"EMPTY_CONTROL_KEY")
    )
    wmi = _build_field(1, 2, key_bytes) + _build_field(3, 0, 1729000000)

    bin_path = str(tmp_path / "messages_empty.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 0
    msg_count = cur.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    assert msg_count == 0
    conn.close()


def test_apply_messages_bin_stub_system_message_no_unhide(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_stub_no_unhide.db")
    cur = conn.cursor()
    logger = logging.getLogger("test")

    # Create message_system table
    cur.execute(
        "CREATE TABLE message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER NOT NULL)"
    )
    cur.execute("ALTER TABLE chat ADD COLUMN display_message_row_id INTEGER")
    conn.commit()

    # Pre-seed a shadow 1-to-1 chat with hidden = 1
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (10, '393926739354', 's.whatsapp.net', '393926739354@s.whatsapp.net')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (5, 10, 1, 1000, NULL)"
    )
    conn.commit()

    # Replay a stub message (wire tag 24 = 67)
    key_bytes = (
        _build_field(1, 2, b"393926739354@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"STUB_SYS_KEY")
    )
    wmi = _build_field(1, 2, key_bytes) + _build_field(3, 0, 1729000500) + _build_field(24, 0, 67)

    bin_path = str(tmp_path / "messages_stub.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 1
    msg_row = cur.execute(
        "SELECT _id, message_type, text_data FROM message WHERE key_id = 'STUB_SYS_KEY'"
    ).fetchone()
    assert msg_row is not None
    assert msg_row[1] == 7
    assert msg_row[2] is None

    # message_system must have recorded action_type 67
    sys_row = cur.execute(
        "SELECT action_type FROM message_system WHERE message_row_id = ?", (msg_row[0],)
    ).fetchone()
    assert sys_row is not None
    assert sys_row[0] == 67

    # 1-to-1 chat MUST remain hidden = 1 and display_message_row_id must remain NULL
    chat_row = cur.execute(
        "SELECT hidden, display_message_row_id FROM chat WHERE _id = 5"
    ).fetchone()
    assert chat_row[0] == 1
    assert chat_row[1] is None
    conn.close()


def test_decode_wmi_stub_revoke():
    # Protobuf StubType.REVOKE (1) must decode to message_type = 15 (deleted), not system message (7)
    key_bytes = (
        _build_field(1, 2, b"393926739354@s.whatsapp.net")
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


def test_apply_messages_bin_stub_revoke(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_stub_revoke.db")
    cur = conn.cursor()
    logger = logging.getLogger("test")

    # Create message_system and message_revoked tables
    cur.execute(
        "CREATE TABLE message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE message_revoked (message_row_id INTEGER PRIMARY KEY, revoked_key_id TEXT NOT NULL, admin_jid_row_id INTEGER, revoke_timestamp INTEGER)"
    )
    cur.execute("CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, file_path TEXT)")
    conn.commit()

    key_bytes = (
        _build_field(1, 2, b"393926739354@s.whatsapp.net")
        + _build_field(2, 0, 1)  # from_me = 1
        + _build_field(3, 2, b"REVOKED_MSG_123")
    )
    wmi = (
        _build_field(1, 2, key_bytes)
        + _build_field(3, 0, 1729000999)
        + _build_field(24, 0, 1)  # REVOKE = 1
    )

    bin_path = str(tmp_path / "messages_revoke.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 1
    msg_row = cur.execute(
        "SELECT _id, message_type, text_data, from_me FROM message WHERE key_id = 'REVOKED_MSG_123'"
    ).fetchone()
    assert msg_row is not None
    assert msg_row[1] == 15  # message_type 15 (revoked), NOT 7
    assert msg_row[2] is None
    assert msg_row[3] == 1

    # message_system must have 0 rows for this message
    sys_count = cur.execute(
        "SELECT COUNT(*) FROM message_system WHERE message_row_id = ?", (msg_row[0],)
    ).fetchone()[0]
    assert sys_count == 0

    # message_revoked must record the revocation
    rev_row = cur.execute(
        "SELECT message_row_id, revoked_key_id, revoke_timestamp FROM message_revoked WHERE message_row_id = ?",
        (msg_row[0],),
    ).fetchone()
    assert rev_row is not None
    assert rev_row[1] == "REVOKED_MSG_123"
    assert rev_row[2] == 1729000999000

    conn.close()


def test_resolve_or_create_chat_no_premature_unhide(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_chat_unhide.db")
    cur = conn.cursor()
    logger = logging.getLogger("test")

    # 1. Existing shadow chat with hidden = 1
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (10, '393926739354', 's.whatsapp.net', '393926739354@s.whatsapp.net')"
    )
    cur.execute("INSERT INTO chat (_id, jid_row_id, hidden) VALUES (5, 10, 1)")
    conn.commit()

    # Calling resolve_or_create_chat on existing shadow chat MUST NOT unhide it
    cid = increment_merger.resolve_or_create_chat(conn, 10, logger, {})
    assert cid == 5
    chat_hidden = cur.execute("SELECT hidden FROM chat WHERE _id = 5").fetchone()[0]
    assert chat_hidden == 1

    # 2. Brand new chat insertion defaults to hidden = 0 per overrides.md rule
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (20, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
    )
    conn.commit()
    new_cid = increment_merger.resolve_or_create_chat(conn, 20, logger, {})
    new_hidden = cur.execute("SELECT hidden FROM chat WHERE _id = ?", (new_cid,)).fetchone()[0]
    assert new_hidden == 0

    # 3. Brand new group chat insertion defaults to hidden = 0
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (30, '120363123456', 'g.us', '120363123456@g.us')"
    )
    conn.commit()
    grp_cid = increment_merger.resolve_or_create_chat(conn, 30, logger, {})
    grp_hidden = cur.execute("SELECT hidden FROM chat WHERE _id = ?", (grp_cid,)).fetchone()[0]
    assert grp_hidden == 0
    conn.close()


def test_step6b_phantom_and_shadow_cleanup(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_cleanup.db")
    cur = conn.cursor()

    # 1. Shadow chat 10 with hidden = 0 and a phantom message (type = 0, text_data = NULL)
    cur.execute("ALTER TABLE chat ADD COLUMN display_message_row_id INTEGER")
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (10, '393926739354', 's.whatsapp.net', '393926739354@s.whatsapp.net')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (5, 10, 0, 1000, 1)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) VALUES (1, 5, 0, 'PHANTOM_KEY', 1000, 0, NULL, 0)"
    )

    # 2. Group chat 20 with hidden = 0 and a system message (type = 7) - must NOT be hidden
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (20, '120363123456', 'g.us', '120363123456@g.us')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (6, 20, 0, 2000, 2)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) VALUES (2, 6, 0, 'GRP_SYS_KEY', 2000, 7, NULL, 0)"
    )

    # 3. Valid conversational chat 30 with hidden = 0 and valid text message - must NOT be hidden
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (30, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (7, 30, 0, 3000, 3)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) VALUES (3, 7, 0, 'VALID_KEY', 3000, 0, 'Hello!', 0)"
    )

    # 4. Valid 1-to-1 chat with poll (type = 66) - must NOT be hidden
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (40, '15559876543', 's.whatsapp.net', '15559876543@s.whatsapp.net')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (8, 40, 0, 4000, 4)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) VALUES (4, 8, 0, 'POLL_KEY', 4000, 66, 'What to eat?', 0)"
    )

    # 5. Valid 1-to-1 chat with sticker (type = 20) - must NOT be hidden
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (50, '15551112233', 's.whatsapp.net', '15551112233@s.whatsapp.net')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (9, 50, 0, 5000, 5)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) VALUES (5, 9, 0, 'STICKER_KEY', 5000, 20, NULL, 0)"
    )
    conn.commit()

    # Run the Step 6b cleanup SQL commands directly on conn
    has_mm = bool(
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media'"
        ).fetchone()
    )
    has_mv = bool(
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_vcard'"
        ).fetchone()
    )
    del_clauses = ["message_type = 0", "(text_data IS NULL OR TRIM(text_data) = '')"]
    if has_mm:
        del_clauses.append("_id NOT IN (SELECT message_row_id FROM message_media)")
    if has_mv:
        del_clauses.append("_id NOT IN (SELECT message_row_id FROM message_vcard)")
    cur.execute(f"DELETE FROM message WHERE {' AND '.join(del_clauses)}")

    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info('chat')").fetchall()}
    if "hidden" in chat_cols:
        subj_clause = "AND (subject IS NULL OR subject = '')" if "subject" in chat_cols else ""
        mm_clause = (
            "OR EXISTS (SELECT 1 FROM message_media mm WHERE mm.message_row_id = m._id)"
            if has_mm
            else ""
        )
        cur.execute(
            f"""
            UPDATE chat
            SET hidden = 1
            WHERE (hidden = 0 OR hidden IS NULL)
              {subj_clause}
              AND jid_row_id NOT IN (
                  SELECT _id FROM jid WHERE server = 'g.us' OR raw_string LIKE '%@g.us'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM message m
                  WHERE m.chat_row_id = chat._id
                    AND (
                        (m.message_type = 0 AND m.text_data IS NOT NULL AND TRIM(m.text_data) != '')
                        OR m.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 25, 28, 42, 43, 64, 66, 81, 82)
                        {mm_clause}
                    )
              )
            """
        )
        if "display_message_row_id" in chat_cols:
            cur.execute(
                f"""
                UPDATE chat
                SET display_message_row_id = NULL
                WHERE hidden = 1
                  AND display_message_row_id IS NOT NULL
                  {subj_clause}
                  AND jid_row_id NOT IN (
                      SELECT _id FROM jid WHERE server = 'g.us' OR raw_string LIKE '%@g.us'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM message m
                      WHERE m.chat_row_id = chat._id
                        AND (
                            (m.message_type = 0 AND m.text_data IS NOT NULL AND TRIM(m.text_data) != '')
                            OR m.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 25, 28, 42, 43, 64, 66, 81, 82)
                            {mm_clause}
                        )
                  )
                """
            )
    conn.commit()

    # Chat 5 (1-to-1 with phantom message):
    # Phantom message deleted
    assert cur.execute("SELECT 1 FROM message WHERE _id = 1").fetchone() is None
    # Chat re-hidden and display_message_row_id cleared
    c5 = cur.execute("SELECT hidden, display_message_row_id FROM chat WHERE _id = 5").fetchone()
    assert c5[0] == 1
    assert c5[1] is None

    # Chat 6 (group chat):
    # Message 2 remains
    assert cur.execute("SELECT 1 FROM message WHERE _id = 2").fetchone() is not None
    # Chat remains hidden = 0
    c6 = cur.execute("SELECT hidden, display_message_row_id FROM chat WHERE _id = 6").fetchone()
    assert c6[0] == 0
    assert c6[1] == 2

    # Chat 7 (valid conversational 1-to-1 chat):
    # Message 3 remains
    assert cur.execute("SELECT 1 FROM message WHERE _id = 3").fetchone() is not None
    # Chat remains hidden = 0
    c7 = cur.execute("SELECT hidden, display_message_row_id FROM chat WHERE _id = 7").fetchone()
    assert c7[0] == 0
    assert c7[1] == 3

    # Chat 8 (1-to-1 with poll):
    assert cur.execute("SELECT 1 FROM message WHERE _id = 4").fetchone() is not None
    c8 = cur.execute("SELECT hidden, display_message_row_id FROM chat WHERE _id = 8").fetchone()
    assert c8[0] == 0
    assert c8[1] == 4

    # Chat 9 (1-to-1 with sticker):
    assert cur.execute("SELECT 1 FROM message WHERE _id = 5").fetchone() is not None
    c9 = cur.execute("SELECT hidden, display_message_row_id FROM chat WHERE _id = 9").fetchone()
    assert c9[0] == 0
    assert c9[1] == 5
    conn.close()


def test_decode_wmi_embedded_reactions_tag41():
    """Verify decode_wmi extracts repeated reactions from WMI Tag 41."""
    # Build reaction 1 (incoming from participant in group)
    rx1_key = (
        _build_field(1, 2, b"12345678@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"RX_KEY_1")
        + _build_field(4, 2, b"5550200@s.whatsapp.net")
    )
    rx1_bytes = (
        _build_field(1, 2, rx1_key)
        + _build_field(2, 2, "👍".encode())
        + _build_field(4, 0, 1729000150000)
    )

    # Build reaction 2 (outgoing from local user)
    rx2_key = (
        _build_field(1, 2, b"12345678@g.us")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"RX_KEY_2")
    )
    rx2_bytes = (
        _build_field(1, 2, rx2_key)
        + _build_field(2, 2, "❤️".encode())
        + _build_field(4, 0, 1729000160000)
    )

    # Outer message carrying tag 41
    key_bytes = (
        _build_field(1, 2, b"12345678@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"PARENT_KEY_WMI")
        + _build_field(4, 2, b"5550100@s.whatsapp.net")
    )
    msg_bytes = _build_field(1, 2, b"Parent message text")
    wmi_bytes = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000100)
        + _build_field(41, 2, rx1_bytes)
        + _build_field(41, 2, rx2_bytes)
    )

    decoded = increment_merger.decode_wmi(wmi_bytes)
    assert decoded["key_id"] == "PARENT_KEY_WMI"
    assert decoded["text_data"] == "Parent message text"
    assert len(decoded["embedded_reactions"]) == 2

    rx1 = decoded["embedded_reactions"][0]
    assert isinstance(rx1, increment_merger.ReactionRecord)
    assert rx1.reaction_key_id == "RX_KEY_1"
    assert rx1.emoji == "👍"
    assert rx1.is_revocation is False
    assert rx1["reaction_key_id"] == "RX_KEY_1"
    assert rx1["emoji"] == "👍"
    assert rx1["from_me"] == 0
    assert rx1["participant"] == "5550200@s.whatsapp.net"
    assert rx1["sender_ts"] == 1729000150000

    rx2 = decoded["embedded_reactions"][1]
    assert isinstance(rx2, increment_merger.ReactionRecord)
    assert rx2.reaction_key_id == "RX_KEY_2"
    assert rx2.emoji == "❤️"
    assert rx2.is_revocation is False
    assert rx2["reaction_key_id"] == "RX_KEY_2"
    assert rx2["emoji"] == "❤️"
    assert rx2["from_me"] == 1
    assert rx2["sender_ts"] == 1729000160000


def test_decode_wmi_reaction_tags_16_and_56():
    """Verify decode_wmi supports reactionMessage tags 16 and 56."""
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"TARGET_MSG_123")
    )
    rx_payload = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, "🔥".encode())
        + _build_field(3, 0, 1729000200000)
    )

    # Test Tag 16
    wmi_tag16 = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 1)
            + _build_field(3, 2, b"REACT_16"),
        )
        + _build_field(2, 2, _build_field(16, 2, rx_payload))
        + _build_field(3, 0, 1729000200)
    )
    dec16 = increment_merger.decode_wmi(wmi_tag16)
    assert dec16["reaction_info"] is not None
    assert dec16["reaction_info"]["target_key_id"] == "TARGET_MSG_123"
    assert dec16["reaction_info"]["emoji"] == "🔥"
    assert dec16["reaction_info"]["sender_ts"] == 1729000200000

    # Test Tag 56 (encReactionMessage)
    wmi_tag56 = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 1)
            + _build_field(3, 2, b"REACT_56"),
        )
        + _build_field(2, 2, _build_field(56, 2, rx_payload))
        + _build_field(3, 0, 1729000200)
    )
    dec56 = increment_merger.decode_wmi(wmi_tag56)
    assert dec56["reaction_info"] is not None
    assert dec56["reaction_info"]["target_key_id"] == "TARGET_MSG_123"
    assert dec56["reaction_info"]["emoji"] == "🔥"


def test_apply_messages_bin_embedded_reactions_replay(tmp_path):
    """Verify apply_messages_bin applies Tag 41 embedded reactions into message_add_on."""
    conn = _make_two_pass_test_db(tmp_path / "test_embedded_rx.db")
    bin_path = str(tmp_path / "messages.bin")

    rx_key = (
        _build_field(1, 2, b"120363000@g.us")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"RX_KEY_STANZA")
        + _build_field(4, 2, b"5550300@s.whatsapp.net")
    )
    rx_bytes = (
        _build_field(1, 2, rx_key)
        + _build_field(2, 2, "🎉".encode())
        + _build_field(4, 0, 1729000120000)
    )
    wmi_parent = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"120363000@g.us")
            + _build_field(2, 0, 0)
            + _build_field(3, 2, b"MSG_WITH_REACTION")
            + _build_field(4, 2, b"5550100@s.whatsapp.net"),
        )
        + _build_field(2, 2, _build_field(1, 2, b"Hooray!"))
        + _build_field(3, 0, 1729000100)
        + _build_field(41, 2, rx_bytes)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 1

    cur = conn.cursor()
    parent_id = cur.execute(
        "SELECT _id FROM message WHERE key_id = 'MSG_WITH_REACTION'"
    ).fetchone()[0]

    rx_rows = cur.execute(
        """
        SELECT ao.parent_message_row_id, r.reaction, r.sender_timestamp, ao.key_id, ao.from_me
        FROM message_add_on ao
        JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
        WHERE ao.parent_message_row_id = ?
        """,
        (parent_id,),
    ).fetchall()

    assert len(rx_rows) == 1
    assert rx_rows[0][0] == parent_id
    assert rx_rows[0][1] == "🎉"
    assert rx_rows[0][2] == 1729000120000
    assert rx_rows[0][3] == "RX_KEY_STANZA"
    assert rx_rows[0][4] == 0
    conn.close()


def test_apply_messages_bin_embedded_reactions_native_type_56(tmp_path):
    """Verify apply_messages_bin writes message_add_on_type = 56 when host is real WhatsApp DB."""
    conn = _make_two_pass_test_db(tmp_path / "test_native_56.db")
    # Simulate real WhatsApp DB props
    conn.execute("CREATE TABLE props (_id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT)")
    conn.execute(
        "INSERT INTO props (key, value) VALUES ('msgtore_db_schema_version', '1050680913')"
    )
    conn.commit()

    bin_path = str(tmp_path / "messages.bin")
    rx_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"RX_56_KEY")
    )
    rx_bytes = (
        _build_field(1, 2, rx_key)
        + _build_field(2, 2, "👏".encode())
        + _build_field(4, 0, 1729000110000)
    )
    wmi = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 1)
            + _build_field(3, 2, b"MSG_TYPE_56"),
        )
        + _build_field(2, 2, _build_field(1, 2, b"Great job!"))
        + _build_field(3, 0, 1729000100)
        + _build_field(41, 2, rx_bytes)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi)) + wmi)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT ao.message_add_on_type, r.reaction
        FROM message_add_on ao
        JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
        WHERE ao.key_id = 'RX_56_KEY'
        """
    ).fetchone()

    assert row is not None
    assert row[0] == 56
    assert row[1] == "👏"
    conn.close()


def test_apply_messages_bin_embedded_reaction_revocation(tmp_path):
    """Verify that an embedded reaction with empty string revokes previously added reaction."""
    conn = _make_two_pass_test_db(tmp_path / "test_revocation.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 1: message with thumbs up reaction
    rx_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"RX_REVOKE_KEY")
    )
    rx_add = (
        _build_field(1, 2, rx_key)
        + _build_field(2, 2, "👍".encode())
        + _build_field(4, 0, 1729000100000)
    )
    wmi_add = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 1)
            + _build_field(3, 2, b"MSG_REVOKE_PARENT"),
        )
        + _build_field(2, 2, _build_field(1, 2, b"Message to react and revoke"))
        + _build_field(3, 0, 1729000100)
        + _build_field(41, 2, rx_add)
    )

    # Frame 2: message with empty string reaction (revocation)
    rx_clear = (
        _build_field(1, 2, rx_key) + _build_field(2, 2, b"") + _build_field(4, 0, 1729000150000)
    )
    wmi_clear = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net")
            + _build_field(2, 0, 1)
            + _build_field(3, 2, b"MSG_REVOKE_PARENT"),
        )
        + _build_field(2, 2, _build_field(1, 2, b"Message to react and revoke"))
        + _build_field(3, 0, 1729000100)
        + _build_field(41, 2, rx_clear)
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_add)) + wmi_add)
        f.write(_encode_varint(len(wmi_clear)) + wmi_clear)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    # Reactions must be cleared
    count = cur.execute("SELECT COUNT(*) FROM message_add_on_reaction").fetchone()[0]
    assert count == 0
    count_ao = cur.execute("SELECT COUNT(*) FROM message_add_on").fetchone()[0]
    assert count_ao == 0
    conn.close()


def test_standalone_reaction_does_not_insert_dummy_message(tmp_path):
    """Verify that standalone reaction messages do not insert fake text bubbles into message table."""
    conn = _make_two_pass_test_db(tmp_path / "test_no_dummy_msg.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Parent message
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "PARENT_NO_DUMMY", "Authentic chat message", 0, 1729000100
    )
    # Frame 1: Standalone reaction message
    wmi_react = _build_reaction_wmi(
        "5550100@s.whatsapp.net", "REACT_STANDALONE", "PARENT_NO_DUMMY", "🔥", 1729000200
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)
        f.write(_encode_varint(len(wmi_react)) + wmi_react)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 2

    cur = conn.cursor()
    # Parent message must exist
    parent_row = cur.execute(
        "SELECT _id, text_data FROM message WHERE key_id = 'PARENT_NO_DUMMY'"
    ).fetchone()
    assert parent_row is not None
    assert parent_row[1] == "Authentic chat message"

    # Reaction must exist in message_add_on
    rx_row = cur.execute(
        "SELECT r.reaction FROM message_add_on ao JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id WHERE ao.parent_message_row_id = ?",
        (parent_row[0],),
    ).fetchone()
    assert rx_row is not None
    assert rx_row[0] == "🔥"

    # Standalone reaction key REACT_STANDALONE must NOT exist in message table
    assert cur.execute("SELECT 1 FROM message WHERE key_id = 'REACT_STANDALONE'").fetchone() is None
    # Only 1 authentic message exists in the message table
    assert cur.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1
    conn.close()


def _build_reaction_wmi_with_from_me(
    remote_jid: str,
    react_key_id: str,
    target_key_id: str,
    emoji: str,
    from_me: int,
    timestamp: int,
) -> bytes:
    target_key = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, 1 - from_me)
        + _build_field(3, 2, target_key_id.encode("utf-8"))
    )
    reaction_bytes = (
        _build_field(1, 2, target_key)
        + _build_field(2, 2, emoji.encode("utf-8"))
        + _build_field(4, 0, timestamp * 1000)
    )
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(2, 0, from_me)
            + _build_field(3, 2, react_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(46, 2, reaction_bytes))
        + _build_field(3, 0, timestamp)
    )


def _build_poll_vote_wmi_with_from_me(
    remote_jid: str,
    vote_key_id: str,
    poll_key_id: str,
    timestamp: int,
    sender_ts_ms: int,
    from_me: int,
) -> bytes:
    target_key = (
        _build_field(1, 2, remote_jid.encode("utf-8"))
        + _build_field(2, 0, 1)
        + _build_field(3, 2, poll_key_id.encode("utf-8"))
    )
    vote_bytes = _build_field(1, 2, target_key) + _build_field(3, 0, sender_ts_ms)
    return (
        _build_field(
            1,
            2,
            _build_field(1, 2, remote_jid.encode("utf-8"))
            + _build_field(2, 0, from_me)
            + _build_field(3, 2, vote_key_id.encode("utf-8")),
        )
        + _build_field(2, 2, _build_field(50, 2, vote_bytes))
        + _build_field(3, 0, timestamp)
    )


def test_1on1_bidirectional_reactions_preserved(tmp_path):
    """Verify that contact and local user reactions in 1-on-1 chats coexist and do not overwrite each other."""
    conn = _make_two_pass_test_db(tmp_path / "test_1on1_rx.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Parent message
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "PARENT_1ON1_RX", "Chatting 1-on-1", 0, 1729000100
    )
    # Frame 1: Contact reaction (from_me = 0)
    wmi_contact_rx = _build_reaction_wmi_with_from_me(
        "5550100@s.whatsapp.net", "RX_CONTACT_1", "PARENT_1ON1_RX", "👍", 0, 1729000200
    )
    # Frame 2: Local user reaction (from_me = 1)
    wmi_local_rx = _build_reaction_wmi_with_from_me(
        "5550100@s.whatsapp.net", "RX_LOCAL_1", "PARENT_1ON1_RX", "❤️", 1, 1729000250
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)
        f.write(_encode_varint(len(wmi_contact_rx)) + wmi_contact_rx)
        f.write(_encode_varint(len(wmi_local_rx)) + wmi_local_rx)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    parent_id = cur.execute("SELECT _id FROM message WHERE key_id = 'PARENT_1ON1_RX'").fetchone()[0]

    rows = cur.execute(
        "SELECT ao.from_me, ao.sender_jid_row_id, r.reaction "
        "FROM message_add_on ao "
        "JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id "
        "WHERE ao.parent_message_row_id = ? "
        "ORDER BY ao.from_me",
        (parent_id,),
    ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] == 0
    assert rows[0][1] == -1
    assert rows[0][2] == "👍"
    assert rows[1][0] == 1
    assert rows[1][1] == -1
    assert rows[1][2] == "❤️"

    # Step B: Contact updates reaction to fire emoji
    bin_path_2 = str(tmp_path / "messages_2.bin")
    wmi_contact_rx_update = _build_reaction_wmi_with_from_me(
        "5550100@s.whatsapp.net", "RX_CONTACT_2", "PARENT_1ON1_RX", "🔥", 0, 1729000300
    )
    with open(bin_path_2, "wb") as f:
        f.write(_encode_varint(len(wmi_contact_rx_update)) + wmi_contact_rx_update)

    increment_merger.apply_messages_bin(conn, bin_path_2, logger, {}, {}, schema_cache)
    conn.commit()

    rows2 = cur.execute(
        "SELECT ao.from_me, r.reaction "
        "FROM message_add_on ao "
        "JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id "
        "WHERE ao.parent_message_row_id = ? "
        "ORDER BY ao.from_me",
        (parent_id,),
    ).fetchall()

    assert len(rows2) == 2
    assert rows2[0][0] == 0
    assert rows2[0][1] == "🔥"
    assert rows2[1][0] == 1
    assert rows2[1][1] == "❤️"

    # Step C: Contact revokes reaction (empty emoji)
    bin_path_3 = str(tmp_path / "messages_3.bin")
    wmi_contact_revoke = _build_reaction_wmi_with_from_me(
        "5550100@s.whatsapp.net", "RX_CONTACT_3", "PARENT_1ON1_RX", "", 0, 1729000350
    )
    with open(bin_path_3, "wb") as f:
        f.write(_encode_varint(len(wmi_contact_revoke)) + wmi_contact_revoke)

    increment_merger.apply_messages_bin(conn, bin_path_3, logger, {}, {}, schema_cache)
    conn.commit()

    rows3 = cur.execute(
        "SELECT ao.from_me, r.reaction "
        "FROM message_add_on ao "
        "JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id "
        "WHERE ao.parent_message_row_id = ?",
        (parent_id,),
    ).fetchall()

    assert len(rows3) == 1
    assert rows3[0][0] == 1
    assert rows3[0][1] == "❤️"
    conn.close()


def test_1on1_bidirectional_poll_votes_preserved(tmp_path):
    """Verify that contact and local user poll votes in 1-on-1 chats coexist."""
    conn = _make_two_pass_test_db(tmp_path / "test_1on1_poll_votes.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Poll creation message
    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_1ON1_MSG", "Dinner plans?", ["Pasta", "Steak"], 1729000100
    )
    # Frame 1: Contact vote (from_me = 0)
    wmi_contact_vote = _build_poll_vote_wmi_with_from_me(
        "5550100@s.whatsapp.net", "VOTE_1ON1_CONTACT", "POLL_1ON1_MSG", 1729000200, 1729000200000, 0
    )
    # Frame 2: Local user vote (from_me = 1)
    wmi_local_vote = _build_poll_vote_wmi_with_from_me(
        "5550100@s.whatsapp.net", "VOTE_1ON1_LOCAL", "POLL_1ON1_MSG", 1729000250, 1729000250000, 1
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_contact_vote)) + wmi_contact_vote)
        f.write(_encode_varint(len(wmi_local_vote)) + wmi_local_vote)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    cur = conn.cursor()
    poll_id = cur.execute("SELECT _id FROM message WHERE key_id = 'POLL_1ON1_MSG'").fetchone()[0]

    votes = cur.execute(
        "SELECT ao.from_me, ao.sender_jid_row_id, ao.key_id "
        "FROM message_add_on ao "
        "JOIN message_add_on_poll_vote v ON v.message_add_on_row_id = ao._id "
        "WHERE ao.parent_message_row_id = ? "
        "ORDER BY ao.from_me",
        (poll_id,),
    ).fetchall()

    assert len(votes) == 2
    assert votes[0][0] == 0
    assert votes[0][1] == -1
    assert votes[0][2] == "VOTE_1ON1_CONTACT"
    assert votes[1][0] == 1
    assert votes[1][1] == -1
    assert votes[1][2] == "VOTE_1ON1_LOCAL"
    conn.close()


def test_standalone_addons_zero_phantom_messages(tmp_path):
    """Verify that standalone pin, poll vote, and edit frames do not insert phantom messages."""
    conn = _make_two_pass_test_db(tmp_path / "test_no_phantoms.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Parent message
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "PARENT_CLEAN", "Authentic parent message", 0, 1729000100
    )
    # Frame 1: Standalone pin (tag 63)
    wmi_pin = _build_pin_wmi(
        "5550100@s.whatsapp.net", "PIN_STANDALONE", "PARENT_CLEAN", 1, 1729000200
    )
    # Frame 2: Standalone poll creation
    wmi_poll = _build_poll_creation_wmi(
        "5550100@s.whatsapp.net", "POLL_CLEAN", "Meeting time?", ["10am", "2pm"], 1729000250
    )
    # Frame 3: Standalone poll vote (tag 50)
    wmi_vote = _build_poll_vote_wmi(
        "5550100@s.whatsapp.net", "VOTE_STANDALONE", "POLL_CLEAN", 1729000300, 1729000300000
    )
    # Frame 4: Standalone edit (tag 12)
    wmi_edit = _build_edit_wmi(
        "5550100@s.whatsapp.net",
        "EDIT_STANDALONE",
        "PARENT_CLEAN",
        "Edited authentic message",
        1729000350,
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_parent)) + wmi_parent)
        f.write(_encode_varint(len(wmi_pin)) + wmi_pin)
        f.write(_encode_varint(len(wmi_poll)) + wmi_poll)
        f.write(_encode_varint(len(wmi_vote)) + wmi_vote)
        f.write(_encode_varint(len(wmi_edit)) + wmi_edit)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    try:
        cur = conn.cursor()
        msg_rows = cur.execute("SELECT _id, key_id, message_type FROM message").fetchall()
        # Exactly 2 authentic messages exist in message table: parent message and poll creation
        assert len(msg_rows) == 2, f"Expected 2 messages, found {len(msg_rows)}: {msg_rows}"

        # None of the standalone add-on keys exist in message table
        assert (
            cur.execute("SELECT 1 FROM message WHERE key_id = 'PIN_STANDALONE'").fetchone() is None
        )
        assert (
            cur.execute("SELECT 1 FROM message WHERE key_id = 'VOTE_STANDALONE'").fetchone() is None
        )
        assert (
            cur.execute("SELECT 1 FROM message WHERE key_id = 'EDIT_STANDALONE'").fetchone() is None
        )

        # Add-on tables are populated
        assert cur.execute("SELECT COUNT(*) FROM message_add_on_pin_in_chat").fetchone()[0] == 1
        assert cur.execute("SELECT COUNT(*) FROM message_add_on_poll_vote").fetchone()[0] == 1
        assert cur.execute("SELECT COUNT(*) FROM message_edit_info").fetchone()[0] == 1

        # Edited text is reflected on parent
        edited_text = cur.execute(
            "SELECT text_data FROM message WHERE key_id = 'PARENT_CLEAN'"
        ).fetchone()[0]
        assert edited_text == "Edited authentic message"
    finally:
        conn.close()


def test_surrogate_churn_reparents_auxiliary_tables(tmp_path):
    """Verify that surrogate ID changes cascade re-parenting across auxiliary tables."""
    conn = _make_two_pass_test_db(tmp_path / "test_churn.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_media (
            message_row_id INTEGER PRIMARY KEY,
            chat_row_id INTEGER,
            from_me INTEGER,
            key_id TEXT,
            sender_jid_row_id INTEGER,
            file_path TEXT,
            file_size INTEGER
        );
        CREATE TABLE IF NOT EXISTS message_quoted (
            message_row_id INTEGER PRIMARY KEY,
            chat_row_id INTEGER NOT NULL,
            parent_message_chat_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER,
            message_type INTEGER,
            text_data TEXT
        );
        CREATE TABLE IF NOT EXISTS receipt_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            receipt_user_jid_row_id INTEGER NOT NULL,
            receipt_timestamp INTEGER,
            read_timestamp INTEGER,
            played_timestamp INTEGER
        );
    """)

    cur = conn.cursor()
    # Insert existing message row
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type, status) "
        "VALUES (10, 1, 1, 'MSG_CHURN_TARGET', -1, 1729000100, 'Existing chat text', 0, 4)"
    )
    # Insert auxiliary records referencing _id = 10
    cur.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, from_me, key_id, sender_jid_row_id, file_path, file_size) "
        "VALUES (10, 1, 1, 'MSG_CHURN_TARGET', -1, 'Media/WhatsApp Images/photo.jpg', 12345)"
    )
    cur.execute(
        "INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, key_id, text_data) "
        "VALUES (10, 1, 1, 0, 'QUOTED_STUB', 'Quoted snippet')"
    )
    cur.execute(
        "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, read_timestamp) "
        "VALUES (10, 1, 1729000555)"
    )
    conn.commit()

    # Replay updated WMI frame with updated status
    bin_path = str(tmp_path / "messages.bin")
    wmi_update = _build_text_wmi(
        "5550100@s.whatsapp.net", "MSG_CHURN_TARGET", "Existing chat text", 1, 1729000100
    )
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_update)) + wmi_update)

    schema_cache = {}
    increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    new_row = cur.execute("SELECT _id FROM message WHERE key_id = 'MSG_CHURN_TARGET'").fetchone()
    assert new_row is not None
    new_id = new_row[0]
    assert new_id != 10

    # Auxiliary tables re-pointed to new_id
    media_path = cur.execute(
        "SELECT file_path FROM message_media WHERE message_row_id = ?", (new_id,)
    ).fetchone()
    assert media_path is not None
    assert media_path[0] == "Media/WhatsApp Images/photo.jpg"

    quoted_key = cur.execute(
        "SELECT key_id FROM message_quoted WHERE message_row_id = ?", (new_id,)
    ).fetchone()
    assert quoted_key is not None
    assert quoted_key[0] == "QUOTED_STUB"

    read_ts = cur.execute(
        "SELECT read_timestamp FROM receipt_user WHERE message_row_id = ?", (new_id,)
    ).fetchone()
    assert read_ts is not None
    assert read_ts[0] == 1729000555

    # No orphaned records pointing to old id 10
    assert (
        cur.execute("SELECT COUNT(*) FROM message_media WHERE message_row_id = 10").fetchone()[0]
        == 0
    )
    assert (
        cur.execute("SELECT COUNT(*) FROM message_quoted WHERE message_row_id = 10").fetchone()[0]
        == 0
    )
    assert (
        cur.execute("SELECT COUNT(*) FROM receipt_user WHERE message_row_id = 10").fetchone()[0]
        == 0
    )
    conn.close()


def test_chat_consolidation_reparents_add_ons(mock_db):
    """Verify that chat consolidation and step D re-point message_add_on chat and sender IDs."""
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    if "hidden" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN hidden INTEGER DEFAULT 0")
    if "sort_timestamp" not in chat_cols:
        conn.execute("ALTER TABLE chat ADD COLUMN sort_timestamp INTEGER")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jid_map (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_add_on (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_row_id INTEGER,
            parent_message_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            key_id TEXT NOT NULL,
            sender_jid_row_id INTEGER,
            timestamp INTEGER NOT NULL,
            status INTEGER DEFAULT 0,
            message_add_on_type INTEGER NOT NULL
        )
    """)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "device" not in cols:
        conn.execute("ALTER TABLE jid ADD COLUMN device INTEGER DEFAULT 0")

    # Primary phone JID and chat
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550888', 's.whatsapp.net', 0, '5550888@s.whatsapp.net')"
    )
    phone_jid = cur.lastrowid
    cur.execute("INSERT INTO chat (jid_row_id, hidden) VALUES (?, 0)", (phone_jid,))
    phone_chat = cur.lastrowid

    # Shadow duplicate chat for same natural identity
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('5550888', 's.whatsapp.net', 0, '5550888.0:0@s.whatsapp.net')"
    )
    dup_jid = cur.lastrowid
    cur.execute("INSERT INTO chat (jid_row_id, hidden) VALUES (?, 1)", (dup_jid,))
    dup_chat = cur.lastrowid

    # Primary and companion LID senders
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('999000888', 'lid', 0, '999000888@lid')"
    )
    primary_lid = cur.lastrowid
    cur.execute(
        "INSERT INTO jid (user, server, device, raw_string) VALUES ('999000888', 'lid', 1, '999000888.1:0@lid')"
    )
    comp_lid = cur.lastrowid

    # Messages in chats
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (80, ?, 0, 'BASE_MSG', ?, 1700000000, 'base', 0)",
        (phone_chat, phone_jid),
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (81, ?, 0, 'DUP_MSG', ?, 1700000100, 'dup', 0)",
        (dup_chat, dup_jid),
    )

    # Add-on pointing to shadow duplicate chat and companion sender
    cur.execute(
        "INSERT INTO message_add_on (_id, chat_row_id, from_me, key_id, sender_jid_row_id, parent_message_row_id, message_add_on_type, timestamp) "
        "VALUES (100, ?, 0, 'AO_KEY', ?, 80, 56, 1700000200)",
        (dup_chat, comp_lid),
    )
    conn.commit()

    increment_merger.migrate_restored_backup_to_lid(conn, logger)

    ao = cur.execute(
        "SELECT chat_row_id, sender_jid_row_id FROM message_add_on WHERE _id = 100"
    ).fetchone()
    assert ao is not None
    # chat_row_id re-pointed to phone_chat
    assert ao[0] == phone_chat
    # sender_jid_row_id re-pointed to primary_lid
    assert ao[1] == primary_lid
    conn.close()


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


def test_message_deletions_full_cascade(tmp_path):
    """Verify that apply_message_deletions purges all auxiliary tables."""
    conn = _make_two_pass_test_db(tmp_path / "test_del_cascade.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_text (message_row_id INTEGER PRIMARY KEY, text_data TEXT);
        CREATE TABLE IF NOT EXISTS message_thumbnail (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB);
        CREATE TABLE IF NOT EXISTS message_view_once_media (message_row_id INTEGER PRIMARY KEY, state INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS message_revoked (message_row_id INTEGER PRIMARY KEY, revoked_key_id TEXT NOT NULL, admin_jid_row_id INTEGER, revoke_timestamp INTEGER);
        CREATE TABLE IF NOT EXISTS message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER NOT NULL);
    """)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (99, 1, 0, 'DEL_TEST_KEY', -1, 1729000100, 'To be deleted', 0)"
    )
    cur.execute("INSERT INTO message_text (message_row_id, text_data) VALUES (99, 'To be deleted')")
    cur.execute(
        "INSERT INTO message_thumbnail (message_row_id, thumbnail) VALUES (99, X'89504E47')"
    )
    cur.execute("INSERT INTO message_view_once_media (message_row_id, state) VALUES (99, 1)")
    cur.execute(
        "INSERT INTO message_revoked (message_row_id, revoked_key_id, admin_jid_row_id, revoke_timestamp) VALUES (99, 'REVOKED_KEY', 1, 1729000200)"
    )
    cur.execute("INSERT INTO message_system (message_row_id, action_type) VALUES (99, 5)")
    conn.commit()

    deleted_json = tmp_path / "deletedMessages_1.json"
    deleted_json.write_text(json.dumps({"deleted_ids": [99]}), encoding="utf-8")

    schema_cache = {}
    deleted_count = increment_merger.apply_message_deletions(
        conn, str(deleted_json), logger, schema_cache, None, None
    )
    conn.commit()

    assert deleted_count == 1
    assert cur.execute("SELECT 1 FROM message WHERE _id = 99").fetchone() is None
    assert cur.execute("SELECT 1 FROM message_text WHERE message_row_id = 99").fetchone() is None
    assert (
        cur.execute("SELECT 1 FROM message_thumbnail WHERE message_row_id = 99").fetchone() is None
    )
    assert (
        cur.execute("SELECT 1 FROM message_view_once_media WHERE message_row_id = 99").fetchone()
        is None
    )
    assert cur.execute("SELECT 1 FROM message_revoked WHERE message_row_id = 99").fetchone() is None
    assert cur.execute("SELECT 1 FROM message_system WHERE message_row_id = 99").fetchone() is None
    conn.close()
