"""Protobuf wire encoders and message builders for tests.

Provides synthetic wire-construction primitives for Protobuf containers, WMI frames,
and iOS CoreData ZRECEIPTINFO blobs without depending on any external protobuf compiler.
"""

from __future__ import annotations


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
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


def build_field(field_num: int, wire_type: int, payload: bytes | int) -> bytes:
    """Encode a single protobuf field tag and payload."""
    tag = (field_num << 3) | wire_type
    tag_bytes = encode_varint(tag)
    if wire_type == 0:
        return tag_bytes + encode_varint(payload)  # type: ignore[arg-type]
    elif wire_type == 1:
        return tag_bytes + payload  # type: ignore[operator]
    elif wire_type == 2:
        return tag_bytes + encode_varint(len(payload)) + payload  # type: ignore[arg-type,operator]
    elif wire_type == 5:
        return tag_bytes + payload  # type: ignore[operator]
    raise ValueError(f"Unsupported wire type {wire_type}")


def build_text_wmi(remote_jid: str, key_id: str, text: str, from_me: int, timestamp: int) -> bytes:
    """Construct a minimal synthetic text WMI frame."""
    key_bytes = (
        build_field(1, 2, remote_jid.encode("utf-8"))
        + build_field(2, 0, from_me)
        + build_field(3, 2, key_id.encode("utf-8"))
    )
    msg_bytes = build_field(1, 2, text.encode("utf-8"))
    return (
        build_field(1, 2, key_bytes) + build_field(2, 2, msg_bytes) + build_field(3, 0, timestamp)
    )


def build_image_wmi(
    remote_jid: str,
    key_id: str,
    caption: str,
    mimetype: str,
    file_sha256: bytes,
    file_length: int,
    from_me: int,
    timestamp: int,
) -> bytes:
    """Construct a minimal synthetic image WMI frame."""
    key_bytes = (
        build_field(1, 2, remote_jid.encode("utf-8"))
        + build_field(2, 0, from_me)
        + build_field(3, 2, key_id.encode("utf-8"))
    )
    img_bytes = (
        build_field(1, 2, b"https://example.com/media.enc")
        + build_field(2, 2, mimetype.encode("utf-8"))
        + build_field(3, 2, caption.encode("utf-8"))
        + build_field(4, 2, file_sha256)
        + build_field(5, 0, file_length)
    )
    msg_bytes = build_field(3, 2, img_bytes)
    return (
        build_field(1, 2, key_bytes) + build_field(2, 2, msg_bytes) + build_field(3, 0, timestamp)
    )


def build_history_sync_frame(remote_jid: str, wmi_bytes_list: list[bytes]) -> bytes:
    """Construct a delimited history sync payload."""
    hsm_bytes = b"".join(build_field(1, 2, wmi) for wmi in wmi_bytes_list)
    conv_bytes = build_field(1, 2, remote_jid.encode("utf-8")) + build_field(2, 2, hsm_bytes)
    hs_bytes = build_field(2, 2, conv_bytes)
    return encode_varint(len(hs_bytes)) + hs_bytes


def lid_field1_bytes(lid_digits: str) -> bytes:
    """Encode a LID as field-1 bytes: prefix byte 0x8c + hex-nibble digits."""
    h = lid_digits if len(lid_digits) % 2 == 0 else lid_digits + "f"
    return b"\x8c" + bytes.fromhex(h)


def make_receipt_blob(base_ts: int, members: list) -> bytes:
    """Build a rich-format ZRECEIPTINFO blob (top field 3 = base_ts)."""
    blob = encode_varint((3 << 3) | 0) + encode_varint(base_ts)
    for m in members:
        l_bytes = lid_field1_bytes(m["lid"])
        entry = encode_varint((1 << 3) | 2) + encode_varint(len(l_bytes)) + l_bytes
        if m["noise4"] is not None:
            entry += encode_varint((4 << 3) | 0) + encode_varint(m["noise4"])
        if m["read_delta"] is not None:
            entry += encode_varint((5 << 3) | 0) + encode_varint(m["read_delta"])
        if m["delivered_delta"] is not None:
            event = (
                encode_varint((1 << 3) | 0)
                + encode_varint(m["delivered_delta"])
                + encode_varint((2 << 3) | 0)
                + encode_varint(1)
            )
            entry += encode_varint((10 << 3) | 2) + encode_varint(len(event)) + event
        blob += encode_varint((2 << 3) | 2) + encode_varint(len(entry)) + entry
    return blob


