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
    elif wire_type == 2:
        return tag_bytes + _encode_varint(len(payload)) + payload
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
    count = increment_merger.apply_message_deletions(conn, str(del_json), logger)
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

    # 2. Staged increment was cleaned up upon successful merge
    assert not inc_file.exists()


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
