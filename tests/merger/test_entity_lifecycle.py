"""Tests for wab_archiver.increment_merger."""

from __future__ import annotations

import json
import logging
import os
import sqlite3

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
    _build_poll_creation_wmi,
    _build_poll_vote_wmi,
    _make_two_pass_test_db,
)
from wab_archiver import increment_merger

_encode_varint = encode_varint
_build_field = build_field
_build_text_wmi = build_text_wmi
_build_image_wmi = build_image_wmi
_build_history_sync_frame = build_history_sync_frame


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

    count = increment_merger.apply_entity_deletions(conn, "props", str(del_json), logger, {})
    conn.commit()
    assert count == 1
    conn.close()


def test_apply_message_deletions(mock_db, tmp_path):
    del_json = tmp_path / "deletedMessages_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [100]}))

    conn = sqlite3.connect(mock_db)
    count = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, None, None, None, None, None, None
    )
    conn.commit()
    assert count == 1

    remaining = conn.execute("SELECT COUNT(*) FROM message WHERE _id = 100").fetchone()[0]
    assert remaining == 0
    conn.close()


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

    count = increment_merger.apply_entity_deletions(conn, "props", str(del_json), logger, {})
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

    count = increment_merger.apply_entity_deletions(conn, "call_log", str(del_json), logger, {})
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
        conn, "group_past_participant_user", str(del_json), logger, {}
    )
    assert count == 1
    assert conn.execute("SELECT COUNT(*) FROM group_past_participant_user").fetchone()[0] == 0
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

    count = increment_merger.apply_entity_deletions(conn, "user_device", str(del_json), logger, {})
    assert count == 0
    # Row must remain intact
    assert conn.execute("SELECT COUNT(*) FROM user_device WHERE _id = 1").fetchone()[0] == 1
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
        conn, str(del_json), logger, None, 1729000000000, 500, None, None, None
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
        conn, str(del_json), logger, None, 1729000100000, 250, None, None, None
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
        conn, str(del_json), logger, None, 1729000100000, 500, None, None, None
    )
    conn.commit()

    assert count == 0
    remaining = conn.execute("SELECT COUNT(*) FROM message WHERE _id = 400").fetchone()[0]
    assert remaining == 1
    conn.close()


def test_apply_message_deletions_maps_intra_increment_offset_to_local_id(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Simulate a local DB where sequence drifted:
    # On phone, candidate was ID 1050 (offset 0 from base_seq 1049).
    # In local DB, message was assigned _id 999 because of local autoincrement drift.
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (999, 1, 0, 'KEY_INTRA_DRIFT', 1729000100000, 0, 'Intra delete msg')"
    )
    conn.commit()

    # messages.bin has frame 0 with key_id 'KEY_INTRA_DRIFT'
    wmi_bytes = _build_text_wmi(
        "5550100@s.whatsapp.net", "KEY_INTRA_DRIFT", "Intra delete msg", 0, 1729000100000
    )
    bin_path = str(tmp_path / "messages.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_bytes)) + wmi_bytes)

    # deletedMessages_1.json has candidate ID 1050 (phone sequence)
    del_json = tmp_path / "deletedMessages_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [1050]}))

    schema_cache = {}
    deleted = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, schema_cache, 1729000200000, 2000, 1049, bin_path, None
    )
    conn.commit()

    assert deleted == 1
    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 999").fetchone()[0] == 0
    conn.close()


def test_apply_message_deletions_protects_active_entity_updated_in_increment(mock_db, tmp_path):
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Pre-existing poll message 950 in database
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (950, 1, 0, 'POLL_KEY_ACTIVE', 1729000000000, 66, 'Lunch poll?')"
    )
    conn.commit()

    # messages.bin contains embedded poll votes for POLL_KEY_ACTIVE
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"POLL_KEY_ACTIVE")
    )
    edit_proto = (
        _build_field(1, 2, target_key) + _build_field(2, 0, 14) + _build_field(10, 0, 1729000100000)
    )
    wmi_poll_update = (
        _build_field(
            1,
            2,
            _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(3, 2, b"POLL_KEY_ACTIVE"),
        )
        + _build_field(2, 2, _build_field(12, 2, edit_proto))
        + _build_field(3, 0, 1729000100000)
    )
    bin_path = str(tmp_path / "messages.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_poll_update)) + wmi_poll_update)

    # deletedMessages_1.json has ID 950 (phone marked old poll row as deleted during re-indexing)
    del_json = tmp_path / "deletedMessages_1.json"
    del_json.write_text(json.dumps({"deleted_ids": [950]}))

    schema_cache = {}
    deleted = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, schema_cache, 1729000200000, 2000, 1049, bin_path, None
    )
    conn.commit()

    # Candidate 950 must be protected from deletion
    assert deleted == 0
    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 950").fetchone()[0] == 1
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
        conn, str(del_json), logger, schema_cache, 1729000500000, 600, None, None, None
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
        conn, str(del_json), logger, schema_cache, None, None, None, None, None
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


