"""Tests for wab_archiver.increment_merger."""

from __future__ import annotations

import logging
import os
import sqlite3
import struct

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

logger = logging.getLogger(__name__)

from tests.fixtures.protobuf_factory import (
    build_audio_wmi,
    build_context_info,
    build_extended_text_wmi,
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
    from wab_viewer.db_cache import _ensure_jid_map_resolved

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


def test_apply_messages_bin_skips_empty_control_frames(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_empty_skip.db")
    cur = conn.cursor()
    logger = logging.getLogger("test")

    # Add empty frame to messages.bin
    key_bytes = (
        _build_field(1, 2, b"390000000010@s.whatsapp.net")
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
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (10, '390000000010', 's.whatsapp.net', '390000000010@s.whatsapp.net')"
    )
    cur.execute(
        "INSERT INTO chat (_id, jid_row_id, hidden, sort_timestamp, display_message_row_id) VALUES (5, 10, 1, 1000, NULL)"
    )
    conn.commit()

    # Replay a stub message (wire tag 24 = 67)
    key_bytes = (
        _build_field(1, 2, b"390000000010@s.whatsapp.net")
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
        _build_field(1, 2, b"390000000010@s.whatsapp.net")
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
        "INSERT INTO jid (_id, user, server, raw_string) VALUES (10, '390000000010', 's.whatsapp.net', '390000000010@s.whatsapp.net')"
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
    assert new_id == 10  # defensive merge preserves existing surrogate ID

    # Auxiliary tables remain intact for new_id
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

    if new_id != 10:
        # No orphaned records pointing to old id 10 if ID churned
        assert (
            cur.execute("SELECT COUNT(*) FROM message_media WHERE message_row_id = 10").fetchone()[
                0
            ]
            == 0
        )
        assert (
            cur.execute("SELECT COUNT(*) FROM message_quoted WHERE message_row_id = 10").fetchone()[
                0
            ]
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


def test_apply_messages_bin_in_batch_duplicate_key_id(mock_db, tmp_path):
    """Verify in-batch updates for the same key_id replace previous record without duplicates (F-02)."""
    conn = sqlite3.connect(mock_db)
    wmi_1 = _build_text_wmi(
        "5550100@s.whatsapp.net", "DUP_BATCH_KEY", "First version", 1, 1729000000
    )
    wmi_2 = _build_text_wmi(
        "5550100@s.whatsapp.net", "DUP_BATCH_KEY", "Updated version", 1, 1729000010
    )

    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_1, wmi_2])
    bin_file = tmp_path / "dup_key_messages.bin"
    bin_file.write_bytes(frame)

    inserted = increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()

    rows = conn.execute(
        "SELECT _id, key_id, text_data FROM message WHERE key_id = 'DUP_BATCH_KEY'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][2] == "Updated version"
    conn.close()


def test_apply_messages_bin_revoked_media_purge(mock_db, tmp_path):
    """Verify revoked media messages purge message_media rows without leaking or re-parenting (F-03)."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_revoked (
            message_row_id INTEGER PRIMARY KEY,
            revoked_key_id TEXT,
            admin_jid_row_id INTEGER,
            revoke_timestamp INTEGER
        );
    """)

    # 1. Base database has an image message with linked message_media
    cur = conn.cursor()
    cur.execute("SELECT _id FROM chat LIMIT 1")
    chat_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO message (chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) "
        "VALUES (?, 0, 'REVOKE_IMG_KEY', 1729000000, 1, 'Photo caption', 0)",
        (chat_id,),
    )
    orig_msg_id = cur.lastrowid
    cur.execute(
        "INSERT INTO message_media (message_row_id, chat_row_id, file_path, file_size, transferred) "
        "VALUES (?, ?, 'Media/WhatsApp Images/photo.jpg', 12345, 1)",
        (orig_msg_id, chat_id),
    )
    conn.commit()

    # 2. Replay an increment containing a revoke frame (stub_type = 1 / REVOKE)
    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"REVOKE_IMG_KEY")
    )
    wmi_revoke = (
        _build_field(1, 2, key_bytes)
        + _build_field(24, 0, 1)  # stubType = 1 (REVOKE) -> message_type = 15
        + _build_field(3, 0, 1729000050)
    )
    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_revoke])
    bin_file = tmp_path / "revoke_media.bin"
    bin_file.write_bytes(frame)

    increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()

    # Verify message is marked revoked and message_media has 0 rows for both old and new IDs
    rev_msg = conn.execute(
        "SELECT _id, message_type FROM message WHERE key_id = 'REVOKE_IMG_KEY'"
    ).fetchone()
    assert rev_msg is not None
    assert rev_msg[1] == 15

    media_rows = conn.execute("SELECT * FROM message_media").fetchall()
    assert len(media_rows) == 0

    revoked_entry = conn.execute(
        "SELECT message_row_id, revoked_key_id FROM message_revoked WHERE revoked_key_id = 'REVOKE_IMG_KEY'"
    ).fetchone()
    assert revoked_entry is not None
    assert revoked_entry[0] == rev_msg[0]
    conn.close()


