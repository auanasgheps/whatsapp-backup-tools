import base64
import contextlib
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

# ==============================================================================
# increment_merger.py — WhatsApp Android Incremental Backup Merger
#
# Implements pure-Python Protocol Buffers decoding for messages.bin, ZIP
# decompression / decryption of msgstore-increment packages, and sequential
# SQLite delta folding (entity mutations, deletions, messages, and sequences).
# ==============================================================================


# ---------------------------------------------------------------------------
# Pure-Python Protobuf Decoder
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
    if 3 in ctx_fields and isinstance(ctx_fields[3][0][1], bytes):
        raw_q_fields = parse_wire_fields(ctx_fields[3][0][1])
        q_fields, _ = unwrap_message_container(raw_q_fields, 4)
        if 1 in q_fields:
            quoted_text = q_fields[1][0][1].decode("utf-8", errors="replace")
        elif 6 in q_fields:
            sub = parse_wire_fields(q_fields[6][0][1])
            quoted_text = (
                sub.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 1 in sub else ""
            )
        elif 3 in q_fields:
            sub = parse_wire_fields(q_fields[3][0][1])
            quoted_text = (
                sub.get(3, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 3 in sub else ""
            )
        elif 7 in q_fields:
            sub = parse_wire_fields(q_fields[7][0][1])
            quoted_text = (
                (sub.get(3, [(2, b"")])[0][1] or sub.get(8, [(2, b"")])[0][1]).decode(
                    "utf-8", errors="replace"
                )
                if (3 in sub or 8 in sub)
                else ""
            )
        elif 9 in q_fields:
            sub = parse_wire_fields(q_fields[9][0][1])
            quoted_text = (
                sub.get(7, [(2, b"")])[0][1].decode("utf-8", errors="replace") if 7 in sub else ""
            )

    if not stanza_id and not quoted_text and not participant:
        return None

    return {
        "stanza_id": stanza_id,
        "participant": participant,
        "quoted_text": quoted_text or "",
        "from_me": q_from_me,
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

    # Field 39: userReceipt (repeated UserReceipt)
    user_receipts: list[dict] = []
    if 39 in fields:
        for tuple_item in fields[39]:
            ur_bytes = tuple_item[1]
            if not isinstance(ur_bytes, bytes):
                continue
            ur_fields = parse_wire_fields(ur_bytes)
            ujid = (
                ur_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ur_fields
                else ""
            )
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

    message_type = 0
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
    is_view_once = 0
    is_ephemeral = 0

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

        # 46 or 48: reactionMessage (Tag 46 official, 48 backwards-compat)
        elif 46 in msg_fields or 48 in msg_fields:
            message_type = 25
            react_payload = msg_fields[46][0][1] if 46 in msg_fields else msg_fields[48][0][1]
            react_fields = parse_wire_fields(react_payload)
            target_key = (
                parse_message_key(react_fields[1][0][1])
                if 1 in react_fields and isinstance(react_fields[1][0][1], bytes)
                else {}
            )
            text_data = (
                react_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in react_fields
                else ""
            )
            sender_ts = react_fields.get(4, [(0, 0)])[0][1] if 4 in react_fields else timestamp_ms
            reaction_info = {
                "target_key_id": target_key.get("key_id", ""),
                "target_remote_jid": target_key.get("remote_jid", ""),
                "target_from_me": target_key.get("from_me", 0),
                "emoji": text_data,
                "sender_ts": sender_ts,
            }

        # 49, 60, 64: pollCreationMessage (v1, v2, v3)
        elif 49 in msg_fields or 60 in msg_fields or 64 in msg_fields:
            message_type = 66
            poll_tag = 49 if 49 in msg_fields else (60 if 60 in msg_fields else 64)
            poll_fields = parse_wire_fields(msg_fields[poll_tag][0][1])
            name = (
                poll_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in poll_fields
                else ""
            )
            options = []
            for opt_tuple in poll_fields.get(3, []):
                if isinstance(opt_tuple[1], bytes):
                    opt_f = parse_wire_fields(opt_tuple[1])
                    opt_name = (
                        opt_f.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                        if 1 in opt_f
                        else ""
                    )
                    if opt_name:
                        options.append(opt_name)
            selectable_count = poll_fields.get(4, [(0, 0)])[0][1] if 4 in poll_fields else 0
            text_data = name
            poll_info = {
                "question": name,
                "options": options,
                "selectable_count": selectable_count,
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
            poll_vote_info = {
                "poll_key_id": poll_key.get("key_id", ""),
                "sender_ts": sender_ts,
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

        if quoted_info is None:
            for sub_tag in (6, 3, 7, 8, 9, 20, 4, 5, 18, 42, 45):
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
        "vcard": vcard,
        "reaction_info": reaction_info,
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
    }


def extract_messages_from_proto_frame(data: bytes) -> list[dict]:
    """
    Extract a list of normalized message dicts from a single protobuf frame.
    Supports both HistorySync payloads and direct WebMessageInfo records.
    """
    fields = parse_wire_fields(data)

    # HistorySync: field 2 contains repeated conversations
    if 2 in fields:
        results = []
        for conv_tuple in fields[2]:
            conv_bytes = conv_tuple[1]
            if not isinstance(conv_bytes, bytes):
                continue
            conv_fields = parse_wire_fields(conv_bytes)
            conv_id = ""
            if 1 in conv_fields:
                conv_id = conv_fields[1][0][1].decode("utf-8", errors="replace")

            # Repeated HistorySyncMsg records (field 2 of Conversation)
            for hsm_tuple in conv_fields.get(2, []):
                hsm_bytes = hsm_tuple[1]
                if not isinstance(hsm_bytes, bytes):
                    continue
                hsm_fields = parse_wire_fields(hsm_bytes)
                # WebMessageInfo is field 1 of HistorySyncMsg
                for wmi_tuple in hsm_fields.get(1, []):
                    wmi_bytes = wmi_tuple[1]
                    if isinstance(wmi_bytes, bytes):
                        msg = decode_wmi(wmi_bytes)
                        if not msg["remote_jid"] and conv_id:
                            msg["remote_jid"] = conv_id
                        results.append(msg)
        if results:
            return results

    # Direct WebMessageInfo: field 1 contains MessageKey
    if 1 in fields:
        return [decode_wmi(data)]

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

    # Check for duplicate IDs across candidates
    seen_ids: set[int] = set()
    has_duplicate_ids = False
    for inc_id, _, _ in candidates:
        if inc_id in seen_ids:
            has_duplicate_ids = True
            break
        seen_ids.add(inc_id)

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
    elif has_duplicate_ids:
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


def apply_entity_modifications(
    conn: sqlite3.Connection, table_name: str, modified_json_path: str, logger: logging.Logger
) -> int:
    """Upsert modified rows from an entity JSON file into SQLite table."""
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
            count += 1
            continue

        if table_name == "chat" and "jid_row_id" in filtered:
            # Chat is uniquely 1:1 with jid_row_id.
            # Match by 'jid_row_id' or 'account_jid_row_id' to prevent foreign auto-increment _id collision.
            existing_chat = cur.execute(
                "SELECT _id FROM chat WHERE jid_row_id = ?", (filtered["jid_row_id"],)
            ).fetchone()
            if existing_chat is None and "account_jid_row_id" in table_cols:
                existing_chat = cur.execute(
                    "SELECT _id FROM chat WHERE account_jid_row_id = ?",
                    (filtered["jid_row_id"],),
                ).fetchone()
            if existing_chat is not None:
                chat_id = existing_chat[0]
                update_cols = [c for c in filtered if c not in ("_id", "jid_row_id")]
                if update_cols:
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    params = [filtered[c] for c in update_cols] + [chat_id]
                    cur.execute(f"UPDATE chat SET {set_clause} WHERE _id = ?", params)
            else:
                id_used = (
                    cur.execute(
                        "SELECT 1 FROM chat WHERE _id = ?", (filtered.get("_id"),)
                    ).fetchone()
                    is not None
                    if "_id" in filtered
                    else False
                )
                insert_cols = (
                    [c for c in filtered if c != "_id"] if id_used else sorted(filtered.keys())
                )
                sql = f"INSERT INTO chat ({', '.join(insert_cols)}) VALUES ({', '.join('?' for _ in insert_cols)})"
                cur.execute(sql, [filtered[c] for c in insert_cols])
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
    conn: sqlite3.Connection, table_name: str, deleted_json_path: str, logger: logging.Logger
) -> int:
    """Delete entity records listed in an entity deleted JSON file."""
    with open(deleted_json_path, encoding="utf-8") as f:
        data = json.load(f)

    ids = data.get("deleted_ids", data) if isinstance(data, dict) else data
    if not isinstance(ids, list) or not ids:
        return 0

    cur = conn.cursor()
    col_info = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not col_info:
        logger.warning(
            f"Table '{table_name}' does not exist in target database; skipping deletions."
        )
        return 0

    pk_col = next((r[1] for r in col_info if r[5] > 0), "_id")
    if table_name == "props" and any(isinstance(x, str) for x in ids):
        pk_col = "key"

    deleted_count = 0
    chunk_size = 500
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM {table_name} WHERE {pk_col} IN ({placeholders})", chunk)
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
) -> None:
    """Apply a reaction message add-on to the database, ensuring key_id satisfies NOT NULL constraints."""
    target_key_id = reaction_info.get("target_key_id", "")
    if not target_key_id:
        return
    parent_row_id = find_message_row_id(conn, target_key_id, key_cache)
    if parent_row_id is None:
        logger.debug(f"Target message '{target_key_id}' for reaction not found; skipping add-on.")
        return

    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    if not add_on_cols:
        return

    cur = conn.cursor()
    emoji = reaction_info.get("emoji", "")
    sender_ts = reaction_info.get("sender_ts", timestamp)

    # If reaction is empty string, reaction was revoked/cleared
    if not emoji:
        cur.execute(
            "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND sender_jid_row_id IS ? AND message_add_on_type = 1",
            (parent_row_id, sender_jid_row_id),
        )
        return

    # Delete any prior reaction by this sender on this message
    reaction_cols = get_table_columns(conn, "message_add_on_reaction", schema_cache)
    if reaction_cols:
        cur.execute(
            "DELETE FROM message_add_on_reaction WHERE message_add_on_row_id IN ("
            "SELECT _id FROM message_add_on WHERE parent_message_row_id = ? AND sender_jid_row_id IS ? AND message_add_on_type = 1)",
            (parent_row_id, sender_jid_row_id),
        )
    cur.execute(
        "DELETE FROM message_add_on WHERE parent_message_row_id = ? AND sender_jid_row_id IS ? AND message_add_on_type = 1",
        (parent_row_id, sender_jid_row_id),
    )

    row_map = {
        "parent_message_row_id": parent_row_id,
        "from_me": from_me,
        "sender_jid_row_id": sender_jid_row_id,
        "status": 0,
        "message_add_on_type": 1,
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


def apply_poll_creation(
    conn: sqlite3.Connection,
    msg_row_id: int,
    poll_info: dict,
    schema_cache: dict[str, set[str]] | None,
    logger: logging.Logger,
) -> None:
    """Insert message_poll and message_poll_option records for a poll message."""
    poll_cols = get_table_columns(conn, "message_poll", schema_cache)
    poll_opt_cols = get_table_columns(conn, "message_poll_option", schema_cache)
    cur = conn.cursor()

    if poll_cols:
        selectable = poll_info.get("selectable_count", 0)
        cur.execute(
            "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (?, ?)",
            (msg_row_id, selectable),
        )

    if poll_opt_cols:
        for opt_name in poll_info.get("options", []):
            cur.execute(
                "INSERT INTO message_poll_option (message_row_id, option_name, vote_total) VALUES (?, ?, 0)",
                (msg_row_id, opt_name),
            )


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
) -> None:
    """Record a pin-in-chat add-on for a message, satisfying message_add_on NOT NULL constraints."""
    target_key_id = pin_info.get("target_key_id", "")
    if not target_key_id:
        return
    parent_row_id = find_message_row_id(conn, target_key_id, key_cache)
    if parent_row_id is None:
        logger.debug(f"Target message '{target_key_id}' for pin not found; skipping add-on.")
        return

    add_on_cols = get_table_columns(conn, "message_add_on", schema_cache)
    pin_cols = get_table_columns(conn, "message_add_on_pin_in_chat", schema_cache)
    if not add_on_cols:
        return

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
) -> None:
    """Apply protocol revoke or edit updates to an existing message, recording edit add-ons."""
    cur = conn.cursor()
    if msg.get("revoke_info"):
        target_key = msg["revoke_info"].get("target_key_id", "")
        if target_key:
            target_id = find_message_row_id(conn, target_key, key_cache)
            if target_id is not None:
                cur.execute(
                    "UPDATE message SET message_type = 15, text_data = NULL WHERE _id = ?",
                    (target_id,),
                )
                cur.execute("DELETE FROM message_media WHERE message_row_id = ?", (target_id,))

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


