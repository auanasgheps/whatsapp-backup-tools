"""Tests for wab_archiver.increment_merger."""

from __future__ import annotations

import base64
import hashlib
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
    _build_edit_wmi,
    _build_pin_wmi,
    _build_poll_creation_wmi,
    _build_poll_vote_wmi,
    _build_poll_vote_wmi_with_from_me,
    _build_reaction_wmi,
    _build_reaction_wmi_with_from_me,
    _make_two_pass_test_db,
)
from wab_archiver import increment_merger

_encode_varint = encode_varint
_build_field = build_field
_build_text_wmi = build_text_wmi
_build_image_wmi = build_image_wmi
_build_history_sync_frame = build_history_sync_frame


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


def test_two_pass_edit_when_parent_message_never_arrives(tmp_path):
    conn = _make_two_pass_test_db(tmp_path / "test_two_pass_edit_orphan.db")
    bin_path = str(tmp_path / "messages.bin")

    # Frame 0: Edit targeting NONEXISTENT_PARENT with updated text
    wmi_edit = _build_edit_wmi(
        "5550100@s.whatsapp.net",
        "EDIT_STANDALONE_ORPHAN",
        "NONEXISTENT_PARENT",
        "Standalone edited text",
        1729000300,
    )

    with open(bin_path, "wb") as f:
        f.write(_encode_varint(len(wmi_edit)) + wmi_edit)

    schema_cache = {}
    inserted = increment_merger.apply_messages_bin(conn, bin_path, logger, {}, {}, schema_cache)
    conn.commit()

    assert inserted == 1

    cur = conn.cursor()
    # Message row must exist for EDIT_STANDALONE_ORPHAN
    msg_row = cur.execute(
        "SELECT _id, text_data FROM message WHERE key_id = 'EDIT_STANDALONE_ORPHAN'"
    ).fetchone()
    assert msg_row is not None
    m_id, text = msg_row
    assert text == "Standalone edited text"

    # message_edit_info must be populated
    edit_row = cur.execute(
        "SELECT message_row_id, original_key_id FROM message_edit_info WHERE message_row_id = ?",
        (m_id,),
    ).fetchone()
    assert edit_row is not None
    assert edit_row[0] == m_id
    assert edit_row[1] == "NONEXISTENT_PARENT"
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
    from wab_viewer.hydration import _hydrate_poll_rows

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
    pytest.importorskip("cryptography.hazmat.primitives.hashes")
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
    assert new_id == 100  # defensive merge preserves existing surrogate ID
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


def test_apply_protocol_record_standalone_revoke(mock_db):
    """Verify apply_protocol_record populates message_revoked and cleans up auxiliary tables (F-06)."""
    conn = sqlite3.connect(mock_db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_revoked (
            message_row_id INTEGER PRIMARY KEY,
            revoked_key_id TEXT,
            admin_jid_row_id INTEGER,
            revoke_timestamp INTEGER
        );
        CREATE TABLE IF NOT EXISTS message_text (
            message_row_id INTEGER PRIMARY KEY,
            page_title TEXT,
            description TEXT,
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

    cur = conn.cursor()
    cur.execute("SELECT _id FROM chat LIMIT 1")
    chat_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO message (chat_row_id, from_me, key_id, timestamp, message_type, text_data, status) "
        "VALUES (?, 0, 'STANDALONE_REVOKE_KEY', 1729000000, 0, 'Check this link https://example.com', 0)",
        (chat_id,),
    )
    target_id = cur.lastrowid
    cur.execute(
        "INSERT INTO message_text (message_row_id, page_title, url) VALUES (?, 'Example Domain', 'https://example.com')",
        (target_id,),
    )
    cur.execute(
        "INSERT INTO message_thumbnail (message_row_id, thumbnail) VALUES (?, X'123456')",
        (target_id,),
    )
    conn.commit()

    proto_msg = {
        "revoke_info": {"target_key_id": "STANDALONE_REVOKE_KEY"},
        "timestamp": 1729000080000,
    }
    schema_cache = {}
    applied = increment_merger.apply_protocol_record(
        conn, proto_msg, -1, 0, 1729000080000, {}, schema_cache, logger, chat_id, {}
    )
    conn.commit()

    assert applied is True
    # Verify message is type 15 with NULL text
    m_row = conn.execute(
        "SELECT message_type, text_data FROM message WHERE _id = ?", (target_id,)
    ).fetchone()
    assert m_row[0] == 15
    assert m_row[1] is None

    # Verify message_revoked is populated
    rev_row = conn.execute(
        "SELECT message_row_id, revoked_key_id, revoke_timestamp FROM message_revoked WHERE message_row_id = ?",
        (target_id,),
    ).fetchone()
    assert rev_row is not None
    assert rev_row[1] == "STANDALONE_REVOKE_KEY"
    assert rev_row[2] == 1729000080000

    # Verify auxiliary tables are purged
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_text WHERE message_row_id = ?", (target_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM message_thumbnail WHERE message_row_id = ?", (target_id,)
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_step6b_phantom_cleanup_preserves_add_on_parents(mock_db, tmp_path):
    """Verify Step 6b phantom cleanup preserves messages with auxiliary records (F-10)."""
    conn = sqlite3.connect(mock_db)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS message_location (message_row_id INTEGER PRIMARY KEY, latitude REAL, longitude REAL)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS message_text (message_row_id INTEGER PRIMARY KEY, url TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS receipt_user (message_row_id INTEGER, receipt_user_jid_row_id INTEGER, read_timestamp INTEGER)"
    )

    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) VALUES (9001, 1, 0, 'K_RECEIPT', 1729000000000, 0, NULL)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) VALUES (9002, 1, 0, 'K_LOC', 1729000000000, 0, NULL)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) VALUES (9003, 1, 0, 'K_TEXT', 1729000000000, 0, NULL)"
    )
    cur.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) VALUES (9004, 1, 0, 'K_TRUE_PHANTOM', 1729000000000, 0, NULL)"
    )

    cur.execute(
        "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, read_timestamp) VALUES (9001, 1, 1729000005000)"
    )
    cur.execute(
        "INSERT INTO message_location (message_row_id, latitude, longitude) VALUES (9002, 45.0, 9.0)"
    )
    cur.execute(
        "INSERT INTO message_text (message_row_id, url) VALUES (9003, 'https://example.com')"
    )
    conn.commit()

    tables_to_check = [
        ("message_media", "message_row_id"),
        ("message_vcard", "message_row_id"),
        ("message_location", "message_row_id"),
        ("message_text", "message_row_id"),
        ("message_thumbnail", "message_row_id"),
        ("message_add_on", "parent_message_row_id"),
        ("receipt_user", "message_row_id"),
        ("message_system", "message_row_id"),
        ("message_revoked", "message_row_id"),
    ]
    del_clauses = ["message_type = 0", "(text_data IS NULL OR TRIM(text_data) = '')"]
    for tbl, col in tables_to_check:
        if bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
        ):
            del_clauses.append(f"NOT EXISTS (SELECT 1 FROM {tbl} t WHERE t.{col} = message._id)")
    cur.execute(f"DELETE FROM message WHERE {' AND '.join(del_clauses)}")
    conn.commit()

    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 9004").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 9001").fetchone()[0] == 1
    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 9002").fetchone()[0] == 1
    assert cur.execute("SELECT COUNT(*) FROM message WHERE _id = 9003").fetchone()[0] == 1
    conn.close()