def test_message_quoted_from_me_and_pass2_resolution(mock_db, tmp_path):
    """Verify quoted replies resolve from_me=1 and sender_jid_row_id=0 for quoted outgoing messages (F-05)."""
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
    """)

    # Quoting message arrives BEFORE the parent message (out-of-order)
    ctx_bytes = _build_field(1, 2, b"OUTGOING_PARENT_KEY") + _build_field(
        3, 2, _build_field(1, 2, b"Original user message")
    )
    ext_bytes = _build_field(1, 2, b"Replying to your ping") + _build_field(17, 2, ctx_bytes)
    msg_bytes = _build_field(6, 2, ext_bytes)
    key_bytes = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"INCOMING_REPLY_KEY")
    )
    wmi_reply = (
        _build_field(1, 2, key_bytes)
        + _build_field(2, 2, msg_bytes)
        + _build_field(3, 0, 1729000050)
    )

    # Later in stream, outgoing parent message arrives
    wmi_parent = _build_text_wmi(
        "5550100@s.whatsapp.net", "OUTGOING_PARENT_KEY", "Original user message", 1, 1729000000
    )

    frame = _build_history_sync_frame("5550100@s.whatsapp.net", [wmi_reply, wmi_parent])
    bin_file = tmp_path / "quoted_from_me.bin"
    bin_file.write_bytes(frame)

    inserted = increment_merger.apply_messages_bin(conn, str(bin_file), logger, None, None, None)
    conn.commit()
    assert inserted == 2

    # Verify Pass 2 resolved both timestamp AND from_me = 1
    q_row = conn.execute(
        "SELECT key_id, from_me, sender_jid_row_id, timestamp FROM message_quoted WHERE key_id = 'OUTGOING_PARENT_KEY'"
    ).fetchone()
    assert q_row is not None
    assert q_row[0] == "OUTGOING_PARENT_KEY"
    assert q_row[1] == 1  # from_me resolved to 1
    assert q_row[2] == 0  # sender_jid_row_id resolved to 0 for from_me=1
    assert q_row[3] == 1729000000000
    conn.close()


def test_apply_messages_bin_resolves_quoted_to_edited_parent(mock_db, tmp_path):
    """Verify quotes of pre-edit stanza IDs resolve against message_edit_info.original_key_id."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_edit_info (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL UNIQUE,
            original_key_id TEXT NOT NULL,
            edited_timestamp INTEGER,
            sender_timestamp INTEGER
        );
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

    # Target parent message in DB has key_id = 'EDITED_NEW_KEY' and original_key_id = 'PRE_EDIT_ORIGINAL_KEY'
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
        "VALUES (70, 1, 0, 'EDITED_NEW_KEY', 1, 1729000050000, 0, 'Edited message text')"
    )
    cur.execute(
        "INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp) "
        "VALUES (70, 'PRE_EDIT_ORIGINAL_KEY', 1729000060000, 1729000050000)"
    )
    conn.commit()

    # Reply arrives quoting PRE_EDIT_ORIGINAL_KEY
    wmi_reply = _build_quoted_wmi(
        "5550100@s.whatsapp.net",
        "REPLY_TO_EDITED",
        "Replying to original key",
        1,
        1729000100,
        "PRE_EDIT_ORIGINAL_KEY",
        "Original pre-edit snippet",
        "5550100@s.whatsapp.net",
    )
    bin_path = str(tmp_path / "messages_quoted_edit.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_reply)) + wmi_reply)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 1
    row = cur.execute(
        "SELECT timestamp, from_me, sender_jid_row_id FROM message_quoted WHERE key_id = 'PRE_EDIT_ORIGINAL_KEY'"
    ).fetchone()
    assert row is not None
    # Timestamp and sender resolved via message_edit_info join
    assert row[0] == 1729000050000
    conn.close()


def test_apply_messages_bin_edited_media_message(mock_db, tmp_path):
    """Verify editing an existing message to media updates type, media table, and edit info."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_edit_info (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL UNIQUE,
            original_key_id TEXT NOT NULL,
            edited_timestamp INTEGER,
            sender_timestamp INTEGER
        );
    """)

    # Target parent message already in DB (row_id 100 from mock_db fixture)
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 0)
        + _build_field(3, 2, b"INITIAL_KEY_1")
    )
    img_bytes = (
        _build_field(1, 2, b"https://example.com/edited_photo.enc")
        + _build_field(2, 2, b"image/jpeg")
        + _build_field(3, 2, b"Updated photo caption")
        + _build_field(4, 2, b"hash_12345678901234567890123456")
        + _build_field(5, 0, 486005)
    )
    edited_msg_bytes = _build_field(3, 2, img_bytes)
    edit_proto = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, 14)
        + _build_field(10, 0, 1729000600000)
        + _build_field(14, 2, edited_msg_bytes)
    )
    wmi_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"EDIT_MSG_KEY_1")
    )
    media_data = _build_field(1, 2, b"Media/WhatsApp Images/Sent/IMG-20261031-WA0002.jpg")
    wmi_edit = (
        _build_field(1, 2, wmi_key)
        + _build_field(2, 2, _build_field(12, 2, edit_proto))
        + _build_field(3, 0, 1729000600)
        + _build_field(38, 2, media_data)
    )

    bin_path = str(tmp_path / "messages_edit_media.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_edit)) + wmi_edit)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, schema_cache)
    conn.commit()

    assert inserted == 1
    # Verify message table updated to media type 1 and new caption
    row = conn.execute("SELECT message_type, text_data FROM message WHERE _id = 100").fetchone()
    assert row[0] == 1
    assert row[1] == "Updated photo caption"

    # Verify message_media populated
    media_row = conn.execute(
        "SELECT file_path, file_size, mime_type FROM message_media WHERE message_row_id = 100"
    ).fetchone()
    assert media_row is not None
    assert media_row[0] == "Media/WhatsApp Images/Sent/IMG-20261031-WA0002.jpg"
    assert media_row[1] == 486005
    assert media_row[2] == "image/jpeg"

    # Verify message_edit_info recorded
    edit_info = conn.execute(
        "SELECT original_key_id, edited_timestamp FROM message_edit_info WHERE message_row_id = 100"
    ).fetchone()
    assert edit_info is not None
    assert edit_info[0] == "INITIAL_KEY_1"
    assert edit_info[1] == 1729000600000
    conn.close()


def test_apply_messages_bin_intra_increment_edited_media_fallback(mock_db, tmp_path):
    """Verify intra-increment edit where parent was never stored creates full media message in Pass 2."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_edit_info (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL UNIQUE,
            original_key_id TEXT NOT NULL,
            edited_timestamp INTEGER,
            sender_timestamp INTEGER
        );
    """)

    # Target key ABSENT_TARGET_KEY does NOT exist in DB
    target_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"ABSENT_TARGET_KEY")
    )
    img_bytes = (
        _build_field(1, 2, b"https://example.com/fallback_photo.enc")
        + _build_field(2, 2, b"image/jpeg")
        + _build_field(3, 2, b"Intra-increment photo caption")
        + _build_field(4, 2, b"hash_abcdef12345678901234567890")
        + _build_field(5, 0, 314159)
    )
    edited_msg_bytes = _build_field(3, 2, img_bytes)
    edit_proto = (
        _build_field(1, 2, target_key)
        + _build_field(2, 0, 14)
        + _build_field(10, 0, 1729000700000)
        + _build_field(14, 2, edited_msg_bytes)
    )
    wmi_key = (
        _build_field(1, 2, b"5550100@s.whatsapp.net")
        + _build_field(2, 0, 1)
        + _build_field(3, 2, b"EDIT_CONTAINER_KEY")
    )
    media_data = _build_field(1, 2, b"Media/WhatsApp Images/Sent/IMG-FALLBACK.jpg")
    wmi_edit = (
        _build_field(1, 2, wmi_key)
        + _build_field(2, 2, _build_field(12, 2, edit_proto))
        + _build_field(3, 0, 1729000700)
        + _build_field(38, 2, media_data)
    )

    bin_path = str(tmp_path / "messages_intra_edit.bin")
    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_edit)) + wmi_edit)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, schema_cache)
    conn.commit()

    assert inserted == 1
    # Verify message table has message_type 1 (not 0!) and caption
    msg_row = conn.execute(
        "SELECT _id, message_type, text_data, from_me FROM message WHERE key_id = 'EDIT_CONTAINER_KEY'"
    ).fetchone()
    assert msg_row is not None
    row_id = msg_row[0]
    assert msg_row[1] == 1
    assert msg_row[2] == "Intra-increment photo caption"
    assert msg_row[3] == 1

    # Verify message_media populated with Tag 38 path and payload size
    media_row = conn.execute(
        "SELECT file_path, file_size, mime_type FROM message_media WHERE message_row_id = ?",
        (row_id,),
    ).fetchone()
    assert media_row is not None
    assert media_row[0] == "Media/WhatsApp Images/Sent/IMG-FALLBACK.jpg"
    assert media_row[1] == 314159
    assert media_row[2] == "image/jpeg"

    # Verify message_edit_info
    edit_info = conn.execute(
        "SELECT original_key_id, edited_timestamp FROM message_edit_info WHERE message_row_id = ?",
        (row_id,),
    ).fetchone()
    assert edit_info is not None
    assert edit_info[0] == "ABSENT_TARGET_KEY"
    assert edit_info[1] == 1729000700000
    conn.close()