def test_is_valid_jid_string():
    # Valid JIDs
    assert increment_merger.is_valid_jid_string("5550100@s.whatsapp.net")
    assert increment_merger.is_valid_jid_string("120363000000000001@g.us")
    assert increment_merger.is_valid_jid_string("145500000000001@lid")
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
        conn, str(deleted_json), logger, schema_cache, None, None, None, None, None
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


def test_step6b_phantom_and_shadow_cleanup(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_cleanup.db")
    cur = conn.cursor()

    # 1. Shadow chat 10 with hidden = 0 and a phantom message (type = 0, text_data = NULL)
    cur.execute("ALTER TABLE chat ADD COLUMN display_message_row_id INTEGER")
    cur.execute(
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (10, '390000000010', 's.whatsapp.net', '390000000010@s.whatsapp.net')"
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


def test_chat_modified_ignores_foreign_message_pointers(mock_db, tmp_path):
    """Verify chat entity modifications do not adopt foreign phone message row IDs (F-07)."""
    conn = sqlite3.connect(mock_db)
    conn.execute("ALTER TABLE chat ADD COLUMN display_message_row_id INTEGER")
    conn.execute("ALTER TABLE chat ADD COLUMN last_message_row_id INTEGER")
    chat_data = [
        {
            "_id": 500,
            "jid_row_id": 1,
            "subject": "Pointer Chat",
            "display_message_row_id": 9999,
            "last_message_row_id": 8888,
            "last_read_message_row_id": 7777,
            "last_read_receipt_sent_message_row_id": 6666,
            "last_message_reaction_row_id": 5555,
        }
    ]
    chat_json = tmp_path / "chat_modified_1.json"
    chat_json.write_text(json.dumps(chat_data), encoding="utf-8")

    count = increment_merger.apply_entity_modifications(conn, "chat", str(chat_json), logger, {})
    conn.commit()

    assert count == 1
    row = conn.execute(
        "SELECT display_message_row_id, last_message_row_id FROM chat WHERE subject = 'Pointer Chat'"
    ).fetchone()
    assert row is not None
    assert row[0] is None or row[0] != 9999
    assert row[1] is None or row[1] != 8888
    conn.close()


def test_apply_entity_modifications_call_log_participant_v2_and_group_participants(
    mock_db, tmp_path
):
    """Verify call_log_participant_v2 and group_participants composite matching (F-09)."""
    conn = sqlite3.connect(mock_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS call_log_participant_v2 ("
        "_id INTEGER PRIMARY KEY AUTOINCREMENT, call_log_row_id INTEGER, jid_row_id INTEGER, call_result INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS group_participants ("
        "group_jid_row_id INTEGER, jid_row_id INTEGER, admin INTEGER, pending INTEGER)"
    )
    conn.commit()

    jid_map = {99: 2}
    call_part_data = [{"call_log_row_id": 10, "jid_row_id": 99, "call_result": 1}]
    call_part_json = tmp_path / "call_part.json"
    call_part_json.write_text(json.dumps(call_part_data), encoding="utf-8")

    count1 = increment_merger.apply_entity_modifications(
        conn, "call_log_participant_v2", str(call_part_json), logger, jid_map
    )
    conn.commit()
    assert count1 == 1
    row = conn.execute(
        "SELECT jid_row_id, call_result FROM call_log_participant_v2 WHERE call_log_row_id = 10"
    ).fetchone()
    assert row[0] == 2
    assert row[1] == 1

    call_part_data[0]["call_result"] = 5
    call_part_json.write_text(json.dumps(call_part_data), encoding="utf-8")
    increment_merger.apply_entity_modifications(
        conn, "call_log_participant_v2", str(call_part_json), logger, jid_map
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM call_log_participant_v2 WHERE call_log_row_id = 10"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT call_result FROM call_log_participant_v2 WHERE call_log_row_id = 10"
        ).fetchone()[0]
        == 5
    )

    gp_data = [{"group_jid_row_id": 5, "jid_row_id": 99, "admin": 0}]
    gp_json = tmp_path / "gp.json"
    gp_json.write_text(json.dumps(gp_data), encoding="utf-8")

    count2 = increment_merger.apply_entity_modifications(
        conn, "group_participants", str(gp_json), logger, jid_map
    )
    conn.commit()
    assert count2 == 1
    assert (
        conn.execute(
            "SELECT admin FROM group_participants WHERE group_jid_row_id = 5 AND jid_row_id = 2"
        ).fetchone()[0]
        == 0
    )

    gp_data[0]["admin"] = 1
    gp_json.write_text(json.dumps(gp_data), encoding="utf-8")
    increment_merger.apply_entity_modifications(
        conn, "group_participants", str(gp_json), logger, jid_map
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM group_participants WHERE group_jid_row_id = 5 AND jid_row_id = 2"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT admin FROM group_participants WHERE group_jid_row_id = 5 AND jid_row_id = 2"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_verify_message_deletion_gates_reject_drifted_ids(mock_db):
    # Arrange: DB with 1 chat (chat._id = 1)
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Message 8001 has timestamp in the future (Time Paradox)
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type) "
        "VALUES (8001, 1, 0, 'K_FUTURE', 1729010000000, 0)"
    )
    # Message 8002 references non-existent chat (chat_row_id = 9999)
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type) "
        "VALUES (8002, 9999, 0, 'K_ORPHAN', 1729000000000, 0)"
    )
    # Message 8003 is valid
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type) "
        "VALUES (8003, 1, 0, 'K_VALID', 1729000000000, 0)"
    )
    conn.commit()

    # Act
    backup_ts = 1729000000000  # 8001 ts is 1h in the future
    verified_ids, audit = increment_merger.verify_message_deletion_candidates(
        conn, [8001, 8002, 8003], backup_ts, 9000, logger, {}, None
    )

    # Assert
    assert 8003 in verified_ids
    assert 8001 not in verified_ids
    assert 8002 not in verified_ids

    audit_map = {r.msg_id: r for r in audit}
    assert audit_map[8001].rejection_reason == "TIME_PARADOX"
    assert audit_map[8002].rejection_reason == "ORPHANED_CHAT"
    conn.close()


