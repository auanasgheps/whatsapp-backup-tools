"""Tests for wab_archiver.increment_merger."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import zipfile

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
    _build_quoted_wmi,
    _build_wmi_with_receipts,
)
from wab_archiver import increment_merger

_encode_varint = encode_varint
_build_field = build_field
_build_text_wmi = build_text_wmi
_build_image_wmi = build_image_wmi
_build_history_sync_frame = build_history_sync_frame


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
        None,
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
    assert seq >= 102

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
        None,
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
    stats = increment_merger.apply_increment_folder(
        conn, str(inc_dir), logger, None, None, None, {}, None, None
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
        conn, str(inc_dir), logger, None, None, schema_cache, {}, None, None
    )

    # props was applied, receipt_device was skipped cleanly
    assert stats["entity_modified"] == 1
    assert stats["entity_deleted"] == 0
    assert (
        conn.execute("SELECT value FROM props WHERE key = 'test_supported_key'").fetchone()[0]
        == "active"
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

    stats = increment_merger.apply_increment_folder(
        conn, str(inc_dir), logger, None, None, None, {}, None, None
    )

    # Only 601 was deleted; 700 was rejected due to sequence ceiling
    assert stats["messages_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 601").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message WHERE _id = 700").fetchone()[0] == 1
    conn.close()


def test_persistent_jid_id_map_across_multiple_increments(mock_db, tmp_path):
    """Verify jid_id_map persists across multiple increment batches and maps subsequent chats (F-07)."""
    init_conn = sqlite3.connect(mock_db)
    init_conn.execute(
        "CREATE TABLE IF NOT EXISTS call_log (_id INTEGER PRIMARY KEY, call_id TEXT, jid_row_id INTEGER)"
    )
    init_conn.commit()
    init_conn.close()

    inc1_dir = tmp_path / "inc_1"
    inc1_dir.mkdir()
    (inc1_dir / "header.json").write_text(json.dumps({"creation_date": 1729000000000}))
    jid_data = [
        {
            "_id": 77,
            "user": "5550300",
            "server": "s.whatsapp.net",
            "raw_string": "5550300@s.whatsapp.net",
        }
    ]
    (inc1_dir / "jid_modified_1.json").write_text(json.dumps(jid_data), encoding="utf-8")

    inc2_dir = tmp_path / "inc_2"
    inc2_dir.mkdir()
    (inc2_dir / "header.json").write_text(json.dumps({"creation_date": 1729000010000}))
    call_data = [{"_id": 1, "call_id": "CALL_ABC", "jid_row_id": 77}]
    (inc2_dir / "call_log_modified_1.json").write_text(json.dumps(call_data), encoding="utf-8")

    import zipfile

    inc1_zip = tmp_path / "inc_1.zip"
    with zipfile.ZipFile(inc1_zip, "w") as zf:
        for f in inc1_dir.iterdir():
            zf.write(f, arcname=f.name)

    inc2_zip = tmp_path / "inc_2.zip"
    with zipfile.ZipFile(inc2_zip, "w") as zf:
        for f in inc2_dir.iterdir():
            zf.write(f, arcname=f.name)

    out_db = tmp_path / "output.db"
    staging_dir = tmp_path / "staging"

    increment_merger.merge_increments(
        mock_db,
        [str(inc1_zip), str(inc2_zip)],
        "",
        str(out_db),
        str(staging_dir),
        logger,
        None,
    )

    conn = sqlite3.connect(out_db)
    target_jid_id = conn.execute("SELECT _id FROM jid WHERE user = '5550300'").fetchone()[0]
    call_row = conn.execute("SELECT jid_row_id FROM call_log WHERE call_id = 'CALL_ABC'").fetchone()
    assert call_row is not None
    assert call_row[0] == target_jid_id
    conn.close()


def test_apply_sequences_advances_sequence_ceiling_post_insertion(mock_db, tmp_path):
    """Verify sequence ceilings roll forward after messages are inserted."""
    conn = sqlite3.connect(mock_db)
    inc_dir = tmp_path / "inc_seq"
    inc_dir.mkdir()
    (inc_dir / "header.json").write_text(json.dumps({"creation_date": 1729000000000}))
    (inc_dir / "sequences.json").write_text(
        json.dumps({"sequences": [{"name": "message", "value": 5000}]})
    )

    wmi_bytes = _build_text_wmi(
        "123456789@s.whatsapp.net", "KEY_SEQ_TEST", "Sequence test msg", 0, 1729000000000
    )
    frame = _build_history_sync_frame("123456789@s.whatsapp.net", [wmi_bytes])
    (inc_dir / "messages.bin").write_bytes(frame)

    schema_cache = {}
    stats = increment_merger.apply_increment_folder(
        conn, str(inc_dir), logger, None, None, schema_cache, {}, None, None
    )
    conn.commit()

    assert stats["messages_inserted"] == 1
    msg_row = conn.execute("SELECT _id FROM message WHERE key_id = 'KEY_SEQ_TEST'").fetchone()
    assert msg_row is not None
    seq_val = conn.execute("SELECT seq FROM SQLITE_SEQUENCE WHERE name = 'message'").fetchone()[0]
    assert seq_val >= 5000
    conn.close()


def test_filter_active_increments_strict_generation_isolation():
    # Arrange: Non-colliding IDs across distinct generations
    paths = [
        "msgstore-increment-1-2026-10-15.1.db.crypt15",
        "msgstore-increment-2.db.crypt15",
    ]
    # Act: Select without explicit base tag
    selected = increment_merger.filter_active_increments(paths, None)
    # Assert: Active undated cycle supersedes rotated dated cycle
    assert selected == ["msgstore-increment-2.db.crypt15"]


def test_validate_increment_sequence_contiguous():
    paths = [
        "msgstore-increment-1.db.crypt15",
        "msgstore-increment-2.db.crypt15",
        "msgstore-increment-3.db.crypt15",
    ]
    res = increment_merger.validate_increment_sequence(paths, logger)
    assert res == paths


def test_validate_increment_sequence_missing_first():
    paths = [
        "msgstore-increment-2.db.crypt15",
        "msgstore-increment-3.db.crypt15",
    ]
    with pytest.raises(ValueError, match="missing starting increment 'msgstore-increment-1'"):
        increment_merger.validate_increment_sequence(paths, logger)


def test_validate_increment_sequence_gap(caplog):
    paths = [
        "msgstore-increment-1.db.crypt15",
        "msgstore-increment-2.db.crypt15",
        "msgstore-increment-4.db.crypt15",
        "msgstore-increment-5.db.crypt15",
    ]
    with caplog.at_level(logging.ERROR):
        res = increment_merger.validate_increment_sequence(paths, logger)
    # Only contiguous prefix 1..2 is returned
    assert res == [
        "msgstore-increment-1.db.crypt15",
        "msgstore-increment-2.db.crypt15",
    ]
    assert "missing increment 3" in caplog.text
    assert (
        "Subsequent increment(s) ['msgstore-increment-4.db.crypt15', 'msgstore-increment-5.db.crypt15'] cannot be applied"
        in caplog.text
    )


def test_merge_increments_atomic_staging_rollback(mock_db, tmp_path):
    output_db = str(tmp_path / "final_output.db")
    staging_dir = str(tmp_path / "staging")
    shutil.copy2(mock_db, output_db)

    # Initial snapshot checksum
    with open(output_db, "rb") as f:
        initial_bytes = f.read()

    # Pass an invalid increment that fails decryption/unpacking
    bad_inc = str(tmp_path / "msgstore-increment-1.db.crypt15")
    with open(bad_inc, "wb") as f:
        f.write(b"not a valid zip or crypt archive")

    with pytest.raises(Exception):
        increment_merger.merge_increments(
            mock_db,
            [bad_inc],
            None,
            output_db,
            staging_dir,
            logger,
            None,
        )

    # Assert .merging staging db and sidecars are completely purged
    assert not os.path.exists(output_db + ".merging")
    assert not os.path.exists(output_db + ".merging-wal")
    assert not os.path.exists(output_db + ".merging-shm")

    # Assert output_db remains untouched and identical to base
    with open(output_db, "rb") as f:
        final_bytes = f.read()
    assert final_bytes == initial_bytes


def test_merge_increments_legacy_jid_schema_detection(tmp_path):
    """Verify merge_increments handles legacy JID tables lacking raw_string column."""
    db_path = tmp_path / "legacy_msgstore.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, server TEXT NOT NULL);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY AUTOINCREMENT, jid_row_id INTEGER NOT NULL);
        CREATE TABLE message (_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL, key_id TEXT NOT NULL, sender_jid_row_id INTEGER, timestamp INTEGER NOT NULL, message_type INTEGER NOT NULL, text_data TEXT);
        CREATE TABLE props (key TEXT UNIQUE NOT NULL, value TEXT);
        INSERT INTO jid (user, server) VALUES ('5550100', 's.whatsapp.net');
        INSERT INTO chat (jid_row_id) VALUES (1);
    """)
    conn.commit()
    conn.close()

    output_db = str(tmp_path / "legacy_output.db")
    staging_dir = str(tmp_path / "staging_legacy")
    shutil.copy2(str(db_path), output_db)

    increment_merger.merge_increments(
        str(db_path),
        [],
        None,
        output_db,
        staging_dir,
        logger,
        None,
    )
    assert os.path.exists(output_db)
    chk_conn = sqlite3.connect(output_db)
    assert chk_conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    chk_conn.close()