def test_apply_messages_bin_audio_waveform(mock_db, tmp_path):
    """Verify apply_messages_bin populates audio_data with waveform BLOB."""
    waveform_bytes = b"\x10\x20\x30\x40\x50\x60"
    wmi_audio = build_audio_wmi(
        "5550100@s.whatsapp.net",
        "AUDIO_REPLAY_1",
        "audio/ogg; codecs=opus",
        b"H" * 32,
        4096,
        waveform_bytes,
        1,
        0,
        1729000800,
    )
    bin_path = str(tmp_path / "audio_waveform.bin")
    with open(bin_path, "wb") as f:
        f.write(encode_varint(len(wmi_audio)) + wmi_audio)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, {})
    conn.commit()

    assert inserted == 1
    row = conn.execute(
        "SELECT a.waveform FROM audio_data a JOIN message m ON m._id = a.message_row_id WHERE m.key_id = 'AUDIO_REPLAY_1'"
    ).fetchone()
    assert row is not None
    assert row[0] == waveform_bytes
    conn.close()


def test_apply_messages_bin_link_preview_metadata(mock_db, tmp_path):
    """Verify apply_messages_bin populates message_text and message_thumbnail for link previews."""
    thumb_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIFsynthetic_thumb"
    wmi_lp = build_extended_text_wmi(
        "5550100@s.whatsapp.net",
        "LP_REPLAY_1",
        "Check this: https://example.com/item",
        "https://example.com/item",
        "Synthetic Item Title",
        "Synthetic Item Description",
        thumb_bytes,
        b"K" * 32,
        0,
        1729000810,
    )
    bin_path = str(tmp_path / "link_preview.bin")
    with open(bin_path, "wb") as f:
        f.write(encode_varint(len(wmi_lp)) + wmi_lp)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, {})
    conn.commit()

    assert inserted == 1
    text_row = conn.execute(
        "SELECT t.page_title, t.description, t.url FROM message_text t JOIN message m ON m._id = t.message_row_id WHERE m.key_id = 'LP_REPLAY_1'"
    ).fetchone()
    assert text_row is not None
    assert text_row[0] == "Synthetic Item Title"
    assert text_row[1] == "Synthetic Item Description"
    assert text_row[2] == "https://example.com/item"

    thumb_row = conn.execute(
        "SELECT th.thumbnail FROM message_thumbnail th JOIN message m ON m._id = th.message_row_id WHERE m.key_id = 'LP_REPLAY_1'"
    ).fetchone()
    assert thumb_row is not None
    assert thumb_row[0] == thumb_bytes
    conn.close()


