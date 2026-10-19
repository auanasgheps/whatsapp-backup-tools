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
    count = increment_merger.apply_message_deletions(conn, str(del_json), logger, None)
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
        CREATE TABLE jid (_id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, server TEXT, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY AUTOINCREMENT, jid_row_id INTEGER);
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
            parent_message_row_id INTEGER NOT NULL,
            from_me INTEGER NOT NULL,
            sender_jid_row_id INTEGER,
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
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
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


def test_cascading_message_deletions_auxiliary(tmp_path):
    db_path = tmp_path / "test_cascade.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE message (_id INTEGER PRIMARY KEY, key_id TEXT);
        CREATE TABLE message_media (message_row_id INTEGER);
        CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER);
        CREATE TABLE message_add_on_reaction (message_add_on_row_id INTEGER);
        CREATE TABLE message_add_on_pin_in_chat (message_add_on_row_id INTEGER);
        CREATE TABLE message_poll (message_row_id INTEGER);
        CREATE TABLE message_poll_option (message_row_id INTEGER);
        CREATE TABLE message_location (message_row_id INTEGER);
        CREATE TABLE message_edit_info (message_row_id INTEGER);

        INSERT INTO message (_id, key_id) VALUES (42, 'DELETE_TARGET');
        INSERT INTO message_media (message_row_id) VALUES (42);
        INSERT INTO message_add_on (_id, parent_message_row_id) VALUES (10, 42);
        INSERT INTO message_add_on_reaction (message_add_on_row_id) VALUES (10);
        INSERT INTO message_add_on (_id, parent_message_row_id) VALUES (11, 42);
        INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id) VALUES (11);
        INSERT INTO message_poll (message_row_id) VALUES (42);
        INSERT INTO message_poll_option (message_row_id) VALUES (42);
        INSERT INTO message_location (message_row_id) VALUES (42);
        INSERT INTO message_edit_info (message_row_id) VALUES (42);
    """)
    conn.commit()

    del_json = tmp_path / "deletedMessages_cascade.json"
    del_json.write_text(json.dumps({"deleted_ids": [42]}))

    schema_cache = {}
    deleted = increment_merger.apply_message_deletions(conn, str(del_json), logger, schema_cache)
    conn.commit()

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 42").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM message_media WHERE message_row_id = 42").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_add_on WHERE parent_message_row_id = 42"
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
        conn.execute("SELECT COUNT(*) FROM message_poll WHERE message_row_id = 42").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_poll_option WHERE message_row_id = 42"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_location WHERE message_row_id = 42").fetchone()[
            0
        ]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM message_edit_info WHERE message_row_id = 42").fetchone()[
            0
        ]
        == 0
    )

    conn.close()