def make_compact_receipt_blob(members: list) -> bytes:
    """Build a compact-format ZRECEIPTINFO blob (NO top field 3, NO field 5)."""
    blob = b""
    for m in members:
        l_bytes = lid_field1_bytes(m["lid"])
        entry = encode_varint((1 << 3) | 2) + encode_varint(len(l_bytes)) + l_bytes
        event = (
            encode_varint((1 << 3) | 0)
            + encode_varint(m["delivered_delta"])
            + encode_varint((2 << 3) | 0)
            + encode_varint(3)
        )
        entry += encode_varint((9 << 3) | 2) + encode_varint(len(event)) + event
        blob += encode_varint((2 << 3) | 2) + encode_varint(len(entry)) + entry
    return blob


def make_zreceipt_blob_with_reactions(reactors: list) -> bytes:
    """Build a ZRECEIPTINFO blob containing reaction entries."""

    def encode_len_delimited(field: int, wire: int, data: bytes) -> bytes:
        tag = (field << 3) | wire
        return encode_varint(tag) + encode_varint(len(data)) + data

    def make_reactor_entry(phone_hex_str: str, emoji_bytes: bytes) -> bytes:
        phone_data = bytes.fromhex(phone_hex_str)
        entry = encode_len_delimited(1, 2, phone_data)
        entry += encode_len_delimited(3, 2, emoji_bytes)
        return entry

    field7_data = b""
    for phone_hex, emoji_bytes in reactors:
        entry_data = make_reactor_entry(phone_hex, emoji_bytes)
        field7_data += encode_varint(0x0A) + encode_varint(len(entry_data)) + entry_data

    top_tag = (7 << 3) | 2
    return encode_varint(top_tag) + encode_varint(len(field7_data)) + field7_data


def make_ios_reaction_blob(entries: list) -> bytes:
    """Build a ZRECEIPTINFO reaction group (field 7) from reactor entry dicts."""

    def ld(field: int, data: bytes) -> bytes:
        tag = (field << 3) | 2
        return encode_varint(tag) + encode_varint(len(data)) + data

    field7 = b""
    for e in entries:
        entry = ld(1, e["token"].encode("utf-8"))
        if e.get("phone"):
            entry += ld(2, f"{e['phone']}@s.whatsapp.net".encode())
        elif e.get("lid"):
            entry += ld(2, f"{e['lid']}@lid".encode())
        entry += ld(3, e["emoji"])
        if e.get("from_me"):
            entry += ld(5, b"\x0a\x07" + b"\x81\x39\x34\x00\x00\x00\x00")
        field7 += ld(1, entry)
    top_tag = (7 << 3) | 2
    return encode_varint(top_tag) + encode_varint(len(field7)) + field7


def build_context_info(
    stanza_id: str = "",
    participant: str = "",
    quoted_msg_bytes: bytes | None = None,
    mentioned_jids: list[str] | None = None,
    thumbnail: bytes | None = None,
) -> bytes:
    """Build a synthetic ContextInfo protobuf payload."""
    ctx = b""
    if stanza_id:
        ctx += build_field(1, 2, stanza_id.encode("utf-8"))
    if participant:
        ctx += build_field(2, 2, participant.encode("utf-8"))
    if quoted_msg_bytes is not None:
        ctx += build_field(3, 2, quoted_msg_bytes)
    if thumbnail:
        ctx += build_field(16, 2, thumbnail)
    if mentioned_jids:
        for jid in mentioned_jids:
            ctx += build_field(15, 2, jid.encode("utf-8"))
    return ctx


def build_ios_poll_blob(
    question: str,
    options: list[str],
    selectable_count: int = 0,
    votes: list[dict] | None = None,
) -> bytes:
    """Build a synthetic iOS ZRECEIPTINFO poll blob (field 8)."""
    poll_inner = b""
    if question:
        poll_inner += build_field(2, 2, question.encode("utf-8"))
    for opt in options:
        opt_payload = build_field(1, 2, opt.encode("utf-8"))
        poll_inner += build_field(3, 2, opt_payload)
    if selectable_count > 0:
        poll_inner += build_field(4, 0, selectable_count)

    if votes:
        for v in votes:
            v_payload = b""
            for idx in v.get("option_indices", []):
                v_payload += build_field(1, 0, idx)
            if v.get("client_ts"):
                v_payload += build_field(2, 0, v["client_ts"])
            if v.get("stanza_id"):
                v_payload += build_field(3, 2, v["stanza_id"].encode("utf-8"))
            if v.get("voter_jid"):
                v_payload += build_field(4, 2, v["voter_jid"].encode("utf-8"))
            if v.get("server_ts"):
                v_payload += build_field(5, 0, v["server_ts"])
            poll_inner += build_field(5, 2, v_payload)

    return build_field(8, 2, poll_inner)