def apply_message_deletions(
    conn: sqlite3.Connection,
    deleted_json_path: str,
    logger: logging.Logger,
    schema_cache: dict[str, set[str]] | None,
) -> int:
    """Delete messages and auxiliary records (media, add-ons, polls, quoted, receipts) in deletedMessages_*.json."""
    with open(deleted_json_path, encoding="utf-8") as f:
        data = json.load(f)

    ids = data.get("deleted_ids", data) if isinstance(data, dict) else data
    if not isinstance(ids, list) or not ids:
        return 0

    cur = conn.cursor()
    total_deleted = 0
    chunk_size = 500

    has_add_on_reaction = bool(get_table_columns(conn, "message_add_on_reaction", schema_cache))
    has_add_on_pin = bool(get_table_columns(conn, "message_add_on_pin_in_chat", schema_cache))
    has_add_on = bool(get_table_columns(conn, "message_add_on", schema_cache))
    has_poll_opt = bool(get_table_columns(conn, "message_poll_option", schema_cache))
    has_poll = bool(get_table_columns(conn, "message_poll", schema_cache))
    has_loc = bool(get_table_columns(conn, "message_location", schema_cache))
    has_vcard = bool(get_table_columns(conn, "message_vcard", schema_cache))
    has_edit = bool(get_table_columns(conn, "message_edit_info", schema_cache))
    has_media = bool(get_table_columns(conn, "message_media", schema_cache))
    has_quoted = bool(get_table_columns(conn, "message_quoted", schema_cache))
    has_receipt_user = bool(get_table_columns(conn, "receipt_user", schema_cache))

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
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
    if jid_cache is not None and jid_str in jid_cache:
        return jid_cache[jid_str]

    if "@" in jid_str:
        user, server = jid_str.split("@", 1)
    else:
        user, server = jid_str, "s.whatsapp.net"

    if ":" in user:
        user = user.split(":", 1)[0]

    canonical_jid = f"{user}@{server}"
    if jid_cache is not None and canonical_jid in jid_cache:
        cached_id = jid_cache[canonical_jid]
        jid_cache[jid_str] = cached_id
        return cached_id

    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    has_device = "device" in cols
    if has_device:
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
    return jid_id