def test_apply_messages_bin_quoted_location(mock_db, tmp_path):
    """Verify apply_messages_bin populates message_quoted_location for quoted location messages."""
    lat_bytes = struct.pack("<d", 37.7749)
    lng_bytes = struct.pack("<d", -122.4194)
    loc_payload = (
        build_field(1, 2, lat_bytes)
        + build_field(2, 2, lng_bytes)
        + build_field(3, 2, b"San Francisco HQ")
        + build_field(4, 2, b"Market Street, CA")
        + build_field(5, 2, b"https://maps.example.com/sf")
    )
    q_loc = build_field(5, 2, loc_payload)
    ctx_bytes = build_context_info(stanza_id="STZ_ORIG_LOC", quoted_msg_bytes=q_loc)

    key_bytes = (
        build_field(1, 2, b"5550100@s.whatsapp.net")
        + build_field(2, 0, 0)
        + build_field(3, 2, b"REPLY_LOC_KEY")
    )
    txt_payload = build_field(1, 2, b"See you at the HQ!") + build_field(17, 2, ctx_bytes)
    wmi_reply = (
        build_field(1, 2, key_bytes)
        + build_field(2, 2, build_field(6, 2, txt_payload))
        + build_field(3, 0, 1729000820)
    )

    bin_path = str(tmp_path / "quoted_location.bin")
    with open(bin_path, "wb") as f:
        f.write(encode_varint(len(wmi_reply)) + wmi_reply)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, {})
    conn.commit()

    assert inserted == 1
    q_row = conn.execute(
        "SELECT ql.place_name, ql.place_address, ql.latitude, ql.longitude "
        "FROM message_quoted_location ql "
        "JOIN message m ON m._id = ql.message_row_id "
        "WHERE m.key_id = 'REPLY_LOC_KEY'"
    ).fetchone()
    assert q_row is not None
    assert q_row[0] == "San Francisco HQ"
    assert q_row[1] == "Market Street, CA"
    assert abs(q_row[2] - 37.7749) < 0.0001
    assert abs(q_row[3] - (-122.4194)) < 0.0001
    conn.close()


