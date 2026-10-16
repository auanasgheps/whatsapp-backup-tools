import base64
import contextlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
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


def decode_wmi(wmi_bytes: bytes) -> dict:
    """Decode a serialized WebMessageInfo (WMI) protobuf into a normalized message dictionary."""
    fields = parse_wire_fields(wmi_bytes)

    key_id = ""
    from_me = 0
    remote_jid = ""
    participant = ""

    # Field 1: MessageKey (remoteJid=1, fromMe=2, id=3, participant=4)
    if 1 in fields:
        key_fields = parse_wire_fields(fields[1][0][1])
        if 1 in key_fields:
            remote_jid = key_fields[1][0][1].decode("utf-8", errors="replace")
        if 2 in key_fields:
            from_me = 1 if key_fields[2][0][1] else 0
        if 3 in key_fields:
            key_id = key_fields[3][0][1].decode("utf-8", errors="replace")
        if 4 in key_fields:
            participant = key_fields[4][0][1].decode("utf-8", errors="replace")

    # Field 3: messageTimestamp
    timestamp_raw = 0
    if 3 in fields:
        timestamp_raw = fields[3][0][1]
    # Convert seconds to milliseconds if timestamp is in Unix epoch seconds
    timestamp_ms = timestamp_raw * 1000 if timestamp_raw < 10000000000 else timestamp_raw

    # Field 4: status
    status = fields.get(4, [(0, 0)])[0][1]

    # Field 16: starred
    starred = 1 if fields.get(16, [(0, 0)])[0][1] else 0

    message_type = 0
    text_data = None
    media_info = None
    vcard = None

    # Field 2: Message payload
    if 2 in fields:
        msg_fields = parse_wire_fields(fields[2][0][1])

        # 1: conversation (plain text)
        if 1 in msg_fields:
            message_type = 0
            text_data = msg_fields[1][0][1].decode("utf-8", errors="replace")

        # 3: imageMessage
        elif 3 in msg_fields:
            message_type = 1
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
            message_type = 2
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
            message_type = 3
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

        # 13: contactsArrayMessage
        elif 13 in msg_fields:
            message_type = 14
            ca_fields = parse_wire_fields(msg_fields[13][0][1])
            text_data = (
                ca_fields.get(1, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 1 in ca_fields
                else None
            )

        # 26: stickerMessage
        elif 26 in msg_fields:
            message_type = 20
            stk_fields = parse_wire_fields(msg_fields[26][0][1])
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

        # 48: reactionMessage
        elif 48 in msg_fields:
            message_type = 25
            react_fields = parse_wire_fields(msg_fields[48][0][1])
            text_data = (
                react_fields.get(2, [(2, b"")])[0][1].decode("utf-8", errors="replace")
                if 2 in react_fields
                else None
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
        "vcard": vcard,
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


def find_increment_files(search_dir: str) -> list[str]:
    """
    Search a directory for files matching 'msgstore-increment-(\\d+)',
    excluding temporary swap artifacts, sorted by sequential integer index.
    If multiple backup generations exist (duplicate increment IDs across tags),
    selects the latest active generation.
    """
    if not os.path.isdir(search_dir):
        return []

    pattern = re.compile(r"msgstore-increment-(\d+)(?:-([a-zA-Z0-9_\-\.]+?))?(?:\.db)?\.crypt\d+$")
    fallback_pattern = re.compile(r"msgstore-increment-(\d+)")

    candidates: list[tuple[int, str, str]] = []
    for entry in os.listdir(search_dir):
        if entry.endswith(".old") or entry.endswith(".tmp") or entry.endswith(".incoming"):
            continue
        m = pattern.search(entry)
        if m:
            candidates.append((int(m.group(1)), m.group(2) or "", os.path.join(search_dir, entry)))
        else:
            m_fb = fallback_pattern.search(entry)
            if m_fb:
                candidates.append((int(m_fb.group(1)), "", os.path.join(search_dir, entry)))

    if not candidates:
        return []

    seen_ids: set[int] = set()
    has_duplicate_ids = False
    for inc_id, _, _ in candidates:
        if inc_id in seen_ids:
            has_duplicate_ids = True
            break
        seen_ids.add(inc_id)

    if has_duplicate_ids:
        by_tag: dict[str, list[tuple[int, str]]] = {}
        for inc_id, tag, path in candidates:
            by_tag.setdefault(tag, []).append((inc_id, path))
        latest_tag = max(by_tag.keys())
        selected = by_tag[latest_tag]
    else:
        selected = [(inc_id, path) for inc_id, _, path in candidates]

    selected.sort(key=lambda item: item[0])
    return [path for _, path in selected]


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
    table_cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if not table_cols:
        logger.debug(
            f"Table '{table_name}' does not exist in target database; skipping modification."
        )
        return 0

    count = 0
    for row_dict in rows:
        if not isinstance(row_dict, dict):
            continue
        filtered = {k: v for k, v in row_dict.items() if k in table_cols}
        if not filtered:
            continue
        cols = list(filtered.keys())
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT OR REPLACE INTO {table_name} ({', '.join(cols)}) VALUES ({placeholders})"
        cur.execute(sql, [filtered[c] for c in cols])
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
    deleted_count = 0
    chunk_size = 500
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM {table_name} WHERE {pk_col} IN ({placeholders})", chunk)
        deleted_count += cur.rowcount
    return deleted_count


def apply_message_deletions(
    conn: sqlite3.Connection, deleted_json_path: str, logger: logging.Logger
) -> int:
    """Delete messages and their associated media records listed in deletedMessages_*.json."""
    with open(deleted_json_path, encoding="utf-8") as f:
        data = json.load(f)

    ids = data.get("deleted_ids", data) if isinstance(data, dict) else data
    if not isinstance(ids, list) or not ids:
        return 0

    cur = conn.cursor()
    total_deleted = 0
    chunk_size = 500
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        cur.execute(
            f"DELETE FROM message_media WHERE message_row_id IN ({placeholders})",
            chunk,
        )
        cur.execute(f"DELETE FROM message WHERE _id IN ({placeholders})", chunk)
        total_deleted += cur.rowcount
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

    cur = conn.cursor()
    cur.execute("SELECT _id FROM jid WHERE user = ? AND server = ?", (user, server))
    row = cur.fetchone()
    if row is not None:
        jid_id = int(row[0])
        if jid_cache is not None:
            jid_cache[jid_str] = jid_id
        return jid_id

    cols = {r[1] for r in cur.execute("PRAGMA table_info(jid)").fetchall()}
    if "raw_string" in cols:
        cur.execute(
            "INSERT INTO jid (user, server, raw_string) VALUES (?, ?, ?)",
            (user, server, jid_str),
        )
    else:
        cur.execute("INSERT INTO jid (user, server) VALUES (?, ?)", (user, server))
    jid_id = int(cur.lastrowid)
    if jid_cache is not None:
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
    cur.execute("SELECT _id FROM chat WHERE jid_row_id = ?", (jid_row_id,))
    row = cur.fetchone()
    if row is not None:
        chat_id = int(row[0])
        if chat_cache is not None:
            chat_cache[jid_row_id] = chat_id
        return chat_id

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
    """Parse messages.bin and replay records into SQLite message and message_media."""
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

    inserted = 0
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

        # Delete conflicting previous message with same (chat_row_id, key_id, from_me)
        if "chat_row_id" in msg_cols and "key_id" in msg_cols:
            cur.execute(
                "DELETE FROM message WHERE chat_row_id = ? AND key_id = ? AND from_me = ?",
                (chat_row_id, msg["key_id"], msg["from_me"]),
            )

        # Build column mapping for message insertion
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
        filtered_msg = {k: v for k, v in row_map.items() if k in msg_cols}
        cols = list(filtered_msg.keys())
        placeholders = ", ".join("?" for _ in cols)
        cur.execute(
            f"INSERT INTO message ({', '.join(cols)}) VALUES ({placeholders})",
            [filtered_msg[c] for c in cols],
        )
        msg_row_id = cur.lastrowid
        inserted += 1

        # Insert media record if present
        if msg["media_info"] and media_cols:
            media = msg["media_info"]
            file_hash_str = (
                base64.b64encode(media["file_sha256"]).decode("ascii")
                if media["file_sha256"]
                else None
            )

            # Reconstruct default expected file_path if document
            file_path = None
            if media["media_type"] == "document" and media["media_name"]:
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
            filtered_media = {k: v for k, v in media_row_map.items() if k in media_cols}
            m_cols = list(filtered_media.keys())
            m_placeholders = ", ".join("?" for _ in m_cols)
            cur.execute(
                f"INSERT INTO message_media ({', '.join(m_cols)}) VALUES ({m_placeholders})",
                [filtered_media[c] for c in m_cols],
            )

        # Insert vcard record if present
        if msg["vcard"] and has_vcard_table:
            cur.execute(
                "INSERT INTO message_vcard (message_row_id, vcard) VALUES (?, ?)",
                (msg_row_id, msg["vcard"]),
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
    jid_cache: dict[str, int] | None = None,
    chat_cache: dict[int, int] | None = None,
    schema_cache: dict[str, set[str]] | None = None,
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
                stats["messages_deleted"] += apply_message_deletions(conn, df_path, logger)

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
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA temp_store = MEMORY")

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

        # Verification
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(
                f"Database integrity check failed after merging increments: {integrity}"
            )

    logger.info(
        f"Incremental merging complete: {len(increment_paths)} increment(s) folded into {output_db_path} "
        f"(+{total_inserted} inserted, -{total_deleted} deleted)."
    )
    return total_inserted
