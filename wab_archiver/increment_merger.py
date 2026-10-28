import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import struct
import zipfile
import zlib
from dataclasses import dataclass
from typing import Any

# ==============================================================================
# increment_merger.py — WhatsApp Android Incremental Backup Merger
#
# Implements pure-Python Protocol Buffers decoding for messages.bin, ZIP
# decompression / decryption of msgstore-increment packages, and sequential
# SQLite delta folding (entity mutations, deletions, messages, and sequences).
# ==============================================================================


# ---------------------------------------------------------------------------
# Pure-Python Protobuf Decoder
#
# Protobuf wire tags, enums, and message structure schemas cross-referenced with
# wppconnect-team/wa-proto (Apache-2.0) and WhiskeySockets/Baileys (MIT).
# ---------------------------------------------------------------------------


def parse_varint(stream: io.BytesIO) -> int | None:
    """Decode a variable-length integer from a binary stream. Returns None on EOF."""
    res = 0
    shift = 0
    while True:
        b = stream.read(1)
        if not b:
            if shift == 0:
                return None
            raise ValueError("Truncated varint in stream")
        val = b[0]
        res |= (val & 0x7F) << shift
        if not (val & 0x80):
            break
        shift += 7
    return res


# Mapping from Protobuf WebMessageInfo.StubType enum integers to Android message_system.action_type
PROTO_STUB_TO_ANDROID_ACTION: dict[int, int] = {
    20: 11,  # GROUP_CREATE -> GROUP_CREATED
    21: 1,  # GROUP_CHANGE_SUBJECT -> SUBJECT_CHANGE
    22: 6,  # GROUP_CHANGE_ICON -> ICON_CHANGED
    24: 27,  # GROUP_CHANGE_DESCRIPTION -> DESCRIPTION_CHANGED
    25: 58,  # GROUP_CHANGE_RESTRICT -> ANNOUNCEMENT_MODE
    26: 58,  # GROUP_CHANGE_ANNOUNCE -> ANNOUNCEMENT_MODE
    27: 12,  # GROUP_PARTICIPANT_ADD -> PARTICIPANT_ADDED
    28: 14,  # GROUP_PARTICIPANT_REMOVE -> PARTICIPANT_REMOVED
    29: 15,  # GROUP_PARTICIPANT_PROMOTE -> ADMIN_PROMOTED
    30: 20,  # GROUP_PARTICIPANT_DEMOTE -> ADMIN_DEMOTED
    31: 79,  # GROUP_PARTICIPANT_INVITE -> JOINED_VIA_INVITE_LINK
    32: 13,  # GROUP_PARTICIPANT_LEAVE -> PARTICIPANT_LEFT
    67: 67,  # E2E_IDENTITY_CHANGED / EPHEMERAL_TIMER
}


def parse_wire_fields(data: bytes) -> dict[int, list[tuple[int, bytes | int]]]:
    """Parse raw protobuf wire bytes into a mapping of field_number -> list of (wire_type, value)."""
    stream = io.BytesIO(data)
    fields: dict[int, list[tuple[int, bytes | int]]] = {}
    while True:
        tag = parse_varint(stream)
        if tag is None:
            break
        wire_type = tag & 0x07
        field_num = tag >> 3
        if wire_type == 0:
            val = parse_varint(stream)
            if val is None:
                raise ValueError(f"Truncated varint value for field {field_num}")
            fields.setdefault(field_num, []).append((wire_type, val))
        elif wire_type == 1:
            val_bytes = stream.read(8)
            if len(val_bytes) < 8:
                raise ValueError(f"Truncated 64-bit field {field_num}")
            fields.setdefault(field_num, []).append((wire_type, val_bytes))
        elif wire_type == 2:
            length = parse_varint(stream)
            if length is None:
                raise ValueError(f"Truncated length for field {field_num}")
            val_bytes = stream.read(length)
            if len(val_bytes) < length:
                raise ValueError(f"Truncated payload for field {field_num}")
            fields.setdefault(field_num, []).append((wire_type, val_bytes))
        elif wire_type == 5:
            val_bytes = stream.read(4)
            if len(val_bytes) < 4:
                raise ValueError(f"Truncated 32-bit field {field_num}")
            fields.setdefault(field_num, []).append((wire_type, val_bytes))
        else:
            raise ValueError(f"Unsupported wire type {wire_type} for field {field_num}")
    return fields


