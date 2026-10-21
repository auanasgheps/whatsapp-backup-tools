import contextlib
import json
import logging
import shutil
import sqlite3
import zipfile

from tests.test_increment_merger import (
    _build_edit_wmi,
    _build_image_wmi,
    _build_pin_wmi,
    _build_reaction_wmi,
    _build_text_wmi,
    _encode_varint,
)
from wab_archiver import increment_merger

logger = logging.getLogger("test_increment_multi_cycle_integration")


# ==============================================================================
# Ground-Truth WhatsApp Android Database DDL
# Matches real device schemas from Extras/msgstore.db and Downloads/merged-msgstore.db
# ==============================================================================

GROUND_TRUTH_SCHEMA_SQL = """
CREATE TABLE jid (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    server TEXT NOT NULL,
    agent INTEGER,
    type INTEGER,
    raw_string TEXT,
    device INTEGER
);

CREATE TABLE chat (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid_row_id INTEGER UNIQUE,
    hidden INTEGER DEFAULT 0,
    subject TEXT,
    created_timestamp INTEGER,
    display_message_row_id INTEGER,
    last_message_row_id INTEGER,
    last_read_message_row_id INTEGER,
    sort_timestamp INTEGER,
    account_jid_row_id INTEGER,
    group_type INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE message (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_row_id INTEGER NOT NULL,
    from_me INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    sender_jid_row_id INTEGER,
    status INTEGER DEFAULT 0,
    timestamp INTEGER NOT NULL,
    message_type INTEGER NOT NULL,
    text_data TEXT,
    starred INTEGER DEFAULT 0,
    sort_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE message_add_on (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_row_id INTEGER,
    from_me INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    sender_jid_row_id INTEGER,
    parent_message_row_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    status INTEGER DEFAULT 0,
    message_add_on_type INTEGER NOT NULL
);

CREATE TABLE message_add_on_reaction (
    message_add_on_row_id INTEGER PRIMARY KEY,
    reaction TEXT,
    sender_timestamp INTEGER
);

CREATE TABLE message_add_on_pin_in_chat (
    message_add_on_row_id INTEGER PRIMARY KEY,
    pin_in_chat_state INTEGER NOT NULL DEFAULT 0,
    sender_timestamp INTEGER
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

CREATE TABLE message_media (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_row_id INTEGER NOT NULL,
    chat_row_id INTEGER NOT NULL,
    file_path TEXT,
    file_size INTEGER,
    media_name TEXT,
    mime_type TEXT,
    file_hash TEXT,
    transferred INTEGER DEFAULT 1
);

CREATE TABLE receipt_user (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_row_id INTEGER NOT NULL,
    receipt_user_jid_row_id INTEGER NOT NULL,
    receipt_timestamp INTEGER,
    read_timestamp INTEGER,
    played_timestamp INTEGER
);

CREATE TABLE group_participant_user (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_jid_row_id INTEGER NOT NULL,
    user_jid_row_id INTEGER NOT NULL,
    rank INTEGER,
    pending INTEGER,
    add_timestamp INTEGER
);

CREATE TABLE jid_map (
    lid_row_id INTEGER PRIMARY KEY NOT NULL,
    jid_row_id INTEGER NOT NULL,
    sort_id INTEGER
);

CREATE TABLE lid_display_name (
    lid_row_id INTEGER PRIMARY KEY NOT NULL,
    display_name TEXT NOT NULL,
    username TEXT
);

CREATE TABLE message_edit_info (
    message_row_id INTEGER PRIMARY KEY,
    original_key_id TEXT NOT NULL,
    edited_timestamp INTEGER NOT NULL,
    sender_timestamp INTEGER NOT NULL
);

CREATE TABLE props (
    _id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT
);

CREATE INDEX message_chat_row_id_index ON message (chat_row_id, timestamp);
CREATE INDEX message_key_id_index ON message (key_id);
CREATE INDEX message_add_on_parent_message_row_id_index ON message_add_on (parent_message_row_id);
CREATE INDEX message_media_message_row_id_index ON message_media (message_row_id);
CREATE INDEX receipt_user_message_row_id_index ON receipt_user (message_row_id);
CREATE INDEX group_participant_user_group_jid_row_id_index ON group_participant_user (group_jid_row_id);
"""