def test_apply_messages_bin_quoted_vcard(mock_db, tmp_path):
    """Verify apply_messages_bin populates message_quoted_vcard for quoted contact messages."""
    synthetic_vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:Alice Smith\nTEL:5550155\nEND:VCARD"
    contact_payload = build_field(1, 2, b"Alice Smith") + build_field(
        2, 2, synthetic_vcard.encode("utf-8")
    )
    q_contact = build_field(4, 2, contact_payload)
    ctx_bytes = build_context_info(stanza_id="STZ_ORIG_CONTACT", quoted_msg_bytes=q_contact)

    key_bytes = (
        build_field(1, 2, b"5550100@s.whatsapp.net")
        + build_field(2, 0, 0)
        + build_field(3, 2, b"REPLY_VCARD_KEY")
    )
    txt_payload = build_field(1, 2, b"Thanks for the contact!") + build_field(17, 2, ctx_bytes)
    wmi_reply = (
        build_field(1, 2, key_bytes)
        + build_field(2, 2, build_field(6, 2, txt_payload))
        + build_field(3, 0, 1729000830)
    )

    bin_path = str(tmp_path / "quoted_vcard.bin")
    with open(bin_path, "wb") as f:
        f.write(encode_varint(len(wmi_reply)) + wmi_reply)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, {})
    conn.commit()

    assert inserted == 1
    qv_row = conn.execute(
        "SELECT qv.vcard FROM message_quoted_vcard qv "
        "JOIN message m ON m._id = qv.message_row_id "
        "WHERE m.key_id = 'REPLY_VCARD_KEY'"
    ).fetchone()
    assert qv_row is not None
    assert qv_row[0] == synthetic_vcard
    conn.close()