def test_apply_entity_deletions_chat_uses_mapped_jid(mock_db, tmp_path):
    # Arrange: Target DB has chat 10 (contact 99) and chat 500 (contact 20)
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (10, 99, 'Mom Chat')")
    cur.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (500, 20, 'Spam Chat')")
    conn.commit()

    # Delta requests deleting phone chat 10, but phone chat 10 was contact 50 (mapped to local 20)
    del_json = tmp_path / "chat_deleted_1.json"
    del_json.write_text(
        json.dumps({"deleted_ids": [{"_id": 10, "jid_row_id": 50}]}),
        encoding="utf-8",
    )
    jid_id_map = {50: 20}

    # Act
    deleted = increment_merger.apply_entity_deletions(
        conn, "chat", str(del_json), logger, jid_id_map
    )
    conn.commit()

    # Assert: Only chat 500 (contact 20) was deleted; chat 10 (Mom) was preserved
    assert deleted == 1
    assert cur.execute("SELECT COUNT(*) FROM chat WHERE _id = 10").fetchone()[0] == 1
    assert cur.execute("SELECT COUNT(*) FROM chat WHERE _id = 500").fetchone()[0] == 0
    conn.close()


def test_apply_message_deletions_resolves_order_id_from_proto_frame(mock_db, tmp_path):
    """Verify deleted message candidate IDs match phone order_id from HistorySyncMsg frames."""
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    # Local message in destination DB has _id = 5000 (diverged from phone _id)
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
        "VALUES (5000, 1, 0, 'ORDER_KEY_123', 1729000100000, 0, 'Message with order id')"
    )
    conn.commit()

    # Build protobuf frame where HistorySyncMsg has msgOrderId = 88888 (phone _id)
    wmi_bytes = _build_text_wmi(
        "5550100@s.whatsapp.net", "ORDER_KEY_123", "Message with order id", 0, 1729000100
    )
    hsm = _build_field(1, 2, wmi_bytes) + _build_field(2, 0, 88888)
    conv = _build_field(1, 2, b"5550100@s.whatsapp.net") + _build_field(2, 2, hsm)
    hs = _build_field(2, 2, conv)
    frame = _encode_varint(len(hs)) + hs

    bin_path = str(tmp_path / "messages_order.bin")
    with open(bin_path, "wb") as f:
        f.write(frame)

    # deletedMessages_1.json contains candidate ID 88888 from phone
    del_json = tmp_path / "deletedMessages_order.json"
    del_json.write_text(json.dumps({"deleted_ids": [88888]}), encoding="utf-8")

    schema_cache = {}
    deleted = increment_merger.apply_message_deletions(
        conn, str(del_json), logger, schema_cache, 1729000200000, 100000, 0, bin_path, None
    )
    conn.commit()

    # Must resolve 88888 -> ORDER_KEY_123 -> local _id 5000 and delete it
    assert deleted == 1
    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 5000").fetchone()[0] == 0
    conn.close()