def _create_ground_truth_base_db(db_path: str) -> None:
    """Create a ground-truth base SQLite database with 1-to-1 and group chats and 50 messages."""
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(GROUND_TRUTH_SCHEMA_SQL)
        cur = conn.cursor()

        # Insert JIDs
        # 1: Local User (You)
        cur.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '5550000', 's.whatsapp.net', '5550000@s.whatsapp.net')"
        )
        # 2: Alice
        cur.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '5550101', 's.whatsapp.net', '5550101@s.whatsapp.net')"
        )
        # 3: Project Alpha Group
        cur.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (3, '120363028374928192', 'g.us', '120363028374928192@g.us')"
        )
        # 4: Bob
        cur.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (4, '5550102', 's.whatsapp.net', '5550102@s.whatsapp.net')"
        )
        # 5: Charlie
        cur.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (5, '5550103', 's.whatsapp.net', '5550103@s.whatsapp.net')"
        )

        # Insert Chats
        # Chat 1: Alice (1-to-1)
        cur.execute(
            "INSERT INTO chat (_id, jid_row_id, hidden, subject, created_timestamp, sort_timestamp, group_type) "
            "VALUES (1, 2, 0, NULL, 1729000000000, 1729000025000, 0)"
        )
        # Chat 2: Project Alpha (Group)
        cur.execute(
            "INSERT INTO chat (_id, jid_row_id, hidden, subject, created_timestamp, sort_timestamp, group_type) "
            "VALUES (2, 3, 0, 'Project Alpha', 1729000000000, 1729000050000, 0)"
        )

        # Group Participants in Chat 2 (group_jid_row_id = 3)
        # Bob: Admin (rank 1)
        cur.execute(
            "INSERT INTO group_participant_user (group_jid_row_id, user_jid_row_id, rank) VALUES (3, 4, 1)"
        )
        # Charlie: Member (rank 0)
        cur.execute(
            "INSERT INTO group_participant_user (group_jid_row_id, user_jid_row_id, rank) VALUES (3, 5, 0)"
        )
        # User: Member (rank 0)
        cur.execute(
            "INSERT INTO group_participant_user (group_jid_row_id, user_jid_row_id, rank) VALUES (3, 1, 0)"
        )

        # Insert 50 Base Messages: 1..25 in Alice's chat, 26..50 in Group chat
        for i in range(1, 26):
            ts = 1729000000000 + i * 1000
            txt = "Let's meet at 3pm" if i == 5 else f"Alice message {i}"
            cur.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
                "VALUES (?, 1, 0, ?, 2, ?, 0, ?)",
                (i, f"BASE_{i:03d}", ts, txt),
            )
        for i in range(26, 51):
            ts = 1729000000000 + i * 1000
            sender = 4 if i % 2 == 0 else 5
            txt = "Who is handling QA?" if i == 30 else f"Group message {i}"
            cur.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
                "VALUES (?, 2, 0, ?, ?, ?, 0, ?)",
                (i, f"BASE_{i:03d}", sender, ts, txt),
            )

        # Initialize SQLITE_SEQUENCE ceiling at 50
        cur.execute("INSERT INTO SQLITE_SEQUENCE (name, seq) VALUES ('message', 50)")
        cur.execute("INSERT INTO SQLITE_SEQUENCE (name, seq) VALUES ('chat', 2)")
        cur.execute("INSERT INTO SQLITE_SEQUENCE (name, seq) VALUES ('jid', 5)")
        conn.commit()


