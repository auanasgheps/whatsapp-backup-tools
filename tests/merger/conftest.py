"""Shared fixtures and test helpers for increment_merger test suites."""

from __future__ import annotations

import sqlite3

import pytest
from tests.fixtures.protobuf_factory import (
    build_field,
    build_history_sync_frame,
    build_image_wmi,
    build_text_wmi,
    encode_varint,
)

_encode_varint = encode_varint
_build_field = build_field
_build_text_wmi = build_text_wmi
_build_image_wmi = build_image_wmi
_build_history_sync_frame = build_history_sync_frame


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
        CREATE TABLE audio_data (
            message_row_id INTEGER PRIMARY KEY,
            waveform BLOB
        );
        CREATE TABLE message_text (
            message_row_id INTEGER PRIMARY KEY,
            description TEXT,
            page_title TEXT,
            url TEXT,
            preview_type INTEGER
        );
        CREATE TABLE message_thumbnail (
            message_row_id INTEGER PRIMARY KEY,
            thumbnail BLOB
        );
        CREATE TABLE message_quoted (
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
        CREATE TABLE message_quoted_location (
            message_row_id INTEGER PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            place_name TEXT,
            place_address TEXT
        );
        CREATE TABLE message_quoted_vcard (
            message_row_id INTEGER PRIMARY KEY,
            vcard TEXT
        );
        CREATE TABLE message_mentions (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER NOT NULL,
            jid_row_id INTEGER NOT NULL
        );
        CREATE TABLE message_location (
            message_row_id INTEGER PRIMARY KEY,
            chat_row_id INTEGER,
            latitude REAL,
            longitude REAL,
            place_name TEXT,
            place_address TEXT,
            url TEXT,
            live_location_share_duration INTEGER
        );
        CREATE TABLE message_vcard (
            message_row_id INTEGER PRIMARY KEY,
            vcard TEXT
        );
        CREATE TABLE message_quoted_text (
            message_row_id INTEGER PRIMARY KEY,
            thumbnail BLOB
        );
        CREATE TABLE message_quoted_media (
            message_row_id INTEGER PRIMARY KEY,
            thumbnail BLOB
        );
        CREATE TABLE mms_thumbnail_metadata (
            message_row_id INTEGER PRIMARY KEY,
            direct_path TEXT,
            media_key BLOB,
            media_key_timestamp INTEGER,
            enc_thumb_hash TEXT,
            thumb_hash TEXT,
            thumb_width INTEGER,
            thumb_height INTEGER,
            transferred INTEGER DEFAULT 0
        );
        CREATE TABLE media_hash_thumbnail (
            media_hash TEXT PRIMARY KEY,
            thumbnail BLOB
        );
        CREATE TABLE jid_map (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            jid_row_id INTEGER NOT NULL,
            lid_row_id INTEGER NOT NULL
        );
        CREATE TABLE lid_display_name (
            lid_row_id INTEGER PRIMARY KEY,
            display_name TEXT
        );
        CREATE TABLE group_participant_user (
            _id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_jid_row_id INTEGER NOT NULL,
            user_jid_row_id INTEGER NOT NULL,
            admin INTEGER DEFAULT 0
        );

        INSERT INTO jid (user, server, raw_string) VALUES ('5550100', 's.whatsapp.net', '5550100@s.whatsapp.net');
        INSERT INTO chat (jid_row_id, subject) VALUES (1, NULL);
        INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
        VALUES (100, 1, 0, 'INITIAL_KEY_1', 1, 1729000000000, 0, 'Original message');
    """)
    conn.commit()
    conn.close()
    return str(db_path)


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
        + (_build_field(2, 2, msg_bytes) if text else b"")
        + _build_field(3, 0, timestamp)
        + _build_field(4, 0, status)
        + b"".join(ur_bytes_list)
    )
    return wmi_data


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
