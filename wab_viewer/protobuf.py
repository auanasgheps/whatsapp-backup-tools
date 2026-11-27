"""Pure-Python protobuf wire parsers for iOS WhatsApp payloads."""

from __future__ import annotations

import base64


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read a base-128 varint from data starting at offset."""
    val = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, offset
        shift += 7
    raise ValueError("Truncated varint in protobuf payload")


def _parse_ios_chat_session_meta(raw_b64: str) -> tuple[int, bool, int]:
    """Extract (ephemeral_duration, is_pinned, pinned_time_ms) from iOS ZLASTMESSAGETEXT."""
    if not raw_b64:
        return 0, False, 0
    try:
        data = base64.b64decode(raw_b64)
    except Exception:
        return 0, False, 0

    ephemeral_duration = 0
    is_pinned = False
    pinned_time = 0
    offset = 0
    while offset < len(data):
        try:
            tag, offset = _read_varint(data, offset)
        except ValueError:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            try:
                val, offset = _read_varint(data, offset)
            except ValueError:
                break
            if field_num == 2 and val > 0:
                ephemeral_duration = int(val)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            try:
                length, offset = _read_varint(data, offset)
            except ValueError:
                break
            offset += length
        elif wire_type == 5:
            offset += 4
        else:
            break
    return ephemeral_duration, is_pinned, pinned_time


def _extract_ios_group_description(raw_pic_id: str) -> str | None:
    """Extract group description text from iOS ZWAGROUPINFO.ZPICTUREID protobuf."""
    if not raw_pic_id:
        return None
    s = raw_pic_id[1:] if raw_pic_id.startswith("+") else raw_pic_id
    try:
        data = base64.b64decode(s, validate=True)
    except Exception:
        return None

    offset = 0
    while offset < len(data):
        try:
            tag, offset = _read_varint(data, offset)
        except ValueError:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:
            try:
                _, offset = _read_varint(data, offset)
            except ValueError:
                break
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            try:
                length, offset = _read_varint(data, offset)
            except ValueError:
                break
            payload = data[offset : offset + length]
            offset += length
            if field_num == 1:
                sub_offset = 0
                while sub_offset < len(payload):
                    try:
                        sub_tag, sub_offset = _read_varint(payload, sub_offset)
                    except ValueError:
                        break
                    sub_num = sub_tag >> 3
                    sub_wire = sub_tag & 0x07
                    if sub_wire == 0:
                        try:
                            _, sub_offset = _read_varint(payload, sub_offset)
                        except ValueError:
                            break
                    elif sub_wire == 1:
                        sub_offset += 8
                    elif sub_wire == 2:
                        try:
                            sub_len, sub_offset = _read_varint(payload, sub_offset)
                        except ValueError:
                            break
                        sub_bytes = payload[sub_offset : sub_offset + sub_len]
                        sub_offset += sub_len
                        if sub_num == 3:
                            desc = sub_bytes.decode("utf-8", errors="replace").strip()
                            return desc if desc else None
                    elif sub_wire == 5:
                        sub_offset += 4
                    else:
                        break
        elif wire_type == 5:
            offset += 4
        else:
            break
    return None


def _parse_ios_poll_blob(data: bytes) -> dict | None:
    """Parse poll question, options, selection mode, and votes from iOS ZRECEIPTINFO protobuf."""
    if not data:
        return None
    offset = 0
    poll_blob = None
    while offset < len(data):
        try:
            tag, offset = _read_varint(data, offset)
        except ValueError:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            try:
                _, offset = _read_varint(data, offset)
            except ValueError:
                break
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            try:
                length, offset = _read_varint(data, offset)
            except ValueError:
                break
            val = data[offset : offset + length]
            offset += length
            if field_num == 8:
                poll_blob = val
        elif wire_type == 5:
            offset += 4
        else:
            break

    if not poll_blob:
        return None

    p_offset = 0
    question = ""
    options = []
    selectable_count = 0
    votes = []

    while p_offset < len(poll_blob):
        try:
            p_tag, p_offset = _read_varint(poll_blob, p_offset)
        except ValueError:
            break
        p_num = p_tag >> 3
        p_wire = p_tag & 0x07
        if p_wire == 0:
            try:
                val, p_offset = _read_varint(poll_blob, p_offset)
            except ValueError:
                break
            if p_num == 4:
                selectable_count = val
        elif p_wire == 1:
            p_offset += 8
        elif p_wire == 2:
            try:
                p_len, p_offset = _read_varint(poll_blob, p_offset)
            except ValueError:
                break
            p_data = poll_blob[p_offset : p_offset + p_len]
            p_offset += p_len
            if p_num == 2:
                question = p_data.decode("utf-8", errors="replace")
            elif p_num == 3:
                opt_offset = 0
                opt_name = ""
                while opt_offset < len(p_data):
                    try:
                        o_tag, opt_offset = _read_varint(p_data, opt_offset)
                    except ValueError:
                        break
                    o_num = o_tag >> 3
                    o_wire = o_tag & 0x07
                    if o_wire == 0:
                        try:
                            _, opt_offset = _read_varint(p_data, opt_offset)
                        except ValueError:
                            break
                    elif o_wire == 1:
                        opt_offset += 8
                    elif o_wire == 2:
                        try:
                            o_len, opt_offset = _read_varint(p_data, opt_offset)
                        except ValueError:
                            break
                        o_bytes = p_data[opt_offset : opt_offset + o_len]
                        opt_offset += o_len
                        if o_num == 1:
                            opt_name = o_bytes.decode("utf-8", errors="replace")
                    elif o_wire == 5:
                        opt_offset += 4
                    else:
                        break
                options.append(opt_name)
            elif p_num == 5:
                v_offset = 0
                v_opt_indices = []
                client_ts = 0
                stanza_id = ""
                voter_jid = ""
                server_ts = 0
                while v_offset < len(p_data):
                    try:
                        v_tag, v_offset = _read_varint(p_data, v_offset)
                    except ValueError:
                        break
                    v_num = v_tag >> 3
                    v_wire = v_tag & 0x07
                    if v_wire == 0:
                        try:
                            v_val, v_offset = _read_varint(p_data, v_offset)
                        except ValueError:
                            break
                        if v_num == 1:
                            v_opt_indices.append(v_val)
                        elif v_num == 2:
                            client_ts = v_val
                        elif v_num == 5:
                            server_ts = v_val
                    elif v_wire == 1:
                        v_offset += 8
                    elif v_wire == 2:
                        try:
                            v_len, v_offset = _read_varint(p_data, v_offset)
                        except ValueError:
                            break
                        v_bytes = p_data[v_offset : v_offset + v_len]
                        v_offset += v_len
                        if v_num == 1:
                            p_idx = 0
                            while p_idx < len(v_bytes):
                                try:
                                    idx_val, p_idx = _read_varint(v_bytes, p_idx)
                                except ValueError:
                                    break
                                v_opt_indices.append(idx_val)
                        elif v_num == 3:
                            stanza_id = v_bytes.decode("utf-8", errors="replace")
                        elif v_num == 4:
                            voter_jid = v_bytes.decode("utf-8", errors="replace")
                    elif v_wire == 5:
                        v_offset += 4
                    else:
                        break
                votes.append(
                    {
                        "option_indices": v_opt_indices,
                        "client_ts": client_ts,
                        "stanza_id": stanza_id,
                        "voter_jid": voter_jid,
                        "server_ts": server_ts,
                    }
                )
        elif p_wire == 5:
            p_offset += 4
        else:
            break

    return {
        "question": question,
        "options": options,
        "multiple_answers": (selectable_count == 0),
        "votes": votes,
    }


def _parse_protobuf(data: bytes) -> list:
    """Parse a protobuf blob into (field, wire_type, value) triples.

    Handles wire types: 0=varint, 1=fixed64, 2=length-delimited, 5=fixed32.
    Wire types 3,4,6,7 are deprecated protobuf — skipped.
    """
    results = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
            f = tag >> 3
            w = tag & 7
            if w == 0:  # varint
                val, pos = _read_varint(data, pos)
                results.append((f, "varint", val))
            elif w == 1:  # 64-bit fixed
                val = int.from_bytes(data[pos : pos + 8], "little")
                pos += 8
                results.append((f, "fixed64", val))
            elif w == 2:  # length-delimited
                length, pos = _read_varint(data, pos)
                val = data[pos : pos + length]
                pos += length
                results.append((f, "len", val))
            elif w == 5:  # 32-bit fixed
                pos += 4
            else:
                break  # deprecated wire types 3,4,6,7 — cannot resync, stop
        except (IndexError, ValueError):
            break
    return results


def _scan_emojis(data: bytes) -> list[str]:
    """Extract UTF-8 emoji sequences from raw bytes.

    Skips Variation Selector 16 (efb88f) which is a presentation modifier, not a
    standalone emoji. VS16 is merged into the preceding emoji's hex string so the
    full emoji character (e.g. ❤️ = e29da4efb88f) is produced after hex decode.
    """
    emojis = []
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0xEF and i + 2 < len(data) and data[i + 1] == 0xB8 and data[i + 2] == 0x8F:
            # VS16 (Variation Selector 16) — merge with the previous emoji
            if emojis:
                emojis[-1] = emojis[-1] + "efb88f"
            i += 3
        elif b == 0xE2 and i + 2 < len(data):
            # 3-byte emoji (e.g. ❤️ = e2 9d a4)
            emojis.append(data[i : i + 3].hex())
            i += 3
        elif b == 0xF0 and i + 3 < len(data):
            # 4-byte emoji (e.g. 😂 = f0 9f 98 82)
            emojis.append(data[i : i + 4].hex())
            i += 4
        else:
            i += 1
    return emojis


def _parse_reactor_entry(data: bytes) -> tuple:
    """Extract (sender_hex, [emoji_hex, ...]) from a reactor entry sub-blob.

    Sub-fields 2 and 3 may contain emoji bytes. Sub-field 1 is the sender phone and
    must NOT be scanned for emojis (phone bytes can accidentally match emoji byte
    sequences and corrupt the output).
    """
    sender = None
    all_emojis = []
    for sf, sw, sv in _parse_protobuf(data):
        if sw == "len":
            if sf == 1:
                sender = sv.hex()
            elif sf in (2, 3):
                all_emojis.extend(_scan_emojis(sv))
    return sender, all_emojis


def _extract_ios_reactions(receipt_bytes: bytes) -> list:
    """Extract (sender_hex, emoji_char) pairs from a ZRECEIPTINFO blob.

    Field 7 is the reactor group; each reactor is a repeated length-delimited
    sub-field (field 1 or 2). Sub-field 1 holds the sender, sub-fields 2/3 the emoji.
    """
    reactors = []
    for f, w, v in _parse_protobuf(receipt_bytes):
        if f == 7 and w == "len":
            for ef, ew, ev in _parse_protobuf(v):
                if ew != "len":
                    continue
                sender, emojis = _parse_reactor_entry(ev)
                for emoji_hex in emojis:
                    emoji_char = bytes.fromhex(emoji_hex).decode("utf-8", errors="ignore")
                    if emoji_char:
                        reactors.append((sender, emoji_char))
            return reactors
    return []


def _extract_ios_reaction_reactors(receipt_bytes: bytes) -> list:
    """Extract per-reactor details from a ZRECEIPTINFO reaction group (field 7).

    Returns a list of dicts: {"phone": str|None, "lid": str|None, "emoji": str}.

    The reactor's identity is carried by a sub-field ending '@s.whatsapp.net'
    (phone JID) or '@lid' (LID JID). Recent group reactions use the LID form,
    which resolves to a name via the same push-name chain as group message
    senders. Reactor sub-field 1 is an opaque per-reaction token that maps to
    nothing in the export and must NOT be used as identity. Entries with neither
    JID are the current user's own reactions (identity is implicit).
    """
    out = []
    for f, w, v in _parse_protobuf(receipt_bytes):
        if f == 7 and w == "len":
            for ef, ew, ev in _parse_protobuf(v):
                if ew != "len":
                    continue
                _, emojis = _parse_reactor_entry(ev)
                if not emojis:
                    continue
                phone = None
                lid = None
                for sf, sw, sv in _parse_protobuf(ev):
                    if sw != "len":
                        continue
                    b = bytes(sv)
                    if b.endswith(b"@s.whatsapp.net"):
                        phone = b.decode("utf-8", errors="ignore").split("@")[0]
                    elif b.endswith(b"@lid"):
                        lid = b.decode("utf-8", errors="ignore")
                emoji_char = "".join(
                    bytes.fromhex(h).decode("utf-8", errors="ignore") for h in emojis
                )
                out.append({"phone": phone, "lid": lid, "emoji": emoji_char})
            return out
    return []


def _extract_ios_quoted_info(metadata_bytes: bytes | None) -> dict | None:
    """Extract quoted message metadata from iOS ZWAMEDIAITEM.ZMETADATA protobuf blob."""
    if not metadata_bytes or not isinstance(metadata_bytes, (bytes, bytearray)):
        return None
    try:
        idx = 0
        n = len(metadata_bytes)
        q_stanza = None
        q_sender = None
        q_text = None
        q_has_thumb = False

        while idx < n:
            val = 0
            shift = 0
            while idx < n:
                b = metadata_bytes[idx]
                idx += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            wire_type = val & 7
            field_num = val >> 3

            if wire_type == 0:
                while idx < n and (metadata_bytes[idx] & 0x80):
                    idx += 1
                idx += 1
            elif wire_type == 2:
                length = 0
                shift = 0
                while idx < n:
                    lb = metadata_bytes[idx]
                    idx += 1
                    length |= (lb & 0x7F) << shift
                    if not (lb & 0x80):
                        break
                    shift += 7
                if idx + length > n:
                    break
                data = metadata_bytes[idx : idx + length]
                idx += length

                if field_num == 5:
                    q_stanza = data.decode("utf-8", errors="replace")
                elif field_num == 6:
                    q_sender = data.decode("utf-8", errors="replace")
                elif field_num == 19:
                    sub_idx = 0
                    sub_n = len(data)
                    while sub_idx < sub_n:
                        sval = 0
                        sshift = 0
                        while sub_idx < sub_n:
                            sb = data[sub_idx]
                            sub_idx += 1
                            sval |= (sb & 0x7F) << sshift
                            if not (sb & 0x80):
                                break
                            sshift += 7
                        swire = sval & 7
                        sfield = sval >> 3

                        if swire == 0:
                            while sub_idx < sub_n and (data[sub_idx] & 0x80):
                                sub_idx += 1
                            sub_idx += 1
                        elif swire == 2:
                            slen = 0
                            sshift = 0
                            while sub_idx < sub_n:
                                slb = data[sub_idx]
                                sub_idx += 1
                                slen |= (slb & 0x7F) << sshift
                                if not (slb & 0x80):
                                    break
                                sshift += 7
                            if sub_idx + slen > sub_n:
                                break
                            sdata = data[sub_idx : sub_idx + slen]
                            sub_idx += slen

                            if sfield == 1:
                                q_text = sdata.decode("utf-8", errors="replace")
                            elif sfield == 6:
                                ex_idx = 0
                                ex_n = len(sdata)
                                ex_text = None
                                ex_matched = None
                                while ex_idx < ex_n:
                                    eval = 0
                                    eshift = 0
                                    while ex_idx < ex_n:
                                        eb = sdata[ex_idx]
                                        ex_idx += 1
                                        eval |= (eb & 0x7F) << eshift
                                        if not (eb & 0x80):
                                            break
                                        eshift += 7
                                    ewire = eval & 7
                                    efield = eval >> 3
                                    if ewire == 0:
                                        while ex_idx < ex_n and (sdata[ex_idx] & 0x80):
                                            ex_idx += 1
                                        ex_idx += 1
                                    elif ewire == 2:
                                        elen = 0
                                        eshift = 0
                                        while ex_idx < ex_n:
                                            elb = sdata[ex_idx]
                                            ex_idx += 1
                                            elen |= (elb & 0x7F) << eshift
                                            if not (elb & 0x80):
                                                break
                                            eshift += 7
                                        if ex_idx + elen > ex_n:
                                            break
                                        edata = sdata[ex_idx : ex_idx + elen]
                                        ex_idx += elen
                                        if efield == 1:
                                            ex_text = edata.decode("utf-8", errors="replace")
                                        elif efield == 2 and not ex_text:
                                            ex_matched = edata.decode("utf-8", errors="replace")
                                        elif efield == 16 and edata:
                                            q_has_thumb = True
                                    elif ewire == 1:
                                        ex_idx += 8
                                    elif ewire == 5:
                                        ex_idx += 4
                                    else:
                                        break
                                q_text = ex_text or ex_matched or ""
                            elif sfield == 3:
                                q_has_thumb = True
                                im_idx = 0
                                im_n = len(sdata)
                                im_caption = None
                                while im_idx < im_n:
                                    ival = 0
                                    ishift = 0
                                    while im_idx < im_n:
                                        ib = sdata[im_idx]
                                        im_idx += 1
                                        ival |= (ib & 0x7F) << ishift
                                        if not (ib & 0x80):
                                            break
                                        ishift += 7
                                    iwire = ival & 7
                                    ifield = ival >> 3
                                    if iwire == 0:
                                        while im_idx < im_n and (sdata[im_idx] & 0x80):
                                            im_idx += 1
                                        im_idx += 1
                                    elif iwire == 2:
                                        ilen = 0
                                        ishift = 0
                                        while im_idx < im_n:
                                            ilb = sdata[im_idx]
                                            im_idx += 1
                                            ilen |= (ilb & 0x7F) << ishift
                                            if not (ilb & 0x80):
                                                break
                                            ishift += 7
                                        if im_idx + ilen > im_n:
                                            break
                                        idata = sdata[im_idx : im_idx + ilen]
                                        im_idx += ilen
                                        if ifield == 3:
                                            im_caption = idata.decode("utf-8", errors="replace")
                                    elif iwire == 1:
                                        im_idx += 8
                                    elif iwire == 5:
                                        im_idx += 4
                                    else:
                                        break
                                q_text = f"📷 {im_caption}" if im_caption else "📷 Photo"
                            elif sfield == 8:
                                q_text = "🎵 Audio"
                            elif sfield == 9:
                                q_has_thumb = True
                                q_text = "🎥 Video"
                            elif sfield == 26:
                                q_has_thumb = True
                                q_text = "Sticker"
                            elif sfield == 7:
                                q_text = "📄 Document"
                        elif swire == 1:
                            sub_idx += 8
                        elif swire == 5:
                            sub_idx += 4
                        else:
                            break
            elif wire_type == 1:
                idx += 8
            elif wire_type == 5:
                idx += 4
            else:
                break

        if q_stanza:
            return {
                "quoted_stanza_id": q_stanza,
                "quoted_sender": q_sender,
                "quoted_text": q_text or "",
                "quoted_has_thumb": q_has_thumb,
            }
    except Exception:
        pass
    return None


def _extract_ios_link_description(metadata_bytes: bytes | None) -> str | None:
    """Extract page description string from wire tag 3 of an iOS ZMETADATA protobuf blob."""
    if not metadata_bytes or not isinstance(metadata_bytes, (bytes, bytearray)):
        return None
    try:
        idx = 0
        n = len(metadata_bytes)
        while idx < n:
            val = 0
            shift = 0
            while idx < n:
                b = metadata_bytes[idx]
                idx += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            wire_type = val & 7
            field_num = val >> 3
            if wire_type == 0:
                while idx < n and (metadata_bytes[idx] & 0x80):
                    idx += 1
                idx += 1
            elif wire_type == 2:
                length = 0
                shift = 0
                while idx < n:
                    lb = metadata_bytes[idx]
                    idx += 1
                    length |= (lb & 0x7F) << shift
                    if not (lb & 0x80):
                        break
                    shift += 7
                if idx + length > n:
                    break
                if field_num == 3:
                    desc_bytes = metadata_bytes[idx : idx + length]
                    if b"\x00" not in desc_bytes and b"\xff\xd8\xff" not in desc_bytes:
                        decoded = desc_bytes.decode("utf-8", errors="replace")
                        if not decoded.startswith("http"):
                            return decoded
                idx += length
            elif wire_type == 5:
                idx += 4
            elif wire_type == 1:
                idx += 8
            else:
                break
    except Exception:
        pass
    return None


def _iter_wire_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    """Decode top-level protobuf wire fields as (field_num, wire_type, value)."""
    fields: list[tuple[int, int, bytes | int]] = []
    offset = 0
    n = len(data)
    while offset < n:
        try:
            tag, offset = _read_varint(data, offset)
        except ValueError:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            try:
                val, offset = _read_varint(data, offset)
            except ValueError:
                break
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:
            try:
                length, offset = _read_varint(data, offset)
            except ValueError:
                break
            if length < 0 or offset + length > n:
                break
            fields.append((field_num, wire_type, data[offset : offset + length]))
            offset += length
        elif wire_type == 1:
            if offset + 8 > n:
                break
            fields.append((field_num, wire_type, data[offset : offset + 8]))
            offset += 8
        elif wire_type == 5:
            if offset + 4 > n:
                break
            fields.append((field_num, wire_type, data[offset : offset + 4]))
            offset += 4
        else:
            break
    return fields


def _extract_context_mentioned_jids(ctx_bytes: bytes) -> list[str]:
    """Extract Tag 15 / Tag 8 (mentionedJid) strings from a serialized ContextInfo blob."""
    jids: list[str] = []
    for field_num, wire_type, val in _iter_wire_fields(ctx_bytes):
        if field_num in (8, 15) and wire_type == 2 and isinstance(val, bytes) and val:
            jid = val.decode("utf-8", errors="replace").strip()
            if jid and "@" in jid:
                jids.append(jid)
    return jids


def _extract_ios_mentions(metadata_bytes: bytes | None) -> list[str]:
    """Extract mentioned JIDs (Tag 8 root, Tag 15 fallback/ContextInfo) from an iOS ZWAMEDIAITEM.ZMETADATA blob."""
    if not metadata_bytes or not isinstance(metadata_bytes, (bytes, bytearray)):
        return []
    raw = bytes(metadata_bytes)
    jids: list[str] = []
    for field_num, wire_type, val in _iter_wire_fields(raw):
        if field_num in (8, 15) and wire_type == 2 and isinstance(val, bytes) and val:
            jid = val.decode("utf-8", errors="replace").strip()
            if jid and "@" in jid:
                jids.append(jid)
        elif field_num == 19 and wire_type == 2 and isinstance(val, bytes) and val:
            # Quoted WebMessageInfo may contain nested sub-messages with ContextInfo (tags 17, 14, 15)
            for q_field, q_wire, q_val in _iter_wire_fields(val):
                if q_wire != 2 or not isinstance(q_val, bytes) or not q_val:
                    continue
                if q_field in (3, 6, 7, 8, 9, 18, 20, 26, 42, 45, 49, 60, 62, 64, 66, 75, 93, 111):
                    for sub_field, sub_wire, sub_val in _iter_wire_fields(q_val):
                        if (
                            sub_field in (14, 15, 17)
                            and sub_wire == 2
                            and isinstance(sub_val, bytes)
                            and sub_val
                        ):
                            jids.extend(_extract_context_mentioned_jids(sub_val))
    return list(dict.fromkeys(jids))