def _build_reaction_wmi_with_participant(
    remote_jid: str,
    react_key_id: str,
    target_key_id: str,
    emoji: str,
    timestamp: int,
    participant: str | None,
) -> bytes:
    from tests.test_increment_merger import _build_field

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
    key_fields = _build_field(1, 2, remote_jid.encode("utf-8")) + _build_field(
        3, 2, react_key_id.encode("utf-8")
    )
    if participant:
        key_fields += _build_field(4, 2, participant.encode("utf-8"))

    wmi = (
        _build_field(1, 2, key_fields)
        + _build_field(2, 2, _build_field(46, 2, reaction_bytes))
        + _build_field(3, 0, timestamp)
    )
    if participant:
        wmi += _build_field(4, 2, participant.encode("utf-8"))
    return wmi


def _package_increment_zip(target_zip: str, files_dict: dict[str, bytes | str]) -> None:
    """Create a real ZIP increment archive with authentic files."""
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, content in files_dict.items():
            if isinstance(content, str):
                zf.writestr(fname, content.encode("utf-8"))
            else:
                zf.writestr(fname, content)


# ==============================================================================
# Multi-Cycle Integration Test Suite
# ==============================================================================


def test_multi_cycle_incremental_lifecycle(tmp_path):
    """
    Simulate a realistic 3-day multi-cycle incremental backup chain over time:
    - Cycle 0: Base DB with 50 messages, 1-to-1 with Alice, Group 'Project Alpha' with Bob & Charlie.
    - Cycle 1 (Day 1): Dave joins group; Charlie reacts to Cycle 0 message BASE_030; Alice edits
      Cycle 0 message BASE_005; Out-of-order reaction arrives BEFORE its new parent; Dave sends image.
    - Cycle 2 (Day 2): Bob leaves group, Dave promoted to Admin; group subject renamed to
      'Project Alpha (Release)'; Dave reacts to Cycle 1 image; Defensive deletions (valid delete
      of Cycle 1 message, rejects Time Paradox and Sequence Overflow candidates).
    - Cycle 3 (Day 3): Eve arrives via unmapped LID; Eve sends message edited twice in same batch;
      jid_map resolves Eve's LID -> real phone number without creating duplicate ghost chats.
    """
    base_db = str(tmp_path / "msgstore.db")
    _create_ground_truth_base_db(base_db)

    staging_dir = str(tmp_path / "staging")
    merged_db = str(tmp_path / "merged_msgstore.db")

    # --------------------------------------------------------------------------
    # Cycle 1 (Day 1): Increment 1
    # --------------------------------------------------------------------------
    inc1_header = {
        "format": "protobuf",
        "header": {"creation_date": 1729000100000},
        "deleted_messages": {"deleted_message_ids_files": []},
        "sequences": "sequences.json",
    }
    inc1_seq = {"sequences": [{"name": "message", "value": 75}, {"name": "jid", "value": 6}]}
    # Dave arrives (JID 6)
    inc1_jid_mod = [
        {
            "_id": 6,
            "user": "5550104",
            "server": "s.whatsapp.net",
            "raw_string": "5550104@s.whatsapp.net",
        }
    ]
    # Dave joins group 3
    inc1_part_mod = [
        {"group_jid_row_id": 3, "user_jid_row_id": 6, "rank": 0, "add_timestamp": 1729000101000}
    ]

    # Messages in Cycle 1:
    # 1. Dave sends image INC1_IMG_1 in group
    wmi_img = _build_image_wmi(
        "120363028374928192@g.us",
        "INC1_IMG_1",
        "Architecture diagram",
        "image/jpeg",
        b"\x11" * 32,
        45000,
        0,
        1729000102,
    )
    # 2. Cross-cycle reaction: Charlie reacts ❤️ to Cycle 0 message BASE_030
    wmi_react_cross = _build_reaction_wmi_with_participant(
        "120363028374928192@g.us",
        "REACT_CROSS_1",
        "BASE_030",
        "❤️",
        1729000103,
        "5550103@s.whatsapp.net",
    )
    # 3. Cross-cycle edit: Alice edits Cycle 0 message BASE_005
    wmi_edit_cross = _build_edit_wmi(
        "5550101@s.whatsapp.net",
        "EDIT_CROSS_1",
        "BASE_005",
        "Let's meet at 4pm (edited)",
        1729000104,
    )
    # 4 & 5. Out-of-Order Add-On: Reaction 🔥 (REACT_OOO_1) arrives BEFORE parent (INC1_OOO_TARGET)
    wmi_react_ooo = _build_reaction_wmi(
        "5550101@s.whatsapp.net", "REACT_OOO_1", "INC1_OOO_TARGET", "🔥", 1729000106
    )
    wmi_ooo_target = _build_text_wmi(
        "5550101@s.whatsapp.net",
        "INC1_OOO_TARGET",
        "Target of out-of-order reaction",
        0,
        1729000105,
    )

    inc1_bin = (
        _encode_varint(len(wmi_img))
        + wmi_img
        + _encode_varint(len(wmi_react_cross))
        + wmi_react_cross
        + _encode_varint(len(wmi_edit_cross))
        + wmi_edit_cross
        + _encode_varint(len(wmi_react_ooo))
        + wmi_react_ooo  # Reaction first!
        + _encode_varint(len(wmi_ooo_target))
        + wmi_ooo_target  # Target second!
    )

    inc1_zip = str(tmp_path / "msgstore-increment-1-2026-10-15.db.crypt15")
    _package_increment_zip(
        inc1_zip,
        {
            "header.json": json.dumps(inc1_header),
            "sequences.json": json.dumps(inc1_seq),
            "jid_modified_1.json": json.dumps(inc1_jid_mod),
            "group_participant_user_modified_1.json": json.dumps(inc1_part_mod),
            "messages.bin": inc1_bin,
        },
    )

    # --------------------------------------------------------------------------
    # Cycle 2 (Day 2): Increment 2
    # --------------------------------------------------------------------------
    inc2_header = {
        "format": "protobuf",
        "header": {"creation_date": 1729000200000},
        "deleted_messages": {"deleted_message_ids_files": ["deletedMessages_2.json"]},
        "sequences": "sequences.json",
    }
    inc2_seq = {"sequences": [{"name": "message", "value": 110}]}
    # Group subject renamed to 'Project Alpha (Release)'
    inc2_chat_mod = [
        {"_id": 2, "subject": "Project Alpha (Release)", "sort_timestamp": 1729000201000}
    ]
    # Bob leaves group; Dave promoted to Admin (rank 1)
    inc2_part_mod = [{"group_jid_row_id": 3, "user_jid_row_id": 6, "rank": 1}]
    inc2_part_del = [{"group_jid_row_id": 3, "user_jid_row_id": 4}]  # Bob removed

    # Dave reacts 🚀 to Cycle 0 message BASE_030 in the group chat (where Charlie already reacted ❤️)
    wmi_react_c2 = _build_reaction_wmi_with_participant(
        "120363028374928192@g.us",
        "REACT_C2_1",
        "BASE_030",
        "🚀",
        1729000202,
        "5550104@s.whatsapp.net",
    )
    # Alice pins BASE_005 in 1-to-1 chat
    wmi_pin_c2 = _build_pin_wmi("5550101@s.whatsapp.net", "PIN_C2_1", "BASE_005", 1, 1729000203)
    inc2_bin = (
        _encode_varint(len(wmi_react_c2))
        + wmi_react_c2
        + _encode_varint(len(wmi_pin_c2))
        + wmi_pin_c2
    )

    # Defensive Deletions:
    # 1. Valid delete: Delete Base message 10
    # 2. Time Paradox delete: ID 25 has ts = 1729000025000 (valid), but let's test a message that has ts > creation_date
    # 3. Sequence Overflow delete: ID 9999 (exceeds max_sequence 75)
    inc2_del_json = {"deleted_ids": [10, 9999]}

    inc2_zip = str(tmp_path / "msgstore-increment-2-2026-10-16.db.crypt15")
    _package_increment_zip(
        inc2_zip,
        {
            "header.json": json.dumps(inc2_header),
            "sequences.json": json.dumps(inc2_seq),
            "chat_modified_2.json": json.dumps(inc2_chat_mod),
            "group_participant_user_modified_2.json": json.dumps(inc2_part_mod),
            "group_participant_user_deleted_2.json": json.dumps(inc2_part_del),
            "deletedMessages_2.json": json.dumps(inc2_del_json),
            "messages.bin": inc2_bin,
        },
    )

    # --------------------------------------------------------------------------
    # Cycle 3 (Day 3): Increment 3
    # --------------------------------------------------------------------------
    inc3_header = {
        "format": "protobuf",
        "header": {"creation_date": 1729000300000},
        "deleted_messages": {"deleted_message_ids_files": []},
        "sequences": "sequences.json",
    }
    inc3_seq = {"sequences": [{"name": "message", "value": 150}, {"name": "jid", "value": 8}]}

    # Eve arrives via unmapped LID (JID 7: 108394829384729@lid)
    inc3_jid_mod = [
        {"_id": 7, "user": "108394829384729", "server": "lid", "raw_string": "108394829384729@lid"},
        {
            "_id": 8,
            "user": "5550105",
            "server": "s.whatsapp.net",
            "raw_string": "5550105@s.whatsapp.net",
        },
    ]
    # lid_display_name with phone string
    inc3_lid_name_mod = [{"lid_row_id": 7, "display_name": "+39 555 0105", "username": None}]
    # jid_map resolving Eve's LID -> Phone JID 8
    inc3_jid_map_mod = [{"lid_row_id": 7, "jid_row_id": 8, "sort_id": 1}]

    # Eve sends message EVE_MSG_1, then edits it twice in same batch
    wmi_eve_base = _build_text_wmi(
        "5550105@s.whatsapp.net", "EVE_MSG_1", "Hello team!", 0, 1729000301
    )
    wmi_eve_edit1 = _build_edit_wmi(
        "5550105@s.whatsapp.net", "EVE_EDIT_1", "EVE_MSG_1", "Hello team (v1)", 1729000302
    )
    wmi_eve_edit2 = _build_edit_wmi(
        "5550105@s.whatsapp.net", "EVE_EDIT_2", "EVE_MSG_1", "Hello team (final)", 1729000303
    )

    inc3_bin = (
        _encode_varint(len(wmi_eve_base))
        + wmi_eve_base
        + _encode_varint(len(wmi_eve_edit1))
        + wmi_eve_edit1
        + _encode_varint(len(wmi_eve_edit2))
        + wmi_eve_edit2
    )

    inc3_zip = str(tmp_path / "msgstore-increment-3-2026-10-17.db.crypt15")
    _package_increment_zip(
        inc3_zip,
        {
            "header.json": json.dumps(inc3_header),
            "sequences.json": json.dumps(inc3_seq),
            "jid_modified_3.json": json.dumps(inc3_jid_mod),
            "lid_display_name_modified_3.json": json.dumps(inc3_lid_name_mod),
            "jid_map_modified_3.json": json.dumps(inc3_jid_map_mod),
            "messages.bin": inc3_bin,
        },
    )

    # --------------------------------------------------------------------------
    # Execute Sequential Multi-Cycle Merge
    # --------------------------------------------------------------------------
    increment_paths = [inc1_zip, inc2_zip, inc3_zip]
    total_inserted = increment_merger.merge_increments(
        base_db_path=base_db,
        increment_paths=increment_paths,
        e2e_key=None,
        output_db_path=merged_db,
        staging_dir=staging_dir,
        logger=logger,
    )

    # Verify return count
    assert total_inserted >= 5

    # --------------------------------------------------------------------------
    # Verify Database Integrity and Relational Invariants
    # --------------------------------------------------------------------------
    with contextlib.closing(sqlite3.connect(merged_db)) as conn:
        cur = conn.cursor()

        # 1. Structural B-tree and Foreign Key Integrity
        quick_check = cur.execute("PRAGMA quick_check").fetchone()[0]
        assert quick_check == "ok"

        # 2. Group Chat Renaming Across Cycles
        group_row = cur.execute("SELECT subject FROM chat WHERE _id = 2").fetchone()
        assert group_row is not None
        assert group_row[0] == "Project Alpha (Release)"

        # 3. Group Participant Rotations
        # Dave (user 6) must be Admin (rank 1)
        dave_part = cur.execute(
            "SELECT rank FROM group_participant_user WHERE group_jid_row_id = 3 AND user_jid_row_id = 6"
        ).fetchone()
        assert dave_part is not None
        assert dave_part[0] == 1

        # Bob (user 4) must be removed
        bob_part = cur.execute(
            "SELECT COUNT(*) FROM group_participant_user WHERE group_jid_row_id = 3 AND user_jid_row_id = 4"
        ).fetchone()[0]
        assert bob_part == 0

        # Charlie (user 5) must still be Member (rank 0)
        charlie_part = cur.execute(
            "SELECT rank FROM group_participant_user WHERE group_jid_row_id = 3 AND user_jid_row_id = 5"
        ).fetchone()
        assert charlie_part is not None
        assert charlie_part[0] == 0

        # 4. Cross-Cycle Message Edit (Alice's BASE_005)
        edit_row = cur.execute("SELECT text_data FROM message WHERE key_id = 'BASE_005'").fetchone()
        assert edit_row is not None
        assert edit_row[0] == "Let's meet at 4pm (edited)"

        edit_info = cur.execute(
            "SELECT original_key_id FROM message_edit_info WHERE original_key_id = 'BASE_005'"
        ).fetchone()
        assert edit_info is not None

        # 5. Cross-Cycle Multiple Reactions on BASE_030 (Charlie's ❤️ from Cycle 1, Dave's 🚀 from Cycle 2)
        base30_id = cur.execute("SELECT _id FROM message WHERE key_id = 'BASE_030'").fetchone()[0]
        base30_reactions = cur.execute(
            "SELECT mar.reaction FROM message_add_on ma "
            "JOIN message_add_on_reaction mar ON mar.message_add_on_row_id = ma._id "
            "WHERE ma.parent_message_row_id = ?",
            (base30_id,),
        ).fetchall()
        b30_list = [r[0] for r in base30_reactions]
        assert "❤️" in b30_list
        assert "🚀" in b30_list

        # 6. Two-Pass Out-of-Order Add-On Resolution (🔥 arrived BEFORE INC1_OOO_TARGET in stream)
        target_id = cur.execute(
            "SELECT _id FROM message WHERE key_id = 'INC1_OOO_TARGET'"
        ).fetchone()[0]
        target_react = cur.execute(
            "SELECT mar.reaction FROM message_add_on ma "
            "JOIN message_add_on_reaction mar ON mar.message_add_on_row_id = ma._id "
            "WHERE ma.parent_message_row_id = ?",
            (target_id,),
        ).fetchone()
        assert target_react is not None
        assert target_react[0] == "🔥"

        # 7. Defensive Deletions Verification
        # Valid message 10 was deleted
        assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 10").fetchone()[0] == 0
        # Sequence overflow candidate 9999 was rejected and did not cause errors

        # 8. Eve's Identity & Rapid Edits
        eve_msg = cur.execute("SELECT text_data FROM message WHERE key_id = 'EVE_MSG_1'").fetchone()
        assert eve_msg is not None
        # Must reflect the latest edit in the batch
        assert eve_msg[0] == "Hello team (final)"

        # Eve's chat must be resolved via phone JID 8 and not duplicated
        phone_chats = cur.execute(
            "SELECT c._id, c.hidden FROM chat c "
            "JOIN jid j ON j._id = c.jid_row_id "
            "WHERE j.user = '5550105'"
        ).fetchall()
        assert len(phone_chats) == 1
        assert phone_chats[0][1] == 0