def test_apply_messages_bin_message_mentions(mock_db, tmp_path):
    """Verify apply_messages_bin populates message_mentions table for mentioned JIDs in group chats."""
    ctx_bytes = build_context_info(
        stanza_id="STZ_MENTION_MSG",
        mentioned_jids=["5550100@s.whatsapp.net", "5550199@s.whatsapp.net"],
    )
    key_bytes = (
        build_field(1, 2, b"120363000000000000@g.us")
        + build_field(2, 0, 0)
        + build_field(3, 2, b"GRP_MENTION_KEY")
    )
    txt_payload = build_field(1, 2, b"Hello @5550100 and @5550199") + build_field(17, 2, ctx_bytes)
    wmi_mention = (
        build_field(1, 2, key_bytes)
        + build_field(2, 2, build_field(6, 2, txt_payload))
        + build_field(3, 0, 1729000840)
        + build_field(21, 2, b"5550100@s.whatsapp.net")
    )

    bin_path = str(tmp_path / "mentions.bin")
    with open(bin_path, "wb") as f:
        f.write(encode_varint(len(wmi_mention)) + wmi_mention)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, {})
    conn.commit()

    assert inserted == 1
    mention_rows = conn.execute(
        "SELECT j.raw_string FROM message_mentions mm "
        "JOIN message m ON m._id = mm.message_row_id "
        "JOIN jid j ON j._id = mm.jid_row_id "
        "WHERE m.key_id = 'GRP_MENTION_KEY' "
        "ORDER BY j.raw_string ASC"
    ).fetchall()
    assert len(mention_rows) == 2
    assert mention_rows[0][0] == "5550100@s.whatsapp.net"
    assert mention_rows[1][0] == "5550199@s.whatsapp.net"
    conn.close()


def test_apply_messages_bin_intra_batch_duplicate_replacement(mock_db, tmp_path):
    """Verify apply_messages_bin replaces earlier message when duplicate key arrives in same batch."""
    wmi_first = build_text_wmi(
        "5550100@s.whatsapp.net", "DUP_STANZA_KEY_1", "Draft text initial", 0, 1729000850
    )
    wmi_second = build_text_wmi(
        "5550100@s.whatsapp.net", "DUP_STANZA_KEY_1", "Final text updated", 0, 1729000860
    )

    bin_path = str(tmp_path / "intra_dup.bin")
    with open(bin_path, "wb") as f:
        f.write(encode_varint(len(wmi_first)) + wmi_first)
        f.write(encode_varint(len(wmi_second)) + wmi_second)

    conn = sqlite3.connect(mock_db)
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, None, None, {})
    conn.commit()

    rows = conn.execute(
        "SELECT text_data, timestamp FROM message WHERE key_id = 'DUP_STANZA_KEY_1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Final text updated"
    assert rows[0][1] == 1729000850000
    conn.close()