def resolve_or_create_chat(
    conn: sqlite3.Connection,
    jid_row_id: int,
    logger: logging.Logger,
    chat_cache: dict[int, int] | None,
) -> int:
    """Resolve a chat row for a jid_row_id, creating a placeholder row if absent."""
    if chat_cache is not None and jid_row_id in chat_cache:
        return chat_cache[jid_row_id]

    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
    has_account_jid = "account_jid_row_id" in cols
    has_hidden = "hidden" in cols

    # 1. Direct match on chat.jid_row_id
    cur.execute("SELECT _id FROM chat WHERE jid_row_id = ?", (jid_row_id,))
    row = cur.fetchone()
    if row is not None:
        chat_id = int(row[0])
        if has_hidden:
            cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (chat_id,))
        if chat_cache is not None:
            chat_cache[jid_row_id] = chat_id
        return chat_id

    # 2. Match on chat.account_jid_row_id if available
    if has_account_jid:
        cur.execute(
            "SELECT _id FROM chat WHERE account_jid_row_id = ? LIMIT 1",
            (jid_row_id,),
        )
        row = cur.fetchone()
        if row is not None:
            chat_id = int(row[0])
            if has_hidden:
                cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (chat_id,))
            if chat_cache is not None:
                chat_cache[jid_row_id] = chat_id
            return chat_id

    # 3. Check bidirectional mapping in jid_map
    try:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid_map'")
        if cur.fetchone() is not None:
            cur.execute(
                """
                SELECT c._id FROM chat c
                JOIN jid_map jm ON jm.jid_row_id = c.jid_row_id
                WHERE jm.lid_row_id = ?
                UNION
                SELECT c._id FROM chat c
                JOIN jid_map jm ON jm.lid_row_id = c.jid_row_id
                WHERE jm.jid_row_id = ?
                LIMIT 1
                """,
                (jid_row_id, jid_row_id),
            )
            row = cur.fetchone()
            if row is not None:
                chat_id = int(row[0])
                if has_hidden:
                    cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (chat_id,))
                if has_account_jid:
                    cur.execute(
                        "UPDATE chat SET account_jid_row_id = ? WHERE _id = ? AND (account_jid_row_id IS NULL OR account_jid_row_id = 0)",
                        (jid_row_id, chat_id),
                    )
                if chat_cache is not None:
                    chat_cache[jid_row_id] = chat_id
                return chat_id
    except sqlite3.OperationalError:
        pass

    # 4. Check lid_display_name matching to an existing chat with phone JID
    try:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lid_display_name'")
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
                        "SELECT c._id FROM chat c JOIN jid j ON j._id = c.jid_row_id "
                        "WHERE (j.user = ? OR j.user LIKE ?) LIMIT 1",
                        (clean_digits, f"%{clean_digits}"),
                    )
                    chat_match = cur.fetchone()
                    if chat_match is not None:
                        chat_id = int(chat_match[0])
                        if has_hidden:
                            cur.execute("UPDATE chat SET hidden = 0 WHERE _id = ?", (chat_id,))
                        if has_account_jid:
                            cur.execute(
                                "UPDATE chat SET account_jid_row_id = ? WHERE _id = ? AND (account_jid_row_id IS NULL OR account_jid_row_id = 0)",
                                (jid_row_id, chat_id),
                            )
                        if chat_cache is not None:
                            chat_cache[jid_row_id] = chat_id
                        return chat_id
    except sqlite3.OperationalError:
        pass

    # 5. Insert new chat row
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

    # Fast batch deletion of conflicting messages by key_id
    existing_media_paths: dict[str, str] = {}
    if "key_id" in msg_cols:
        key_ids_to_delete = [msg["key_id"] for msg in messages if msg.get("key_id")]
        chunk_size = 500
        for i in range(0, len(key_ids_to_delete), chunk_size):
            chunk = key_ids_to_delete[i : i + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
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
    max_ts_per_chat: dict[int, int] = {}
    max_display_msg_per_chat: dict[int, int] = {}
    for msg in messages:
        remote_jid = msg["remote_jid"]
        if not remote_jid:
            continue

        chat_jid_row_id = resolve_or_create_jid(conn, remote_jid, logger, jid_cache)
        chat_row_id = resolve_or_create_chat(conn, chat_jid_row_id, logger, chat_cache)

        # Resolve sender
        if msg["from_me"] == 1:
            sender_jid_row_id = None
        elif msg["participant"]:
            sender_jid_row_id = resolve_or_create_jid(conn, msg["participant"], logger, jid_cache)
        else:
            sender_jid_row_id = chat_jid_row_id

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

        is_add_on = bool(
            msg.get("reaction_info")
            or msg.get("pin_info")
            or msg.get("poll_vote_info")
            or msg.get("revoke_info")
            or msg.get("edit_info")
            or msg["message_type"] in (25, 67, 74, 79)
        )
        if not is_add_on and msg["timestamp"] >= max_ts_per_chat.get(chat_row_id, 0):
            max_ts_per_chat[chat_row_id] = msg["timestamp"]
            if msg_row_id:
                max_display_msg_per_chat[chat_row_id] = msg_row_id

        if msg.get("key_id") and msg_row_id:
            key_cache[msg["key_id"]] = msg_row_id

        # Insert media record if present
        if msg["media_info"] and sql_insert_media:
            media = msg["media_info"]
            file_hash_str = (
                base64.b64encode(media["file_sha256"]).decode("ascii")
                if media["file_sha256"]
                else None
            )

            # Reconstruct default expected file_path if document or preserve existing
            file_path = existing_media_paths.get(msg.get("key_id", ""))
            if not file_path and media["media_type"] == "document" and media["media_name"]:
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

        # Apply location metadata if present
        if msg.get("location_info") and msg_row_id:
            apply_location_record(conn, msg_row_id, msg["location_info"], schema_cache, logger)

        # Apply poll creation metadata if present
        if msg.get("poll_info") and msg_row_id:
            apply_poll_creation(conn, msg_row_id, msg["poll_info"], schema_cache, logger)

        # Insert message_quoted record if present
        if msg.get("quoted_info") and msg_row_id:
            quoted_cols = get_table_columns(conn, "message_quoted", schema_cache)
            if quoted_cols:
                qi = msg["quoted_info"]
                q_part = qi.get("participant", "")
                if q_part:
                    q_sender_id = resolve_or_create_jid(conn, q_part, logger, jid_cache)
                else:
                    q_sender_id = None if qi.get("from_me") == 1 else chat_jid_row_id
                q_row_map = {
                    "message_row_id": msg_row_id,
                    "chat_row_id": chat_row_id,
                    "parent_message_chat_row_id": chat_row_id,
                    "text_data": qi.get("quoted_text", ""),
                    "from_me": qi.get("from_me", 0),
                    "sender_jid_row_id": q_sender_id,
                    "key_id": qi.get("stanza_id") or msg.get("key_id", "") or "",
                    "timestamp": 0,
                    "message_type": 0,
                }
                valid_q_cols = [c for c in q_row_map if c in quoted_cols]
                cur.execute(
                    f"INSERT INTO message_quoted ({', '.join(valid_q_cols)}) VALUES ({', '.join('?' for _ in valid_q_cols)})",
                    [q_row_map[c] for c in valid_q_cols],
                )

        # Insert user receipts if present
        if msg.get("user_receipts") and msg_row_id:
            ru_cols = get_table_columns(conn, "receipt_user", schema_cache)
            if ru_cols:
                for ur in msg["user_receipts"]:
                    ur_jid = ur.get("user_jid", "")
                    ur_jid_id = (
                        resolve_or_create_jid(conn, ur_jid, logger, jid_cache)
                        if ur_jid
                        else chat_jid_row_id
                    )
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

        # Apply reaction add-on if present
        if msg.get("reaction_info"):
            apply_reaction_record(
                conn,
                msg["reaction_info"],
                sender_jid_row_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                msg.get("key_id", ""),
                chat_row_id,
            )

        # Apply pin-in-chat add-on if present
        if msg.get("pin_info"):
            apply_pin_in_chat(
                conn,
                msg["pin_info"],
                sender_jid_row_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                msg.get("key_id", ""),
                chat_row_id,
            )

        # Apply protocol revoke or edit updates if present
        if msg.get("revoke_info") or msg.get("edit_info"):
            apply_protocol_record(
                conn,
                msg,
                sender_jid_row_id,
                msg["from_me"],
                msg["timestamp"],
                key_cache,
                schema_cache,
                logger,
                chat_row_id,
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
        if "hidden" in chat_cols:
            cur.executemany(
                "UPDATE chat SET hidden = 0 WHERE _id = ? AND (hidden IS NULL OR hidden = 1)",
                [(cid,) for cid in max_ts_per_chat.keys()],
            )
        if "display_message_row_id" in chat_cols and max_display_msg_per_chat:
            cur.executemany(
                "UPDATE chat SET display_message_row_id = ? WHERE _id = ?",
                [(mid, cid) for cid, mid in max_display_msg_per_chat.items()],
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
) -> dict:
    """Apply an unpacked increment directory onto the active SQLite database."""
    header_path = os.path.join(inc_dir, "header.json")
    header = {}
    if os.path.isfile(header_path):
        with open(header_path, encoding="utf-8") as f:
            header = json.load(f)

    stats = {
        "entity_modified": 0,
        "entity_deleted": 0,
        "messages_deleted": 0,
        "messages_inserted": 0,
        "sequences_updated": 0,
    }

    with conn:
        # Step 1: Auxiliary entity modifications
        mod_files = [
            fn for fn in os.listdir(inc_dir) if "_modified_" in fn and fn.endswith(".json")
        ]
        for mf in sorted(mod_files):
            table_name = mf.split("_modified_")[0]
            stats["entity_modified"] += apply_entity_modifications(
                conn, table_name, os.path.join(inc_dir, mf), logger
            )

        # Step 2: Auxiliary entity deletions
        del_files = [fn for fn in os.listdir(inc_dir) if "_deleted_" in fn and fn.endswith(".json")]
        for df in sorted(del_files):
            table_name = df.split("_deleted_")[0]
            stats["entity_deleted"] += apply_entity_deletions(
                conn, table_name, os.path.join(inc_dir, df), logger
            )

        # Sanitize display_message_row_id in chat to prevent referencing messages from another chat
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
                WHERE display_message_row_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM message m
                      WHERE m._id = chat.display_message_row_id
                        AND m.chat_row_id = chat._id
                  )
                """
            )

        # Step 3: Message deletions
        del_msg_files = [
            fn
            for fn in os.listdir(inc_dir)
            if fn.startswith("deletedMessages_") and fn.endswith(".json")
        ]
        if isinstance(header.get("deleted_message_ids_files"), list):
            for dmf in header["deleted_message_ids_files"]:
                if dmf not in del_msg_files:
                    del_msg_files.append(dmf)
        for df in sorted(del_msg_files):
            df_path = os.path.join(inc_dir, df)
            if os.path.isfile(df_path):
                stats["messages_deleted"] += apply_message_deletions(
                    conn, df_path, logger, schema_cache
                )

        # Step 4: Protobuf message replay
        messages_bin_path = os.path.join(inc_dir, "messages.bin")
        if os.path.isfile(messages_bin_path):
            stats["messages_inserted"] += apply_messages_bin(
                conn, messages_bin_path, logger, jid_cache, chat_cache, schema_cache
            )

        # Step 5: Sequence roll-forward
        seq_name = (
            header.get("sequences")
            if isinstance(header.get("sequences"), str)
            else "sequences.json"
        )
        seq_path = os.path.join(inc_dir, seq_name)
        if os.path.isfile(seq_path):
            stats["sequences_updated"] += apply_sequences(conn, seq_path, logger)

    return stats


def merge_increments(
    base_db_path: str,
    increment_paths: list[str],
    e2e_key: object,
    output_db_path: str,
    staging_dir: str,
    logger: logging.Logger,
) -> int:
    """
    Merge a base decrypted database and a sequence of increment archives into output_db_path.
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

        # Pre-warm JID and chat caches upfront to avoid N+1 queries during delta folding
        cur = conn.cursor()
        jid_cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
        has_raw_string = "raw_string" in jid_cols
        jid_sql = (
            "SELECT user, server, _id, raw_string FROM jid"
            if has_raw_string
            else "SELECT user, server, _id FROM jid"
        )
        for r in cur.execute(jid_sql).fetchall():
            jid_cache[f"{r[0]}@{r[1]}"] = r[2]
            jid_cache[r[0]] = r[2]
            if has_raw_string and r[3]:
                jid_cache[r[3]] = r[2]

        chat_cols = {r[1] for r in cur.execute("PRAGMA table_info(chat)").fetchall()}
        if "jid_row_id" in chat_cols:
            for r in cur.execute("SELECT jid_row_id, _id FROM chat").fetchall():
                chat_cache[r[0]] = r[1]

        for idx, inc_path in enumerate(increment_paths):
            logger.info(
                f"Merging incremental backup [{idx + 1}/{len(increment_paths)}]: {os.path.basename(inc_path)}"
            )
            work_dir = os.path.join(staging_dir, f"unpacked_inc_{idx}")
            unpack_increment_archive(inc_path, key_obj, work_dir, logger)

            stats = apply_increment_folder(
                conn, work_dir, logger, jid_cache, chat_cache, schema_cache
            )
            total_inserted += stats["messages_inserted"]
            total_deleted += stats["messages_deleted"]
            logger.info(
                f"Increment {os.path.basename(inc_path)} applied: "
                f"+{stats['messages_inserted']} inserted, "
                f"-{stats['messages_deleted']} deleted, "
                f"{stats['entity_modified']} entities modified."
            )

        # Step 6: Rebuild database indexes after all increments are applied, matching vendor MessageStoreBackupUtils/reindex
        logger.info("Rebuilding database indexes (REINDEX)...")
        conn.execute("REINDEX")

        # Restore synchronous mode and execute quick integrity check
        conn.execute("PRAGMA synchronous = NORMAL")
        integrity = conn.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(
                f"Database integrity check failed after merging increments: {integrity}"
            )

    logger.info(
        f"Incremental merging complete: {len(increment_paths)} increment(s) folded into {output_db_path} "
        f"(+{total_inserted} inserted, -{total_deleted} deleted)."
    )
    return total_inserted