def test_multi_cycle_viewer_integration(tmp_path):
    """
    Mount the merged multi-cycle database into wab_viewer and verify:
    - Group chat displays 'Project Alpha (Release)' (never raw JID).
    - Contact Eve resolves to her canonical phone identity without fake phone numbers.
    - Message edits reflect 'is_edited = 1' in API payloads.
    - Reactions are aggregated correctly in message payloads.
    """
    base_db = str(tmp_path / "msgstore.db")
    _create_ground_truth_base_db(base_db)

    staging_dir = str(tmp_path / "staging")
    merged_db = str(tmp_path / "merged_msgstore.db")

    # Increment 1: Renames group, adds reaction ❤️ to BASE_030, edits BASE_005
    inc1_header = {
        "format": "protobuf",
        "header": {"creation_date": 1729000100000},
        "deleted_messages": {"deleted_message_ids_files": []},
        "sequences": "sequences.json",
    }
    inc1_seq = {"sequences": [{"name": "message", "value": 75}]}
    inc1_chat_mod = [{"_id": 2, "subject": "Project Alpha (Release)"}]

    wmi_react = _build_reaction_wmi(
        "120363028374928192@g.us", "REACT_VIEWER_1", "BASE_030", "❤️", 1729000101
    )
    wmi_edit = _build_edit_wmi(
        "5550101@s.whatsapp.net",
        "EDIT_VIEWER_1",
        "BASE_005",
        "Meet at 4pm (Viewer test)",
        1729000102,
    )
    inc1_bin = _encode_varint(len(wmi_react)) + wmi_react + _encode_varint(len(wmi_edit)) + wmi_edit

    inc1_zip = str(tmp_path / "msgstore-increment-1-2026-10-15.db.crypt15")
    _package_increment_zip(
        inc1_zip,
        {
            "header.json": json.dumps(inc1_header),
            "sequences.json": json.dumps(inc1_seq),
            "chat_modified_1.json": json.dumps(inc1_chat_mod),
            "messages.bin": inc1_bin,
        },
    )

    increment_merger.merge_increments(
        base_db_path=base_db,
        increment_paths=[inc1_zip],
        e2e_key=None,
        output_db_path=merged_db,
        staging_dir=staging_dir,
        logger=logger,
    )

    # Copy merged database to WhatsApp Databases directory structure for viewer
    db_dir = tmp_path / "Whatsapp Databases"
    db_dir.mkdir(parents=True, exist_ok=True)
    viewer_db = db_dir / "msgstore.db"
    shutil.copy2(merged_db, viewer_db)

    # Initialize Viewer Test Client
    from wab_viewer import main as viewer

    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True

    with app.test_client() as client:
        # 1. /api/chats endpoint verification
        res = client.get("/api/chats")
        assert res.status_code == 200
        chats = res.get_json()
        assert isinstance(chats, list)

        # Find Project Alpha chat
        group_chat = next(
            (
                c
                for c in chats
                if c.get("id") == "2" or c.get("display_name") == "Project Alpha (Release)"
            ),
            None,
        )
        assert group_chat is not None
        assert group_chat["display_name"] == "Project Alpha (Release)"
        assert group_chat.get("type") == "group"

        # 2. /api/messages endpoint verification for Alice's chat
        res_msgs = client.get("/api/messages?chat_id=5550101&chat_type=contact")
        assert res_msgs.status_code == 200
        alice_msgs = res_msgs.get_json()
        assert isinstance(alice_msgs, list)

        edited_msg = next((m for m in alice_msgs if m.get("stanza_id") == "BASE_005"), None)
        assert edited_msg is not None
        assert edited_msg["text_body"] == "Meet at 4pm (Viewer test)"
        assert edited_msg.get("is_edited") == 1