def parse_message_key(key_bytes: bytes) -> dict:
    """Parse a serialized MessageKey protobuf into a dictionary."""
    kf = parse_wire_fields(key_bytes)
    remote_jid = kf.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 1 in kf else ""
    from_me = 1 if (2 in kf and kf[2][0][1]) else 0
    key_id = kf.get(3, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 3 in kf else ""
    participant = kf.get(4, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 4 in kf else ""
    return {
        "remote_jid": remote_jid,
        "from_me": from_me,
        "key_id": key_id,
        "participant": participant,
    }


def unwrap_message_container(msg_fields: dict, max_depth: int) -> tuple[dict, dict]:
    """
    Recursively unwrap container messages (ephemeral, view-once, captioned document).
    Returns (unwrapped_fields, flags) where flags tracks is_view_once and is_ephemeral.
    """
    flags = {"is_view_once": 0, "is_ephemeral": 0}
    current_fields = msg_fields
    for _ in range(max_depth):
        # 40: ephemeralMessage -> field 1: message
        if 40 in current_fields and current_fields[40]:
            flags["is_ephemeral"] = 1
            payload = current_fields[40][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 37: viewOnceMessage -> field 1: message
        if 37 in current_fields and current_fields[37]:
            flags["is_view_once"] = 1
            payload = current_fields[37][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 55: viewOnceMessageV2 -> field 1: message
        if 55 in current_fields and current_fields[55]:
            flags["is_view_once"] = 1
            payload = current_fields[55][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 59: viewOnceMessageV2Extension -> field 1: message
        if 59 in current_fields and current_fields[59]:
            flags["is_view_once"] = 1
            payload = current_fields[59][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 53: documentWithCaptionMessage -> field 1: message
        if 53 in current_fields and current_fields[53]:
            payload = current_fields[53][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 58: editedMessage -> field 1: message
        if 58 in current_fields and current_fields[58]:
            payload = current_fields[58][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 62: groupMentionedMessage -> field 1: message
        if 62 in current_fields and current_fields[62]:
            payload = current_fields[62][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        # 67: botInvokeMessage -> field 1: message
        if 67 in current_fields and current_fields[67]:
            payload = current_fields[67][0][1]
            if isinstance(payload, bytes):
                inner = parse_wire_fields(payload)
                if 1 in inner and inner[1] and isinstance(inner[1][0][1], bytes):
                    current_fields = parse_wire_fields(inner[1][0][1])
                    continue
        break
    return current_fields, flags


PROTO_TO_ANDROID_STATUS: dict[int, int] = {
    0: 0,
    1: 0,
    2: 4,  # SERVER_ACK -> 4 (sent to server)
    3: 5,  # DELIVERY_ACK -> 5 (delivered to recipient)
    4: 13,  # READ -> 13 (read by recipient)
    5: 6,  # PLAYED -> 6 (played by recipient)
}


def extract_context_info(payload_fields: dict) -> dict | None:
    """Extract quoted message information from a ContextInfo protobuf field."""
    raw_ctx = None
    if 17 in payload_fields and isinstance(payload_fields[17][0][1], bytes):
        raw_ctx = payload_fields[17][0][1]
    elif 14 in payload_fields and isinstance(payload_fields[14][0][1], bytes):
        raw_ctx = payload_fields[14][0][1]
    elif 15 in payload_fields and isinstance(payload_fields[15][0][1], bytes):
        raw_ctx = payload_fields[15][0][1]
    if raw_ctx is None:
        return None

    ctx_fields = parse_wire_fields(raw_ctx)
    stanza_id = (
        ctx_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
        if 1 in ctx_fields
        else ""
    )
    participant = (
        ctx_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
        if 2 in ctx_fields
        else ""
    )

    quoted_text = None
    q_from_me = 0
    q_type = 0
    q_thumb = None
    if 3 in ctx_fields and isinstance(ctx_fields[3][0][1], bytes):
        raw_q_fields = parse_wire_fields(ctx_fields[3][0][1])
        q_fields, _ = unwrap_message_container(raw_q_fields, 4)
        if 1 in q_fields:
            quoted_text = q_fields[1][0][1].decode("utf-8", errors="replace")
            q_type = 0
        elif 6 in q_fields:
            sub = parse_wire_fields(q_fields[6][0][1])
            quoted_text = (
                sub.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 1 in sub else ""
            )
            q_type = 0
            if 16 in sub and isinstance(sub[16][0][1], bytes) and sub[16][0][1]:
                q_thumb = sub[16][0][1]
        elif 3 in q_fields:
            sub = parse_wire_fields(q_fields[3][0][1])
            quoted_text = (
                sub.get(3, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 3 in sub else ""
            )
            q_type = 1
            if 16 in sub and isinstance(sub[16][0][1], bytes) and sub[16][0][1]:
                q_thumb = sub[16][0][1]
        elif 7 in q_fields:
            sub = parse_wire_fields(q_fields[7][0][1])
            quoted_text = (
                (sub.get(3, [(2, b"")])[0][1] or sub.get(8, [(2, b"")])[0][1]).decode(
                    "utf-8", errors="replace"
                )
                if (3 in sub or 8 in sub)
                else ""
            )
            q_type = 9
            if 16 in sub and isinstance(sub[16][0][1], bytes) and sub[16][0][1]:
                q_thumb = sub[16][0][1]
        elif 9 in q_fields:
            sub = parse_wire_fields(q_fields[9][0][1])
            quoted_text = (
                sub.get(7, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 7 in sub else ""
            )
            q_type = 3
            if 16 in sub and isinstance(sub[16][0][1], bytes) and sub[16][0][1]:
                q_thumb = sub[16][0][1]
        elif 20 in q_fields or 26 in q_fields:
            stk_payload = q_fields[20][0][1] if 20 in q_fields else q_fields[26][0][1]
            sub = parse_wire_fields(stk_payload)
            q_type = 20
            quoted_text = ""
            if 16 in sub and isinstance(sub[16][0][1], bytes) and sub[16][0][1]:
                q_thumb = sub[16][0][1]

    if not stanza_id and not quoted_text and not participant and not q_thumb:
        return None

    return {
        "stanza_id": stanza_id,
        "participant": participant,
        "quoted_text": quoted_text or "",
        "from_me": q_from_me,
        "message_type": q_type,
        "thumbnail": q_thumb,
    }


def decode_wmi(wmi_bytes: bytes) -> dict:
    """Decode a serialized WebMessageInfo (WMI) protobuf into a normalized message dictionary."""
    fields = parse_wire_fields(wmi_bytes)

    key_id = ""
    from_me = 0
    remote_jid = ""
    participant = ""

    # Field 1: MessageKey (remoteJid=1, fromMe=2, id=3, participant=4)
    if 1 in fields:
        parsed_key = parse_message_key(fields[1][0][1])
        remote_jid = parsed_key["remote_jid"]
        from_me = parsed_key["from_me"]
        key_id = parsed_key["key_id"]
        participant = parsed_key["participant"]

    # Field 5: participant (optional string participant = 5 in modern WebMessageInfo)
    if not participant and 5 in fields and fields[5]:
        val = fields[5][0][1]
        if isinstance(val, bytes):
            participant = val.decode("utf-8", errors="replace").strip()

    # Field 21: fallback participant
    if not participant and 21 in fields and fields[21]:
        val = fields[21][0][1]
        if isinstance(val, bytes):
            participant = val.decode("utf-8", errors="replace").strip()

    # Field 3: messageTimestamp
    timestamp_raw = 0
    if 3 in fields:
        timestamp_raw = fields[3][0][1]
    # Convert seconds to milliseconds if timestamp is in Unix epoch seconds
    timestamp_ms = timestamp_raw * 1000 if timestamp_raw < 10000000000 else timestamp_raw

    # Field 4: status
    raw_status = fields.get(4, [(0, 0)])[0][1]
    status = PROTO_TO_ANDROID_STATUS.get(raw_status, raw_status) if from_me else 0

    # Field 16: starred
    starred = 1 if fields.get(16, [(0, 0)])[0][1] else 0

    # Field 39: photoChange (PhotoChange; e.g. group icon updates containing JPEG image bytes)
    # Field 40: userReceipt (repeated UserReceipt)
    user_receipts: list[dict] = []
    if 40 in fields:
        for tuple_item in fields[40]:
            ur_bytes = tuple_item[1]
            if not isinstance(ur_bytes, bytes):
                continue
            try:
                ur_fields = parse_wire_fields(ur_bytes)
            except Exception:
                continue
            ujid = (
                ur_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ur_fields
                else ""
            )
            if ujid and not is_valid_jid_string(ujid):
                continue
            rec_raw = ur_fields.get(2, [(0, 0)])[0][1] if 2 in ur_fields else 0
            read_raw = ur_fields.get(3, [(0, 0)])[0][1] if 3 in ur_fields else 0
            play_raw = ur_fields.get(4, [(0, 0)])[0][1] if 4 in ur_fields else 0

            def _ts_to_ms(ts_val: int) -> int | None:
                if not ts_val:
                    return None
                return ts_val * 1000 if ts_val < 10000000000 else ts_val

            user_receipts.append(
                {
                    "user_jid": ujid,
                    "receipt_timestamp": _ts_to_ms(rec_raw),
                    "read_timestamp": _ts_to_ms(read_raw),
                    "played_timestamp": _ts_to_ms(play_raw),
                }
            )

    # Field 19: pushName
    push_name = ""
    if 19 in fields and fields[19]:
        val = fields[19][0][1]
        if isinstance(val, bytes):
            push_name = val.decode("utf-8", errors="replace").strip()

    # Field 38: MediaData (localPath=1)
    local_path = None
    if 38 in fields and fields[38]:
        for tuple_item in fields[38]:
            md_bytes = tuple_item[1]
            if isinstance(md_bytes, bytes):
                try:
                    md_fields = parse_wire_fields(md_bytes)
                    if 1 in md_fields and isinstance(md_fields[1][0][1], bytes):
                        raw_lp = (
                            md_fields[1][0][1]
                            .decode("utf-8", errors="replace")
                            .strip()
                            .replace("\\", "/")
                        )
                        while raw_lp.startswith("./"):
                            raw_lp = raw_lp[2:]
                        if raw_lp.startswith("/"):
                            raw_lp = raw_lp.lstrip("/")
                        if raw_lp:
                            local_path = raw_lp
                            break
                except (ValueError, Exception):
                    pass

    # Field 24: messageStubType
    stub_type = None
    if 24 in fields and fields[24]:
        raw_stub = fields[24][0][1]
        if raw_stub == 1:
            # Protobuf StubType.REVOKE (1): message deleted by sender, maps to Android message_type = 15
            message_type = 15
            stub_type = None
        else:
            stub_type = PROTO_STUB_TO_ANDROID_ACTION.get(raw_stub, raw_stub)
            message_type = 7
    else:
        message_type = 0

    # Field 25: messageStubParameters
    stub_params: list[str] = []
    if 25 in fields and fields[25]:
        for sp in fields[25]:
            if isinstance(sp[1], bytes):
                stub_params.append(sp[1].decode("utf-8", errors="replace"))
    text_data = None
    media_info = None
    vcard = None
    reaction_info = None
    poll_info = None
    poll_vote_info = None
    pin_info = None
    location_info = None
    revoke_info = None
    edit_info = None
    quoted_info = None
    link_preview_info = None
    is_view_once = 0
    is_ephemeral = 0

    # Field 41: Embedded reactions (repeated Reaction in WMI)
    embedded_reactions: list[ReactionRecord] = []
    if 41 in fields and fields[41]:
        for entry in fields[41]:
            if not isinstance(entry[1], bytes):
                continue
            try:
                rx_fields = parse_wire_fields(entry[1])
                target_key = (
                    parse_message_key(rx_fields[1][0][1])
                    if 1 in rx_fields and isinstance(rx_fields[1][0][1], bytes)
                    else {}
                )
                rx_emoji = (
                    rx_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                    if 2 in rx_fields and isinstance(rx_fields[2][0][1], bytes)
                    else ""
                )
                rx_ts_raw = (
                    rx_fields.get(4, [(0, 0)])[0][1]
                    if 4 in rx_fields
                    else (rx_fields.get(3, [(0, 0)])[0][1] if 3 in rx_fields else 0)
                )
                rx_sender_ts = (
                    rx_ts_raw * 1000
                    if 0 < rx_ts_raw < 10000000000
                    else (rx_ts_raw if rx_ts_raw else timestamp_ms)
                )
                unread = rx_fields.get(5, [(0, 0)])[0][1] if 5 in rx_fields else 0
                embedded_reactions.append(
                    ReactionRecord(
                        target_key_id=target_key.get("key_id", ""),
                        target_remote_jid=target_key.get("remote_jid", ""),
                        target_from_me=target_key.get("from_me", 0),
                        reaction_key_id=target_key.get("key_id", ""),
                        reactor_jid=target_key.get("participant", ""),
                        emoji=rx_emoji,
                        sender_ts=rx_sender_ts,
                        is_revocation=(not rx_emoji),
                        unread=unread,
                    )
                )
            except (ValueError, Exception):
                pass

    # Field 45: Embedded poll votes (in poll creation/update WMI envelope)
    embedded_poll_votes: list[dict[str, Any]] = []
    if 45 in fields and fields[45]:
        for entry in fields[45]:
            if not isinstance(entry[1], bytes):
                continue
            try:
                sub = parse_wire_fields(entry[1])
                v_key = (
                    parse_message_key(sub[1][0][1])
                    if 1 in sub and isinstance(sub[1][0][1], bytes)
                    else {}
                )
                v_sender_ts = sub.get(3, [(0, 0)])[0][1] if 3 in sub else timestamp_ms
                v2 = (
                    parse_wire_fields(sub[2][0][1])
                    if 2 in sub and isinstance(sub[2][0][1], bytes)
                    else {}
                )
                selected_hashes = [
                    h[1] for h in v2.get(1, []) if isinstance(h[1], bytes) and len(h[1]) == 32
                ]
                embedded_poll_votes.append(
                    {
                        "key_id": v_key.get("key_id", ""),
                        "from_me": v_key.get("from_me", 0),
                        "participant": v_key.get("participant"),
                        "sender_ts": v_sender_ts,
                        "selected_option_hashes": selected_hashes,
                    }
                )
            except (ValueError, Exception):
                pass

    # Field 2: Message payload
    if 2 in fields:
        raw_msg_fields = parse_wire_fields(fields[2][0][1])
        msg_fields, container_flags = unwrap_message_container(raw_msg_fields, 4)
        is_view_once = container_flags["is_view_once"]
        is_ephemeral = container_flags["is_ephemeral"]

        # 1: conversation (plain text)
        if 1 in msg_fields:
            message_type = 0
            text_data = msg_fields[1][0][1].decode("utf-8", errors="replace")

        # 3: imageMessage
        elif 3 in msg_fields:
            message_type = 42 if is_view_once else 1
            img_fields = parse_wire_fields(msg_fields[3][0][1])
            caption = (
                img_fields.get(3, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 3 in img_fields
                else None
            )
            text_data = caption
            mimetype = (
                img_fields.get(2, [(2, b"image/jpeg")])[0][1].decode("utf-8", errors="replace")
                if 2 in img_fields
                else "image/jpeg"
            )
            url = (
                img_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in img_fields
                else ""
            )
            file_sha256 = img_fields.get(4, [(2, b"")])[0][1] if 4 in img_fields else b""
            file_length = img_fields.get(5, [(0, 0)])[0][1] if 5 in img_fields else 0
            media_info = {
                "mime_type": mimetype,
                "file_size": file_length,
                "file_sha256": file_sha256,
                "media_name": caption,
                "message_url": url,
                "media_type": "image",
            }

        # 4: contactMessage
        elif 4 in msg_fields:
            message_type = 4
            contact_fields = parse_wire_fields(msg_fields[4][0][1])
            display_name = (
                contact_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in contact_fields
                else None
            )
            vcard_text = (
                contact_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in contact_fields
                else None
            )
            text_data = display_name
            vcard = vcard_text

        # 5: locationMessage
        elif 5 in msg_fields:
            message_type = 5
            loc_fields = parse_wire_fields(msg_fields[5][0][1])
            lat = (
                struct.unpack("<d", loc_fields[1][0][1])[0]
                if 1 in loc_fields and len(loc_fields[1][0][1]) == 8
                else 0.0
            )
            lng = (
                struct.unpack("<d", loc_fields[2][0][1])[0]
                if 2 in loc_fields and len(loc_fields[2][0][1]) == 8
                else 0.0
            )
            name = (
                loc_fields.get(3, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 3 in loc_fields
                else None
            )
            address = (
                loc_fields.get(4, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 4 in loc_fields
                else None
            )
            url = (
                loc_fields.get(5, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 5 in loc_fields
                else None
            )
            comment = (
                loc_fields.get(7, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 7 in loc_fields
                else None
            )
            text_data = name or address or comment or f"{lat}, {lng}"
            location_info = {
                "latitude": lat,
                "longitude": lng,
                "place_name": name,
                "place_address": address,
                "url": url,
            }

        # 6: extendedTextMessage
        elif 6 in msg_fields:
            message_type = 0
            ext_fields = parse_wire_fields(msg_fields[6][0][1])
            text_data = (
                ext_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ext_fields
                else None
            )
            matched_text = (
                ext_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in ext_fields
                else None
            )
            canonical_url = (
                ext_fields.get(4, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 4 in ext_fields
                else None
            )
            description = (
                ext_fields.get(5, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 5 in ext_fields
                else None
            )
            title = (
                ext_fields.get(6, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 6 in ext_fields
                else None
            )
            preview_type = ext_fields.get(10, [(0, 0)])[0][1] if 10 in ext_fields else 0
            jpeg_thumbnail = (
                ext_fields.get(16, [(2, b"")])[0][1]
                if 16 in ext_fields and ext_fields[16][0][1]
                else None
            )
            if matched_text or canonical_url or description or title or jpeg_thumbnail:
                link_preview_info = {
                    "url": canonical_url or matched_text,
                    "title": title,
                    "description": description,
                    "preview_type": preview_type,
                    "thumbnail": jpeg_thumbnail,
                }

        # 7: documentMessage
        elif 7 in msg_fields:
            message_type = 9
            doc_fields = parse_wire_fields(msg_fields[7][0][1])
            title = (
                doc_fields.get(3, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 3 in doc_fields
                else None
            )
            file_name = (
                doc_fields.get(8, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 8 in doc_fields
                else title
            )
            text_data = title or file_name
            mimetype = (
                doc_fields.get(2, [(2, b"application/octet-stream")])[0][1].decode(
                    "utf-8", errors="replace"
                )
                if 2 in doc_fields
                else "application/octet-stream"
            )
            url = (
                doc_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in doc_fields
                else ""
            )
            file_sha256 = doc_fields.get(4, [(2, b"")])[0][1] if 4 in doc_fields else b""
            file_length = doc_fields.get(5, [(0, 0)])[0][1] if 5 in doc_fields else 0
            media_info = {
                "mime_type": mimetype,
                "file_size": file_length,
                "file_sha256": file_sha256,
                "media_name": file_name,
                "message_url": url,
                "media_type": "document",
            }

        # 8: audioMessage
        elif 8 in msg_fields:
            message_type = 82 if is_view_once else 2
            aud_fields = parse_wire_fields(msg_fields[8][0][1])
            mimetype = (
                aud_fields.get(2, [(2, b"audio/ogg")])[0][1].decode("utf-8", errors="replace")
                if 2 in aud_fields
                else "audio/ogg"
            )
            url = (
                aud_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in aud_fields
                else ""
            )
            file_sha256 = aud_fields.get(3, [(2, b"")])[0][1] if 3 in aud_fields else b""
            file_length = aud_fields.get(4, [(0, 0)])[0][1] if 4 in aud_fields else 0
            is_ptt = bool(aud_fields.get(6, [(0, 0)])[0][1]) if 6 in aud_fields else False
            media_info = {
                "mime_type": mimetype,
                "file_size": file_length,
                "file_sha256": file_sha256,
                "media_name": None,
                "message_url": url,
                "media_type": "audio",
                "ptt": is_ptt,
            }

        # 9: videoMessage
        elif 9 in msg_fields:
            message_type = 43 if is_view_once else 3
            vid_fields = parse_wire_fields(msg_fields[9][0][1])
            caption = (
                vid_fields.get(7, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 7 in vid_fields
                else None
            )
            text_data = caption
            mimetype = (
                vid_fields.get(2, [(2, b"video/mp4")])[0][1].decode("utf-8", errors="replace")
                if 2 in vid_fields
                else "video/mp4"
            )
            url = (
                vid_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in vid_fields
                else ""
            )
            file_sha256 = vid_fields.get(3, [(2, b"")])[0][1] if 3 in vid_fields else b""
            file_length = vid_fields.get(4, [(0, 0)])[0][1] if 4 in vid_fields else 0
            media_info = {
                "mime_type": mimetype,
                "file_size": file_length,
                "file_sha256": file_sha256,
                "media_name": caption,
                "message_url": url,
                "media_type": "video",
            }

        # 12: protocolMessage (Revoke / Message Edit)
        elif 12 in msg_fields:
            proto_fields = parse_wire_fields(msg_fields[12][0][1])
            target_key = (
                parse_message_key(proto_fields[1][0][1])
                if 1 in proto_fields and isinstance(proto_fields[1][0][1], bytes)
                else {}
            )
            proto_type = proto_fields.get(2, [(0, 0)])[0][1] if 2 in proto_fields else 0
            if proto_type == 0:  # REVOKE
                message_type = 15
                revoke_info = {"target_key_id": target_key.get("key_id", "")}
            elif proto_type == 14:  # MESSAGE_EDIT
                edited_ts = (
                    proto_fields.get(10, [(0, 0)])[0][1] if 10 in proto_fields else timestamp_ms
                )
                new_text = None
                if 14 in proto_fields and isinstance(proto_fields[14][0][1], bytes):
                    edit_msg_fields = parse_wire_fields(proto_fields[14][0][1])
                    unwrapped_edit, _ = unwrap_message_container(edit_msg_fields, 4)
                    if 1 in unwrapped_edit:
                        new_text = unwrapped_edit[1][0][1].decode("utf-8", errors="replace")
                    elif 6 in unwrapped_edit:
                        ext = parse_wire_fields(unwrapped_edit[6][0][1])
                        new_text = (
                            ext.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                            if 1 in ext
                            else None
                        )
                message_type = 0
                text_data = new_text
                edit_info = {
                    "target_key_id": target_key.get("key_id", ""),
                    "new_text": new_text,
                    "edited_ts": edited_ts,
                }

        # 13: contactsArrayMessage
        elif 13 in msg_fields:
            message_type = 14
            ca_fields = parse_wire_fields(msg_fields[13][0][1])
            text_data = (
                ca_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ca_fields
                else None
            )

        # 18: liveLocationMessage
        elif 18 in msg_fields:
            message_type = 16
            ll_fields = parse_wire_fields(msg_fields[18][0][1])
            lat = (
                struct.unpack("<d", ll_fields[1][0][1])[0]
                if 1 in ll_fields and len(ll_fields[1][0][1]) == 8
                else 0.0
            )
            lng = (
                struct.unpack("<d", ll_fields[2][0][1])[0]
                if 2 in ll_fields and len(ll_fields[2][0][1]) == 8
                else 0.0
            )
            caption = (
                ll_fields.get(6, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 6 in ll_fields
                else None
            )
            duration = ll_fields.get(8, [(0, 0)])[0][1] if 8 in ll_fields else 0
            seq_num = ll_fields.get(7, [(0, 0)])[0][1] if 7 in ll_fields else 0
            text_data = caption or f"Live Location: {lat}, {lng}"
            location_info = {
                "latitude": lat,
                "longitude": lng,
                "place_name": None,
                "place_address": None,
                "url": None,
                "duration": duration,
                "sequence_number": seq_num,
            }

        # 20 or 26: stickerMessage
        elif 20 in msg_fields or 26 in msg_fields:
            message_type = 20
            stk_payload = msg_fields[20][0][1] if 20 in msg_fields else msg_fields[26][0][1]
            stk_fields = parse_wire_fields(stk_payload)
            mimetype = (
                stk_fields.get(5, [(2, b"image/webp")])[0][1].decode("utf-8", errors="replace")
                if 5 in stk_fields
                else "image/webp"
            )
            url = (
                stk_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in stk_fields
                else ""
            )
            file_sha256 = stk_fields.get(2, [(2, b"")])[0][1] if 2 in stk_fields else b""
            file_length = stk_fields.get(9, [(0, 0)])[0][1] if 9 in stk_fields else 0
            media_info = {
                "mime_type": mimetype,
                "file_size": file_length,
                "file_sha256": file_sha256,
                "media_name": None,
                "message_url": url,
                "media_type": "sticker",
            }

        # 42: buttonsMessage
        elif 42 in msg_fields:
            message_type = 0
            btn_fields = parse_wire_fields(msg_fields[42][0][1])
            text_data = (
                btn_fields.get(6, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 6 in btn_fields
                else None
            )

        # 45: interactiveMessage
        elif 45 in msg_fields:
            message_type = 0
            im_fields = parse_wire_fields(msg_fields[45][0][1])
            if 1 in im_fields and isinstance(im_fields[1][0][1], bytes):
                body_fields = parse_wire_fields(im_fields[1][0][1])
                text_data = (
                    body_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                    if 1 in body_fields
                    else None
                )

        # 16, 46, 48, 56: reactionMessage (Tag 16 standard, 46 official, 48 backwards-compat, 56 encReactionMessage)
        elif any(t in msg_fields for t in (16, 46, 48, 56)):
            message_type = 25
            rx_tag = next(t for t in (16, 46, 48, 56) if t in msg_fields)
            react_payload = msg_fields[rx_tag][0][1]
            react_fields = parse_wire_fields(react_payload)
            target_key = (
                parse_message_key(react_fields[1][0][1])
                if 1 in react_fields and isinstance(react_fields[1][0][1], bytes)
                else {}
            )
            text_data = (
                react_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in react_fields and isinstance(react_fields[2][0][1], bytes)
                else ""
            )
            sender_ts_raw = (
                react_fields.get(4, [(0, 0)])[0][1]
                if 4 in react_fields
                else (react_fields.get(3, [(0, 0)])[0][1] if 3 in react_fields else 0)
            )
            sender_ts = (
                sender_ts_raw * 1000
                if 0 < sender_ts_raw < 10000000000
                else (sender_ts_raw if sender_ts_raw else timestamp_ms)
            )
            reaction_info = {
                "target_key_id": target_key.get("key_id", ""),
                "target_remote_jid": target_key.get("remote_jid", ""),
                "target_from_me": target_key.get("from_me", 0),
                "target_participant": target_key.get("participant", ""),
                "emoji": text_data,
                "sender_ts": sender_ts,
            }

        # 49, 60, 64, 93, 111: pollCreationMessage (v1, v2, v3, v4, v5)
        elif any(t in msg_fields for t in (49, 60, 64, 93, 111)):
            message_type = 66
            poll_tag = next(t for t in (49, 60, 64, 93, 111) if t in msg_fields)
            raw_poll_payload = msg_fields[poll_tag][0][1]
            poll_fields = parse_wire_fields(raw_poll_payload)

            # Check if wrapped in FutureProofMessage (field 1 contains inner message or poll)
            if 1 in poll_fields and isinstance(poll_fields[1][0][1], bytes):
                try:
                    inner_candidate = parse_wire_fields(poll_fields[1][0][1])
                    nested_tag = next(
                        (t for t in (49, 60, 64, 93, 111) if t in inner_candidate), None
                    )
                    if nested_tag is not None and isinstance(
                        inner_candidate[nested_tag][0][1], bytes
                    ):
                        poll_fields = parse_wire_fields(inner_candidate[nested_tag][0][1])
                    elif 2 in inner_candidate and any(
                        isinstance(opt[1], bytes) for opt in inner_candidate.get(2, [])
                    ):
                        poll_fields = inner_candidate
                except Exception:
                    pass

            # Polymorphic field extraction:
            # Pattern A (V1, Tag 49): 1=encKey, 2=name (str), 3=options, 4=selectableCount
            # Pattern B (V3/V4/V5, Tag 64/93/111): 1=name (str), 2=options, 3=selectableCount
            name = ""
            options = []
            selectable_count = 0

            tag3_has_options = False
            if 3 in poll_fields and poll_fields[3]:
                first_opt = poll_fields[3][0][1]
                if isinstance(first_opt, bytes):
                    try:
                        f_opt = parse_wire_fields(first_opt)
                        if 1 in f_opt:
                            tag3_has_options = True
                    except Exception:
                        pass

            if tag3_has_options:
                if 2 in poll_fields and isinstance(poll_fields[2][0][1], bytes):
                    name = poll_fields[2][0][1].decode("utf-8", errors="replace")
                raw_opts = poll_fields.get(3, [])
                selectable_count = poll_fields.get(4, [(0, 0)])[0][1] if 4 in poll_fields else 0
            else:
                if 1 in poll_fields and isinstance(poll_fields[1][0][1], bytes):
                    name = poll_fields[1][0][1].decode("utf-8", errors="replace")
                raw_opts = poll_fields.get(2, [])
                selectable_count = poll_fields.get(3, [(0, 0)])[0][1] if 3 in poll_fields else 0

            for opt_tuple in raw_opts:
                if isinstance(opt_tuple[1], bytes):
                    try:
                        opt_f = parse_wire_fields(opt_tuple[1])
                        opt_name = (
                            opt_f.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                            if 1 in opt_f
                            else ""
                        )
                        if opt_name:
                            options.append(opt_name)
                    except Exception:
                        pass

            text_data = name

            m_secret = None
            for container_fields in (msg_fields, poll_fields):
                if 35 in container_fields and isinstance(container_fields[35][0][1], bytes):
                    try:
                        ctx_f = parse_wire_fields(container_fields[35][0][1])
                        if (
                            3 in ctx_f
                            and isinstance(ctx_f[3][0][1], bytes)
                            and len(ctx_f[3][0][1]) == 32
                        ):
                            m_secret = ctx_f[3][0][1]
                            break
                    except (ValueError, Exception):
                        pass

            if quoted_info is None:
                quoted_info = extract_context_info(poll_fields)

            poll_info = {
                "question": name,
                "options": options,
                "selectable_count": selectable_count,
                "message_secret": m_secret,
                "embedded_votes": embedded_poll_votes,
            }

        # 50: pollUpdateMessage (poll vote)
        elif 50 in msg_fields:
            message_type = 67
            pu_fields = parse_wire_fields(msg_fields[50][0][1])
            poll_key = (
                parse_message_key(pu_fields[1][0][1])
                if 1 in pu_fields and isinstance(pu_fields[1][0][1], bytes)
                else {}
            )
            sender_ts = pu_fields.get(3, [(0, 0)])[0][1] if 3 in pu_fields else timestamp_ms
            opt_hashes: list[bytes] = []
            enc_payload: bytes | None = None
            enc_iv: bytes | None = None
            if 2 in pu_fields and isinstance(pu_fields[2][0][1], bytes):
                try:
                    v2_fields = parse_wire_fields(pu_fields[2][0][1])
                    for h in v2_fields.get(1, []) + v2_fields.get(3, []):
                        if isinstance(h[1], bytes) and len(h[1]) == 32:
                            opt_hashes.append(h[1])
                    if not opt_hashes:
                        if 1 in v2_fields and isinstance(v2_fields[1][0][1], bytes):
                            enc_payload = v2_fields[1][0][1]
                        if (
                            2 in v2_fields
                            and isinstance(v2_fields[2][0][1], bytes)
                            and len(v2_fields[2][0][1]) == 12
                        ):
                            enc_iv = v2_fields[2][0][1]
                except (ValueError, Exception):
                    pass
            poll_vote_info = {
                "poll_key_id": poll_key.get("key_id", ""),
                "sender_ts": sender_ts,
                "selected_option_hashes": opt_hashes,
                "enc_payload": enc_payload,
                "enc_iv": enc_iv,
            }

        # 63: pinInChatMessage
        elif 63 in msg_fields:
            message_type = 79
            pin_fields = parse_wire_fields(msg_fields[63][0][1])
            target_key = (
                parse_message_key(pin_fields[1][0][1])
                if 1 in pin_fields and isinstance(pin_fields[1][0][1], bytes)
                else {}
            )
            pin_type = pin_fields.get(2, [(0, 1)])[0][1] if 2 in pin_fields else 1
            sender_ts = pin_fields.get(3, [(0, 0)])[0][1] if 3 in pin_fields else timestamp_ms
            pin_info = {
                "target_key_id": target_key.get("key_id", ""),
                "pin_state": 1 if pin_type == 1 else 0,
                "sender_ts": sender_ts,
            }

        # 66: ptvMessage (Push-To-Video / Video Note)
        elif 66 in msg_fields:
            message_type = 28
            ptv_fields = parse_wire_fields(msg_fields[66][0][1])
            mimetype = (
                ptv_fields.get(2, [(2, b"video/mp4")])[0][1].decode("utf-8", errors="replace")
                if 2 in ptv_fields
                else "video/mp4"
            )
            url = (
                ptv_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ptv_fields
                else ""
            )
            file_sha256 = ptv_fields.get(3, [(2, b"")])[0][1] if 3 in ptv_fields else b""
            file_length = ptv_fields.get(4, [(0, 0)])[0][1] if 4 in ptv_fields else 0
            media_info = {
                "mime_type": mimetype,
                "file_size": file_length,
                "file_sha256": file_sha256,
                "media_name": None,
                "message_url": url,
                "media_type": "video",
            }

        # 75: eventMessage (Scheduled Calendar Event)
        elif 75 in msg_fields:
            message_type = 0
            ev_fields = parse_wire_fields(msg_fields[75][0][1])
            title = (
                ev_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ev_fields
                else ""
            )
            desc = (
                ev_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in ev_fields
                else ""
            )
            text_data = (
                f"📅 {title}" + (f" - {desc}" if desc else "") if title else (desc or "📅 Event")
            )

        if quoted_info is None:
            for sub_tag in (6, 3, 7, 8, 9, 20, 4, 5, 18, 42, 45, 75):
                if (
                    sub_tag in msg_fields
                    and msg_fields[sub_tag]
                    and isinstance(msg_fields[sub_tag][0][1], bytes)
                ):
                    try:
                        sub_fields = parse_wire_fields(msg_fields[sub_tag][0][1])
                        qi = extract_context_info(sub_fields)
                        if qi:
                            quoted_info = qi
                            break
                    except (ValueError, Exception):
                        pass

    if media_info is not None and local_path:
        media_info["file_path"] = local_path

    has_content = bool(
        (text_data is not None and text_data != "")
        or media_info is not None
        or vcard is not None
        or poll_info is not None
        or poll_vote_info is not None
        or reaction_info is not None
        or pin_info is not None
        or location_info is not None
        or revoke_info is not None
        or edit_info is not None
        or link_preview_info is not None
        or embedded_poll_votes
        or embedded_reactions
        or stub_type is not None
        or message_type in (15, 64)
        or user_receipts
        or (from_me and status > 0)
    )

    return {
        "key_id": key_id,
        "from_me": from_me,
        "remote_jid": remote_jid,
        "participant": participant,
        "timestamp": timestamp_ms,
        "status": status,
        "starred": starred,
        "message_type": message_type,
        "text_data": text_data,
        "media_info": media_info,
        "local_path": local_path,
        "vcard": vcard,
        "reaction_info": reaction_info,
        "embedded_reactions": embedded_reactions,
        "poll_info": poll_info,
        "poll_vote_info": poll_vote_info,
        "pin_info": pin_info,
        "location_info": location_info,
        "revoke_info": revoke_info,
        "edit_info": edit_info,
        "is_view_once": is_view_once,
        "is_ephemeral": is_ephemeral,
        "user_receipts": user_receipts,
        "quoted_info": quoted_info,
        "push_name": push_name,
        "embedded_poll_votes": embedded_poll_votes,
        "link_preview_info": link_preview_info,
        "stub_type": stub_type,
        "stub_params": stub_params,
        "has_content": has_content,
    }


_VALID_JID_SERVERS = (
    "s.whatsapp.net",
    "g.us",
    "lid",
    "broadcast",
    "newsletter",
    "bot",
    "temp",
    "hosted",
    "hosted.lid",
    "status_me",
    "lid_me",
)


def sanitize_jid_string(jid_str: str) -> str:
    """Strip companion device and agent tokens from a JID string."""
    if not isinstance(jid_str, str) or not jid_str:
        return ""
    if "@" in jid_str:
        user, server = jid_str.split("@", 1)
        if server in ("s.whatsapp.net", "lid"):
            if ":" in user:
                user = user.split(":", 1)[0]
            if "." in user:
                user = user.split(".", 1)[0]
        elif ":" in user:
            user = user.split(":", 1)[0]
        return f"{user}@{server}"
    if ":" in jid_str:
        jid_str = jid_str.split(":", 1)[0]
    if "." in jid_str:
        jid_str = jid_str.split(".", 1)[0]
    return jid_str


def is_valid_jid_string(s: str) -> bool:
    """Validate that a string conforms to a WhatsApp JID structure."""
    if not isinstance(s, str) or not s or len(s) > 150:
        return False
    if any(ord(c) < 32 or ord(c) > 126 for c in s):
        return False
    if "@" in s:
        user_part, server_part = s.split("@", 1)
        if ":" in user_part:
            user_part = user_part.split(":", 1)[0]
        if not user_part:
            return False
        if server_part not in _VALID_JID_SERVERS and not server_part.endswith(
            (".whatsapp.net", ".g.us")
        ):
            return False
        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", user_part):
            return False
        return True
    return s == "0"


def extract_messages_from_proto_frame(data: bytes) -> list[dict]:
    """
    Extract a list of normalized message dicts from a single protobuf frame.
    Supports both HistorySync payloads and direct WebMessageInfo records.
    """
    try:
        fields = parse_wire_fields(data)
    except (ValueError, Exception):
        return []

    # HistorySync: field 2 contains repeated conversations
    if 2 in fields:
        results = []
        for conv_tuple in fields[2]:
            conv_bytes = conv_tuple[1]
            if not isinstance(conv_bytes, bytes):
                continue
            try:
                conv_fields = parse_wire_fields(conv_bytes)
            except (ValueError, Exception):
                continue
            conv_id = ""
            if 1 in conv_fields:
                conv_id = conv_fields[1][0][1].decode("utf-8", errors="replace")

            # Repeated HistorySyncMsg records (field 2 of Conversation)
            for hsm_tuple in conv_fields.get(2, []):
                hsm_bytes = hsm_tuple[1]
                if not isinstance(hsm_bytes, bytes):
                    continue
                try:
                    hsm_fields = parse_wire_fields(hsm_bytes)
                except (ValueError, Exception):
                    continue
                # WebMessageInfo is field 1 of HistorySyncMsg
                for wmi_tuple in hsm_fields.get(1, []):
                    wmi_bytes = wmi_tuple[1]
                    if isinstance(wmi_bytes, bytes):
                        try:
                            msg = decode_wmi(wmi_bytes)
                        except (ValueError, Exception):
                            continue
                        if not msg.get("remote_jid") and conv_id:
                            msg["remote_jid"] = conv_id
                        if is_valid_jid_string(msg.get("remote_jid", "")):
                            results.append(msg)
        if results:
            return results

    # Direct WebMessageInfo: field 1 contains MessageKey
    if 1 in fields:
        try:
            msg = decode_wmi(data)
        except (ValueError, Exception):
            return []
        if msg and is_valid_jid_string(msg.get("remote_jid", "")):
            return [msg]

    return []


def read_delimited_messages_bin(messages_bin_path: str) -> list[dict]:
    """Read all length-delimited protobuf records from a messages.bin file."""
    messages = []
    with open(messages_bin_path, "rb") as f:
        data = f.read()
    stream = io.BytesIO(data)
    while True:
        frame_len = parse_varint(stream)
        if frame_len is None:
            break
        frame_bytes = stream.read(frame_len)
        if len(frame_bytes) < frame_len:
            raise ValueError(
                f"Truncated message frame in '{messages_bin_path}': "
                f"expected {frame_len} bytes, got {len(frame_bytes)}"
            )
        extracted = extract_messages_from_proto_frame(frame_bytes)
        messages.extend(extracted)
    return messages


# ---------------------------------------------------------------------------
# Decryption & Unpacking
# ---------------------------------------------------------------------------


def resolve_e2e_key(e2e_key: object, logger: logging.Logger) -> object:
    """Resolve an e2e key input (str, file path, or KeyFactory object) into a Key object."""
    if not isinstance(e2e_key, str):
        return e2e_key

    try:
        from wa_crypt_tools.lib.key.keyfactory import KeyFactory
    except ImportError:
        logger.error("wa-crypt-tools is required to decrypt incremental backups.")
        raise

    key_obj = KeyFactory.new(e2e_key)
    if key_obj is None and os.path.isfile(e2e_key):
        try:
            with open(e2e_key, encoding="utf-8") as f:
                hex_candidate = f.read().strip()
            if len(hex_candidate) == 64:
                key_obj = KeyFactory.new(hex_candidate)
        except OSError:
            pass

    if key_obj is None:
        raise ValueError(
            f"Could not load decryption key from: {e2e_key}\n"
            "  Make sure --e2e-key points to a valid WhatsApp key file or 64-character hex key."
        )
    return key_obj


def decrypt_increment_archive(
    encrypted_path: str, e2e_key: object, dest_zip_path: str, logger: logging.Logger
) -> str:
    """Decrypt a msgstore-increment-*.db.crypt15 file into a standard ZIP archive."""
    try:
        from wa_crypt_tools.lib.db.dbfactory import DatabaseFactory
    except ImportError:
        logger.error("wa-crypt-tools is required to decrypt incremental backups.")
        raise

    key_obj = resolve_e2e_key(e2e_key, logger)

    with open(encrypted_path, "rb") as f:
        db_file = DatabaseFactory.from_file(f)
        if db_file is None:
            raise ValueError(
                f"Failed to parse encrypted database header from increment '{encrypted_path}'."
            )
        encrypted_payload = f.read()

    decrypted = db_file.decrypt(key_obj, encrypted_payload)
    try:
        payload = zlib.decompressobj().decompress(decrypted)
    except zlib.error:
        payload = decrypted

    if not payload.startswith(b"PK\x03\x04"):
        raise ValueError(
            f"Decrypted payload for '{encrypted_path}' is not a valid ZIP archive (magic bytes mismatch)."
        )

    os.makedirs(os.path.dirname(os.path.abspath(dest_zip_path)), exist_ok=True)
    with open(dest_zip_path, "wb") as out:
        out.write(payload)
    return dest_zip_path


def unpack_increment_archive(
    encrypted_path: str, e2e_key: object, dest_dir: str, logger: logging.Logger
) -> str:
    """Decrypt and unpack an increment package into dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)

    # Check if the file is already a ZIP archive (e.g. unencrypted mock or test fixture)
    if zipfile.is_zipfile(encrypted_path):
        with zipfile.ZipFile(encrypted_path, "r") as z:
            z.extractall(dest_dir)
        return dest_dir

    temp_zip = os.path.join(dest_dir, "_temp_increment.zip")
    try:
        decrypt_increment_archive(encrypted_path, e2e_key, temp_zip, logger)
        with zipfile.ZipFile(temp_zip, "r") as z:
            z.extractall(dest_dir)
    finally:
        if os.path.exists(temp_zip):
            with contextlib.suppress(OSError):
                os.remove(temp_zip)
    return dest_dir


# ---------------------------------------------------------------------------
# Incremental Database Replay
# ---------------------------------------------------------------------------


_INCREMENT_REGEX = re.compile(r"msgstore-increment-(\d+)(?:-([\d\.\-]+))?")


def filter_active_increments(paths: list[str], base_filename: str | None) -> list[str]:
    """Filter and sort increment paths belonging to the active backup generation.

    If multiple backup cycles exist (e.g. duplicate increment IDs across different tags),
    selects the generation that matches the base database or the latest active generation.
    """
    pattern = re.compile(r"msgstore-increment-(\d+)(?:-([a-zA-Z0-9_\-\.]+?))?(?:\.db)?\.crypt\d+$")
    fallback_pattern = re.compile(r"msgstore-increment-(\d+)")

    candidates: list[tuple[int, str, str]] = []
    for path in paths:
        entry = os.path.basename(path.replace("\\", "/"))
        if entry.endswith(".old") or entry.endswith(".tmp") or entry.endswith(".incoming"):
            continue
        m = pattern.search(entry)
        if m:
            candidates.append((int(m.group(1)), m.group(2) or "", path))
        else:
            m_fb = fallback_pattern.search(entry)
            if m_fb:
                candidates.append((int(m_fb.group(1)), "", path))

    if not candidates:
        return []

    by_tag: dict[str, list[tuple[int, str]]] = {}
    for inc_id, tag, path in candidates:
        by_tag.setdefault(tag, []).append((inc_id, path))

    matched_base_tag: str | None = None
    if base_filename:
        base_entry = os.path.basename(base_filename.replace("\\", "/"))
        base_match = re.search(r"msgstore-([a-zA-Z0-9_\-\.]+?)(?:\.db)?\.crypt\d+$", base_entry)
        if base_match:
            candidate_base_tag = base_match.group(1)
            if candidate_base_tag in by_tag:
                matched_base_tag = candidate_base_tag
        else:
            # Base filename has no date tag (e.g. msgstore.db.crypt15, msgstore.db)
            # In WhatsApp's lifecycle, untagged increments ('') belong to the active untagged base.
            if "" in by_tag:
                matched_base_tag = ""

    if matched_base_tag is not None:
        selected = by_tag[matched_base_tag]
    elif len(by_tag) > 1:
        # Multiple generations detected without a matched base tag.
        # In WhatsApp's lifecycle, undated files ('') represent the active live generation
        # and supersede historical rotated dates.
        # If no undated files exist, select the latest dated generation tag.
        if "" in by_tag:
            latest_tag = ""
        else:
            tagged_keys = [k for k in by_tag.keys() if k]
            latest_tag = max(tagged_keys) if tagged_keys else ""
        selected = by_tag[latest_tag]
    else:
        selected = [(inc_id, path) for inc_id, _, path in candidates]

    # Deduplicate by inc_id if any duplicates remain in selected
    seen: dict[int, str] = {}
    for inc_id, path in selected:
        seen[inc_id] = path
    return [seen[inc_id] for inc_id in sorted(seen.keys())]


def find_increment_files(search_dir: str, base_filename: str | None = None) -> list[str]:
    """Search a directory for files matching 'msgstore-increment-(\\d+)',

    excluding temporary swap artifacts, sorted by sequential integer index.
    If multiple backup generations exist (duplicate increment IDs across tags),
    selects the generation that matches base_filename or the latest active generation.
    """
    if not os.path.isdir(search_dir):
        return []

    entries = [os.path.join(search_dir, e) for e in os.listdir(search_dir)]
    if base_filename is None:
        for candidate in ("msgstore.db.crypt15", "msgstore.db.crypt14", "msgstore.db"):
            candidate_path = os.path.join(search_dir, candidate)
            if os.path.isfile(candidate_path):
                base_filename = candidate_path
                break
    return filter_active_increments(entries, base_filename)


_ENTITY_TABLE_PRIORITY: dict[str, int] = {
    "props": 10,
    "jid": 20,
    "jid_map": 30,
    "lid_display_name": 40,
    "chat": 50,
    "group_participant_user": 60,
    "group_participants": 61,
    "group_past_participant_user": 62,
    "call_log": 70,
    "call_log_participant_v2": 71,
}

_SUPPORTED_ENTITY_TABLES: frozenset[str] = frozenset(
    {
        "props",
        "jid",
        "chat",
        "jid_map",
        "lid_display_name",
        "group_participant_user",
        "group_participants",
        "group_past_participant_user",
        "call_log",
        "call_log_participant_v2",
    }
)


def _entity_file_sort_key(filename: str) -> tuple[int, str]:
    table = filename.split("_modified_")[0].split("_deleted_")[0]
    return (_ENTITY_TABLE_PRIORITY.get(table, 100), filename)


def apply_entity_modifications(
    conn: sqlite3.Connection,
    table_name: str,
    modified_json_path: str,
    logger: logging.Logger,
    jid_id_map: dict[int, int] | None = None,
) -> int:
    """Upsert modified rows from an entity JSON file into SQLite table."""
    if table_name not in _SUPPORTED_ENTITY_TABLES:
        logger.debug(
            f"EntityTableProcessor: skipping import for unknown/unsupported table '{table_name}'"
        )
        return 0

    with open(modified_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        if table_name in data and isinstance(data[table_name], list):
            rows = data[table_name]
        elif "rows" in data and isinstance(data["rows"], list):
            rows = data["rows"]
        else:
            rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        return 0

    cur = conn.cursor()
    col_info = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    table_cols = {r[1] for r in col_info}
    if not table_cols:
        logger.debug(
            f"Table '{table_name}' does not exist in target database; skipping modification."
        )
        return 0

    pk_cols = [r[1] for r in col_info if r[5] > 0]
    pk_col = pk_cols[0] if len(pk_cols) == 1 else (pk_cols if pk_cols else ["_id"])

    count = 0
    for row_dict in rows:
        if not isinstance(row_dict, dict):
            continue
        filtered = {k: v for k, v in row_dict.items() if k in table_cols}
        if not filtered:
            continue

        if table_name == "props" and "key" in filtered:
            # Props is a key-value entity whose natural identifier is 'key'.
            # Match by 'key' rather than autoincrement '_id' to avoid UNIQUE constraint
            # collisions when phone-local _id sequence numbers diverge from target DB.
            val_cols = [c for c in filtered if c not in ("_id", "key")]
            prop_exists = bool(
                cur.execute("SELECT 1 FROM props WHERE key = ?", (filtered["key"],)).fetchone()
            )
            if prop_exists:
                if val_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in val_cols)
                    params = [filtered[c] for c in val_cols] + [filtered["key"]]
                    cur.execute(f"UPDATE props SET {set_clause} WHERE key = ?", params)
            else:
                insert_cols = ["key"] + val_cols
                placeholders = ", ".join("?" for _ in insert_cols)
                sql = f"INSERT INTO props ({', '.join(insert_cols)}) VALUES ({placeholders})"
                cur.execute(sql, [filtered[c] for c in insert_cols])
            count += 1
            continue

        if table_name == "jid" and "user" in filtered and "server" in filtered:
            raw_u = filtered["user"]
            u = raw_u.split(":", 1)[0] if ":" in raw_u else raw_u
            s = filtered["server"]
            if s in ("s.whatsapp.net", "lid") and "." in u:
                u = u.split(".", 1)[0]
            has_device = "device" in table_cols
            if has_device:
                existing_jid = cur.execute(
                    "SELECT _id FROM jid WHERE user = ? AND server = ? "
                    "ORDER BY CASE WHEN device IS NULL OR device = 0 THEN 0 ELSE 1 END, _id ASC LIMIT 1",
                    (u, s),
                ).fetchone()
            else:
                existing_jid = cur.execute(
                    "SELECT _id FROM jid WHERE user = ? AND server = ? ORDER BY _id ASC LIMIT 1",
                    (u, s),
                ).fetchone()

            if existing_jid is not None:
                jid_pk = existing_jid[0]
                update_cols = [c for c in filtered if c not in ("_id", "user", "server")]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    params = [filtered[c] for c in update_cols] + [jid_pk]
                    cur.execute(f"UPDATE jid SET {set_clause} WHERE _id = ?", params)
                if jid_id_map is not None and "_id" in filtered:
                    jid_id_map[filtered["_id"]] = jid_pk
            else:
                id_used = (
                    cur.execute(
                        "SELECT 1 FROM jid WHERE _id = ?", (filtered.get("_id"),)
                    ).fetchone()
                    is not None
                    if "_id" in filtered
                    else False
                )
                insert_cols = (
                    [c for c in filtered if c != "_id"] if id_used else sorted(filtered.keys())
                )
                filtered_copy = dict(filtered)
                filtered_copy["user"] = u
                sql = f"INSERT INTO jid ({', '.join(insert_cols)}) VALUES ({', '.join('?' for _ in insert_cols)})"
                cur.execute(sql, [filtered_copy[c] for c in insert_cols])
                new_id = cur.lastrowid
                if jid_id_map is not None and "_id" in filtered:
                    jid_id_map[filtered["_id"]] = new_id
            count += 1
            continue

        if table_name == "chat" and "jid_row_id" in filtered:
            # Chat is uniquely 1:1 with jid_row_id.
            # Map foreign jid_row_id through jid_id_map if phone IDs diverged.
            raw_jid_id = filtered["jid_row_id"]
            mapped_jid_id = jid_id_map.get(raw_jid_id, raw_jid_id) if jid_id_map else raw_jid_id

            # Priority 1: If mapped_jid_id is a LID, check if jid_map maps to an existing phone chat
            existing_chat = None
            has_hidden_col = "hidden" in table_cols
            c_order = (
                "ORDER BY CASE WHEN c.hidden = 0 THEN 0 ELSE 1 END, c._id ASC"
                if has_hidden_col
                else "ORDER BY c._id ASC"
            )
            try:
                cur.execute(
                    f"""
                    SELECT c._id FROM chat c
                    JOIN jid_map jm ON c.jid_row_id = jm.jid_row_id
                    WHERE jm.lid_row_id = ?
                    {c_order}
                    LIMIT 1
                    """,
                    (mapped_jid_id,),
                )
                existing_chat = cur.fetchone()
            except sqlite3.OperationalError:
                pass

            # Priority 2: Direct match on jid_row_id
            if existing_chat is None:
                existing_chat = cur.execute(
                    "SELECT _id FROM chat WHERE jid_row_id = ?", (mapped_jid_id,)
                ).fetchone()

            # Priority 3: Match on account_jid_row_id
            if existing_chat is None and "account_jid_row_id" in table_cols:
                raw_acct_id = filtered.get("account_jid_row_id")
                mapped_acct_id = (
                    jid_id_map.get(raw_acct_id, raw_acct_id)
                    if (jid_id_map and raw_acct_id)
                    else raw_acct_id
                )
                if mapped_acct_id:
                    existing_chat = cur.execute(
                        "SELECT _id FROM chat WHERE account_jid_row_id = ? OR jid_row_id = ?",
                        (mapped_acct_id, mapped_acct_id),
                    ).fetchone()

            # Priority 4: Match on natural key (user, server)
            if existing_chat is None and mapped_jid_id is not None:
                existing_chat = cur.execute(
                    f"""
                    SELECT c._id FROM chat c
                    JOIN jid j_target ON j_target._id = ?
                    JOIN jid j_exist ON c.jid_row_id = j_exist._id
                    WHERE j_exist.user = j_target.user AND j_exist.server = j_target.server
                    {c_order}
                    LIMIT 1
                    """,
                    (mapped_jid_id,),
                ).fetchone()

            chat_pointer_cols = {
                "display_message_row_id",
                "last_message_row_id",
                "last_read_message_row_id",
                "last_read_receipt_sent_message_row_id",
                "last_message_reaction_row_id",
            }

            if existing_chat is not None:
                chat_id = existing_chat[0]
                update_cols = [
                    c
                    for c in filtered
                    if c not in ("_id", "jid_row_id") and c not in chat_pointer_cols
                ]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    params = [filtered[c] for c in update_cols] + [chat_id]
                    cur.execute(f"UPDATE chat SET {set_clause} WHERE _id = ?", params)
                # Ensure account_jid_row_id is linked if mapped_jid_id was a LID
                if "account_jid_row_id" in table_cols:
                    cur.execute(
                        "UPDATE chat SET account_jid_row_id = ? WHERE _id = ? AND (account_jid_row_id IS NULL OR account_jid_row_id = 0)",
                        (mapped_jid_id, chat_id),
                    )
            else:
                id_used = (
                    cur.execute(
                        "SELECT 1 FROM chat WHERE _id = ?", (filtered.get("_id"),)
                    ).fetchone()
                    is not None
                    if "_id" in filtered
                    else False
                )
                insert_cols = [
                    c
                    for c in filtered
                    if (c != "_id" if id_used else True) and c not in chat_pointer_cols
                ]
                filtered_copy = dict(filtered)
                filtered_copy["jid_row_id"] = mapped_jid_id
                if "account_jid_row_id" in filtered_copy and jid_id_map:
                    raw_a = filtered_copy["account_jid_row_id"]
                    if raw_a in jid_id_map:
                        filtered_copy["account_jid_row_id"] = jid_id_map[raw_a]
                sql = f"INSERT INTO chat ({', '.join(insert_cols)}) VALUES ({', '.join('?' for _ in insert_cols)})"
                cur.execute(sql, [filtered_copy[c] for c in insert_cols])
            count += 1
            continue

        if table_name == "jid_map" and "lid_row_id" in filtered and "jid_row_id" in filtered:
            raw_lid = filtered["lid_row_id"]
            raw_jid = filtered["jid_row_id"]
            mapped_lid = jid_id_map.get(raw_lid, raw_lid) if jid_id_map else raw_lid
            mapped_jid = jid_id_map.get(raw_jid, raw_jid) if jid_id_map else raw_jid
            filtered_copy = dict(filtered)
            filtered_copy["lid_row_id"] = mapped_lid
            filtered_copy["jid_row_id"] = mapped_jid
            cols_tuple = tuple(sorted(filtered_copy.keys()))
            placeholders = ", ".join("?" for _ in cols_tuple)
            cur.execute(
                f"INSERT OR IGNORE INTO jid_map ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                [filtered_copy[c] for c in cols_tuple],
            )
            count += 1
            continue

        if table_name == "lid_display_name" and "lid_row_id" in filtered:
            raw_lid = filtered["lid_row_id"]
            mapped_lid = jid_id_map.get(raw_lid, raw_lid) if jid_id_map else raw_lid
            filtered_copy = dict(filtered)
            filtered_copy["lid_row_id"] = mapped_lid
            cols_tuple = tuple(sorted(filtered_copy.keys()))
            placeholders = ", ".join("?" for _ in cols_tuple)
            cur.execute(
                f"INSERT OR REPLACE INTO lid_display_name ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                [filtered_copy[c] for c in cols_tuple],
            )
            count += 1
            continue

        if (
            table_name == "group_participant_user"
            and "group_jid_row_id" in filtered
            and "user_jid_row_id" in filtered
        ):
            raw_g = filtered["group_jid_row_id"]
            raw_u = filtered["user_jid_row_id"]
            mapped_g = jid_id_map.get(raw_g, raw_g) if jid_id_map else raw_g
            mapped_u = jid_id_map.get(raw_u, raw_u) if jid_id_map else raw_u
            filtered_copy = dict(filtered)
            filtered_copy["group_jid_row_id"] = mapped_g
            filtered_copy["user_jid_row_id"] = mapped_u

            existing_part = cur.execute(
                "SELECT _id FROM group_participant_user WHERE group_jid_row_id = ? AND user_jid_row_id = ?",
                (mapped_g, mapped_u),
            ).fetchone()

            if existing_part:
                part_id = existing_part[0]
                update_cols = [c for c in filtered_copy if c != "_id"]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    cur.execute(
                        f"UPDATE group_participant_user SET {set_clause} WHERE _id = ?",
                        [filtered_copy[c] for c in update_cols] + [part_id],
                    )
            else:
                if (
                    "_id" in filtered_copy
                    and cur.execute(
                        "SELECT 1 FROM group_participant_user WHERE _id = ?",
                        (filtered_copy["_id"],),
                    ).fetchone()
                ):
                    del filtered_copy["_id"]
                cols_tuple = tuple(sorted(filtered_copy.keys()))
                placeholders = ", ".join("?" for _ in cols_tuple)
                cur.execute(
                    f"INSERT INTO group_participant_user ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                    [filtered_copy[c] for c in cols_tuple],
                )
            count += 1
            continue

        if (
            table_name == "group_past_participant_user"
            and "group_jid_row_id" in filtered
            and "user_jid_row_id" in filtered
        ):
            raw_g = filtered["group_jid_row_id"]
            raw_u = filtered["user_jid_row_id"]
            mapped_g = jid_id_map.get(raw_g, raw_g) if jid_id_map else raw_g
            mapped_u = jid_id_map.get(raw_u, raw_u) if jid_id_map else raw_u
            filtered_copy = dict(filtered)
            filtered_copy["group_jid_row_id"] = mapped_g
            filtered_copy["user_jid_row_id"] = mapped_u

            existing_past = cur.execute(
                "SELECT _id FROM group_past_participant_user WHERE group_jid_row_id = ? AND user_jid_row_id = ?",
                (mapped_g, mapped_u),
            ).fetchone()

            if existing_past:
                past_id = existing_past[0]
                update_cols = [c for c in filtered_copy if c != "_id"]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    cur.execute(
                        f"UPDATE group_past_participant_user SET {set_clause} WHERE _id = ?",
                        [filtered_copy[c] for c in update_cols] + [past_id],
                    )
            else:
                if (
                    "_id" in filtered_copy
                    and cur.execute(
                        "SELECT 1 FROM group_past_participant_user WHERE _id = ?",
                        (filtered_copy["_id"],),
                    ).fetchone()
                ):
                    del filtered_copy["_id"]
                cols_tuple = tuple(sorted(filtered_copy.keys()))
                placeholders = ", ".join("?" for _ in cols_tuple)
                cur.execute(
                    f"INSERT INTO group_past_participant_user ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                    [filtered_copy[c] for c in cols_tuple],
                )
            count += 1
            continue

        if table_name == "group_participants":
            filtered_copy = dict(filtered)
            existing_gp = None
            if "group_jid_row_id" in filtered and "jid_row_id" in filtered:
                raw_g = filtered["group_jid_row_id"]
                raw_j = filtered["jid_row_id"]
                mapped_g = jid_id_map.get(raw_g, raw_g) if jid_id_map else raw_g
                mapped_j = jid_id_map.get(raw_j, raw_j) if jid_id_map else raw_j
                filtered_copy["group_jid_row_id"] = mapped_g
                filtered_copy["jid_row_id"] = mapped_j
                existing_gp = cur.execute(
                    "SELECT rowid FROM group_participants WHERE group_jid_row_id = ? AND jid_row_id = ?",
                    (mapped_g, mapped_j),
                ).fetchone()
            elif "gjid" in filtered and "jid" in filtered:
                existing_gp = cur.execute(
                    "SELECT rowid FROM group_participants WHERE gjid = ? AND jid = ?",
                    (filtered["gjid"], filtered["jid"]),
                ).fetchone()

            if existing_gp:
                row_id = existing_gp[0]
                update_cols = [c for c in filtered_copy if c != "_id"]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    cur.execute(
                        f"UPDATE group_participants SET {set_clause} WHERE rowid = ?",
                        [filtered_copy[c] for c in update_cols] + [row_id],
                    )
            else:
                if (
                    "_id" in filtered_copy
                    and cur.execute(
                        "SELECT 1 FROM group_participants WHERE _id = ?",
                        (filtered_copy["_id"],),
                    ).fetchone()
                ):
                    del filtered_copy["_id"]
                cols_tuple = tuple(sorted(filtered_copy.keys()))
                placeholders = ", ".join("?" for _ in cols_tuple)
                cur.execute(
                    f"INSERT INTO group_participants ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                    [filtered_copy[c] for c in cols_tuple],
                )
            count += 1
            continue

        if table_name == "call_log" and "call_id" in filtered:
            filtered_copy = dict(filtered)
            if "jid_row_id" in filtered_copy and jid_id_map:
                raw_j = filtered_copy["jid_row_id"]
                filtered_copy["jid_row_id"] = jid_id_map.get(raw_j, raw_j)
            if "group_jid_row_id" in filtered_copy and jid_id_map:
                raw_g = filtered_copy["group_jid_row_id"]
                if raw_g and raw_g > 0:
                    filtered_copy["group_jid_row_id"] = jid_id_map.get(raw_g, raw_g)
            if "call_creator_device_jid_row_id" in filtered_copy and jid_id_map:
                raw_d = filtered_copy["call_creator_device_jid_row_id"]
                if raw_d and raw_d > 0:
                    filtered_copy["call_creator_device_jid_row_id"] = jid_id_map.get(raw_d, raw_d)

            existing_call = cur.execute(
                "SELECT _id FROM call_log WHERE call_id = ?",
                (filtered_copy["call_id"],),
            ).fetchone()

            if existing_call:
                call_row_id = existing_call[0]
                update_cols = [c for c in filtered_copy if c != "_id"]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    cur.execute(
                        f"UPDATE call_log SET {set_clause} WHERE _id = ?",
                        [filtered_copy[c] for c in update_cols] + [call_row_id],
                    )
            else:
                if (
                    "_id" in filtered_copy
                    and cur.execute(
                        "SELECT 1 FROM call_log WHERE _id = ?", (filtered_copy["_id"],)
                    ).fetchone()
                ):
                    del filtered_copy["_id"]
                cols_tuple = tuple(sorted(filtered_copy.keys()))
                placeholders = ", ".join("?" for _ in cols_tuple)
                cur.execute(
                    f"INSERT INTO call_log ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                    [filtered_copy[c] for c in cols_tuple],
                )
            count += 1
            continue

        if (
            table_name == "call_log_participant_v2"
            and "call_log_row_id" in filtered
            and "jid_row_id" in filtered
        ):
            raw_c = filtered["call_log_row_id"]
            raw_j = filtered["jid_row_id"]
            mapped_j = jid_id_map.get(raw_j, raw_j) if jid_id_map else raw_j
            filtered_copy = dict(filtered)
            filtered_copy["jid_row_id"] = mapped_j

            existing_part = cur.execute(
                "SELECT _id FROM call_log_participant_v2 WHERE call_log_row_id = ? AND jid_row_id = ?",
                (raw_c, mapped_j),
            ).fetchone()

            if existing_part:
                part_id = existing_part[0]
                update_cols = [c for c in filtered_copy if c != "_id"]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    cur.execute(
                        f"UPDATE call_log_participant_v2 SET {set_clause} WHERE _id = ?",
                        [filtered_copy[c] for c in update_cols] + [part_id],
                    )
            else:
                if (
                    "_id" in filtered_copy
                    and cur.execute(
                        "SELECT 1 FROM call_log_participant_v2 WHERE _id = ?",
                        (filtered_copy["_id"],),
                    ).fetchone()
                ):
                    del filtered_copy["_id"]
                cols_tuple = tuple(sorted(filtered_copy.keys()))
                placeholders = ", ".join("?" for _ in cols_tuple)
                cur.execute(
                    f"INSERT INTO call_log_participant_v2 ({', '.join(cols_tuple)}) VALUES ({placeholders})",
                    [filtered_copy[c] for c in cols_tuple],
                )
            count += 1
            continue

        # Check if row has a single primary key and already exists in table
        has_single_pk = isinstance(pk_col, str) and pk_col in filtered
        row_exists = False
        if has_single_pk:
            row_exists = bool(
                cur.execute(
                    f"SELECT 1 FROM {table_name} WHERE {pk_col} = ?", (filtered[pk_col],)
                ).fetchone()
            )

        if row_exists and has_single_pk:
            update_cols = [c for c in filtered if c != pk_col]
            if update_cols:
                set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                params = [filtered[c] for c in update_cols] + [filtered[pk_col]]
                cur.execute(f"UPDATE {table_name} SET {set_clause} WHERE {pk_col} = ?", params)
        else:
            cols_tuple = tuple(sorted(filtered.keys()))
            placeholders = ", ".join("?" for _ in cols_tuple)
            sql = f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols_tuple)}) VALUES ({placeholders})"
            cur.execute(sql, [filtered[c] for c in cols_tuple])

        count += 1

    return count


def apply_entity_deletions(
    conn: sqlite3.Connection,
    table_name: str,
    deleted_json_path: str,
    logger: logging.Logger,
    jid_id_map: dict[int, int],
) -> int:
    """Delete entity records listed in an entity deleted JSON file.

    For 'chat', deletes by mapped natural JID (jid_row_id or account_jid_row_id) translated
    via jid_id_map to prevent surrogate ID drift from destroying unrelated chats.
    """
    if table_name not in _SUPPORTED_ENTITY_TABLES:
        logger.debug(
            f"EntityTableProcessor: skipping deletions for unknown/unsupported table '{table_name}'"
        )
        return 0

    with open(deleted_json_path, encoding="utf-8") as f:
        data = json.load(f)

    raw_items = data.get("deleted_ids", data) if isinstance(data, dict) else data
    if not isinstance(raw_items, list) or not raw_items:
        return 0

    cur = conn.cursor()
    col_info = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not col_info:
        logger.debug(f"Table '{table_name}' does not exist in target database; skipping deletions.")
        return 0

    col_names = {r[1] for r in col_info}
    pk_col = next((r[1] for r in col_info if r[5] > 0), "_id")
    if table_name == "props" and any(isinstance(x, str) for x in raw_items):
        pk_col = "key"
    if table_name == "call_log" and any(
        isinstance(x, str) and str(x).startswith("call:") for x in raw_items
    ):
        pk_col = "call_id"

    deleted_count = 0
    scalar_ids: list[object] = []
    dict_items: list[dict] = []

    for item in raw_items:
        if table_name == "chat" and isinstance(item, dict) and ("jid_row_id" in item or "account_jid_row_id" in item):
            raw_j = item.get("jid_row_id")
            raw_a = item.get("account_jid_row_id")
            mapped_j = jid_id_map.get(raw_j, raw_j) if (jid_id_map and raw_j is not None) else raw_j
            mapped_a = jid_id_map.get(raw_a, raw_a) if (jid_id_map and raw_a is not None) else raw_a
            clauses = []
            params = []
            if mapped_j is not None:
                clauses.append("jid_row_id = ?")
                params.append(mapped_j)
            if mapped_a is not None and "account_jid_row_id" in col_names:
                clauses.append("account_jid_row_id = ?")
                params.append(mapped_a)
            if clauses:
                cur.execute(f"DELETE FROM chat WHERE {' OR '.join(clauses)}", params)
                deleted_count += cur.rowcount
            continue

        if isinstance(item, dict):
            if pk_col in item and item[pk_col] is not None:
                scalar_ids.append(item[pk_col])
            else:
                dict_items.append(item)
        elif isinstance(item, (int, str)):
            scalar_ids.append(item)

    chunk_size = 500
    for i in range(0, len(scalar_ids), chunk_size):
        chunk = scalar_ids[i : i + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM {table_name} WHERE {pk_col} IN ({placeholders})", chunk)
        deleted_count += cur.rowcount

    for d_item in dict_items:
        valid_cols = [c for c in d_item if c in col_names]
        if not valid_cols:
            continue
        where_clause = " AND ".join(f"{c} = ?" for c in valid_cols)
        cur.execute(
            f"DELETE FROM {table_name} WHERE {where_clause}", [d_item[c] for c in valid_cols]
        )
        deleted_count += cur.rowcount

    return deleted_count


def get_table_columns(
    conn: sqlite3.Connection,
    table_name: str,
    schema_cache: dict[str, set[str]] | None,
) -> set[str]:
    """Retrieve column set for a table, using schema_cache when available."""
    if schema_cache is not None and table_name in schema_cache:
        return schema_cache[table_name]
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if schema_cache is not None:
        schema_cache[table_name] = cols
    return cols


def find_message_row_id(
    conn: sqlite3.Connection,
    key_id: str,
    key_cache: dict[str, int],
) -> int | None:
    """Find message._id by key_id, using key_cache for O(1) repeated lookups."""
    if not key_id:
        return None
    if key_id in key_cache:
        return key_cache[key_id]
    cur = conn.cursor()
    cur.execute("SELECT _id FROM message WHERE key_id = ?", (key_id,))
    row = cur.fetchone()
    if row is not None:
        row_id = int(row[0])
        key_cache[key_id] = row_id
        return row_id
    return None


@dataclass(frozen=True, slots=True)
class ReactionRecord:
    target_key_id: str
    target_remote_jid: str
    target_from_me: int
    reaction_key_id: str
    reactor_jid: str
    emoji: str
    sender_ts: int
    is_revocation: bool
    unread: int

    def __getitem__(self, item: str) -> Any:
        if item == "from_me":
            return self.target_from_me
        if item == "participant":
            return self.reactor_jid
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except AttributeError:
            return default


@dataclass(slots=True)
class PendingAddOn:
    add_on_type: str
    target_key_id: str
    msg: dict
    sender_jid_row_id: int | None
    from_me: int
    timestamp: int
    key_id: str
    chat_row_id: int | None


def get_reaction_add_on_type(
    conn: sqlite3.Connection,
    schema_cache: dict[str, set[str]] | None,
) -> int:
    """Introspect host schema to find native message_add_on_type for reactions (56 or 1)."""
    if schema_cache is not None and "_reaction_add_on_type" in schema_cache:
        return schema_cache["_reaction_add_on_type"]

    detected = 1
    try:
        row = conn.execute(
            """
            SELECT ao.message_add_on_type
            FROM message_add_on ao
            JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
            WHERE ao.message_add_on_type IS NOT NULL
            LIMIT 1
            """
        ).fetchone()
        if row and row[0] is not None:
            detected = int(row[0])
        else:
            props_row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'props'"
            ).fetchone()
            if props_row:
                ver_row = conn.execute(
                    "SELECT 1 FROM props WHERE key = 'msgtore_db_schema_version' LIMIT 1"
                ).fetchone()
                if ver_row:
                    detected = 56
    except sqlite3.OperationalError:
        detected = 1

    if schema_cache is not None:
        schema_cache["_reaction_add_on_type"] = detected
    return detected


def apply_reaction_record(
    conn: sqlite3.Connection,
    reaction_info: dict,
    sender_jid_row_id: int | None,
    from_me: int,
    timestamp: int,
    key_cache: dict[str, int],
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    key_id: str,
    chat_row_id: int | None,
) -> bool:
    """Apply a reaction message add-on to the database, ensuring key_id satisfies NOT NULL constraints. Returns True if applied, False otherwise."""
    target_key_id = reaction_info.get("target_key_id", "")
    if not target_key_id:
        return False
    parent_row_id = find_message_row_id(conn, target_key_id, key_cache)
    if parent_row_id is None:
        logger.debug(f"Target message '{target_key_id}' for reaction not found in cache/database.")
        return False

    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    if not add_on_cols:
        return False

    target_type = get_reaction_add_on_type(conn, schema_cache)
    cur = conn.cursor()
    emoji = reaction_info.get("emoji", "")
    sender_ts = reaction_info.get("sender_ts", timestamp)

    # Delete any prior reaction or revocation by this sender and from_me on this message
    reaction_cols = get_table_columns(conn, "message_add_on_reaction", schema_cache)
    if reaction_cols:
        cur.execute(
            "DELETE FROM message_add_on_reaction WHERE message_add_on_row_id IN ("
            "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type IN (56, 1))",
            (parent_row_id, from_me, sender_jid_row_id),
        )
    cur.execute(
        "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type IN (56, 1)",
        (parent_row_id, from_me, sender_jid_row_id),
    )

    # If reaction is empty string, reaction was revoked/cleared
    if not emoji:
        return True

    row_map = {
        "parent_message_row_id": parent_row_id,
        "from_me": from_me,
        "sender_jid_row_id": sender_jid_row_id,
        "status": 4 if from_me else 17,
        "message_add_on_type": target_type,
        "timestamp": timestamp,
        "key_id": key_id or "",
        "chat_row_id": chat_row_id,
    }
    valid_cols = [c for c in row_map if c in add_on_cols]
    cur.execute(
        f"INSERT INTO message_add_on ({', '.join(valid_cols)}) VALUES ({', '.join('?' for _ in valid_cols)})",
        [row_map[c] for c in valid_cols],
    )
    add_on_id = cur.lastrowid
    if reaction_cols and add_on_id:
        cur.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) VALUES (?, ?, ?)",
            (add_on_id, emoji, sender_ts),
        )
    return True


def apply_embedded_reactions(
    conn: sqlite3.Connection,
    parent_row_id: int,
    chat_row_id: int | None,
    is_group: bool,
    embedded_reactions: list[ReactionRecord | dict],
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    jid_cache: dict[str, int],
) -> int:
    """Apply embedded reactions from WMI Tag 41 to message_add_on and message_add_on_reaction."""
    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    if not add_on_cols:
        return 0
    reaction_cols = get_table_columns(conn, "message_add_on_reaction", schema_cache)
    target_type = get_reaction_add_on_type(conn, schema_cache)

    applied_count = 0
    cur = conn.cursor()
    for rx in embedded_reactions:
        from_me = int(rx.get("from_me", 0) or rx.get("target_from_me", 0))
        part = rx.get("participant", "")
        if from_me == 1 or not is_group:
            rx_sender_id = -1
        elif part:
            try:
                rx_sender_id = resolve_or_create_jid(conn, part, logger, jid_cache)
            except (ValueError, Exception):
                rx_sender_id = -1
        else:
            rx_sender_id = -1

        emoji = rx.get("emoji", "")
        sender_ts = rx.get("sender_ts", 0)
        rx_key_id = rx.get("reaction_key_id") or rx.get("target_key_id") or ""

        # Clean prior reaction or revocation for this specific reactor and from_me state
        if reaction_cols:
            cur.execute(
                "DELETE FROM message_add_on_reaction WHERE message_add_on_row_id IN ("
                "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type IN (56, 1))",
                (parent_row_id, from_me, rx_sender_id),
            )
        cur.execute(
            "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type IN (56, 1)",
            (parent_row_id, from_me, rx_sender_id),
        )

        if not emoji:
            applied_count += 1
            continue

        row_map = {
            "parent_message_row_id": parent_row_id,
            "from_me": from_me,
            "sender_jid_row_id": rx_sender_id,
            "status": 4 if from_me else 17,
            "message_add_on_type": target_type,
            "timestamp": sender_ts,
            "key_id": rx_key_id,
            "chat_row_id": chat_row_id,
        }
        valid_cols = [c for c in row_map if c in add_on_cols]
        cur.execute(
            f"INSERT INTO message_add_on ({', '.join(valid_cols)}) VALUES ({', '.join('?' for _ in valid_cols)})",
            [row_map[c] for c in valid_cols],
        )
        add_on_id = cur.lastrowid
        if reaction_cols and add_on_id:
            cur.execute(
                "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) VALUES (?, ?, ?)",
                (add_on_id, emoji, sender_ts),
            )
        applied_count += 1

    return applied_count


def decrypt_poll_vote_payload(
    enc_payload: bytes,
    enc_iv: bytes,
    poll_msg_id: str,
    poll_creator_jid_str: str,
    voter_jid_str: str,
    orig_msg_secret: bytes,
) -> list[bytes]:
    """Decrypt AES-256-GCM poll vote payload using HKDF derived key and return option SHA256 hashes."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        return []

    try:
        orig_clean = (
            poll_creator_jid_str.split(":")[0].split("@")[0]
            + "@"
            + poll_creator_jid_str.split("@")[-1]
            if "@" in poll_creator_jid_str
            else poll_creator_jid_str
        )
        mod_clean = (
            voter_jid_str.split(":")[0].split("@")[0] + "@" + voter_jid_str.split("@")[-1]
            if "@" in voter_jid_str
            else voter_jid_str
        )
        use_case_secret = (
            poll_msg_id.encode("utf-8")
            + orig_clean.encode("utf-8")
            + mod_clean.encode("utf-8")
            + b"Poll Vote"
        )
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=use_case_secret,
        )
        secret_key = hkdf.derive(orig_msg_secret)
        aad = f"{poll_msg_id}\x00{mod_clean}".encode()
        aesgcm = AESGCM(secret_key)
        decrypted = aesgcm.decrypt(enc_iv, enc_payload, aad)

        vote_fields = parse_wire_fields(decrypted)
        return [
            item[1]
            for item in vote_fields.get(1, [])
            if isinstance(item[1], bytes) and len(item[1]) == 32
        ]
    except Exception:
        return []


def apply_embedded_poll_votes(
    conn: sqlite3.Connection,
    parent_row_id: int,
    embedded_votes: list[dict],
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    chat_row_id: int | None,
    is_group: bool,
    jid_cache: dict[str, int],
) -> None:
    """Apply embedded poll votes from WMI Tag 45 into message_add_on and message_add_on_poll_vote."""
    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    vote_cols = get_table_columns(conn, "message_add_on_poll_vote", schema_cache)
    vote_opt_cols = get_table_columns(
        conn, "message_add_on_poll_vote_selected_option", schema_cache
    )
    poll_opt_cols = get_table_columns(conn, "message_poll_option", schema_cache)
    if not add_on_cols or not vote_opt_cols or chat_row_id is None:
        return

    cur = conn.cursor()

    # Pre-map option sha256 to option _id for this poll
    hash_to_opt_id: dict[bytes, int] = {}
    if poll_opt_cols:
        cur.execute(
            "SELECT _id, option_name, option_sha256 FROM message_poll_option WHERE message_row_id = ?",
            (parent_row_id,),
        )
        for opt_r in cur.fetchall():
            opt_id, opt_name, opt_sha = opt_r[0], opt_r[1], opt_r[2]
            if opt_sha:
                try:
                    h = base64.b64decode(opt_sha)
                    hash_to_opt_id[h] = opt_id
                except (ValueError, Exception):
                    pass
            if opt_name:
                h_from_name = hashlib.sha256(opt_name.encode("utf-8")).digest()
                hash_to_opt_id[h_from_name] = opt_id

    for v in embedded_votes:
        v_key_id = v.get("key_id", "")
        from_me = v.get("from_me", 0)
        part = v.get("participant")
        if not is_group or from_me == 1:
            s_jid_id = -1
        elif part:
            try:
                s_jid_id = resolve_or_create_jid(conn, part, logger, jid_cache)
            except (ValueError, Exception):
                s_jid_id = -1
        else:
            s_jid_id = -1

        v_ts = v.get("sender_ts", 0)
        sel_hashes = v.get("selected_option_hashes", [])

        # Delete prior vote for this sender and from_me on this poll
        cur.execute(
            "DELETE FROM message_add_on_poll_vote_selected_option WHERE message_add_on_row_id IN ("
            "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type = 67)",
            (parent_row_id, from_me, s_jid_id),
        )
        if vote_cols:
            cur.execute(
                "DELETE FROM message_add_on_poll_vote WHERE message_add_on_row_id IN ("
                "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type = 67)",
                (parent_row_id, from_me, s_jid_id),
            )
        cur.execute(
            "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type = 67",
            (parent_row_id, from_me, s_jid_id),
        )

        row_map = {
            "parent_message_row_id": parent_row_id,
            "from_me": from_me,
            "sender_jid_row_id": s_jid_id,
            "status": 0,
            "message_add_on_type": 67,
            "timestamp": v_ts,
            "key_id": v_key_id,
            "chat_row_id": chat_row_id,
            "received_timestamp": v_ts,
        }
        valid_cols = [c for c in row_map if c in add_on_cols]
        cur.execute(
            f"INSERT INTO message_add_on ({', '.join(valid_cols)}) VALUES ({', '.join('?' for _ in valid_cols)})",
            [row_map[c] for c in valid_cols],
        )
        add_on_id = cur.lastrowid

        if vote_cols and add_on_id:
            cur.execute(
                "INSERT INTO message_add_on_poll_vote (message_add_on_row_id, sender_timestamp) VALUES (?, ?)",
                (add_on_id, v_ts),
            )

        if add_on_id:
            for h in sel_hashes:
                if h in hash_to_opt_id:
                    cur.execute(
                        "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (?, ?)",
                        (add_on_id, hash_to_opt_id[h]),
                    )

    # Recalculate vote_total for all options of this poll
    if poll_opt_cols:
        cur.execute(
            "UPDATE message_poll_option SET vote_total = ("
            "  SELECT COUNT(DISTINCT ps.message_add_on_row_id) "
            "  FROM message_add_on_poll_vote_selected_option ps "
            "  JOIN message_add_on mao ON mao._id = ps.message_add_on_row_id "
            "  WHERE ps.message_poll_option_id = message_poll_option._id AND mao.parent_message_row_id = ?"
            ") WHERE message_row_id = ?",
            (parent_row_id, parent_row_id),
        )


def apply_poll_creation(
    conn: sqlite3.Connection,
    msg_row_id: int,
    poll_info: dict,
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    chat_row_id: int | None,
    is_group: bool,
    jid_cache: dict[str, int],
) -> None:
    """Insert message_poll and message_poll_option records for a poll message, and apply embedded votes."""
    poll_cols = get_table_columns(conn, "message_poll", schema_cache)
    poll_opt_cols = get_table_columns(conn, "message_poll_option", schema_cache)
    secret_cols = get_table_columns(conn, "message_secret", schema_cache)
    cur = conn.cursor()

    if poll_cols:
        selectable = poll_info.get("selectable_count", 0)
        cur.execute(
            "INSERT OR REPLACE INTO message_poll (message_row_id, selectable_options_count) VALUES (?, ?)",
            (msg_row_id, selectable),
        )

    msg_secret = poll_info.get("message_secret")
    if secret_cols and msg_secret:
        cur.execute(
            "INSERT OR REPLACE INTO message_secret (message_row_id, message_secret) VALUES (?, ?)",
            (msg_row_id, msg_secret),
        )

    if poll_opt_cols:
        cur.execute(
            "SELECT COUNT(*) FROM message_poll_option WHERE message_row_id = ?",
            (msg_row_id,),
        )
        existing_count = cur.fetchone()[0]
        has_sha_col = "option_sha256" in poll_opt_cols
        if existing_count == 0:
            for opt_name in poll_info.get("options", []):
                h = hashlib.sha256(opt_name.encode("utf-8")).digest()
                h_b64 = base64.b64encode(h).decode("ascii")
                if has_sha_col:
                    cur.execute(
                        "INSERT INTO message_poll_option (message_row_id, option_name, option_sha256, vote_total) VALUES (?, ?, ?, 0)",
                        (msg_row_id, opt_name, h_b64),
                    )
                else:
                    cur.execute(
                        "INSERT INTO message_poll_option (message_row_id, option_name, vote_total) VALUES (?, ?, 0)",
                        (msg_row_id, opt_name),
                    )

        # Recalculate vote totals if any votes were already recorded in message_add_on
        vote_opt_cols = get_table_columns(
            conn, "message_add_on_poll_vote_selected_option", schema_cache
        )
        if vote_opt_cols:
            try:
                cur.execute(
                    "UPDATE message_poll_option SET vote_total = ("
                    "  SELECT COUNT(DISTINCT ps.message_add_on_row_id) "
                    "  FROM message_add_on_poll_vote_selected_option ps "
                    "  JOIN message_add_on mao ON mao._id = ps.message_add_on_row_id "
                    "  WHERE ps.message_poll_option_id = message_poll_option._id AND mao.parent_message_row_id = ?"
                    ") WHERE message_row_id = ?",
                    (msg_row_id, msg_row_id),
                )
            except sqlite3.OperationalError:
                pass

    embedded_votes = poll_info.get("embedded_votes", [])
    if embedded_votes:
        apply_embedded_poll_votes(
            conn,
            msg_row_id,
            embedded_votes,
            schema_cache,
            logger,
            chat_row_id,
            is_group,
            jid_cache,
        )


def apply_poll_vote(
    conn: sqlite3.Connection,
    poll_vote_info: dict,
    sender_jid_row_id: int | None,
    from_me: int,
    timestamp: int,
    key_cache: dict[str, int],
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    key_id: str,
    chat_row_id: int | None,
) -> bool:
    """Record a poll vote add-on for a message, satisfying message_add_on NOT NULL constraints. Returns True if applied, False otherwise."""
    poll_key_id = poll_vote_info.get("poll_key_id", "")
    if not poll_key_id:
        return False
    parent_row_id = find_message_row_id(conn, poll_key_id, key_cache)
    if parent_row_id is None:
        logger.debug(f"Target poll message '{poll_key_id}' for vote not found in cache/database.")
        return False

    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    vote_cols = get_table_columns(conn, "message_add_on_poll_vote", schema_cache)
    vote_opt_cols = get_table_columns(
        conn, "message_add_on_poll_vote_selected_option", schema_cache
    )
    poll_opt_cols = get_table_columns(conn, "message_poll_option", schema_cache)
    if not add_on_cols:
        return False

    cur = conn.cursor()
    sender_ts = poll_vote_info.get("sender_ts", timestamp)

    # A sender can have at most one active vote add-on per poll. Remove any prior vote.
    if vote_opt_cols:
        cur.execute(
            "DELETE FROM message_add_on_poll_vote_selected_option WHERE message_add_on_row_id IN ("
            "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type = 67)",
            (parent_row_id, from_me, sender_jid_row_id),
        )
    if vote_cols:
        cur.execute(
            "DELETE FROM message_add_on_poll_vote WHERE message_add_on_row_id IN ("
            "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type = 67)",
            (parent_row_id, from_me, sender_jid_row_id),
        )
    cur.execute(
        "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND from_me = ? AND sender_jid_row_id IS ? AND message_add_on_type = 67",
        (parent_row_id, from_me, sender_jid_row_id),
    )

    row_map = {
        "parent_message_row_id": parent_row_id,
        "from_me": from_me,
        "sender_jid_row_id": sender_jid_row_id,
        "status": 0,
        "message_add_on_type": 67,
        "timestamp": timestamp,
        "key_id": key_id or "",
        "chat_row_id": chat_row_id,
        "received_timestamp": timestamp,
    }
    valid_cols = [c for c in row_map if c in add_on_cols]
    cur.execute(
        f"INSERT INTO message_add_on ({', '.join(valid_cols)}) VALUES ({', '.join('?' for _ in valid_cols)})",
        [row_map[c] for c in valid_cols],
    )
    add_on_id = cur.lastrowid
    if vote_cols and add_on_id:
        cur.execute(
            "INSERT INTO message_add_on_poll_vote (message_add_on_row_id, sender_timestamp) VALUES (?, ?)",
            (add_on_id, sender_ts),
        )

    # Link selected options if present or encrypted
    sel_hashes = list(poll_vote_info.get("selected_option_hashes", []))
    enc_payload = poll_vote_info.get("enc_payload")
    enc_iv = poll_vote_info.get("enc_iv")
    if not sel_hashes and enc_payload and enc_iv:
        secret_cols = get_table_columns(conn, "message_secret", schema_cache)
        if secret_cols:
            cur.execute(
                "SELECT message_secret FROM message_secret WHERE message_row_id = ?",
                (parent_row_id,),
            )
            s_row = cur.fetchone()
            if s_row and s_row[0]:
                orig_secret = s_row[0]
                cur.execute(
                    "SELECT m.key_id, COALESCE(NULLIF(j.raw_string, ''), j_chat.raw_string, ''), m.from_me "
                    "FROM message m "
                    "LEFT JOIN jid j ON j._id = m.sender_jid_row_id "
                    "LEFT JOIN chat c ON c._id = m.chat_row_id "
                    "LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id "
                    "WHERE m._id = ?",
                    (parent_row_id,),
                )
                p_info = cur.fetchone()
                p_key_id = p_info[0] if p_info else poll_key_id
                p_creator_jid = p_info[1] if p_info else ""
                if p_info and p_info[2] == 1:
                    p_c_row = cur.execute(
                        "SELECT j.raw_string FROM chat c JOIN jid j ON j._id = c.account_jid_row_id WHERE c._id = ?",
                        (chat_row_id,),
                    ).fetchone()
                    if not p_c_row:
                        p_c_row = cur.execute(
                            "SELECT raw_string FROM jid WHERE server IN ('s.whatsapp.net', 'lid') AND user IS NOT NULL ORDER BY _id ASC LIMIT 1"
                        ).fetchone()
                    if p_c_row and p_c_row[0]:
                        p_creator_jid = p_c_row[0]

                if from_me == 1 or sender_jid_row_id is None or sender_jid_row_id <= 0:
                    v_jid_row = cur.execute(
                        "SELECT j.raw_string FROM chat c JOIN jid j ON j._id = c.account_jid_row_id WHERE c._id = ?",
                        (chat_row_id,),
                    ).fetchone()
                    if not v_jid_row:
                        v_jid_row = cur.execute(
                            "SELECT raw_string FROM jid WHERE server IN ('s.whatsapp.net', 'lid') AND user IS NOT NULL ORDER BY _id ASC LIMIT 1"
                        ).fetchone()
                    v_jid_str = v_jid_row[0] if v_jid_row else ""
                else:
                    cur.execute(
                        "SELECT raw_string FROM jid WHERE _id = ?",
                        (sender_jid_row_id,),
                    )
                    v_jid_row = cur.fetchone()
                    v_jid_str = v_jid_row[0] if v_jid_row else ""

                sel_hashes = decrypt_poll_vote_payload(
                    enc_payload,
                    enc_iv,
                    p_key_id,
                    p_creator_jid,
                    v_jid_str,
                    orig_secret,
                )

    if vote_opt_cols and add_on_id and sel_hashes and poll_opt_cols:
        cur.execute(
            "SELECT _id, option_name, option_sha256 FROM message_poll_option WHERE message_row_id = ?",
            (parent_row_id,),
        )
        opt_lookup: dict[bytes, int] = {}
        for opt_r in cur.fetchall():
            opt_id, opt_name, opt_sha = opt_r[0], opt_r[1], opt_r[2]
            if opt_sha:
                try:
                    h = base64.b64decode(opt_sha)
                    opt_lookup[h] = opt_id
                except (ValueError, Exception):
                    pass
            if opt_name:
                h_name = hashlib.sha256(opt_name.encode("utf-8")).digest()
                opt_lookup[h_name] = opt_id

        for h in sel_hashes:
            if h in opt_lookup:
                cur.execute(
                    "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (?, ?)",
                    (add_on_id, opt_lookup[h]),
                )

        cur.execute(
            "UPDATE message_poll_option SET vote_total = ("
            "  SELECT COUNT(DISTINCT ps.message_add_on_row_id) "
            "  FROM message_add_on_poll_vote_selected_option ps "
            "  JOIN message_add_on mao ON mao._id = ps.message_add_on_row_id "
            "  WHERE ps.message_poll_option_id = message_poll_option._id AND mao.parent_message_row_id = ?"
            ") WHERE message_row_id = ?",
            (parent_row_id, parent_row_id),
        )

    return True


def apply_pin_in_chat(
    conn: sqlite3.Connection,
    pin_info: dict,
    sender_jid_row_id: int | None,
    from_me: int,
    timestamp: int,
    key_cache: dict[str, int],
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    key_id: str,
    chat_row_id: int | None,
) -> bool:
    """Record a pin-in-chat add-on for a message, satisfying message_add_on NOT NULL constraints. Returns True if applied, False otherwise."""
    target_key_id = pin_info.get("target_key_id", "")
    if not target_key_id:
        return False
    parent_row_id = find_message_row_id(conn, target_key_id, key_cache)
    if parent_row_id is None:
        logger.debug(f"Target message '{target_key_id}' for pin not found in cache/database.")
        return False

    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    pin_cols = get_table_columns(conn, "message_add_on_pin_in_chat", schema_cache)
    if not add_on_cols:
        return False

    cur = conn.cursor()
    pin_state = pin_info.get("pin_state", 1)
    sender_ts = pin_info.get("sender_ts", timestamp)

    # Remove existing pin add-on if any
    if pin_cols:
        cur.execute(
            "DELETE FROM message_add_on_pin_in_chat WHERE message_add_on_row_id IN ("
            "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND message_add_on_type = 79)",
            (parent_row_id,),
        )
    cur.execute(
        "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND message_add_on_type = 79",
        (parent_row_id,),
    )

    row_map = {
        "parent_message_row_id": parent_row_id,
        "from_me": from_me,
        "sender_jid_row_id": sender_jid_row_id,
        "status": 0,
        "message_add_on_type": 79,
        "timestamp": timestamp,
        "key_id": key_id or "",
        "chat_row_id": chat_row_id,
    }
    valid_cols = [c for c in row_map if c in add_on_cols]
    cur.execute(
        f"INSERT INTO message_add_on ({', '.join(valid_cols)}) VALUES ({', '.join('?' for _ in valid_cols)})",
        [row_map[c] for c in valid_cols],
    )
    add_on_id = cur.lastrowid
    if pin_cols and add_on_id:
        cur.execute(
            "INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id, pin_in_chat_state, sender_timestamp) VALUES (?, ?, ?)",
            (add_on_id, pin_state, sender_ts),
        )
    return True


def apply_location_record(
    conn: sqlite3.Connection,
    msg_row_id: int,
    location_info: dict,
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
) -> None:
    """Insert location metadata into message_location."""
    loc_cols = get_table_columns(conn, "message_location", schema_cache)
    if not loc_cols:
        return

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message_location (message_row_id, latitude, longitude, place_name, place_address, url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            msg_row_id,
            location_info.get("latitude", 0.0),
            location_info.get("longitude", 0.0),
            location_info.get("place_name"),
            location_info.get("place_address"),
            location_info.get("url"),
        ),
    )


def apply_protocol_record(
    conn: sqlite3.Connection,
    msg: dict,
    sender_jid_row_id: int | None,
    from_me: int,
    timestamp: int,
    key_cache: dict[str, int],
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
    chat_row_id: int | None,
) -> bool:
    """Apply protocol revoke or edit updates to an existing message, recording edit add-ons. Returns True if applied, False otherwise."""
    cur = conn.cursor()
    applied = False

    if msg.get("revoke_info"):
        target_key = msg["revoke_info"].get("target_key_id", "")
        if target_key:
            target_id = find_message_row_id(conn, target_key, key_cache)
            if target_id is not None:
                cur.execute(
                    "UPDATE message SET message_type = 15, text_data = NULL WHERE _id = ?",
                    (target_id,),
                )
                revoked_cols = get_table_columns(conn, "message_revoked", schema_cache)
                if revoked_cols:
                    try:
                        cur.execute(
                            "INSERT OR REPLACE INTO message_revoked (message_row_id, revoked_key_id, admin_jid_row_id, revoke_timestamp) VALUES (?, ?, ?, ?)",
                            (target_id, target_key, 0, timestamp),
                        )
                    except sqlite3.OperationalError:
                        pass
                if get_table_columns(conn, "message_media", schema_cache):
                    cur.execute("DELETE FROM message_media WHERE message_row_id = ?", (target_id,))
                if get_table_columns(conn, "message_text", schema_cache):
                    cur.execute("DELETE FROM message_text WHERE message_row_id = ?", (target_id,))
                if get_table_columns(conn, "message_thumbnail", schema_cache):
                    cur.execute("DELETE FROM message_thumbnail WHERE message_row_id = ?", (target_id,))
                if get_table_columns(conn, "message_location", schema_cache):
                    cur.execute("DELETE FROM message_location WHERE message_row_id = ?", (target_id,))
                if get_table_columns(conn, "message_vcard", schema_cache):
                    cur.execute("DELETE FROM message_vcard WHERE message_row_id = ?", (target_id,))
                applied = True

    if msg.get("edit_info"):
        target_key = msg["edit_info"].get("target_key_id", "")
        new_text = msg["edit_info"].get("new_text")
        edited_ts = msg["edit_info"].get("edited_ts", timestamp)
        if target_key:
            target_id = find_message_row_id(conn, target_key, key_cache)
            if target_id is not None and new_text is not None:
                cur.execute(
                    "UPDATE message SET text_data = ? WHERE _id = ?",
                    (new_text, target_id),
                )
                edit_cols = get_table_columns(conn, "message_edit_info", schema_cache)
                if edit_cols:
                    cur.execute(
                        "INSERT OR REPLACE INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp) "
                        "VALUES (?, ?, ?, ?)",
                        (target_id, target_key, edited_ts, edited_ts),
                    )
                add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
                if add_on_cols:
                    row_map = {
                        "parent_message_row_id": target_id,
                        "from_me": from_me,
                        "sender_jid_row_id": sender_jid_row_id,
                        "status": 0,
                        "message_add_on_type": 74,
                        "timestamp": edited_ts,
                        "key_id": target_key or "",
                        "chat_row_id": chat_row_id,
                    }
                    valid_cols = [c for c in row_map if c in add_on_cols]
                    cur.execute(
                        f"INSERT INTO message_add_on ({', '.join(valid_cols)}) VALUES ({', '.join('?' for _ in valid_cols)})",
                        [row_map[c] for c in valid_cols],
                    )
                applied = True

    return applied


@dataclass(slots=True)
class DeletionAuditRecord:
    msg_id: int
    key_id: str | None
    chat_row_id: int | None
    timestamp: int | None
    verified: bool
    rejection_reason: str | None


CLOCK_SKEW_TOLERANCE_MS = 3_600_000  # 1 hour clock skew tolerance


def verify_message_deletion_candidates(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    backup_timestamp: int | None,
    max_sequence: int | None,
    logger: logging.Logger,
    schema_cache: dict[str, set[str]] | None,
) -> tuple[list[int], list[DeletionAuditRecord]]:
    """Verify deletion candidate IDs against message timestamps, sequence ceilings, and stanza keys.

    Gates candidate IDs to prevent accidental message loss from drifted SQLite autoincrement sequences:
    - Gate 1: Causality verification (message timestamp <= backup_timestamp + 1h).
    - Gate 2: Sequence ceiling verification (candidate _id <= max_sequence).
    - Gate 3: Stanza key validation (key_id must be non-empty valid string).
    - Gate 4: Active chat context verification (chat_row_id must reference an existing chat record).
    """
    if not candidate_ids:
        return [], []

    # Defensive normalization: convert epoch seconds to epoch milliseconds if needed
    normalized_backup_ts = backup_timestamp
    if normalized_backup_ts is not None and normalized_backup_ts < 100_000_000_000:
        normalized_backup_ts *= 1000

    msg_cols = get_table_columns(conn, "message", schema_cache)
    has_key_id = "key_id" in msg_cols
    has_timestamp = "timestamp" in msg_cols
    has_chat_row_id = "chat_row_id" in msg_cols

    sel_key = "key_id" if has_key_id else "NULL"
    sel_ts = "timestamp" if has_timestamp else "NULL"
    sel_chat = "chat_row_id" if has_chat_row_id else "NULL"

    chunk_size = 500
    cur = conn.cursor()
    verified_ids: list[int] = []
    audit_records: list[DeletionAuditRecord] = []

    has_chat_table = bool(
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'").fetchone()
    )
    existing_chat_ids: set[int] = set()
    if has_chat_table:
        existing_chat_ids = {r[0] for r in cur.execute("SELECT _id FROM chat").fetchall()}

    for i in range(0, len(candidate_ids), chunk_size):
        chunk = candidate_ids[i : i + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        cur.execute(
            f"SELECT _id, {sel_key}, {sel_ts}, {sel_chat} FROM message WHERE _id IN ({placeholders})",
            chunk,
        )
        row_map = {row[0]: row for row in cur.fetchall()}

        for cid in chunk:
            row = row_map.get(cid)
            if row is None:
                audit_records.append(
                    DeletionAuditRecord(
                        msg_id=cid,
                        key_id=None,
                        chat_row_id=None,
                        timestamp=None,
                        verified=False,
                        rejection_reason="ROW_NOT_FOUND",
                    )
                )
                continue

            msg_id, key_id, ts, chat_row_id = row

            # Gate 1: Causality check (timestamp upper-bound)
            if (
                normalized_backup_ts is not None
                and ts is not None
                and ts > (normalized_backup_ts + CLOCK_SKEW_TOLERANCE_MS)
            ):
                logger.warning(
                    "Rejected deletion of message %d (key_id=%s, ts=%d): timestamp exceeds backup creation date %d (Gate 1 Time Paradox)",
                    msg_id,
                    key_id,
                    ts,
                    normalized_backup_ts,
                )
                audit_records.append(
                    DeletionAuditRecord(
                        msg_id=msg_id,
                        key_id=key_id,
                        chat_row_id=chat_row_id,
                        timestamp=ts,
                        verified=False,
                        rejection_reason="TIME_PARADOX",
                    )
                )
                continue

            # Gate 2: Sequence ceiling check
            if max_sequence is not None and msg_id > max_sequence:
                logger.warning(
                    "Rejected deletion of message %d (key_id=%s): ID exceeds backup max sequence %d (Gate 2 Sequence Overflow)",
                    msg_id,
                    key_id,
                    max_sequence,
                )
                audit_records.append(
                    DeletionAuditRecord(
                        msg_id=msg_id,
                        key_id=key_id,
                        chat_row_id=chat_row_id,
                        timestamp=ts,
                        verified=False,
                        rejection_reason="SEQUENCE_OVERFLOW",
                    )
                )
                continue

            # Gate 3: Stanza key integrity
            if has_key_id and (not key_id or not isinstance(key_id, str) or not key_id.strip()):
                logger.warning(
                    "Rejected deletion of message %d: missing or invalid stanza key (Gate 3 Invalid Key)",
                    msg_id,
                )
                audit_records.append(
                    DeletionAuditRecord(
                        msg_id=msg_id,
                        key_id=key_id,
                        chat_row_id=chat_row_id,
                        timestamp=ts,
                        verified=False,
                        rejection_reason="INVALID_KEY",
                    )
                )
                continue

            # Gate 4: Active Chat Context check
            if has_chat_table and has_chat_row_id and chat_row_id is not None:
                if chat_row_id not in existing_chat_ids:
                    logger.warning(
                        "Rejected deletion of message %d (key_id=%s): references non-existent chat_row_id %d (Gate 4 Orphaned Chat Context)",
                        msg_id,
                        key_id,
                        chat_row_id,
                    )
                    audit_records.append(
                        DeletionAuditRecord(
                            msg_id=msg_id,
                            key_id=key_id,
                            chat_row_id=chat_row_id,
                            timestamp=ts,
                            verified=False,
                            rejection_reason="ORPHANED_CHAT",
                        )
                    )
                    continue

            verified_ids.append(msg_id)
            audit_records.append(
                DeletionAuditRecord(
                    msg_id=msg_id,
                    key_id=key_id,
                    chat_row_id=chat_row_id,
                    timestamp=ts,
                    verified=True,
                    rejection_reason=None,
                )
            )

    return verified_ids, audit_records


def apply_message_deletions(
    conn: sqlite3.Connection,
    deleted_json_path: str,
    logger: logging.Logger,
    schema_cache: dict[str, set[str]] | None,
    backup_timestamp: int | None,
    max_sequence: int | None,
) -> int:
    """Delete messages and auxiliary records (media, add-ons, polls, quoted, receipts) in deletedMessages_*.json."""
    with open(deleted_json_path, encoding="utf-8") as f:
        data = json.load(f)

    raw_ids = data.get("deleted_ids", data) if isinstance(data, dict) else data
    if not isinstance(raw_ids, list) or not raw_ids:
        return 0

    candidate_ids = [int(i) for i in raw_ids if isinstance(i, (int, str)) and str(i).isdigit()]
    if not candidate_ids:
        return 0

    verified_ids, _ = verify_message_deletion_candidates(
        conn, candidate_ids, backup_timestamp, max_sequence, logger, schema_cache
    )
    if not verified_ids:
        return 0

    cur = conn.cursor()
    total_deleted = 0
    chunk_size = 500

    has_add_on_reaction = bool(get_table_columns(conn, "message_add_on_reaction", schema_cache))
    has_add_on_pin = bool(get_table_columns(conn, "message_add_on_pin_in_chat", schema_cache))
    has_add_on_poll_vote = bool(get_table_columns(conn, "message_add_on_poll_vote", schema_cache))
    has_add_on_poll_opt = bool(
        get_table_columns(conn, "message_add_on_poll_vote_selected_option", schema_cache)
    )
    has_add_on = bool(get_table_columns(conn, "message_add_on", schema_cache))
    has_poll_opt = bool(get_table_columns(conn, "message_poll_option", schema_cache))
    has_poll = bool(get_table_columns(conn, "message_poll", schema_cache))
    has_loc = bool(get_table_columns(conn, "message_location", schema_cache))
    has_vcard = bool(get_table_columns(conn, "message_vcard", schema_cache))
    has_edit = bool(get_table_columns(conn, "message_edit_info", schema_cache))
    has_media = bool(get_table_columns(conn, "message_media", schema_cache))
    has_quoted = bool(get_table_columns(conn, "message_quoted", schema_cache))
    has_receipt_user = bool(get_table_columns(conn, "receipt_user", schema_cache))
    has_secret = bool(get_table_columns(conn, "message_secret", schema_cache))
    has_text = bool(get_table_columns(conn, "message_text", schema_cache))
    has_thumb = bool(get_table_columns(conn, "message_thumbnail", schema_cache))
    has_view_once = bool(get_table_columns(conn, "message_view_once_media", schema_cache))
    has_revoked = bool(get_table_columns(conn, "message_revoked", schema_cache))
    has_system = bool(get_table_columns(conn, "message_system", schema_cache))

    for i in range(0, len(verified_ids), chunk_size):
        chunk = verified_ids[i : i + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)

        if has_add_on_reaction and has_add_on:
            cur.execute(
                f"DELETE FROM message_add_on_reaction WHERE message_add_on_row_id IN ("
                f"SELECT _id FROM message_add_on WHERE parent_message_row_id IN ({placeholders}))",
                chunk,
            )
        if has_add_on_pin and has_add_on:
            cur.execute(
                f"DELETE FROM message_add_on_pin_in_chat WHERE message_add_on_row_id IN ("
                f"SELECT _id FROM message_add_on WHERE parent_message_row_id IN ({placeholders}))",
                chunk,
            )
        if has_add_on_poll_opt and has_add_on:
            cur.execute(
                f"DELETE FROM message_add_on_poll_vote_selected_option WHERE message_add_on_row_id IN ("
                f"SELECT _id FROM message_add_on WHERE parent_message_row_id IN ({placeholders}))",
                chunk,
            )
        if has_add_on_poll_vote and has_add_on:
            cur.execute(
                f"DELETE FROM message_add_on_poll_vote WHERE message_add_on_row_id IN ("
                f"SELECT _id FROM message_add_on WHERE parent_message_row_id IN ({placeholders}))",
                chunk,
            )
        if has_add_on:
            cur.execute(
                f"DELETE FROM message_add_on WHERE parent_message_row_id IN ({placeholders})",
                chunk,
            )
        if has_poll_opt:
            cur.execute(
                f"DELETE FROM message_poll_option WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_poll:
            cur.execute(
                f"DELETE FROM message_poll WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_secret:
            cur.execute(
                f"DELETE FROM message_secret WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_loc:
            cur.execute(
                f"DELETE FROM message_location WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_vcard:
            cur.execute(
                f"DELETE FROM message_vcard WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_edit:
            cur.execute(
                f"DELETE FROM message_edit_info WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_media:
            cur.execute(
                f"DELETE FROM message_media WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_quoted:
            cur.execute(
                f"DELETE FROM message_quoted WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_receipt_user:
            cur.execute(
                f"DELETE FROM receipt_user WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_text:
            cur.execute(
                f"DELETE FROM message_text WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_thumb:
            cur.execute(
                f"DELETE FROM message_thumbnail WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_view_once:
            cur.execute(
                f"DELETE FROM message_view_once_media WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_revoked:
            cur.execute(
                f"DELETE FROM message_revoked WHERE message_row_id IN ({placeholders})",
                chunk,
            )
        if has_system:
            cur.execute(
                f"DELETE FROM message_system WHERE message_row_id IN ({placeholders})",
                chunk,
            )

        cur.execute(f"DELETE FROM message WHERE _id IN ({placeholders})", chunk)
        total_deleted += cur.rowcount

    if total_deleted > 0:
        chat_cols = get_table_columns(conn, "chat", schema_cache)
        if "display_message_row_id" in chat_cols:
            cur.execute(
                """
                UPDATE chat
                SET display_message_row_id = (
                    SELECT _id FROM message
                    WHERE chat_row_id = chat._id
                      AND message_type NOT IN (25, 67, 74, 79)
                    ORDER BY timestamp DESC, _id DESC LIMIT 1
                )
                WHERE display_message_row_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM message m
                      WHERE m._id = chat.display_message_row_id
                        AND m.chat_row_id = chat._id
                  )
                """
            )
    return total_deleted


def resolve_or_create_jid(
    conn: sqlite3.Connection,
    jid_str: str,
    logger: logging.Logger,
    jid_cache: dict[str, int] | None,
) -> int:
    """Resolve a JID string to its jid._id primary key, creating it if absent."""
    if not is_valid_jid_string(jid_str) and jid_str != "0":
        logger.warning(f"Rejected invalid WhatsApp JID: {repr(jid_str)[:50]}")
        raise ValueError(f"Invalid WhatsApp JID: {repr(jid_str)[:50]}")

    canonical_jid = sanitize_jid_string(jid_str)
    if jid_cache is not None:
        if canonical_jid in jid_cache:
            return jid_cache[canonical_jid]
        if jid_str in jid_cache:
            return jid_cache[jid_str]

    if "@" in canonical_jid:
        user, server = canonical_jid.split("@", 1)
    else:
        user, server = canonical_jid, "s.whatsapp.net"
        canonical_jid = f"{user}@{server}"

    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    has_device = "device" in cols
    has_raw_string = "raw_string" in cols
    if has_device and has_raw_string:
        cur.execute(
            "SELECT _id FROM jid WHERE user = ? AND server = ? "
            "ORDER BY "
            "CASE WHEN (raw_string = user || '@' || server OR raw_string IS NULL OR raw_string = '') AND (device IS NULL OR device = 0) THEN 0 "
            "     WHEN device IS NULL OR device = 0 THEN 1 ELSE 2 END, "
            "_id ASC LIMIT 1",
            (user, server),
        )
    elif has_device:
        cur.execute(
            "SELECT _id FROM jid WHERE user = ? AND server = ? "
            "ORDER BY CASE WHEN device IS NULL OR device = 0 THEN 0 ELSE 1 END, _id ASC LIMIT 1",
            (user, server),
        )
    else:
        cur.execute(
            "SELECT _id FROM jid WHERE user = ? AND server = ? ORDER BY _id ASC LIMIT 1",
            (user, server),
        )
    row = cur.fetchone()
    if row is not None:
        jid_id = int(row[0])
        if jid_cache is not None:
            jid_cache[canonical_jid] = jid_id
            jid_cache[jid_str] = jid_id
            jid_cache[user] = jid_id
        return jid_id

    insert_cols = ["user", "server"]
    vals = [user, server]
    if "raw_string" in cols:
        insert_cols.append("raw_string")
        vals.append(canonical_jid)
    if "device" in cols:
        insert_cols.append("device")
        vals.append(0)

    sql = (
        f"INSERT INTO jid ({', '.join(insert_cols)}) VALUES ({', '.join('?' for _ in insert_cols)})"
    )
    cur.execute(sql, vals)
    jid_id = int(cur.lastrowid)
    if jid_cache is not None:
        jid_cache[canonical_jid] = jid_id
        jid_cache[jid_str] = jid_id
        jid_cache[user] = jid_id
    return jid_id


def resolve_or_create_chat(
    conn: sqlite3.Connection,
    jid_row_id: int,
    logger: logging.Logger,
    chat_cache: dict[int, int] | None,
) -> int:
    """Resolve a chat row for a jid_row_id, preferring primary phone chats over shadow LID chats."""
    if chat_cache is not None and jid_row_id in chat_cache:
        return chat_cache[jid_row_id]

    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    has_account_jid = "account_jid_row_id" in cols
    has_hidden = "hidden" in cols

    cur.execute("SELECT user, server FROM jid WHERE _id = ?", (jid_row_id,))
    jid_row = cur.fetchone()
    jid_user, jid_server = (jid_row[0], jid_row[1]) if jid_row else ("", "")

    has_jid_map = False
    try:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid_map'")
        has_jid_map = cur.fetchone() is not None
    except sqlite3.OperationalError:
        pass

    c_order = (
        "ORDER BY CASE WHEN c.hidden = 0 THEN 0 ELSE 1 END, c._id ASC"
        if has_hidden
        else "ORDER BY c._id ASC"
    )
    chat_order = (
        "ORDER BY CASE WHEN hidden = 0 THEN 0 ELSE 1 END, _id ASC"
        if has_hidden
        else "ORDER BY _id ASC"
    )

    chat_id = None

    if jid_server == "lid":
        # 1. Incoming JID is a LID. Check if jid_map maps this LID to an existing phone chat.
        if has_jid_map:
            cur.execute(
                f"""
                SELECT c._id FROM chat c
                JOIN jid_map jm ON c.jid_row_id = jm.jid_row_id
                WHERE jm.lid_row_id = ?
                {c_order}
                LIMIT 1
                """,
                (jid_row_id,),
            )
            match = cur.fetchone()
            if match is not None:
                chat_id = int(match[0])
                if has_hidden:
                    cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (chat_id,))
                if has_account_jid:
                    cur.execute(
                        "UPDATE chat SET account_jid_row_id = ? WHERE _id = ? AND (account_jid_row_id IS NULL OR account_jid_row_id = 0)",
                        (jid_row_id, chat_id),
                    )

        # 2. Check if a chat already has account_jid_row_id pointing to this LID
        if chat_id is None and has_account_jid:
            cur.execute(
                f"SELECT _id FROM chat WHERE account_jid_row_id = ? {chat_order} LIMIT 1",
                (jid_row_id,),
            )
            match = cur.fetchone()
            if match is not None:
                chat_id = int(match[0])

        # 3. Check lid_display_name matching to an existing chat with phone JID
        if chat_id is None:
            try:
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lid_display_name'"
                )
                if cur.fetchone() is not None:
                    cur.execute(
                        "SELECT display_name FROM lid_display_name WHERE lid_row_id = ? AND display_name IS NOT NULL",
                        (jid_row_id,),
                    )
                    ldn_row = cur.fetchone()
                    if ldn_row and ldn_row[0]:
                        clean_digits = re.sub(r"\D", "", ldn_row[0])
                        if clean_digits and len(clean_digits) >= 8:
                            cur.execute(
                                f"SELECT c._id FROM chat c JOIN jid j ON j._id = c.jid_row_id "
                                f"WHERE (j.user = ? OR j.user LIKE ?) "
                                f"{c_order} LIMIT 1",
                                (clean_digits, f"%{clean_digits}"),
                            )
                            match = cur.fetchone()
                            if match is not None:
                                chat_id = int(match[0])
                                if has_account_jid:
                                    cur.execute(
                                        "UPDATE chat SET account_jid_row_id = ? WHERE _id = ? AND (account_jid_row_id IS NULL OR account_jid_row_id = 0)",
                                        (jid_row_id, chat_id),
                                    )
            except sqlite3.OperationalError:
                pass

        # 4. If no phone chat found, check direct match on chat.jid_row_id = LID or same natural LID user
        if chat_id is None:
            cur.execute(
                f"""
                SELECT c._id FROM chat c
                JOIN jid j ON c.jid_row_id = j._id
                WHERE (c.jid_row_id = ? OR (j.user = ? AND j.server = 'lid'))
                {c_order} LIMIT 1
                """,
                (jid_row_id, jid_user),
            )
            match = cur.fetchone()
            if match is not None:
                chat_id = int(match[0])

    else:
        # Incoming JID is phone JID or group JID or other
        # 1. Direct match on chat.jid_row_id
        cur.execute("SELECT _id FROM chat WHERE jid_row_id = ? LIMIT 1", (jid_row_id,))
        match = cur.fetchone()
        if match is not None:
            chat_id = int(match[0])

        # 2. Natural identity match: check if chat exists for (user, server)
        if chat_id is None and jid_user and jid_server:
            cur.execute(
                f"""
                SELECT c._id FROM chat c
                JOIN jid j ON c.jid_row_id = j._id
                WHERE j.user = ? AND j.server = ?
                {c_order}
                LIMIT 1
                """,
                (jid_user, jid_server),
            )
            match = cur.fetchone()
            if match is not None:
                chat_id = int(match[0])

        # 3. If phone JID, check if chat exists mapped via jid_map to account_jid_row_id or shadow chat
        if chat_id is None and jid_server == "s.whatsapp.net" and has_jid_map:
            cur.execute(
                f"""
                SELECT c._id FROM chat c
                JOIN jid_map jm ON (c.account_jid_row_id = jm.lid_row_id OR c.jid_row_id = jm.lid_row_id)
                WHERE jm.jid_row_id = ?
                {c_order}
                LIMIT 1
                """,
                (jid_row_id,),
            )
            match = cur.fetchone()
            if match is not None:
                chat_id = int(match[0])

    # 5. If still no chat found, insert a new chat row
    if chat_id is None:
        if has_hidden:
            cur.execute("INSERT INTO chat (jid_row_id, hidden) VALUES (?, 0)", (jid_row_id,))
        else:
            cur.execute("INSERT INTO chat (jid_row_id) VALUES (?)", (jid_row_id,))
        chat_id = int(cur.lastrowid)

    if chat_cache is not None:
        chat_cache[jid_row_id] = chat_id
    return chat_id


def apply_messages_bin(
    conn: sqlite3.Connection,
    messages_bin_path: str,
    logger: logging.Logger,
    jid_cache: dict[str, int] | None,
    chat_cache: dict[int, int] | None,
    schema_cache: dict[str, set[str]] | None,
) -> int:
    """Parse messages.bin and replay records into SQLite message, message_media, message_quoted, and add-ons."""
    messages = read_delimited_messages_bin(messages_bin_path)
    if not messages:
        return 0

    cur = conn.cursor()
    if schema_cache is not None and "message" in schema_cache:
        msg_cols = schema_cache["message"]
    else:
        msg_cols = {r[1] for r in cur.execute("PRAGMA table_info(message)").fetchall()}
        if schema_cache is not None:
            schema_cache["message"] = msg_cols

    if schema_cache is not None and "message_media" in schema_cache:
        media_cols = schema_cache["message_media"]
    else:
        media_cols = {r[1] for r in cur.execute("PRAGMA table_info(message_media)").fetchall()}
        if schema_cache is not None:
            schema_cache["message_media"] = media_cols

    if schema_cache is not None and "message_vcard" in schema_cache:
        has_vcard_table = bool(schema_cache["message_vcard"])
    else:
        has_vcard_table = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_vcard'"
            ).fetchone()
        )
        if schema_cache is not None:
            schema_cache["message_vcard"] = {"exists"} if has_vcard_table else set()

    if schema_cache is not None and "message_text" in schema_cache:
        has_message_text_table = bool(schema_cache["message_text"])
    else:
        has_message_text_table = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_text'"
            ).fetchone()
        )
        if schema_cache is not None:
            schema_cache["message_text"] = {"exists"} if has_message_text_table else set()

    if schema_cache is not None and "message_thumbnail" in schema_cache:
        has_message_thumbnail_table = bool(schema_cache["message_thumbnail"])
    else:
        has_message_thumbnail_table = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_thumbnail'"
            ).fetchone()
        )
        if schema_cache is not None:
            schema_cache["message_thumbnail"] = {"exists"} if has_message_thumbnail_table else set()

    if schema_cache is not None and "message_view_once_media" in schema_cache:
        has_view_once_table = bool(schema_cache["message_view_once_media"])
    else:
        has_view_once_table = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_view_once_media'"
            ).fetchone()
        )
        if schema_cache is not None:
            schema_cache["message_view_once_media"] = {"exists"} if has_view_once_table else set()

    if schema_cache is not None and "message_system" in schema_cache:
        has_message_system_table = bool(schema_cache["message_system"])
    else:
        has_message_system_table = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_system'"
            ).fetchone()
        )
        if schema_cache is not None:
            schema_cache["message_system"] = {"exists"} if has_message_system_table else set()

    if schema_cache is not None and "message_revoked" in schema_cache:
        has_message_revoked_table = bool(schema_cache["message_revoked"])
    else:
        has_message_revoked_table = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_revoked'"
            ).fetchone()
        )
        if schema_cache is not None:
            schema_cache["message_revoked"] = {"exists"} if has_message_revoked_table else set()

    # Fast batch deletion of conflicting messages by key_id
    existing_media_paths: dict[str, str] = {}
    existing_row_ids: dict[str, int] = {}
    if "key_id" in msg_cols:
        key_ids_to_delete = [
            msg["key_id"]
            for msg in messages
            if msg.get("key_id")
            and (
                (msg.get("text_data") and msg["text_data"].strip())
                or msg.get("media_info")
                or msg.get("vcard")
                or msg.get("poll_info")
                or msg.get("location_info")
                or msg.get("link_preview_info")
                or msg.get("stub_type") is not None
                or msg.get("message_type") in (15, 64)
            )
        ]
        chunk_size = 500
        for i in range(0, len(key_ids_to_delete), chunk_size):
            chunk = key_ids_to_delete[i : i + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
            try:
                for r_id, k_id in cur.execute(
                    f"SELECT _id, key_id FROM message WHERE key_id IN ({placeholders})", chunk
                ).fetchall():
                    existing_row_ids[k_id] = r_id
            except sqlite3.OperationalError:
                pass
            if media_cols:
                try:
                    for k_id, f_path in cur.execute(
                        f"SELECT m.key_id, mm.file_path FROM message m "
                        f"JOIN message_media mm ON mm.message_row_id = m._id "
                        f"WHERE m.key_id IN ({placeholders}) AND mm.file_path IS NOT NULL",
                        chunk,
                    ).fetchall():
                        if f_path:
                            existing_media_paths[k_id] = f_path
                except sqlite3.OperationalError:
                    pass
            old_chunk_ids = [existing_row_ids[k] for k in chunk if k in existing_row_ids]
            if old_chunk_ids:
                old_ph = ", ".join("?" for _ in old_chunk_ids)
                try:
                    cur.execute(
                        f"DELETE FROM message_poll_option WHERE message_row_id IN ({old_ph})",
                        old_chunk_ids,
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    cur.execute(
                        f"DELETE FROM message_poll WHERE message_row_id IN ({old_ph})",
                        old_chunk_ids,
                    )
                except sqlite3.OperationalError:
                    pass
            cur.execute(f"DELETE FROM message WHERE key_id IN ({placeholders})", chunk)

    # Pre-compute static message insert statement
    static_msg_keys = [
        "chat_row_id",
        "from_me",
        "key_id",
        "sender_jid_row_id",
        "timestamp",
        "message_type",
        "text_data",
        "starred",
        "status",
    ]
    target_msg_cols = [c for c in static_msg_keys if c in msg_cols]
    msg_placeholders = ", ".join("?" for _ in target_msg_cols)
    sql_insert_msg = (
        f"INSERT INTO message ({', '.join(target_msg_cols)}) VALUES ({msg_placeholders})"
    )

    # Pre-compute static media insert statement if media table exists
    sql_insert_media = None
    target_media_cols: list[str] = []
    if media_cols:
        static_media_keys = [
            "message_row_id",
            "chat_row_id",
            "file_path",
            "file_size",
            "media_name",
            "message_url",
            "mime_type",
            "file_hash",
            "transferred",
        ]
        target_media_cols = [c for c in static_media_keys if c in media_cols]
        m_placeholders = ", ".join("?" for _ in target_media_cols)
        sql_insert_media = (
            f"INSERT INTO message_media ({', '.join(target_media_cols)}) VALUES ({m_placeholders})"
        )

    inserted = 0
    key_cache: dict[str, int] = {}
    key_ts_cache: dict[str, int] = {}
    pending_add_ons: list[PendingAddOn] = []
    pending_quotes: list[tuple[int, str]] = []
    max_ts_per_chat: dict[int, int] = {}
    chats_to_unhide: set[int] = set()
    for msg in messages:
        remote_jid = msg["remote_jid"]
        if not remote_jid or not msg.get("has_content", True):
            continue

        is_group = bool(
            remote_jid.endswith("@g.us")
            or remote_jid.endswith("@temp")
            or ("@" in remote_jid and remote_jid.split("@", 1)[1] == "g.us")
        )

        is_stub = msg.get("stub_type") is not None
        is_revoked = msg["message_type"] in (15, 64)
        is_add_on = bool(
            msg.get("reaction_info")
            or msg.get("pin_info")
            or msg.get("poll_vote_info")
            or msg.get("revoke_info")
            or msg.get("edit_info")
            or msg["message_type"] in (25, 67, 74, 79)
        )
        is_conversational = bool(
            msg["message_type"] != 7
            and not is_revoked
            and (
                (msg.get("text_data") and msg["text_data"].strip())
                or msg.get("media_info")
                or msg.get("vcard")
                or msg.get("location_info")
                or msg.get("poll_info")
                or msg.get("link_preview_info")
            )
        )

        if not is_conversational and not is_stub and not is_add_on and not is_revoked:
            # Control / empty / status frame without conversational message or system stub.
            # If user receipts are present on an existing message, attach them without inserting a blank message.
            if msg.get("user_receipts") and msg.get("key_id"):
                existing_id = key_cache.get(msg["key_id"]) or find_message_row_id(
                    conn, msg["key_id"], key_cache
                )
                if existing_id:
                    ru_cols = get_table_columns(conn, "receipt_user", schema_cache)
                    if ru_cols:
                        for ur in msg["user_receipts"]:
                            ur_jid = ur.get("user_jid", "")
                            if ur_jid and not is_valid_jid_string(ur_jid):
                                continue
                            try:
                                ur_jid_id = (
                                    resolve_or_create_jid(conn, ur_jid, logger, jid_cache)
                                    if ur_jid
                                    else resolve_or_create_jid(conn, remote_jid, logger, jid_cache)
                                )
                            except ValueError:
                                continue
                            cur.execute(
                                "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, receipt_timestamp, read_timestamp, played_timestamp) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (
                                    existing_id,
                                    ur_jid_id,
                                    ur.get("receipt_timestamp"),
                                    ur.get("read_timestamp"),
                                    ur.get("played_timestamp"),
                                ),
                            )
            if msg.get("from_me") and msg.get("status", 0) > 0 and msg.get("key_id"):
                existing_id = key_cache.get(msg["key_id"]) or find_message_row_id(
                    conn, msg["key_id"], key_cache
                )
                if existing_id:
                    cur.execute(
                        "UPDATE message SET status = MAX(status, ?) WHERE _id = ?",
                        (msg["status"], existing_id),
                    )
            continue

        chat_jid_row_id = resolve_or_create_jid(conn, remote_jid, logger, jid_cache)
        chat_row_id = resolve_or_create_chat(conn, chat_jid_row_id, logger, chat_cache)

        if is_conversational or is_group or is_revoked:
            chats_to_unhide.add(chat_row_id)

        # Resolve sender: In Android WhatsApp, 1-on-1 messages always have sender_jid_row_id = 0
        if not is_group or msg["from_me"] == 1:
            sender_jid_row_id = 0
        elif msg.get("participant"):
            try:
                sender_jid_row_id = resolve_or_create_jid(
                    conn, msg["participant"], logger, jid_cache
                )
            except (ValueError, Exception):
                sender_jid_row_id = 0
        else:
            sender_jid_row_id = 0

        # For add-ons (reactions, pins, poll votes, edits, revokes):
        # 1-on-1 and outgoing messages have sender_jid_row_id = -1
        if not is_group or msg["from_me"] == 1:
            add_on_sender_id = -1
        elif msg.get("participant"):
            try:
                add_on_sender_id = resolve_or_create_jid(
                    conn, msg["participant"], logger, jid_cache
                )
            except (ValueError, Exception):
                add_on_sender_id = -1
        else:
            add_on_sender_id = -1

        # Standalone reaction message: apply add-on directly without inserting phantom message row
        if msg.get("reaction_info"):
            applied = apply_reaction_record(
                conn,
                msg["reaction_info"],
                add_on_sender_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                msg.get("key_id", ""),
                chat_row_id,
            )
            if not applied and msg["reaction_info"].get("target_key_id"):
                pending_add_ons.append(
                    PendingAddOn(
                        add_on_type="reaction",
                        target_key_id=msg["reaction_info"]["target_key_id"],
                        msg=msg,
                        sender_jid_row_id=add_on_sender_id,
                        from_me=msg["from_me"],
                        timestamp=msg["timestamp"],
                        key_id=msg.get("key_id", ""),
                        chat_row_id=chat_row_id,
                    )
                )
            inserted += 1
            continue

        # Standalone pin message: apply add-on directly without inserting phantom message row
        if msg.get("pin_info"):
            applied = apply_pin_in_chat(
                conn,
                msg["pin_info"],
                add_on_sender_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                msg.get("key_id", ""),
                chat_row_id,
            )
            if not applied and msg["pin_info"].get("target_key_id"):
                pending_add_ons.append(
                    PendingAddOn(
                        add_on_type="pin",
                        target_key_id=msg["pin_info"]["target_key_id"],
                        msg=msg,
                        sender_jid_row_id=add_on_sender_id,
                        from_me=msg["from_me"],
                        timestamp=msg["timestamp"],
                        key_id=msg.get("key_id", ""),
                        chat_row_id=chat_row_id,
                    )
                )
            inserted += 1
            continue

        # Standalone poll vote message: apply add-on directly without inserting phantom message row
        if msg.get("poll_vote_info"):
            applied = apply_poll_vote(
                conn,
                msg["poll_vote_info"],
                add_on_sender_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                msg.get("key_id", ""),
                chat_row_id,
            )
            if not applied and msg["poll_vote_info"].get("poll_key_id"):
                pending_add_ons.append(
                    PendingAddOn(
                        add_on_type="poll_vote",
                        target_key_id=msg["poll_vote_info"]["poll_key_id"],
                        msg=msg,
                        sender_jid_row_id=add_on_sender_id,
                        from_me=msg["from_me"],
                        timestamp=msg["timestamp"],
                        key_id=msg.get("key_id", ""),
                        chat_row_id=chat_row_id,
                    )
                )
            inserted += 1
            continue

        # Standalone edit protocol message: apply edit directly without inserting phantom message row
        if msg.get("edit_info"):
            applied = apply_protocol_record(
                conn,
                msg,
                add_on_sender_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                chat_row_id,
            )
            if not applied:
                target_k = msg["edit_info"].get("target_key_id", "")
                if target_k:
                    pending_add_ons.append(
                        PendingAddOn(
                            add_on_type="edit",
                            target_key_id=target_k,
                            msg=msg,
                            sender_jid_row_id=add_on_sender_id,
                            from_me=msg["from_me"],
                            timestamp=msg["timestamp"],
                            key_id=msg.get("key_id", ""),
                            chat_row_id=chat_row_id,
                        )
                    )
            inserted += 1
            continue

        # Standalone protocol revoke message: apply revoke directly without inserting phantom message row
        if msg.get("revoke_info") and not is_revoked:
            applied = apply_protocol_record(
                conn,
                msg,
                add_on_sender_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                chat_row_id,
            )
            if not applied:
                target_k = msg["revoke_info"].get("target_key_id", "")
                if target_k:
                    pending_add_ons.append(
                        PendingAddOn(
                            add_on_type="revoke",
                            target_key_id=target_k,
                            msg=msg,
                            sender_jid_row_id=add_on_sender_id,
                            from_me=msg["from_me"],
                            timestamp=msg["timestamp"],
                            key_id=msg.get("key_id", ""),
                            chat_row_id=chat_row_id,
                        )
                    )
            inserted += 1
            continue

        # Fallback delete for messages without key_id
        if not msg.get("key_id") and "chat_row_id" in msg_cols:
            cur.execute(
                "DELETE FROM message WHERE chat_row_id = ? AND timestamp = ? AND from_me = ?",
                (chat_row_id, msg["timestamp"], msg["from_me"]),
            )

        row_map = {
            "chat_row_id": chat_row_id,
            "from_me": msg["from_me"],
            "key_id": msg["key_id"],
            "sender_jid_row_id": sender_jid_row_id,
            "timestamp": msg["timestamp"],
            "message_type": msg["message_type"],
            "text_data": msg["text_data"],
            "starred": msg["starred"],
            "status": msg["status"],
        }
        cur.execute(sql_insert_msg, [row_map[c] for c in target_msg_cols])
        msg_row_id = cur.lastrowid
        inserted += 1

        if is_revoked:
            if has_message_revoked_table and msg_row_id and msg.get("key_id"):
                try:
                    cur.execute(
                        "INSERT OR REPLACE INTO message_revoked (message_row_id, revoked_key_id, admin_jid_row_id, revoke_timestamp) VALUES (?, ?, ?, ?)",
                        (msg_row_id, msg["key_id"], 0, msg["timestamp"]),
                    )
                except sqlite3.OperationalError:
                    pass
            if msg_row_id:
                try:
                    cur.execute("DELETE FROM message_media WHERE message_row_id = ?", (msg_row_id,))
                except sqlite3.OperationalError:
                    pass
        elif is_stub and has_message_system_table and msg_row_id and msg["message_type"] == 7:
            try:
                cur.execute(
                    "INSERT OR REPLACE INTO message_system (message_row_id, action_type) VALUES (?, ?)",
                    (msg_row_id, int(msg["stub_type"])),
                )
            except sqlite3.OperationalError:
                pass

        old_id = (
            key_cache.get(msg["key_id"]) if msg.get("key_id") else None
        ) or existing_row_ids.get(msg.get("key_id", ""))
        if is_revoked and old_id:
            try:
                cur.execute("DELETE FROM message_media WHERE message_row_id = ?", (old_id,))
            except sqlite3.OperationalError:
                pass
        if old_id and msg_row_id and old_id != msg_row_id:
            try:
                cur.execute(
                    "UPDATE message_add_on SET parent_message_row_id = ? WHERE parent_message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            if msg.get("key_id"):
                try:
                    cur.execute("DELETE FROM message WHERE _id = ?", (old_id,))
                except sqlite3.OperationalError:
                    pass
            try:
                cur.execute(
                    "UPDATE poll_name_hash_history SET poll_message_row_id = ? WHERE poll_message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute(
                    "UPDATE reporting_info SET message_row_id = ? WHERE message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute(
                    "UPDATE chat SET last_message_row_id = ? WHERE last_message_row_id = ?",
                    (msg_row_id, old_id),
                )
                cur.execute(
                    "UPDATE chat SET display_message_row_id = ? WHERE display_message_row_id = ?",
                    (msg_row_id, old_id),
                )
                cur.execute(
                    "UPDATE chat SET last_read_message_row_id = ? WHERE last_read_message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass

            # Cascade surrogate ID re-parenting across all auxiliary tables
            try:
                cur.execute(
                    "UPDATE receipt_user SET message_row_id = ? WHERE message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute(
                    "UPDATE message_edit_info SET message_row_id = ? WHERE message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute(
                    "UPDATE message_view_once_media SET message_row_id = ? WHERE message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute(
                    "UPDATE message_secret SET message_row_id = ? WHERE message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute(
                    "UPDATE message_association SET message_row_id = ? WHERE message_row_id = ?",
                    (msg_row_id, old_id),
                )
                cur.execute(
                    "UPDATE message_association SET parent_message_row_id = ? WHERE parent_message_row_id = ?",
                    (msg_row_id, old_id),
                )
            except sqlite3.OperationalError:
                pass

            if not msg.get("quoted_info"):
                try:
                    cur.execute(
                        "UPDATE message_quoted SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass
            else:
                try:
                    cur.execute("DELETE FROM message_quoted WHERE message_row_id = ?", (old_id,))
                except sqlite3.OperationalError:
                    pass

            if not (msg.get("quoted_info") and msg["quoted_info"].get("thumbnail")):
                try:
                    cur.execute(
                        "UPDATE message_quoted_media SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass
            else:
                try:
                    cur.execute(
                        "DELETE FROM message_quoted_media WHERE message_row_id = ?", (old_id,)
                    )
                except sqlite3.OperationalError:
                    pass

            if not msg.get("media_info") and not is_revoked:
                try:
                    cur.execute(
                        "UPDATE message_media SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass
            else:
                try:
                    cur.execute("DELETE FROM message_media WHERE message_row_id = ?", (old_id,))
                except sqlite3.OperationalError:
                    pass

            if not msg.get("location_info"):
                try:
                    cur.execute(
                        "UPDATE message_location SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass

            if not msg.get("vcard"):
                try:
                    cur.execute(
                        "UPDATE message_vcard SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass

            if not msg.get("link_preview_info"):
                try:
                    cur.execute(
                        "UPDATE message_text SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                    cur.execute(
                        "UPDATE message_thumbnail SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass
            else:
                try:
                    cur.execute(
                        "UPDATE message_thumbnail SET message_row_id = ? WHERE message_row_id = ?",
                        (msg_row_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass

        is_displayable = is_conversational or (is_group and msg["message_type"] == 7)
        if (
            is_displayable
            and not is_add_on
            and msg["timestamp"] >= max_ts_per_chat.get(chat_row_id, 0)
        ):
            max_ts_per_chat[chat_row_id] = msg["timestamp"]

        if msg.get("key_id") and msg_row_id:
            key_cache[msg["key_id"]] = msg_row_id
            existing_row_ids[msg["key_id"]] = msg_row_id
            if msg.get("timestamp"):
                key_ts_cache[msg["key_id"]] = msg["timestamp"]

        # Insert media record if present
        if msg["media_info"] and sql_insert_media:
            media = msg["media_info"]
            file_hash_str = (
                base64.b64encode(media["file_sha256"]).decode("ascii")
                if media["file_sha256"]
                else None
            )

            # Preserve existing on-device path or use native WMI Tag 38 local_path
            file_path = existing_media_paths.get(msg.get("key_id", "")) or (
                media.get("file_path") if media else None
            )
            if not file_path and media["media_type"] == "document" and media.get("media_name"):
                file_path = f"Media/WhatsApp Documents/{media['media_name']}"

            media_row_map = {
                "message_row_id": msg_row_id,
                "chat_row_id": chat_row_id,
                "file_path": file_path,
                "file_size": media["file_size"],
                "media_name": media["media_name"],
                "message_url": media["message_url"],
                "mime_type": media["mime_type"],
                "file_hash": file_hash_str,
                "transferred": 1,
            }
            cur.execute(sql_insert_media, [media_row_map[c] for c in target_media_cols])

        # Insert vcard record if present
        if msg["vcard"] and has_vcard_table:
            cur.execute(
                "INSERT INTO message_vcard (message_row_id, vcard) VALUES (?, ?)",
                (msg_row_id, msg["vcard"]),
            )

        # Insert view-once record if present
        if msg.get("is_view_once") and has_view_once_table and msg_row_id:
            try:
                cur.execute(
                    "INSERT OR REPLACE INTO message_view_once_media (message_row_id, state) VALUES (?, ?)",
                    (msg_row_id, 2 if msg.get("from_me") else 1),
                )
            except sqlite3.OperationalError:
                pass

        # Insert link preview records if present
        if msg.get("link_preview_info") and msg_row_id:
            lp = msg["link_preview_info"]
            if has_message_text_table:
                try:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO message_text
                            (message_row_id, description, page_title, url, preview_type)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            msg_row_id,
                            lp.get("description"),
                            lp.get("title"),
                            lp.get("url"),
                            lp.get("preview_type", 0),
                        ),
                    )
                except sqlite3.OperationalError:
                    pass
            if has_message_thumbnail_table and lp.get("thumbnail"):
                try:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO message_thumbnail
                            (message_row_id, thumbnail)
                        VALUES (?, ?)
                        """,
                        (msg_row_id, lp["thumbnail"]),
                    )
                except sqlite3.OperationalError:
                    pass

        # Apply location metadata if present
        if msg.get("location_info") and msg_row_id:
            apply_location_record(conn, msg_row_id, msg["location_info"], schema_cache, logger)

        # Apply poll creation metadata and embedded votes if present
        if msg.get("poll_info") and msg_row_id:
            apply_poll_creation(
                conn,
                msg_row_id,
                msg["poll_info"],
                schema_cache,
                logger,
                chat_row_id,
                is_group,
                jid_cache,
            )
        elif msg.get("embedded_poll_votes") and msg.get("key_id"):
            parent_id = key_cache.get(msg["key_id"]) or find_message_row_id(
                conn, msg["key_id"], key_cache
            )
            if parent_id:
                apply_embedded_poll_votes(
                    conn,
                    parent_id,
                    msg["embedded_poll_votes"],
                    schema_cache,
                    logger,
                    chat_row_id,
                    is_group,
                    jid_cache,
                )

        # Insert message_quoted record if present
        if msg.get("quoted_info") and msg_row_id:
            quoted_cols = get_table_columns(conn, "message_quoted", schema_cache)
            if quoted_cols:
                qi = msg["quoted_info"]
                q_part = qi.get("participant", "")
                if is_group and q_part and qi.get("from_me") != 1:
                    try:
                        q_sender_id = resolve_or_create_jid(conn, q_part, logger, jid_cache)
                    except (ValueError, Exception):
                        q_sender_id = 0
                else:
                    q_sender_id = 0
                q_stanza_id = qi.get("stanza_id") or msg.get("key_id", "") or ""
                quoted_ts = key_ts_cache.get(q_stanza_id, 0)
                q_from_me = qi.get("from_me", 0)
                if q_stanza_id:
                    cur.execute(
                        "SELECT timestamp, from_me, sender_jid_row_id FROM message WHERE key_id = ?",
                        (q_stanza_id,),
                    )
                    t_row = cur.fetchone()
                    if t_row and t_row[0]:
                        quoted_ts = int(t_row[0])
                        key_ts_cache[q_stanza_id] = quoted_ts
                        if t_row[1] is not None:
                            q_from_me = int(t_row[1])
                        if q_from_me == 1:
                            q_sender_id = 0
                        elif t_row[2] is not None:
                            q_sender_id = int(t_row[2])
                    else:
                        pending_quotes.append((msg_row_id, q_stanza_id))
                q_row_map = {
                    "message_row_id": msg_row_id,
                    "chat_row_id": chat_row_id,
                    "parent_message_chat_row_id": chat_row_id,
                    "text_data": qi.get("quoted_text", ""),
                    "from_me": q_from_me,
                    "sender_jid_row_id": q_sender_id,
                    "key_id": q_stanza_id,
                    "timestamp": quoted_ts,
                    "message_type": qi.get("message_type", 0),
                }
                valid_q_cols = [c for c in q_row_map if c in quoted_cols]
                cur.execute(
                    f"INSERT INTO message_quoted ({', '.join(valid_q_cols)}) VALUES ({', '.join('?' for _ in valid_q_cols)})",
                    [q_row_map[c] for c in valid_q_cols],
                )
                if qi.get("thumbnail"):
                    mqm_cols = get_table_columns(conn, "message_quoted_media", schema_cache)
                    if mqm_cols:
                        mqm_map = {
                            "message_row_id": msg_row_id,
                            "thumbnail": qi["thumbnail"],
                        }
                        valid_mqm = [c for c in mqm_map if c in mqm_cols]
                        cur.execute(
                            f"INSERT OR REPLACE INTO message_quoted_media ({', '.join(valid_mqm)}) VALUES ({', '.join('?' for _ in valid_mqm)})",
                            [mqm_map[c] for c in valid_mqm],
                        )

        # Insert user receipts if present
        if msg.get("user_receipts") and msg_row_id:
            ru_cols = get_table_columns(conn, "receipt_user", schema_cache)
            if ru_cols:
                for ur in msg["user_receipts"]:
                    ur_jid = ur.get("user_jid", "")
                    if ur_jid and not is_valid_jid_string(ur_jid):
                        continue
                    try:
                        ur_jid_id = (
                            resolve_or_create_jid(conn, ur_jid, logger, jid_cache)
                            if ur_jid
                            else chat_jid_row_id
                        )
                    except ValueError:
                        continue
                    cur.execute(
                        "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, receipt_timestamp, read_timestamp, played_timestamp) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            msg_row_id,
                            ur_jid_id,
                            ur.get("receipt_timestamp"),
                            ur.get("read_timestamp"),
                            ur.get("played_timestamp"),
                        ),
                    )

        # Apply embedded reactions from WMI Tag 41 if present
        if msg.get("embedded_reactions") and msg_row_id:
            apply_embedded_reactions(
                conn,
                msg_row_id,
                chat_row_id,
                is_group,
                msg["embedded_reactions"],
                schema_cache,
                logger,
                jid_cache,
            )
        elif msg.get("embedded_reactions") and msg.get("key_id"):
            parent_id = key_cache.get(msg["key_id"]) or find_message_row_id(
                conn, msg["key_id"], key_cache
            )
            if parent_id:
                apply_embedded_reactions(
                    conn,
                    parent_id,
                    chat_row_id,
                    is_group,
                    msg["embedded_reactions"],
                    schema_cache,
                    logger,
                    jid_cache,
                )
            else:
                pending_add_ons.append(
                    PendingAddOn(
                        add_on_type="embedded_reactions",
                        target_key_id=msg["key_id"],
                        msg=msg,
                        sender_jid_row_id=None,
                        from_me=msg["from_me"],
                        timestamp=msg["timestamp"],
                        key_id=msg.get("key_id", ""),
                        chat_row_id=chat_row_id,
                    )
                )

    if pending_add_ons:
        # Pass 2: Deferred resolution of unlinked add-ons now that all base messages are indexed
        pending_add_ons.sort(key=lambda p: p.timestamp)
        pass2_recovered = 0
        pass2_orphans = 0
        for pending in pending_add_ons:
            applied = False
            if pending.add_on_type == "reaction":
                applied = apply_reaction_record(
                    conn,
                    pending.msg["reaction_info"],
                    pending.sender_jid_row_id,
                    pending.from_me,
                    pending.timestamp,
                    key_cache,
                    schema_cache,
                    logger,
                    pending.key_id,
                    pending.chat_row_id,
                )
            elif pending.add_on_type == "embedded_reactions":
                p_id = key_cache.get(pending.target_key_id) or find_message_row_id(
                    conn, pending.target_key_id, key_cache
                )
                if p_id:
                    p_remote_jid = pending.msg.get("remote_jid", "")
                    p_is_group = bool(
                        p_remote_jid.endswith("@g.us")
                        or p_remote_jid.endswith("@temp")
                        or ("@" in p_remote_jid and p_remote_jid.split("@", 1)[1] == "g.us")
                    )
                    applied = bool(
                        apply_embedded_reactions(
                            conn,
                            p_id,
                            pending.chat_row_id,
                            p_is_group,
                            pending.msg.get("embedded_reactions", []),
                            schema_cache,
                            logger,
                            jid_cache,
                        )
                    )
            elif pending.add_on_type == "pin":
                applied = apply_pin_in_chat(
                    conn,
                    pending.msg["pin_info"],
                    pending.sender_jid_row_id,
                    pending.from_me,
                    pending.timestamp,
                    key_cache,
                    schema_cache,
                    logger,
                    pending.key_id,
                    pending.chat_row_id,
                )
            elif pending.add_on_type == "poll_vote":
                applied = apply_poll_vote(
                    conn,
                    pending.msg["poll_vote_info"],
                    pending.sender_jid_row_id,
                    pending.from_me,
                    pending.timestamp,
                    key_cache,
                    schema_cache,
                    logger,
                    pending.key_id,
                    pending.chat_row_id,
                )
            elif pending.add_on_type in ("edit", "revoke"):
                applied = apply_protocol_record(
                    conn,
                    pending.msg,
                    pending.sender_jid_row_id,
                    pending.from_me,
                    pending.timestamp,
                    key_cache,
                    schema_cache,
                    logger,
                    pending.chat_row_id,
                )

            if applied:
                pass2_recovered += 1
            else:
                pass2_orphans += 1
                logger.debug(
                    f"Target message '{pending.target_key_id}' for {pending.add_on_type} "
                    f"not found in database; orphan add-on discarded."
                )

        if pass2_recovered > 0:
            logger.info(
                f"Two-pass add-on staging resolved {pass2_recovered} out-of-order add-on(s) "
                f"({pass2_orphans} unresolvable orphan(s))."
            )

    if pending_quotes:
        recovered_quotes = 0
        for q_msg_id, q_stanza_id in pending_quotes:
            q_ts = key_ts_cache.get(q_stanza_id, 0)
            q_fm = 0
            q_s_id = 0
            cur.execute(
                "SELECT timestamp, from_me, sender_jid_row_id FROM message WHERE key_id = ?",
                (q_stanza_id,),
            )
            t_row = cur.fetchone()
            if t_row and t_row[0]:
                q_ts = int(t_row[0])
                key_ts_cache[q_stanza_id] = q_ts
                q_fm = int(t_row[1] or 0)
                q_s_id = 0 if q_fm == 1 else (int(t_row[2]) if t_row[2] is not None else 0)
            if q_ts:
                cur.execute(
                    "UPDATE message_quoted SET timestamp = ?, from_me = ?, sender_jid_row_id = ? WHERE message_row_id = ?",
                    (q_ts, q_fm, q_s_id, q_msg_id),
                )
                recovered_quotes += 1
        if recovered_quotes > 0:
            logger.info(
                f"Two-pass quote reconciliation resolved {recovered_quotes} out-of-order quoted timestamp(s)."
            )

    if chats_to_unhide:
        chat_cols = schema_cache.get("chat") if schema_cache is not None else None
        if chat_cols is None:
            chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
            if schema_cache is not None:
                schema_cache["chat"] = chat_cols
        if "hidden" in chat_cols:
            cur.executemany(
                "UPDATE chat SET hidden = 0 WHERE _id = ? AND (hidden IS NULL OR hidden = 1)",
                [(cid,) for cid in chats_to_unhide],
            )

    if max_ts_per_chat:
        chat_cols = schema_cache.get("chat") if schema_cache is not None else None
        if chat_cols is None:
            chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
            if schema_cache is not None:
                schema_cache["chat"] = chat_cols
        if "sort_timestamp" in chat_cols:
            cur.executemany(
                "UPDATE chat SET sort_timestamp = MAX(COALESCE(sort_timestamp, 0), ?) WHERE _id = ?",
                [(ts, cid) for cid, ts in max_ts_per_chat.items()],
            )

    return inserted


def apply_sequences(
    conn: sqlite3.Connection, sequences_json_path: str, logger: logging.Logger
) -> int:
    """Update SQLITE_SEQUENCE table with sequence snapshots from an increment."""
    with open(sequences_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        items = data.get("sequences", [])
    elif isinstance(data, list):
        items = data
    else:
        return 0

    cur = conn.cursor()
    updated = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        val = item.get("value", item.get("seq"))
        if not name or val is None:
            continue
        cur.execute("UPDATE SQLITE_SEQUENCE SET seq = MAX(seq, ?) WHERE name = ?", (val, name))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO SQLITE_SEQUENCE (name, seq) VALUES (?, ?)", (name, val))
        updated += 1
    return updated


def apply_increment_folder(
    conn: sqlite3.Connection,
    inc_dir: str,
    logger: logging.Logger,
    jid_cache: dict[str, int] | None,
    chat_cache: dict[int, int] | None,
    schema_cache: dict[str, set[str]] | None,
    jid_id_map: dict[int, int],
) -> dict:
    """Apply an unpacked increment directory onto the active SQLite database.

    Enforces the strict 6-step DAG execution pipeline:
    - Step 1: Auxiliary Entity Updates (topological order) with foreign pointer quarantine
    - Step 2: Auxiliary Entity Deletions with mapped JID translation for chats
    - Step 3: Autoincrement Sequence Roll-Forward (before deletions and message insertion)
    - Step 4: Defensive Message Deletions (4-gate verification)
    - Step 5: Protobuf Message Delta Ingestion (messages.bin with Two-Pass Add-On Staging)
    - Step 6: Chat Message Pointer Sanitization & Step 6b Correlated Phantom Pruning
    """
    header_path = os.path.join(inc_dir, "header.json")
    header = {}
    if os.path.isfile(header_path):
        with open(header_path, encoding="utf-8") as f:
            header = json.load(f)

    # Extract backup creation timestamp defensively from header.json (flat or nested)
    header_obj = header.get("header") if isinstance(header.get("header"), dict) else {}
    backup_timestamp = (
        header.get("creation_date")
        if header.get("creation_date") is not None
        else header_obj.get("creation_date")
    )
    if isinstance(backup_timestamp, (int, float)):
        backup_timestamp = int(backup_timestamp)
    else:
        backup_timestamp = None

    # Inspect sequence ceiling for message table from sequences.json before deletions
    seq_name = (
        header.get("sequences") if isinstance(header.get("sequences"), str) else "sequences.json"
    )
    seq_path = os.path.join(inc_dir, seq_name)
    max_msg_seq = None
    if os.path.isfile(seq_path):
        try:
            with open(seq_path, encoding="utf-8") as sf:
                seq_data = json.load(sf)
            seq_items = (
                seq_data.get("sequences", [])
                if isinstance(seq_data, dict)
                else (seq_data if isinstance(seq_data, list) else [])
            )
            for item in seq_items:
                if isinstance(item, dict) and item.get("name") == "message":
                    val = item.get("value", item.get("seq"))
                    if val is not None:
                        max_msg_seq = int(val)
                        break
        except Exception as ex:
            logger.warning("Could not read message sequence ceiling from %s: %s", seq_path, ex)

    stats = {
        "entity_modified": 0,
        "entity_deleted": 0,
        "messages_deleted": 0,
        "messages_inserted": 0,
        "sequences_updated": 0,
    }

    with conn:
        # Step 1: Auxiliary entity modifications sorted by foreign key dependency (DAG)
        mod_files = [
            fn for fn in os.listdir(inc_dir) if "_modified_" in fn and fn.endswith(".json")
        ]
        for mf in sorted(mod_files, key=_entity_file_sort_key):
            table_name = mf.split("_modified_")[0]
            stats["entity_modified"] += apply_entity_modifications(
                conn, table_name, os.path.join(inc_dir, mf), logger, jid_id_map
            )

        # Step 2: Auxiliary entity deletions sorted by foreign key dependency
        del_files = [fn for fn in os.listdir(inc_dir) if "_deleted_" in fn and fn.endswith(".json")]
        for df in sorted(del_files, key=_entity_file_sort_key):
            table_name = df.split("_deleted_")[0]
            stats["entity_deleted"] += apply_entity_deletions(
                conn, table_name, os.path.join(inc_dir, df), logger, jid_id_map
            )

        # Step 3: Sequence roll-forward (Must run before message replay to prevent surrogate key drift)
        seq_name = (
            header.get("sequences")
            if isinstance(header.get("sequences"), str)
            else "sequences.json"
        )
        seq_path = os.path.join(inc_dir, seq_name)
        if os.path.isfile(seq_path):
            stats["sequences_updated"] += apply_sequences(conn, seq_path, logger)

        # Step 4: Message deletions
        del_msg_files = [
            fn
            for fn in os.listdir(inc_dir)
            if fn.startswith("deletedMessages_") and fn.endswith(".json")
        ]
        raw_dmf = header.get("deleted_message_ids_files")
        if not raw_dmf and isinstance(header.get("deleted_messages"), dict):
            raw_dmf = header["deleted_messages"].get("deleted_message_ids_files")
        if isinstance(raw_dmf, list):
            for dmf in raw_dmf:
                if dmf not in del_msg_files:
                    del_msg_files.append(dmf)
        for df in sorted(del_msg_files):
            df_path = os.path.join(inc_dir, df)
            if os.path.isfile(df_path):
                stats["messages_deleted"] += apply_message_deletions(
                    conn, df_path, logger, schema_cache, backup_timestamp, max_msg_seq
                )

        # Step 5: Protobuf message replay
        messages_bin_path = os.path.join(inc_dir, "messages.bin")
        if os.path.isfile(messages_bin_path):
            stats["messages_inserted"] += apply_messages_bin(
                conn, messages_bin_path, logger, jid_cache, chat_cache, schema_cache
            )

        # Step 6: Sanitize chat message pointers to prevent referencing foreign or invalid messages
        chat_cols = get_table_columns(conn, "chat", schema_cache)
        if "display_message_row_id" in chat_cols:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE chat
                SET display_message_row_id = (
                    SELECT _id FROM message
                    WHERE chat_row_id = chat._id
                      AND message_type NOT IN (25, 67, 74, 79)
                    ORDER BY timestamp DESC, _id DESC LIMIT 1
                )
                WHERE display_message_row_id IS NULL
                   OR NOT EXISTS (
                      SELECT 1 FROM message m
                      WHERE m._id = chat.display_message_row_id
                        AND m.chat_row_id = chat._id
                  )
                """
            )
        if "last_message_row_id" in chat_cols:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE chat
                SET last_message_row_id = (
                    SELECT _id FROM message
                    WHERE chat_row_id = chat._id
                    ORDER BY timestamp DESC, _id DESC LIMIT 1
                )
                WHERE last_message_row_id IS NULL
                   OR NOT EXISTS (
                      SELECT 1 FROM message m
                      WHERE m._id = chat.last_message_row_id
                        AND m.chat_row_id = chat._id
                  )
                """
            )

    return stats


def migrate_restored_backup_to_lid(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    """
    Vendor-aligned post-restore LID migration (RestoredBackupLidMigrationHelper).
    Links phone chats with account_jid_row_id from jid_map and consolidates
    any duplicate split shadow LID chats back into primary phone chats.
    """
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid_map'")
    if not cur.fetchone():
        return
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'")
    if not cur.fetchone():
        return

    chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    has_account_jid = "account_jid_row_id" in chat_cols
    has_hidden = "hidden" in chat_cols

    # Step A: Link phone chats to their LID in account_jid_row_id
    if has_account_jid:
        cur.execute(
            """
            UPDATE chat
            SET account_jid_row_id = (
                SELECT jm.lid_row_id
                FROM jid_map jm
                WHERE jm.jid_row_id = chat.jid_row_id
            )
            WHERE (account_jid_row_id IS NULL OR account_jid_row_id = 0)
              AND EXISTS (
                  SELECT 1 FROM jid_map jm WHERE jm.jid_row_id = chat.jid_row_id
              )
            """
        )
        if cur.rowcount > 0:
            logger.debug(f"Updated account_jid_row_id for {cur.rowcount} chat(s) from jid_map")

    # Step B: Identify and consolidate duplicate split chats (phone chat + LID chat)
    query = """
        SELECT c_phone._id, c_lid._id, jm.lid_row_id
        FROM chat c_phone
        JOIN jid_map jm ON c_phone.jid_row_id = jm.jid_row_id
        JOIN chat c_lid ON c_lid.jid_row_id = jm.lid_row_id
        WHERE c_phone._id != c_lid._id
    """
    dup_pairs = cur.execute(query).fetchall()
    if dup_pairs:
        logger.debug(f"Consolidating {len(dup_pairs)} split phone/LID chat pairs...")
        for phone_chat_id, lid_chat_id, lid_row_id in dup_pairs:
            cur.execute(
                "UPDATE message SET chat_row_id = ? WHERE chat_row_id = ?",
                (phone_chat_id, lid_chat_id),
            )
            msgs_moved = cur.rowcount

            if msgs_moved > 0:
                try:
                    cur.execute(
                        "UPDATE message_media SET chat_row_id = ? WHERE chat_row_id = ?",
                        (phone_chat_id, lid_chat_id),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        "UPDATE message_quoted SET chat_row_id = ?, parent_message_chat_row_id = ? WHERE chat_row_id = ?",
                        (phone_chat_id, phone_chat_id, lid_chat_id),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        "UPDATE message_add_on SET chat_row_id = ? WHERE chat_row_id = ?",
                        (phone_chat_id, lid_chat_id),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        "UPDATE message_location SET chat_row_id = ? WHERE chat_row_id = ?",
                        (phone_chat_id, lid_chat_id),
                    )
                except sqlite3.OperationalError:
                    pass

            if has_hidden:
                cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (phone_chat_id,))
                cur.execute("UPDATE chat SET hidden = 1 WHERE _id = ?", (lid_chat_id,))
            if has_account_jid:
                cur.execute(
                    "UPDATE chat SET account_jid_row_id = ? WHERE _id = ?",
                    (lid_row_id, phone_chat_id),
                )

            if "sort_timestamp" in chat_cols:
                cur.execute(
                    """
                    UPDATE chat
                    SET sort_timestamp = MAX(
                        COALESCE(sort_timestamp, 0),
                        COALESCE((SELECT sort_timestamp FROM chat WHERE _id = ?), 0),
                        COALESCE((SELECT MAX(timestamp) FROM message WHERE chat_row_id = ?), 0)
                    )
                    WHERE _id = ?
                    """,
                    (lid_chat_id, phone_chat_id, phone_chat_id),
                )

            if "display_message_row_id" in chat_cols:
                cur.execute(
                    """
                    UPDATE chat
                    SET display_message_row_id = (
                        SELECT _id FROM message
                        WHERE chat_row_id = ? AND message_type NOT IN (25, 67, 74, 79)
                        ORDER BY timestamp DESC, _id DESC LIMIT 1
                    )
                    WHERE _id = ?
                    """,
                    (phone_chat_id, phone_chat_id),
                )
            if "last_message_row_id" in chat_cols:
                cur.execute(
                    """
                    UPDATE chat
                    SET last_message_row_id = (
                        SELECT _id FROM message
                        WHERE chat_row_id = ?
                        ORDER BY timestamp DESC, _id DESC LIMIT 1
                    )
                    WHERE _id = ?
                    """,
                    (phone_chat_id, phone_chat_id),
                )

            if msgs_moved > 0:
                logger.debug(
                    f"Consolidated chat pair (phone_chat={phone_chat_id}, lid_chat={lid_chat_id}): moved {msgs_moved} messages."
                )

    # Step C: Consolidate any duplicate chats sharing the same natural user and server
    dup_query = """
        SELECT c1._id, c2._id, c1.jid_row_id, c2.jid_row_id
        FROM chat c1
        JOIN jid j1 ON c1.jid_row_id = j1._id
        JOIN chat c2 ON c1._id < c2._id
        JOIN jid j2 ON c2.jid_row_id = j2._id
        WHERE j1.user = j2.user AND j1.server = j2.server
        ORDER BY c1._id ASC, c2._id ASC
    """
    dup_chats = cur.execute(dup_query).fetchall()
    if dup_chats:
        logger.debug(f"Consolidating {len(dup_chats)} duplicate natural chat pairs...")
        for c1_id, c2_id, _j1, _j2 in dup_chats:
            cur.execute(
                "UPDATE message SET chat_row_id = ? WHERE chat_row_id = ?",
                (c1_id, c2_id),
            )
            msgs_moved = cur.rowcount

            if msgs_moved > 0:
                try:
                    cur.execute(
                        "UPDATE message_media SET chat_row_id = ? WHERE chat_row_id = ?",
                        (c1_id, c2_id),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        "UPDATE message_quoted SET chat_row_id = ?, parent_message_chat_row_id = ? WHERE chat_row_id = ?",
                        (c1_id, c1_id, c2_id),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        "UPDATE message_add_on SET chat_row_id = ? WHERE chat_row_id = ?",
                        (c1_id, c2_id),
                    )
                except sqlite3.OperationalError:
                    pass

                try:
                    cur.execute(
                        "UPDATE message_location SET chat_row_id = ? WHERE chat_row_id = ?",
                        (c1_id, c2_id),
                    )
                except sqlite3.OperationalError:
                    pass

            if has_hidden:
                cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (c1_id,))
                cur.execute("UPDATE chat SET hidden = 1 WHERE _id = ?", (c2_id,))

            if "sort_timestamp" in chat_cols:
                cur.execute(
                    """
                    UPDATE chat
                    SET sort_timestamp = MAX(
                        COALESCE(sort_timestamp, 0),
                        COALESCE((SELECT sort_timestamp FROM chat WHERE _id = ?), 0),
                        COALESCE((SELECT MAX(timestamp) FROM message WHERE chat_row_id = ?), 0)
                    )
                    WHERE _id = ?
                    """,
                    (c2_id, c1_id, c1_id),
                )

            if "display_message_row_id" in chat_cols:
                cur.execute(
                    """
                    UPDATE chat
                    SET display_message_row_id = (
                        SELECT _id FROM message
                        WHERE chat_row_id = ? AND message_type NOT IN (25, 67, 74, 79)
                        ORDER BY timestamp DESC, _id DESC LIMIT 1
                    )
                    WHERE _id = ?
                    """,
                    (c1_id, c1_id),
                )
            if "last_message_row_id" in chat_cols:
                cur.execute(
                    """
                    UPDATE chat
                    SET last_message_row_id = (
                        SELECT _id FROM message
                        WHERE chat_row_id = ?
                        ORDER BY timestamp DESC, _id DESC LIMIT 1
                    )
                    WHERE _id = ?
                    """,
                    (c1_id, c1_id),
                )

            if msgs_moved > 0:
                logger.debug(
                    f"Consolidated natural duplicate chat pair (base_chat={c1_id}, dup_chat={c2_id}): moved {msgs_moved} messages."
                )

    # Step D: Re-point any companion sender JIDs in message to canonical primary JID
    try:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'")
        if cur.fetchone():
            msg_cols = {r[1] for r in cur.execute("PRAGMA table_info(message)").fetchall()}
            jid_cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
            if "sender_jid_row_id" in msg_cols and "user" in jid_cols and "server" in jid_cols:
                has_dev = "device" in jid_cols
                has_raw = "raw_string" in jid_cols

                dev_cond = "j_comp.device > 0" if has_dev else "0"
                raw_cond = "j_comp.raw_string LIKE '%.%:%@%'" if has_raw else "0"
                comp_filter = f"({dev_cond} OR {raw_cond})"

                pri_dev_cond = "(j_pri.device IS NULL OR j_pri.device = 0)" if has_dev else "1"
                pri_raw_cond = (
                    "(j_pri.raw_string = j_pri.user || '@' || j_pri.server OR j_pri.raw_string IS NULL OR j_pri.raw_string = '')"
                    if has_raw
                    else "1"
                )

                cur.execute("DROP TABLE IF EXISTS _comp_to_pri")
                cur.execute(f"""
                    CREATE TEMP TABLE _comp_to_pri AS
                    SELECT j_comp._id AS comp_id, MIN(j_pri._id) AS pri_id
                    FROM jid j_comp
                    JOIN jid j_pri ON j_pri.user = j_comp.user AND j_pri.server = j_comp.server
                    WHERE {comp_filter}
                      AND {pri_dev_cond}
                      AND {pri_raw_cond}
                      AND j_comp._id != j_pri._id
                    GROUP BY j_comp._id
                """)
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS _idx_comp_to_pri ON _comp_to_pri(comp_id)"
                )

                cur.execute("SELECT 1 FROM _comp_to_pri LIMIT 1")
                if cur.fetchone():
                    cur.execute("""
                        UPDATE message
                        SET sender_jid_row_id = (
                            SELECT pri_id FROM _comp_to_pri WHERE comp_id = message.sender_jid_row_id
                        )
                        WHERE sender_jid_row_id IN (SELECT comp_id FROM _comp_to_pri)
                    """)
                    try:
                        cur.execute("""
                            UPDATE message_add_on
                            SET sender_jid_row_id = (
                                SELECT pri_id FROM _comp_to_pri WHERE comp_id = message_add_on.sender_jid_row_id
                            )
                            WHERE sender_jid_row_id IN (SELECT comp_id FROM _comp_to_pri)
                        """)
                    except sqlite3.OperationalError:
                        pass
                    try:
                        cur.execute("""
                            UPDATE receipt_user
                            SET receipt_user_jid_row_id = (
                                SELECT pri_id FROM _comp_to_pri WHERE comp_id = receipt_user.receipt_user_jid_row_id
                            )
                            WHERE receipt_user_jid_row_id IN (SELECT comp_id FROM _comp_to_pri)
                        """)
                    except sqlite3.OperationalError:
                        pass
                    if cur.rowcount > 0:
                        logger.debug(
                            "Re-pointed message, add-on, and receipt sender JIDs from companion devices to primary canonical JIDs."
                        )
                cur.execute("DROP TABLE IF EXISTS _comp_to_pri")
    except sqlite3.OperationalError:
        pass

    conn.commit()


def merge_increments(
    base_db_path: str,
    increment_paths: list[str],
    e2e_key: object,
    output_db_path: str,
    staging_dir: str,
    logger: logging.Logger,
) -> int:
    """Merge a base decrypted database and a sequence of increment archives into output_db_path.

    Maintains a persistent jid_id_map across all increment folders in the merge session
    to prevent cross-increment surrogate ID divergence.
    Returns the total count of messages inserted across all increments.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_db_path)), exist_ok=True)
    os.makedirs(staging_dir, exist_ok=True)

    if os.path.abspath(base_db_path) != os.path.abspath(output_db_path):
        shutil.copy2(base_db_path, output_db_path)

    total_inserted = 0
    total_deleted = 0

    has_encrypted = any(not zipfile.is_zipfile(p) for p in increment_paths)
    key_obj = resolve_e2e_key(e2e_key, logger) if has_encrypted else e2e_key
    jid_cache: dict[str, int] = {}
    chat_cache: dict[int, int] = {}
    schema_cache: dict[str, set[str]] = {}

    with contextlib.closing(sqlite3.connect(output_db_path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -64000")
        conn.execute("PRAGMA mmap_size = 268435456")

        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_key_id ON message(key_id)")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_add_on_parent ON message_add_on(parent_message_row_id, message_add_on_type)"
            )
        except sqlite3.OperationalError:
            pass

        # Pre-warm JID and chat caches upfront to avoid N+1 queries during delta folding
        cur = conn.cursor()
        jid_cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
        has_raw_string = "raw_string" in jid_cols
        has_device = "device" in jid_cols
        jid_sql = (
            "SELECT user, server, _id, raw_string, device FROM jid "
            "ORDER BY "
            "CASE WHEN (raw_string = user || '@' || server OR raw_string IS NULL OR raw_string = '') AND (device IS NULL OR device = 0) THEN 0 "
            "     WHEN device IS NULL OR device = 0 THEN 1 ELSE 2 END, "
            "_id ASC"
            if (has_raw_string and has_device)
            else "SELECT user, server, _id, raw_string, 0 FROM jid ORDER BY _id ASC"
            if has_raw_string
            else "SELECT user, server, _id, '', 0 FROM jid ORDER BY _id ASC"
        )
        for r in cur.execute(jid_sql).fetchall():
            u, s, jid_id, raw_str, _dev = r[0], r[1], r[2], r[3], r[4]
            canonical_key = f"{u}@{s}"
            if canonical_key not in jid_cache:
                jid_cache[canonical_key] = jid_id
            if u not in jid_cache:
                jid_cache[u] = jid_id
            if raw_str and raw_str not in jid_cache:
                jid_cache[raw_str] = jid_id

        # Map companion device raw strings to canonical primary JID
        for raw_k in list(jid_cache.keys()):
            sanitized = sanitize_jid_string(raw_k)
            if sanitized in jid_cache:
                jid_cache[raw_k] = jid_cache[sanitized]

        chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
        has_hidden = "hidden" in chat_cols
        has_jid_map = False
        try:
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid_map'")
            has_jid_map = cur.fetchone() is not None
        except sqlite3.OperationalError:
            pass

        if "jid_row_id" in chat_cols:
            # First cache visible chats (hidden = 0 or NULL)
            c_sql = (
                "SELECT jid_row_id, _id FROM chat WHERE hidden = 0 OR hidden IS NULL"
                if has_hidden
                else "SELECT jid_row_id, _id FROM chat"
            )
            for r in cur.execute(c_sql).fetchall():
                chat_cache[r[0]] = r[1]

            # If jid_map is available, map lid_row_id directly to the visible phone chat
            if has_jid_map:
                jm_sql = (
                    """
                    SELECT jm.lid_row_id, c._id
                    FROM chat c
                    JOIN jid_map jm ON c.jid_row_id = jm.jid_row_id
                    WHERE c.hidden = 0 OR c.hidden IS NULL
                    """
                    if has_hidden
                    else """
                    SELECT jm.lid_row_id, c._id
                    FROM chat c
                    JOIN jid_map jm ON c.jid_row_id = jm.jid_row_id
                    """
                )
                for r in cur.execute(jm_sql).fetchall():
                    chat_cache[r[0]] = r[1]

        jid_id_map: dict[int, int] = {}
        for idx, inc_path in enumerate(increment_paths):
            logger.info(
                f"Merging incremental backup [{idx + 1}/{len(increment_paths)}]: {os.path.basename(inc_path)}"
            )
            work_dir = os.path.join(staging_dir, f"unpacked_inc_{idx}")
            unpack_increment_archive(inc_path, key_obj, work_dir, logger)

            stats = apply_increment_folder(
                conn, work_dir, logger, jid_cache, chat_cache, schema_cache, jid_id_map
            )
            total_inserted += stats["messages_inserted"]
            total_deleted += stats["messages_deleted"]
            logger.info(
                f"Increment {os.path.basename(inc_path)} applied: "
                f"+{stats['messages_inserted']} inserted, "
                f"-{stats['messages_deleted']} deleted, "
                f"{stats['entity_modified']} entities modified."
            )

        # Step 6: Post-restore LID migration, matching vendor RestoredBackupLidMigrationHelper
        migrate_restored_backup_to_lid(conn, logger)

        # Step 6b: Post-merge shadow chat and phantom message cleanup
        try:
            has_mm = bool(
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media'"
                ).fetchone()
            )
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
                    del_clauses.append(
                        f"NOT EXISTS (SELECT 1 FROM {tbl} t WHERE t.{col} = message._id)"
                    )
            cur.execute(f"DELETE FROM message WHERE {' AND '.join(del_clauses)}")
            deleted_phantoms = cur.rowcount
            if deleted_phantoms > 0:
                logger.info(
                    f"Purged {deleted_phantoms} phantom empty message(s) from message table."
                )

            chat_cols = {r[1] for r in cur.execute("PRAGMA table_info('chat')").fetchall()}
            if "hidden" in chat_cols:
                subj_clause = (
                    "AND (subject IS NULL OR subject = '')" if "subject" in chat_cols else ""
                )
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
        except sqlite3.OperationalError:
            pass

        # Step 7: Rebuild database indexes after all increments are applied, matching vendor MessageStoreBackupUtils/reindex
        logger.info("Rebuilding database indexes...")
        conn.execute("REINDEX")

        # Commit all pending transactions before changing PRAGMA synchronous
        conn.commit()

        # Restore synchronous mode and execute quick integrity check
        conn.execute("PRAGMA synchronous = NORMAL")
        integrity = conn.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(
                f"Database integrity check failed after merging increments: {integrity}"
            )

    # Invalidate viewer recent messages cache if archive database exists in output directory
    out_parent = os.path.dirname(os.path.abspath(output_db_path))
    for check_dir in (out_parent, os.path.dirname(out_parent)):
        for cand_name in ("archive.db", ".wa_media_archiver.db"):
            cand_path = os.path.join(check_dir, cand_name)
            if os.path.isfile(cand_path):
                try:
                    with sqlite3.connect(cand_path) as a_conn:
                        a_conn.execute("DELETE FROM recent_messages")
                        a_conn.execute("DELETE FROM reactions_cache")
                        a_conn.commit()
                    logger.debug(f"Invalidated viewer message cache in {cand_path}")
                except Exception:
                    pass

    logger.info(
        f"Incremental merging complete: {len(increment_paths)} increment(s) folded into {output_db_path} "
        f"(+{total_inserted} inserted, -{total_deleted} deleted)."
    )
    return total_inserted
