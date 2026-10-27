#!/usr/bin/env python3
"""
wab_viewer — Browse archived WhatsApp chats via a local Flask web UI.

Usage:
    python -m wab_viewer <output_root> [--port PORT] [--host HOST] [--no-browser] [--rescan] [--verbose]

Dependencies:
    pip install flask
"""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from shared.schema_validator import validate_viewer_schema

try:
    from flask import (
        Flask,
        Response,
        jsonify,
        make_response,
        render_template_string,
        request,
        send_from_directory,
    )
except ImportError:
    sys.exit("Flask is not installed. Run: pip install flask")

from wab_viewer.chat_viewer.template import HTML_TEMPLATE

from .system_events import (
    ALLOWED_GROUP_SYSTEM_ACTIONS,
    format_android_system_event,
    format_ios_system_event,
    format_participant_names,
)


def _get_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("whatsapp-backup-tools")
    except Exception:
        import tomllib

        root = Path(__file__).resolve().parents[1]
        return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


_CHAT_VIEWER_DIR = Path(__file__).parent / "chat_viewer"


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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(description="Browse archived WhatsApp chats")
    p.add_argument(
        "output_root", nargs="?", default=None, help="Path to the archive output directory"
    )
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    p.add_argument(
        "--no-browser", action="store_true", help="Do not open the browser automatically"
    )
    p.add_argument("--rescan", action="store_true", help="Force rebuild of the FTS index")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose HTTP request logging (default: error only)",
    )
    p.add_argument(
        "--version", action="version", version=f"WhatsApp Backup Tools — Viewer v{_get_version()}"
    )
    args = p.parse_args()
    if not args.output_root:
        p.error("output_root is required")
    return args


def resolve_browser_url(host: str, port: int) -> str:
    """Resolve the URL to open in the local browser.

    Wildcard bind addresses (0.0.0.0 and ::) bind to all network interfaces,
    which modern web browsers reject as non-routable destination addresses.
    They are replaced with loopback 127.0.0.1 for local browser launching.
    Specific hostnames and IP addresses are preserved verbatim.
    """
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{browser_host}:{port}"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def get_cache_db_path(output_root: Path) -> Path:
    return output_root / ".wa_viewer.db"


def get_archive_db_path(output_root: Path) -> Path:
    target = output_root / ".wa_media_archiver.db"
    if not target.exists() and (output_root / ".wa_archiver.db").exists():
        return output_root / ".wa_archiver.db"
    return target


def _ensure_writable(path: Path) -> None:
    """Clear the read-only file attribute if present so SQLite can write to it."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if not p.exists():
            continue
        try:
            if sys.platform == "win32":
                import ctypes

                attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
                if attrs != -1 and (attrs & 1):  # FILE_ATTRIBUTE_READONLY
                    ctypes.windll.kernel32.SetFileAttributesW(str(p), attrs & ~1)
            mode = p.stat().st_mode
            if not (mode & stat.S_IWRITE):
                p.chmod(mode | stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache schema
# ---------------------------------------------------------------------------

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_index (
    rowid        INTEGER PRIMARY KEY,
    chat_id      TEXT NOT NULL,
    chat_type    TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_midx_chat_ts ON message_index(chat_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_midx_chat_type_ts ON message_index(chat_id, chat_type, timestamp_ms);

CREATE VIRTUAL TABLE IF NOT EXISTS message_index_fts USING fts5(
    text_body,
    content=''
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS indexed_chats (
    chat_id   TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    PRIMARY KEY (chat_id, chat_type)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

VALID_PREF_VALUES = {
    "theme": {"dark", "light"},
    "date_format": {"DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"},
    "font_size": {"small", "medium", "large"},
}

GALLERY_PAGE_SIZE = 100

_MEDIA_TYPE_EXPR = """
    CASE
        WHEN {col} IS NULL THEN 'text'
        WHEN LOWER({col}) LIKE '%.vcf' THEN 'vcard'
        WHEN LOWER({col}) LIKE '%sticker%' THEN 'sticker'
        WHEN LOWER({col}) LIKE '%.jpg'  OR LOWER({col}) LIKE '%.jpeg'
          OR LOWER({col}) LIKE '%.png'  OR LOWER({col}) LIKE '%.webp'
          OR LOWER({col}) LIKE '%.heic' OR LOWER({col}) LIKE '%.heif' THEN 'image'
        WHEN LOWER({col}) LIKE '%.mp4' OR LOWER({col}) LIKE '%.mov'
          OR LOWER({col}) LIKE '%.avi' OR LOWER({col}) LIKE '%.mkv'
          OR LOWER({col}) LIKE '%.3gp' THEN 'video'
        WHEN LOWER({col}) LIKE '%.mp3'  OR LOWER({col}) LIKE '%.ogg'
          OR LOWER({col}) LIKE '%.aac'  OR LOWER({col}) LIKE '%.opus'
          OR LOWER({col}) LIKE '%.m4a' THEN 'audio'
        WHEN LOWER({col}) LIKE '%.gif' THEN 'gif'
        ELSE 'document'
    END
"""


def _ensure_wa_indexes(wa_db_path: Path) -> None:
    try:
        conn = sqlite3.connect(str(wa_db_path))
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            print(f"[wab_viewer] Notice: Source DB is read-only; skipping index optimization: {e}")
            return
        raise

    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as e:
            if "readonly" in str(e).lower():
                print(
                    f"[wab_viewer] Notice: Source DB is read-only; skipping index optimization: {e}"
                )
                return
            raise

        existing = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        if "idx_message_chat_ts" not in existing:
            has_android = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
            ).fetchone()
            if has_android:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_message_chat_ts "
                    "ON message(chat_row_id, timestamp)"
                )
        if "idx_poll_option_msg" not in existing:
            has_poll_opt = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_poll_option'"
            ).fetchone()
            if has_poll_opt:
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_poll_option)").fetchall()
                }
                if "message_row_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_poll_option_msg "
                        "ON message_poll_option(message_row_id)"
                    )
        if "idx_message_add_on_parent" not in existing:
            has_add_on = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_add_on'"
            ).fetchone()
            if has_add_on:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()}
                if "parent_message_row_id" in cols and "message_add_on_type" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_add_on_parent "
                        "ON message_add_on(parent_message_row_id, message_add_on_type)"
                    )
        if "idx_zwamessage_chat_ts" not in existing:
            has_ios = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
            ).fetchone()
            if has_ios:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_zwamessage_chat_ts "
                    "ON ZWAMESSAGE(ZCHATSESSION, ZMESSAGEDATE)"
                )
        if "idx_message_key_id" not in existing:
            has_android = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
            ).fetchone()
            if has_android:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(message)").fetchall()}
                if "key_id" in cols:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_key_id ON message(key_id)")
        if "idx_pres_cur_key_id" in existing:
            conn.execute("DROP INDEX IF EXISTS idx_pres_cur_key_id")
        if "idx_message_quoted_key_id" not in existing:
            has_mq = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_quoted'"
            ).fetchone()
            if has_mq:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(message_quoted)").fetchall()}
                if "key_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_quoted_key_id "
                        "ON message_quoted(key_id)"
                    )
        if "idx_message_quoted_media_row_id" not in existing:
            has_mqm = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_quoted_media'"
            ).fetchone()
            if has_mqm:
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_quoted_media)").fetchall()
                }
                if "message_row_id" in cols:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_quoted_media_row_id "
                        "ON message_quoted_media(message_row_id)"
                    )

        # Database self-healing for Android: sanitize empty phantom messages and re-hide empty shadow chats
        has_android_msg = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
        ).fetchone()
        has_android_chat = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'"
        ).fetchone()
        if has_android_msg and has_android_chat:
            try:
                has_mm = bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media'"
                    ).fetchone()
                )
                has_mv = bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_vcard'"
                    ).fetchone()
                )
                del_clauses = ["message_type = 0", "(text_data IS NULL OR text_data = '')"]
                if has_mm:
                    del_clauses.append("_id NOT IN (SELECT message_row_id FROM message_media)")
                if has_mv:
                    del_clauses.append("_id NOT IN (SELECT message_row_id FROM message_vcard)")
                conn.execute(f"DELETE FROM message WHERE {' AND '.join(del_clauses)}")
                chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat)").fetchall()}
                if "hidden" in chat_cols:
                    conn.execute("""
                        UPDATE chat
                        SET hidden = 1
                        WHERE (hidden = 0 OR hidden IS NULL)
                          AND (subject IS NULL OR subject = '')
                          AND jid_row_id NOT IN (
                              SELECT _id FROM jid WHERE server = 'g.us' OR raw_string LIKE '%@g.us'
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM message m
                              WHERE m.chat_row_id = chat._id
                                AND (
                                    (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
                                    OR m.message_type IN (1, 2, 3, 4, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82)
                                )
                          )
                    """)
                if "display_message_row_id" in chat_cols:
                    conn.execute("""
                        UPDATE chat
                        SET display_message_row_id = NULL
                        WHERE display_message_row_id IS NOT NULL
                          AND display_message_row_id NOT IN (SELECT _id FROM message)
                    """)
            except sqlite3.OperationalError:
                pass

        conn.commit()
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            print(f"[wab_viewer] Notice: Source DB is read-only; skipping index optimization: {e}")
            return
        raise
    finally:
        conn.close()


def _strip_none_reactions(rows: list) -> list:
    result = []
    for r in rows:
        d = dict(r)
        if d.get("reactions") is None:
            d.pop("reactions", None)
        result.append(d)
    return result


def _resolve_ios_jid(jid_str: str, contacts_map: dict) -> str:
    """Resolve an iOS JID (phone or @lid) to a contact name, phone number, or Unknown."""
    if not jid_str:
        return "Someone"
    jid_str = jid_str.strip()
    if jid_str in ("0@s.whatsapp.net", "0"):
        return "WhatsApp"
    if jid_str in contacts_map:
        full_name, phone_number = contacts_map[jid_str]
        if full_name:
            return full_name
        if phone_number:
            return f"+{phone_number}"
    if not jid_str.endswith("@s.whatsapp.net") and not jid_str.endswith("@lid"):
        alt_s = f"{jid_str}@s.whatsapp.net"
        if alt_s in contacts_map:
            fn, pn = contacts_map[alt_s]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
        alt_lid = f"{jid_str}@lid"
        if alt_lid in contacts_map:
            fn, pn = contacts_map[alt_lid]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
    elif jid_str.endswith("@s.whatsapp.net"):
        bare = jid_str.split("@")[0]
        if bare in contacts_map:
            fn, pn = contacts_map[bare]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
    elif jid_str.endswith("@lid"):
        bare = jid_str.split("@")[0]
        if bare in contacts_map:
            fn, pn = contacts_map[bare]
            if fn:
                return fn
            if pn:
                return f"+{pn}"
    if jid_str.endswith("@s.whatsapp.net"):
        num = jid_str.split("@")[0]
        return f"+{num}"
    if jid_str.endswith("@lid"):
        return "Unknown"
    if jid_str.isdigit():
        return f"+{jid_str}"
    return jid_str


def _format_ios_service_row(row: dict, contacts_map: dict) -> str:
    """Format an iOS service event row into a friendly human-readable string."""
    ev = row.get("group_event_type")
    txt = (row.get("text_body") or "").strip()
    member_jid = (row.get("member_jid") or "").strip()
    from_me = row.get("from_me", 0)
    sender = row.get("sender") or ""
    is_from_me = from_me == 1

    actor = None
    targets: list[str] = []

    if ev == 12:  # Group created
        subj = txt
        if txt.startswith("{"):
            try:
                data = json.loads(txt)
                subj = data.get("subject", "")
                author_jid = data.get("author")
                if author_jid:
                    actor = _resolve_ios_jid(author_jid, contacts_map)
            except (json.JSONDecodeError, AttributeError):
                pass
        if not actor:
            if from_me == 1:
                actor = "You"
            elif member_jid:
                actor = _resolve_ios_jid(member_jid, contacts_map)
            elif sender and sender != "Someone":
                actor = sender
            else:
                actor = "Someone"
        return format_ios_system_event(ev, actor, [], subj, is_from_me)

    if ev in (1, 3, 10):  # Subject, icon, or description changed
        if row.get("chat_type") == "contact":
            return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."
        actor = (
            "You"
            if from_me == 1
            else (
                _resolve_ios_jid(member_jid, contacts_map) if member_jid else (sender or "Someone")
            )
        )
        return format_ios_system_event(ev, actor, [], txt, is_from_me)

    if ev in (4, 15):  # Left or joined via invite link
        target = (
            _resolve_ios_jid(member_jid, contacts_map)
            if member_jid
            else ("You" if from_me == 1 else "A participant")
        )
        return format_ios_system_event(ev, sender or "Someone", [target], txt, is_from_me)

    if ev == 2:  # Participant added or joined / E2E notice for 1:1 contact chats
        if row.get("chat_type") == "contact" or (not member_jid and not txt):
            return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."
        if member_jid:
            actor = (
                "You" if from_me == 1 else (_resolve_ios_jid(txt, contacts_map) if txt else None)
            )
            target = _resolve_ios_jid(member_jid, contacts_map)
        elif from_me == 1:
            actor = "You"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        elif sender == "WhatsApp":
            actor = "WhatsApp"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        else:
            actor = _resolve_ios_jid(txt, contacts_map) if txt else None
            target = "you"
        return format_ios_system_event(ev, actor or "Someone", [target], txt, is_from_me)

    if ev in (21, 50):  # Multiple participants added or shared via contact
        if ";" in txt:
            actor_part, targets_part = txt.split(";", 1)
            actor = "You" if from_me == 1 else _resolve_ios_jid(actor_part.strip(), contacts_map)
            target_jids = [j.strip() for j in targets_part.split(",") if j.strip()]
        else:
            actor = "You" if from_me == 1 else None
            target_jids = [j.strip() for j in txt.split(",") if j.strip()]
        targets = [_resolve_ios_jid(j, contacts_map) for j in target_jids]
        return format_ios_system_event(ev, actor or "Someone", targets, txt, is_from_me)

    if ev == 7:  # Participant removed
        if member_jid:
            actor = (
                "You" if from_me == 1 else (_resolve_ios_jid(txt, contacts_map) if txt else None)
            )
            target = _resolve_ios_jid(member_jid, contacts_map)
        elif from_me == 1:
            actor = "You"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        elif sender == "WhatsApp":
            actor = "WhatsApp"
            target = _resolve_ios_jid(txt, contacts_map) if txt else "someone"
        else:
            actor = _resolve_ios_jid(txt, contacts_map) if txt else None
            target = "you"
        return format_ios_system_event(ev, actor or "Someone", [target], txt, is_from_me)

    if ev in (5, 9):  # Admin changed
        target = (
            _resolve_ios_jid(member_jid, contacts_map)
            if member_jid
            else (_resolve_ios_jid(txt, contacts_map) if txt else "You")
        )
        return format_ios_system_event(ev, sender or "Someone", [target], txt, is_from_me)

    if ev == 6:  # Phone number changed
        target_phone = _resolve_ios_jid(txt, contacts_map) if txt else ""
        return format_ios_system_event(ev, sender or "A contact", [], target_phone, is_from_me)

    if ev == 56:  # Security code changed
        contact = sender or (
            _resolve_ios_jid(member_jid, contacts_map) if member_jid else "this contact"
        )
        return format_ios_system_event(ev, contact, [], txt, is_from_me)

    if ev in (26, 36, 37, 38, 58):  # Settings, approvals, and announcements
        actor_name = (
            "You"
            if from_me == 1
            else (
                _resolve_ios_jid(member_jid, contacts_map) if member_jid else (sender or "Someone")
            )
        )
        return format_ios_system_event(ev, actor_name, [], txt, is_from_me)

    if txt and not txt.startswith("{") and "@lid" not in txt and not txt.isdigit():
        return txt
    return "Group event"


def _format_android_service_row(row: dict) -> str:
    """Format an Android service event row into a friendly human-readable string."""
    act = row.get("action_type")
    txt = (row.get("text_body") or "").strip()
    from_me = row.get("from_me")
    sender = row.get("sender")
    part_name = row.get("participant_name")
    targets = row.get("participant_names")
    if targets is None:
        targets = [part_name] if part_name else []

    # Guard: In a 1-to-1 chat, group actions must never emit group labels
    if row.get("chat_type") == "contact" and act in ALLOWED_GROUP_SYSTEM_ACTIONS:
        if act == 1:
            return "You deleted this message" if from_me == 1 else "This message was deleted"
        return "Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them."

    actor = sender if sender else ("You" if from_me == 1 else "Someone")
    if not targets and act in (4, 5, 13, 79, 123):
        targets = [sender] if sender else (["You"] if from_me == 1 else [])

    metadata = row.get("service_metadata") or {}
    return format_android_system_event(act, actor, targets, txt, metadata)


def _hydrate_android_service_participants(
    conn: sqlite3.Connection,
    msg_ids: list,
    has_mcp: bool,
    has_lid_dn: bool,
    has_jid_server: bool,
    has_jid_raw_string: bool,
) -> dict[int, list[str]]:
    """Hydrate participant names list for Android service messages on demand."""
    if not has_mcp or not msg_ids:
        return {}
    ph = ",".join("?" * len(msg_ids))
    ldn_col = "ldn.display_name AS lid_name," if has_lid_dn else "NULL AS lid_name,"
    ldn_join = (
        "LEFT JOIN lid_display_name ldn ON ldn.lid_row_id = mcp.user_jid_row_id"
        if has_lid_dn
        else ""
    )
    server_col = "j_part_raw.server AS raw_server," if has_jid_server else "NULL AS raw_server,"
    raw_str_col = (
        "j_part_raw.raw_string AS raw_string," if has_jid_raw_string else "NULL AS raw_string,"
    )
    is_lid_pred = "j_part_raw.server = 'lid'" if has_jid_server else "0"

    sql = f"""
        SELECT
            mcp.message_row_id,
            j_part_raw.user AS raw_user,
            {server_col}
            {raw_str_col}
            j_part_real.user AS real_user,
            con_part.display_name AS contact_name,
            {ldn_col}
            con_part.number AS contact_num
        FROM message_system_chat_participant mcp
        LEFT JOIN _jid_map_resolved jm_part ON jm_part.lid_row_id = mcp.user_jid_row_id
        LEFT JOIN jid j_part_real ON j_part_real._id = jm_part.jid_row_id
        LEFT JOIN jid j_part_raw ON j_part_raw._id = mcp.user_jid_row_id
        {ldn_join}
        LEFT JOIN arch.contacts con_part ON con_part.number = COALESCE(j_part_real.user, CASE WHEN NOT ({is_lid_pred}) THEN j_part_raw.user END)
        WHERE mcp.message_row_id IN ({ph})
    """
    try:
        p_rows = conn.execute(sql, msg_ids).fetchall()
    except sqlite3.OperationalError:
        return {}

    p_map: dict[int, list[str]] = {}
    for pr in p_rows:
        mid = pr["message_row_id"]
        raw_str = pr["raw_string"] or ""
        raw_u = pr["raw_user"] or ""
        raw_s = pr["raw_server"] or ""
        real_u = pr["real_user"] or ""
        c_name = pr["contact_name"] or ""
        lid_n = pr["lid_name"] or ""

        if raw_str == "lid_me" or raw_u == "me":
            name = "You"
        elif c_name:
            name = c_name
        elif (real_u or (raw_s != "lid" and raw_u)) == "0":
            name = "WhatsApp"
        elif real_u:
            name = f"+{real_u}"
        elif lid_n:
            name = lid_n
        elif raw_s != "lid" and raw_u:
            name = f"+{raw_u}"
        else:
            name = "Unknown"

        p_map.setdefault(mid, []).append(name)

    return p_map


def _format_service_rows(
    rows: list,
    source_type: str,
    conn: sqlite3.Connection,
    has_mcp: bool,
    has_lid_dn: bool,
    has_jid_server: bool,
    has_jid_raw_string: bool,
) -> None:
    """Format in-place text_body for all service event rows in a result set."""
    service_rows = [r for r in rows if r.get("media_type") == "service"]
    if not service_rows:
        return

    if source_type == "ios":
        needed_jids = set()
        for r in service_rows:
            m_jid = (r.get("member_jid") or "").strip()
            if m_jid:
                needed_jids.add(m_jid)
                if not m_jid.endswith("@s.whatsapp.net") and not m_jid.endswith("@lid"):
                    needed_jids.add(f"{m_jid}@s.whatsapp.net")
                    needed_jids.add(f"{m_jid}@lid")
                elif m_jid.endswith("@s.whatsapp.net"):
                    needed_jids.add(m_jid.split("@")[0])
            txt = (r.get("text_body") or "").strip()
            if txt.startswith("{"):
                try:
                    data = json.loads(txt)
                    author = data.get("author")
                    if author:
                        needed_jids.add(author)
                        if not author.endswith("@s.whatsapp.net") and not author.endswith("@lid"):
                            needed_jids.add(f"{author}@s.whatsapp.net")
                            needed_jids.add(f"{author}@lid")
                        elif author.endswith("@s.whatsapp.net"):
                            needed_jids.add(author.split("@")[0])
                except (json.JSONDecodeError, AttributeError):
                    pass
            for part in re.split(r"[;,]", txt):
                part = part.strip()
                if part:
                    needed_jids.add(part)
                    if not part.endswith("@s.whatsapp.net") and not part.endswith("@lid"):
                        needed_jids.add(f"{part}@s.whatsapp.net")
                        needed_jids.add(f"{part}@lid")
                    elif part.endswith("@s.whatsapp.net"):
                        needed_jids.add(part.split("@")[0])

        contacts_map = {}
        if needed_jids:
            ph = ",".join("?" * len(needed_jids))
            try:
                c_rows = conn.execute(
                    f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
                    list(needed_jids),
                ).fetchall()
                for cr in c_rows:
                    contacts_map[cr["jid"]] = (cr["full_name"], cr["phone_number"])
            except sqlite3.OperationalError:
                pass
            try:
                arch_rows = conn.execute(
                    f"SELECT number, display_name FROM arch.contacts WHERE number IN ({ph})",
                    list(needed_jids),
                ).fetchall()
                for ar in arch_rows:
                    if ar["display_name"]:
                        contacts_map[ar["number"]] = (ar["display_name"], ar["number"])
                        contacts_map[f"{ar['number']}@s.whatsapp.net"] = (
                            ar["display_name"],
                            ar["number"],
                        )
            except sqlite3.OperationalError:
                pass
            try:
                push_rows = conn.execute(
                    f"SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID IN ({ph})",
                    list(needed_jids),
                ).fetchall()
                for pr in push_rows:
                    if pr["ZPUSHNAME"]:
                        if pr["ZJID"] not in contacts_map:
                            contacts_map[pr["ZJID"]] = (pr["ZPUSHNAME"], None)
                        bare_p = pr["ZJID"].split("@")[0]
                        if bare_p not in contacts_map:
                            contacts_map[bare_p] = (pr["ZPUSHNAME"], None)
            except sqlite3.OperationalError:
                pass

        try:
            user_row = conn.execute(
                "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZFROMJID LIKE '%@g.us' AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
            ).fetchone()
            if not user_row:
                user_row = conn.execute(
                    "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZISFROMME = 0 AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                ).fetchone()
            if user_row and user_row[0]:
                u_jid = user_row[0]
                u_phone = u_jid.split("@")[0]
                contacts_map[u_jid] = ("You", u_phone)
                contacts_map[u_phone] = ("You", u_phone)
                contacts_map[f"+{u_phone}"] = ("You", u_phone)
        except sqlite3.OperationalError:
            pass

        for r in service_rows:
            r["text_body"] = _format_ios_service_row(r, contacts_map)

    elif source_type == "android":
        p_map = _hydrate_android_service_participants(
            conn,
            [r["msg_id"] for r in service_rows],
            has_mcp,
            has_lid_dn,
            has_jid_server,
            has_jid_raw_string,
        )
        meta_map: dict[int, dict[str, str]] = {}
        m_ids = [r["msg_id"] for r in service_rows if r.get("msg_id")]
        if m_ids:
            ph = ",".join("?" * len(m_ids))
            try:
                eph_rows = conn.execute(
                    f"SELECT message_row_id, setting_duration FROM message_ephemeral_setting WHERE message_row_id IN ({ph})",
                    m_ids,
                ).fetchall()
                for er in eph_rows:
                    meta_map.setdefault(er[0], {})["setting_duration"] = str(er[1])
            except sqlite3.OperationalError:
                pass
            try:
                priv_rows = conn.execute(
                    f"SELECT message_row_id, verified_biz_name FROM message_system_initial_privacy_provider WHERE message_row_id IN ({ph}) AND verified_biz_name IS NOT NULL",
                    m_ids,
                ).fetchall()
                for pr in priv_rows:
                    if pr[1]:
                        meta_map.setdefault(pr[0], {})["verified_biz_name"] = str(pr[1])
            except sqlite3.OperationalError:
                pass

        for r in service_rows:
            names = p_map.get(r["msg_id"], [])
            if not r.get("participant_names"):
                r["participant_names"] = names
            if not r.get("participant_name"):
                r["participant_name"] = format_participant_names(names) if names else ""
            if r.get("msg_id") in meta_map:
                r.setdefault("service_metadata", {}).update(meta_map[r["msg_id"]])
            act = r.get("service_action_type") or r.get("action_type")
            if r.get("chat_type") == "contact" and act == 1:
                r["media_type"] = "deleted"
                r["text_body"] = (
                    "You deleted this message"
                    if r.get("from_me") == 1
                    else "This message was deleted"
                )
            else:
                r["text_body"] = _format_android_service_row(r)
                if r.get("chat_type") == "contact" and r["text_body"] in (
                    "You deleted this message",
                    "This message was deleted",
                ):
                    r["media_type"] = "deleted"


# ---------------------------------------------------------------------------
# iOS reactions — ZRECEIPTINFO protobuf extraction
# ---------------------------------------------------------------------------


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


def _ios_reactions(wa_conn: sqlite3.Connection, chat_id: str, msg_ids: list) -> dict:
    """Batch-fetch iOS reactions from ZRECEIPTINFO for a list of message IDs.

    Returns a dict: {msg_id: [(sender_hex, emoji_hex), ...]}.
    Reactions from the current user are annotated with from_me=1 so the frontend can
    place them on the correct corner. "Me" is resolved from sent messages in this chat.
    """
    if not msg_ids:
        return {}

    placeholders = ",".join("?" * len(msg_ids))

    # Resolve "me" from a sent message in this chat so we can annotate reactions.
    # In group chats ZGROUPMEMBER.ZMEMBERJID is populated; in 1-to-1 chats we fall
    # back to ZWAMESSAGE.ZFROMJID.
    my_row = wa_conn.execute(
        """
        SELECT COALESCE(gm.ZMEMBERJID, m.ZFROMJID) AS me_jid
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        WHERE m.ZCHATSESSION = CAST(? AS INTEGER)
          AND m.ZISFROMME = 1
        LIMIT 1
    """,
        (chat_id,),
    ).fetchone()

    my_phone = None
    if my_row and my_row["me_jid"]:
        jid = my_row["me_jid"]
        at = jid.find("@")
        if at > 0:
            my_phone = jid[:at]

    rows = wa_conn.execute(
        f"""
        SELECT m.Z_PK AS msg_id,
               mi.ZRECEIPTINFO AS receipt_info
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAMESSAGEINFO mi ON mi.Z_PK = m.ZMESSAGEINFO
        WHERE m.Z_PK IN ({placeholders})
          AND mi.ZRECEIPTINFO IS NOT NULL
    """,
        msg_ids,
    ).fetchall()

    if not rows:
        return {}

    reactions_by_msg = {}
    for row in rows:
        raw = row["receipt_info"]
        if not raw:
            continue
        reactors = _extract_ios_reactions(bytes(raw))
        if not reactors:
            continue
        annotated = []
        for sender, emoji in reactors:
            from_me = 0
            if sender and my_phone:
                if sender == my_phone:
                    from_me = 1
            annotated.append((sender, emoji, from_me))
        reactions_by_msg[row["msg_id"]] = annotated

    return reactions_by_msg


def _resolve_and_cache_reactions(
    source_type: str, wa_conn: sqlite3.Connection, archive_conn: sqlite3.Connection, rows: list
) -> None:
    if source_type != "android" or not rows:
        return

    msg_ids = [r["msg_id"] for r in rows if r.get("msg_id") is not None]
    if not msg_ids:
        return

    placeholders = ",".join("?" * len(msg_ids))
    reaction_rows = wa_conn.execute(
        f"""
        SELECT ao.parent_message_row_id AS msg_id,
               GROUP_CONCAT(r.reaction) AS reactions
        FROM message_add_on ao
        JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
        WHERE ao.parent_message_row_id IN ({placeholders})
        GROUP BY ao.parent_message_row_id
    """,
        msg_ids,
    ).fetchall()

    reaction_map = {row["msg_id"]: row["reactions"] for row in reaction_rows}

    for row in rows:
        rx = reaction_map.get(row["msg_id"])
        row["reactions"] = rx

    cache_sql = """
        INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions)
        VALUES (?, ?, ?)
    """
    archive_conn.executemany(
        cache_sql,
        [
            (row.get("chat_id", ""), row["msg_id"], reaction_map.get(row["msg_id"], ""))
            for row in rows
            if row.get("msg_id") is not None
        ],
    )
    archive_conn.commit()


def _hydrate_poll_rows(rows: list, source_type: str, wa_conn: sqlite3.Connection) -> None:
    """Hydrate poll structured data for poll messages in rows."""
    if not rows or wa_conn is None:
        return

    if source_type == "android":
        tables = {
            r[0]
            for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        candidate_mids = [
            r["msg_id"] for r in rows if r.get("media_type") != "poll" and r.get("msg_id")
        ]
        if candidate_mids:
            c_ph = ",".join("?" * len(candidate_mids))
            db_poll_mids = set()
            if "message_poll" in tables:
                try:
                    db_poll_mids.update(
                        r[0]
                        for r in wa_conn.execute(
                            f"SELECT message_row_id FROM message_poll WHERE message_row_id IN ({c_ph})",
                            candidate_mids,
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    pass
            if "message_add_on" in tables:
                mao_cols = {
                    r[1] for r in wa_conn.execute("PRAGMA table_info(message_add_on)").fetchall()
                }
                if "message_add_on_type" in mao_cols:
                    try:
                        db_poll_mids.update(
                            r[0]
                            for r in wa_conn.execute(
                                f"SELECT parent_message_row_id FROM message_add_on WHERE parent_message_row_id IN ({c_ph}) AND message_add_on_type = 67",
                                candidate_mids,
                            ).fetchall()
                        )
                    except sqlite3.OperationalError:
                        pass
            for r in rows:
                if r.get("msg_id") in db_poll_mids:
                    r["media_type"] = "poll"

    poll_rows = [r for r in rows if r.get("media_type") == "poll"]
    if not poll_rows:
        return

    if source_type == "android":
        tables = {
            r[0]
            for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        mids = [r["msg_id"] for r in poll_rows]
        ph = ",".join("?" * len(mids))

        meta_map = {}
        if "message_poll" in tables:
            try:
                meta_rows = wa_conn.execute(
                    f"SELECT message_row_id, selectable_options_count FROM message_poll WHERE message_row_id IN ({ph})",
                    mids,
                ).fetchall()
                meta_map = {r["message_row_id"]: r["selectable_options_count"] for r in meta_rows}
            except sqlite3.OperationalError:
                pass

        opt_rows = []
        if "message_poll_option" in tables:
            try:
                opt_rows = wa_conn.execute(
                    f"SELECT _id, message_row_id, option_name, vote_total FROM message_poll_option WHERE message_row_id IN ({ph}) ORDER BY _id ASC",
                    mids,
                ).fetchall()
            except sqlite3.OperationalError:
                pass

        user_voted_options = set()
        voter_counts = {}
        addon_votes_by_opt = {}
        if "message_add_on" in tables:
            mao_cols = {
                r[1] for r in wa_conn.execute("PRAGMA table_info(message_add_on)").fetchall()
            }
            if "message_add_on_type" in mao_cols:
                try:
                    vc_rows = wa_conn.execute(
                        f"""
                        SELECT parent_message_row_id, COUNT(DISTINCT _id) AS voter_cnt
                        FROM message_add_on
                        WHERE parent_message_row_id IN ({ph}) AND message_add_on_type = 67
                        GROUP BY parent_message_row_id
                        """,
                        mids,
                    ).fetchall()
                    voter_counts = {r["parent_message_row_id"]: r["voter_cnt"] for r in vc_rows}
                except sqlite3.OperationalError:
                    pass

                if "message_add_on_poll_vote_selected_option" in tables:
                    try:
                        user_v_rows = wa_conn.execute(
                            f"""
                            SELECT ps.message_poll_option_id
                            FROM message_add_on mao
                            JOIN message_add_on_poll_vote_selected_option ps ON ps.message_add_on_row_id = mao._id
                            WHERE mao.parent_message_row_id IN ({ph}) AND mao.from_me = 1 AND mao.message_add_on_type = 67
                            """,
                            mids,
                        ).fetchall()
                        user_voted_options = {r[0] for r in user_v_rows}
                    except sqlite3.OperationalError:
                        pass

                    try:
                        opt_ids = [opt["_id"] for opt in opt_rows]
                        if opt_ids:
                            opt_ph = ",".join("?" * len(opt_ids))
                            v_cnt_rows = wa_conn.execute(
                                f"""
                                SELECT ps.message_poll_option_id, COUNT(DISTINCT ps.message_add_on_row_id)
                                FROM message_add_on_poll_vote_selected_option ps
                                WHERE ps.message_poll_option_id IN ({opt_ph})
                                GROUP BY ps.message_poll_option_id
                                """,
                                opt_ids,
                            ).fetchall()
                            addon_votes_by_opt = {r[0]: r[1] for r in v_cnt_rows}
                    except sqlite3.OperationalError:
                        pass

        options_by_mid = collections.defaultdict(list)
        for opt in opt_rows:
            v_cnt = max(opt["vote_total"] or 0, addon_votes_by_opt.get(opt["_id"], 0))
            options_by_mid[opt["message_row_id"]].append(
                {
                    "id": opt["_id"],
                    "name": opt["option_name"] or "",
                    "vote_count": v_cnt,
                    "user_selected": opt["_id"] in user_voted_options,
                }
            )

        for r in poll_rows:
            mid = r["msg_id"]
            opts = options_by_mid.get(mid, [])
            total_votes = sum(o["vote_count"] for o in opts)
            total_voters = voter_counts.get(mid, total_votes)
            question = r.get("text_body") or "Poll"
            r["text_body"] = question
            r["poll"] = {
                "question": question,
                "multiple_answers": (meta_map.get(mid, 0) == 0),
                "total_votes": total_votes,
                "total_voters": total_voters,
                "options": opts,
            }

    elif source_type == "ios":
        tables = {
            r[0]
            for r in wa_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "ZWAMESSAGEINFO" not in tables:
            return
        mids = [r["msg_id"] for r in poll_rows]
        ph = ",".join("?" * len(mids))
        info_rows = wa_conn.execute(
            f"SELECT ZMESSAGE, ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZMESSAGE IN ({ph}) AND ZRECEIPTINFO IS NOT NULL",
            mids,
        ).fetchall()
        info_map = {r["ZMESSAGE"]: r["ZRECEIPTINFO"] for r in info_rows}

        for r in poll_rows:
            mid = r["msg_id"]
            blob = info_map.get(mid)
            if not blob:
                r["poll"] = None
                continue
            parsed = _parse_ios_poll_blob(bytes(blob))
            if not parsed:
                r["poll"] = None
                continue
            votes = parsed["votes"]
            opts = []
            for idx, opt_name in enumerate(parsed["options"]):
                cnt = sum(1 for v in votes if idx in v["option_indices"])
                user_selected = any(
                    idx in v["option_indices"]
                    for v in votes
                    if not v["voter_jid"] or v["voter_jid"] == "You"
                )
                opts.append(
                    {
                        "id": idx,
                        "name": opt_name,
                        "vote_count": cnt,
                        "user_selected": user_selected,
                    }
                )
            total_votes = sum(o["vote_count"] for o in opts)
            r["text_body"] = parsed["question"]
            r["poll"] = {
                "question": parsed["question"],
                "multiple_answers": parsed["multiple_answers"],
                "total_votes": total_votes,
                "total_voters": len(votes),
                "options": opts,
            }


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


def _hydrate_quoted_rows(
    rows: list[dict],
    source_type: str,
    wa_conn: sqlite3.Connection | None,
) -> None:
    """Hydrate quoted message fields on iOS rows from embedded ZMETADATA blobs."""
    if not rows or source_type != "ios":
        return

    extracted_senders = {}
    for row in rows:
        if row.get("quoted_stanza_id"):
            continue
        raw_meta = row.get("_raw_metadata")
        if not raw_meta:
            continue
        q_info = _extract_ios_quoted_info(raw_meta)
        if q_info:
            row["quoted_stanza_id"] = q_info["quoted_stanza_id"]
            if not row.get("quoted_text"):
                row["quoted_text"] = q_info["quoted_text"]
            if q_info.get("quoted_has_thumb"):
                row["quoted_has_thumb"] = 1
            row["link_title"] = None
            row["link_url"] = None
            row["link_desc"] = None
            if row.get("media_type") == "text":
                row["has_thumbnail"] = 0

            sender_jid = q_info.get("quoted_sender")
            if sender_jid and not row.get("quoted_sender"):
                extracted_senders[row["msg_id"]] = sender_jid

    if extracted_senders and wa_conn is not None:
        jids = list(set(extracted_senders.values()))
        ph = ",".join("?" * len(jids))
        contact_map = {}
        try:
            c_rows = wa_conn.execute(
                f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
                jids,
            ).fetchall()
            for cr in c_rows:
                name = cr["full_name"] or cr["phone_number"]
                if name:
                    contact_map[cr["jid"]] = name
        except sqlite3.OperationalError:
            pass

        for row in rows:
            mid = row.get("msg_id")
            if mid in extracted_senders:
                raw_s = extracted_senders[mid]
                if raw_s in contact_map:
                    row["quoted_sender"] = contact_map[raw_s]
                else:
                    phone = raw_s.split("@")[0] if "@" in raw_s else raw_s
                    row["quoted_sender"] = f"+{phone}" if phone.isdigit() else phone


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


def _hydrate_link_preview_rows(rows: list[dict], source_type: str) -> None:
    """Hydrate link preview fields across Android and iOS rows."""
    if not rows:
        return
    for row in rows:
        raw_meta = row.pop("_raw_metadata", None)
        if source_type == "ios" and raw_meta and not row.get("link_desc"):
            if (
                row.get("media_type") == "link"
                or row.get("raw_msg_type") == 7
                or row.get("link_url")
                or row.get("link_title")
            ):
                desc = _extract_ios_link_description(raw_meta)
                if desc:
                    row["link_desc"] = desc
        if not row.get("link_url") and (row.get("link_title") or row.get("media_type") == "link"):
            m = re.search(r"https?://[^\s<>\"']+", row.get("text_body") or "")
            if m:
                row["link_url"] = m.group(0).rstrip(".,;:!?)'\"")


def _backfill_recent_messages(archive_target: sqlite3.Connection | Path, rows: list) -> None:
    if not rows or not archive_target:
        return
    conn = None
    should_close = False
    try:
        if isinstance(archive_target, (str, Path)):
            p = Path(archive_target)
            if not p.exists():
                return
            conn = sqlite3.connect(str(p), timeout=10.0)
            conn.row_factory = sqlite3.Row
            should_close = True
        else:
            conn = archive_target
        cur_cols = {r[1] for r in conn.execute("PRAGMA table_info(recent_messages)").fetchall()}
        has_qmid = "quoted_msg_id" in cur_cols
        has_lp = "link_title" in cur_cols and "has_thumbnail" in cur_cols
        has_qthumb = "quoted_has_thumb" in cur_cols
        if has_lp:
            qthumb_col = ", quoted_has_thumb" if has_qthumb else ""
            qthumb_val = ", ?" if has_qthumb else ""
            sql = f"""
                INSERT OR REPLACE INTO recent_messages
                    (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me,
                     archive_path, media_type, media_name, text_body,
                     quoted_text, quoted_sender, quoted_ts, quoted_msg_id{qthumb_col}, reactions,
                     is_edited, edited_ts, is_view_once, link_title, link_desc, link_url,
                     link_preview_type, has_thumbnail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{qthumb_val}, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        elif has_qmid:
            qthumb_col = ", quoted_has_thumb" if has_qthumb else ""
            qthumb_val = ", ?" if has_qthumb else ""
            sql = f"""
                INSERT OR REPLACE INTO recent_messages
                    (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me,
                     archive_path, media_type, media_name, text_body,
                     quoted_text, quoted_sender, quoted_ts, quoted_msg_id{qthumb_col}, reactions,
                     is_edited, edited_ts, is_view_once)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{qthumb_val}, ?, ?, ?, ?)
            """
        else:
            sql = """
                INSERT OR REPLACE INTO recent_messages
                    (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me,
                     archive_path, media_type, media_name, text_body,
                     quoted_text, quoted_sender, quoted_ts, reactions,
                     is_edited, edited_ts, is_view_once)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        keys = rows[0].keys()
        params = []
        for r in rows:
            row_vals = [
                r["chat_id"],
                r["chat_type"],
                r["msg_id"],
                r["timestamp_ms"],
                r["sender"],
                r["from_me"],
                r["archive_path"] if "archive_path" in keys else None,
                r["media_type"],
                r["media_name"] if "media_name" in keys else None,
                r["text_body"],
                r["quoted_text"] if "quoted_text" in keys else None,
                r["quoted_sender"] if "quoted_sender" in keys else None,
                r["quoted_ts"] if "quoted_ts" in keys else None,
            ]
            if has_qmid or has_lp:
                row_vals.append(r["quoted_msg_id"] if "quoted_msg_id" in keys else None)
            if has_qthumb and (has_qmid or has_lp):
                row_vals.append(r.get("quoted_has_thumb", 0) or 0)
            row_vals.extend(
                [
                    r["reactions"] if "reactions" in keys else None,
                    r["is_edited"] if "is_edited" in keys else 0,
                    r["edited_ts"] if "edited_ts" in keys else None,
                    r["is_view_once"] if "is_view_once" in keys else 0,
                ]
            )
            if has_lp:
                row_vals.extend(
                    [
                        r["link_title"] if "link_title" in keys else None,
                        r["link_desc"] if "link_desc" in keys else None,
                        r["link_url"] if "link_url" in keys else None,
                        r["link_preview_type"] if "link_preview_type" in keys else 0,
                        r["has_thumbnail"] if "has_thumbnail" in keys else 0,
                    ]
                )
            params.append(tuple(row_vals))
        conn.executemany(sql, params)
        conn.commit()
    except Exception:
        pass
    finally:
        if should_close and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _media_type_from_path(path: str) -> str:
    if path is None:
        return "text"
    p = path.lower()
    if p.endswith(".vcf"):
        return "vcard"
    if (
        "sticker" in p
        or "stickers" in p
        or (os.path.basename(p).startswith("stk-") and (p.endswith(".webp") or p.endswith(".was")))
        or p.endswith(".webp")
        or p.endswith(".was")
    ):
        return "sticker"
    ext = os.path.splitext(p)[1]
    if ext in (".jpg", ".jpeg", ".png", ".heic", ".heif"):
        return "image"
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp"):
        return "video"
    if ext in (".mp3", ".ogg", ".aac", ".opus", ".m4a"):
        return "audio"
    if ext == ".gif":
        return "gif"
    return "document"


def _open_cache_db(output_root: Path) -> sqlite3.Connection:
    cache_path = get_cache_db_path(output_root)
    _ensure_writable(cache_path)
    try:
        conn = sqlite3.connect(str(cache_path), check_same_thread=False)
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -8000")
        conn.executescript(CACHE_SCHEMA)
        conn.execute("SELECT 1 FROM message_index_fts LIMIT 0")
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.execute("PRAGMA user_version = 1")
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('theme', 'dark')")
        conn.execute(
            "INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('date_format', 'DD/MM/YYYY')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('font_size', 'medium')"
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            raise sqlite3.OperationalError(
                f"Cannot open cache database at '{cache_path}' for writing ({e}). "
                "Ensure the archive folder and its database files are not marked read-only."
            ) from e
        if "contentless_delete" in str(e).lower():
            try:
                conn.close()
            except Exception:
                pass
            for p in (
                cache_path,
                cache_path.with_name(cache_path.name + "-wal"),
                cache_path.with_name(cache_path.name + "-shm"),
            ):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            return _open_cache_db(output_root)
        raise
    return conn


def _run_db_maintenance(
    cache_conn: sqlite3.Connection, archive_conn: sqlite3.Connection | None
) -> None:
    """Run lightweight non-blocking maintenance on cache and archive databases."""
    try:
        cache_conn.execute("PRAGMA incremental_vacuum(500)")
        cache_conn.execute("PRAGMA optimize")
        cache_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception as e:
        print(f"[wab_viewer] Notice: Cache DB maintenance: {e}")

    if archive_conn is not None:
        try:
            archive_conn.execute("PRAGMA incremental_vacuum(500)")
            archive_conn.execute("PRAGMA optimize")
            archive_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass


def _detect_source_db(output_root: Path):
    wa_db_dir = output_root / "Whatsapp Databases"
    for base in (wa_db_dir, output_root):
        msgstore = base / "msgstore.db"
        chat_storage = base / "ChatStorage.sqlite"
        if msgstore.exists():
            return ("android", str(msgstore))
        if chat_storage.exists():
            return ("ios", str(chat_storage))
    return (None, None)


def _detect_preserved_db(output_root: Path) -> Path | None:
    wa_db_dir = output_root / "Whatsapp Databases"
    for base in (wa_db_dir, output_root):
        cand = base / "preserved_messages.db"
        if cand.exists():
            return cand
    return None


def _fts_preserved_chat(
    pres_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    on_progress,
) -> None:
    rows = pres_conn.execute(
        """
        SELECT rowid, chat_id, chat_type, timestamp_ms, text_body, media_type, archive_path
        FROM preserved_messages
        WHERE chat_id = ? AND chat_type = ?
        """,
        (chat_id, chat_type),
    ).fetchall()
    idx_batch = []
    fts_batch = []
    for r in rows:
        rowid = -int(r["rowid"])
        text_body = r["text_body"] or ""
        is_media = bool(r["archive_path"] or r["media_type"] not in ("text", "service"))
        if not text_body and not is_media:
            continue
        idx_batch.append((rowid, r["chat_id"], r["chat_type"], r["timestamp_ms"]))
        if text_body:
            fts_batch.append((rowid, text_body))
    if idx_batch:
        cache_conn.executemany(
            "INSERT OR IGNORE INTO message_index (rowid, chat_id, chat_type, timestamp_ms) VALUES (?, ?, ?, ?)",
            idx_batch,
        )
    if fts_batch:
        cache_conn.executemany(
            "INSERT OR IGNORE INTO message_index_fts (rowid, text_body) VALUES (?, ?)",
            fts_batch,
        )
    cache_conn.commit()
    if on_progress and idx_batch:
        on_progress(len(idx_batch))


def _query_preserved_messages(
    pres_conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    before: int | None,
    after: int | None,
    limit: int,
) -> list[dict]:
    if before is not None:
        sql = """
            SELECT rowid, * FROM preserved_messages
            WHERE chat_id = ? AND chat_type = ? AND timestamp_ms < ?
            ORDER BY timestamp_ms DESC LIMIT ?
        """
        rows = pres_conn.execute(sql, (chat_id, chat_type, before, limit)).fetchall()
    elif after is not None:
        sql = """
            SELECT rowid, * FROM preserved_messages
            WHERE chat_id = ? AND chat_type = ? AND timestamp_ms > ?
            ORDER BY timestamp_ms ASC LIMIT ?
        """
        rows = pres_conn.execute(sql, (chat_id, chat_type, after, limit)).fetchall()
    else:
        sql = """
            SELECT rowid, * FROM preserved_messages
            WHERE chat_id = ? AND chat_type = ?
            ORDER BY timestamp_ms DESC LIMIT ?
        """
        rows = pres_conn.execute(sql, (chat_id, chat_type, limit)).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["msg_id"] = -int(d["rowid"])
        d["sort_id"] = -int(d["rowid"])
        d["is_preserved"] = 1
        d["is_view_once"] = int(d.get("is_view_once") or 0)
        d["is_edited"] = int(d.get("is_edited") or 0)
        d["quoted_msg_id"] = None
        if d.get("poll_data"):
            try:
                p = (
                    json.loads(d["poll_data"])
                    if isinstance(d["poll_data"], str)
                    else d["poll_data"]
                )
                if isinstance(p, dict):
                    d["poll"] = p
            except Exception:
                pass
        d["link_title"] = None
        d["link_desc"] = None
        d["link_url"] = None
        d["link_preview_type"] = 0
        if d.get("service_metadata"):
            try:
                sm = (
                    json.loads(d["service_metadata"])
                    if isinstance(d["service_metadata"], str)
                    else d["service_metadata"]
                )
                if isinstance(sm, dict) and "link_preview" in sm:
                    lp = sm["link_preview"]
                    d["link_title"] = lp.get("title")
                    d["link_desc"] = lp.get("desc")
                    d["link_url"] = lp.get("url")
                    d["link_preview_type"] = int(lp.get("preview_type", 0) or 0)
            except Exception:
                pass
        d.pop("rowid", None)
        d.pop("action_type", None)
        d.pop("participant_name", None)
        result.append(d)
    return result


def _resolve_quoted_msg_ids(
    rows: list[dict],
    source_type: str,
    wa_conn: sqlite3.Connection | None,
    pres_conn: sqlite3.Connection | None,
) -> None:
    if not rows:
        return

    try:
        unresolved = [
            r for r in rows if r.get("quoted_msg_id") is None and r.get("quoted_stanza_id")
        ]
        if not unresolved:
            return

        local_map = {
            r.get("stanza_id"): (r["msg_id"], r.get("timestamp_ms", 0))
            for r in rows
            if r.get("stanza_id") and r.get("msg_id") is not None
        }
        still_needed = set()
        for r in unresolved:
            q_stanza = r["quoted_stanza_id"]
            if q_stanza in local_map:
                r["quoted_msg_id"], loc_ts = local_map[q_stanza]
                if not r.get("quoted_ts") and loc_ts:
                    r["quoted_ts"] = loc_ts
            else:
                still_needed.add(q_stanza)

        if not still_needed:
            return

        if pres_conn is not None:
            ph = ",".join("?" * len(still_needed))
            try:
                pres_matches = pres_conn.execute(
                    f"SELECT stanza_id, -rowid AS pres_id FROM preserved_messages WHERE stanza_id IN ({ph})",
                    list(still_needed),
                ).fetchall()
                pres_map = {m["stanza_id"]: m["pres_id"] for m in pres_matches}
                for r in unresolved:
                    if r.get("quoted_msg_id") is None and r["quoted_stanza_id"] in pres_map:
                        r["quoted_msg_id"] = pres_map[r["quoted_stanza_id"]]
                        still_needed.discard(r["quoted_stanza_id"])
            except sqlite3.OperationalError:
                pass

        if not still_needed or wa_conn is None:
            return

        ph = ",".join("?" * len(still_needed))
        try:
            if source_type == "android":
                live_matches = wa_conn.execute(
                    f"SELECT key_id AS stanza_id, _id AS live_id, timestamp FROM message WHERE key_id IN ({ph})",
                    list(still_needed),
                ).fetchall()
                live_map = {m["stanza_id"]: (m["live_id"], m["timestamp"]) for m in live_matches}
            else:
                live_matches = wa_conn.execute(
                    f"SELECT ZSTANZAID AS stanza_id, Z_PK AS live_id, CAST((ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS ts FROM ZWAMESSAGE WHERE ZSTANZAID IN ({ph})",
                    list(still_needed),
                ).fetchall()
                live_map = {m["stanza_id"]: (m["live_id"], m["ts"]) for m in live_matches}
            for r in unresolved:
                if r.get("quoted_msg_id") is None and r["quoted_stanza_id"] in live_map:
                    live_id, live_ts = live_map[r["quoted_stanza_id"]]
                    r["quoted_msg_id"] = live_id
                    if not r.get("quoted_ts"):
                        r["quoted_ts"] = live_ts
        except sqlite3.OperationalError:
            pass
    finally:
        for r in rows:
            r.pop("quoted_stanza_id", None)


def _merge_messages(
    live_rows: list[dict],
    pres_rows: list[dict],
    descending: bool,
    limit: int,
) -> list[dict]:
    if not pres_rows:
        return live_rows[:limit]
    if not live_rows:
        return pres_rows[:limit]

    merged = list(live_rows)
    used_pres_indexes = set()

    for i, p in enumerate(pres_rows):
        if p.get("preservation_reason") == "revoked_by_sender":
            p_stanza = p.get("stanza_id")
            p_ts = p.get("timestamp_ms", 0)
            p_fm = p.get("from_me", 0)
            for j, live in enumerate(merged):
                if live.get("media_type") == "deleted" and live.get("from_me") == p_fm:
                    if p_stanza and live.get("stanza_id") == p_stanza:
                        merged[j] = p
                        used_pres_indexes.add(i)
                        break
                    elif abs(live.get("timestamp_ms", 0) - p_ts) < 2000:
                        merged[j] = p
                        used_pres_indexes.add(i)
                        break

    live_stanzas = {r["stanza_id"] for r in merged if r.get("stanza_id")}
    live_keys = {(r.get("timestamp_ms"), r.get("from_me")) for r in merged}

    for i, p in enumerate(pres_rows):
        if i in used_pres_indexes:
            continue
        p_stanza = p.get("stanza_id")
        if p_stanza:
            if p_stanza not in live_stanzas:
                merged.append(p)
                live_stanzas.add(p_stanza)
        else:
            key = (p.get("timestamp_ms"), p.get("from_me"))
            if key not in live_keys:
                merged.append(p)
                live_keys.add(key)

    merged.sort(
        key=lambda r: (r.get("timestamp_ms", 0), r.get("sort_id") or r.get("msg_id") or 0),
        reverse=descending,
    )
    return merged[:limit]


def _merge_preserved_chats(
    result: list[dict],
    pres_conn: sqlite3.Connection,
    archive_conn: sqlite3.Connection,
) -> list[dict]:
    existing_chats = {(c["id"], c["type"]) for c in result}
    for c in result:
        c.setdefault("is_preserved_only", False)

    try:
        pres_chats = pres_conn.execute(
            "SELECT chat_id, chat_type, newest_ts, subject FROM preserved_chats"
        ).fetchall()
    except sqlite3.OperationalError:
        pres_chats = pres_conn.execute(
            "SELECT chat_id, chat_type, newest_ts FROM preserved_chats"
        ).fetchall()

    for pc in pres_chats:
        cid = pc["chat_id"]
        ctype = pc["chat_type"]
        if (cid, ctype) in existing_chats:
            continue

        display_name = cid
        if ctype == "group":
            grp_row = archive_conn.execute(
                "SELECT subject, folder FROM groups WHERE chat_row_id = ?", (cid,)
            ).fetchone()
            if grp_row and grp_row["subject"]:
                display_name = grp_row["subject"]
            elif "subject" in pc.keys() and pc["subject"]:
                display_name = pc["subject"]
            elif grp_row and grp_row["folder"]:
                display_name = grp_row["folder"]
        else:
            con_row = archive_conn.execute(
                "SELECT display_name, folder FROM contacts WHERE number = ?", (cid,)
            ).fetchone()
            if con_row and con_row["display_name"]:
                display_name = con_row["display_name"]
            elif cid == "0":
                display_name = "WhatsApp"
            elif con_row and con_row["folder"]:
                display_name = con_row["folder"]
            else:
                display_name = f"+{cid}"

        try:
            last_msg = pres_conn.execute(
                """
                SELECT text_body, media_type, from_me, media_name, is_view_once
                FROM preserved_messages
                WHERE chat_id = ? AND chat_type = ?
                ORDER BY timestamp_ms DESC LIMIT 1
                """,
                (cid, ctype),
            ).fetchone()
        except sqlite3.OperationalError:
            last_msg = pres_conn.execute(
                """
                SELECT text_body, media_type, from_me, media_name
                FROM preserved_messages
                WHERE chat_id = ? AND chat_type = ?
                ORDER BY timestamp_ms DESC LIMIT 1
                """,
                (cid, ctype),
            ).fetchone()

        last_preview = ""
        last_type = "text"
        last_from_me = 0
        is_vo = 0
        if last_msg:
            last_preview = last_msg["text_body"] or last_msg["media_name"] or ""
            last_type = last_msg["media_type"] or "text"
            last_from_me = int(last_msg["from_me"])
            if "is_view_once" in last_msg.keys() and last_msg["is_view_once"]:
                is_vo = int(last_msg["is_view_once"])

        result.append(
            {
                "id": cid,
                "type": ctype,
                "display_name": display_name,
                "newest_ts": pc["newest_ts"],
                "last_msg_preview": last_preview,
                "last_msg_type": last_type,
                "last_msg_from_me": last_from_me,
                "is_view_once": is_vo,
                "is_preserved_only": True,
            }
        )
        existing_chats.add((cid, ctype))

    result.sort(key=lambda c: c.get("newest_ts") or 0, reverse=True)
    return result


def _source_changed(cache_conn: sqlite3.Connection, source_path: str) -> bool:
    try:
        st = os.stat(source_path)
        current_mtime = str(st.st_mtime)
        current_size = str(st.st_size)
    except OSError:
        return True

    rows = {
        r["key"]: r["value"]
        for r in cache_conn.execute(
            "SELECT key, value FROM sync_meta WHERE key IN ('source_mtime', 'source_size')"
        ).fetchall()
    }
    return rows.get("source_mtime") != current_mtime or rows.get("source_size") != current_size


def _save_source_stamp(cache_conn: sqlite3.Connection, source_path: str):
    st = os.stat(source_path)
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('source_mtime', ?)",
        (str(st.st_mtime),),
    )
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('source_size', ?)",
        (str(st.st_size),),
    )
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_scanned_at', ?)",
        (str(int(time.time())),),
    )
    cache_conn.commit()


def _stream_fts_rows(
    cursor, cache_conn: sqlite3.Connection, chunk_size: int = 2000, on_progress=None
):
    idx_sql = """
        INSERT OR IGNORE INTO message_index (rowid, chat_id, chat_type, timestamp_ms)
        VALUES (?, ?, ?, ?)
    """
    fts_sql = """
        INSERT OR IGNORE INTO message_index_fts (rowid, text_body)
        VALUES (?, ?)
    """
    idx_batch = []
    fts_batch = []
    for row in cursor:
        text_body = row["text_body"] or ""
        msg_type = row["message_type"]
        if msg_type in (6, 7) and (
            "@lid" in text_body
            or text_body.startswith("{")
            or text_body.isdigit()
            or text_body in ("true", "false")
        ):
            continue
        if msg_type == 46 and not text_body:
            receipt = row["poll_receipt_info"] if "poll_receipt_info" in row.keys() else None
            if receipt:
                parsed_poll = _parse_ios_poll_blob(bytes(receipt))
                if parsed_poll:
                    opts_text = " ".join(parsed_poll["options"])
                    text_body = f"{parsed_poll['question']} {opts_text}".strip()
        is_media = (
            row["message_type"] is not None
            and row["message_type"] != 0
            and row["message_type"] not in (46, 66)
            and (
                row["media_file"] is not None
                or row["message_type"] in (1, 2, 3, 4, 5, 9, 13, 14, 16, 20, 28, 42, 43, 82)
            )
        )
        if not text_body and not is_media:
            continue
        idx_batch.append((row["rowid"], row["chat_id"], row["chat_type"], row["timestamp_ms"]))
        if text_body:
            fts_batch.append((row["rowid"], text_body))
        if len(idx_batch) >= chunk_size:
            cache_conn.executemany(idx_sql, idx_batch)
            if fts_batch:
                cache_conn.executemany(fts_sql, fts_batch)
            cache_conn.commit()
            if on_progress:
                on_progress(len(idx_batch))
            idx_batch.clear()
            fts_batch.clear()
    if idx_batch:
        cache_conn.executemany(idx_sql, idx_batch)
        if fts_batch:
            cache_conn.executemany(fts_sql, fts_batch)
        cache_conn.commit()
        if on_progress:
            on_progress(len(idx_batch))


def _clear_fts_index(cache_conn: sqlite3.Connection):
    cache_conn.execute("DELETE FROM message_index")
    cache_conn.execute("DROP TABLE IF EXISTS message_index_fts")
    cache_conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS message_index_fts USING fts5(\n"
        "    text_body,\n"
        "    content=''\n"
        ")"
    )
    cache_conn.execute("DELETE FROM indexed_chats")
    cache_conn.commit()


def _clean_user_sql(col_expr: str) -> str:
    """Return SQL expression stripping multi-device ':device' and '.agent' suffixes from a JID user string."""
    return (
        f"CASE "
        f"WHEN INSTR({col_expr}, ':') > 0 AND INSTR(SUBSTR({col_expr}, 1, INSTR({col_expr}, ':') - 1), '.') > 0 "
        f"THEN SUBSTR({col_expr}, 1, INSTR(SUBSTR({col_expr}, 1, INSTR({col_expr}, ':') - 1), '.') - 1) "
        f"WHEN INSTR({col_expr}, ':') > 0 "
        f"THEN SUBSTR({col_expr}, 1, INSTR({col_expr}, ':') - 1) "
        f"WHEN INSTR({col_expr}, '.') > 0 "
        f"THEN SUBSTR({col_expr}, 1, INSTR({col_expr}, '.') - 1) "
        f"ELSE {col_expr} END"
    )


def _ensure_jid_map_resolved(
    conn: sqlite3.Connection,
    archive_conn: sqlite3.Connection | None,
) -> None:
    """Populate temporary lookup tables for resolved JIDs, LIDs, and contacts."""
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)"
    )
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _lid_map_resolved (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)"
    )
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _contacts_resolved (clean_number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
    )

    # 1. Populate _contacts_resolved from attached arch.contacts or archive_conn
    has_arch_contacts = False
    try:
        conn.execute("SELECT 1 FROM arch.contacts LIMIT 1")
        has_arch_contacts = True
    except sqlite3.OperationalError:
        pass

    if has_arch_contacts:
        contacts_query_conn = conn
        contacts_sql = "SELECT number, folder, display_name FROM arch.contacts"
    elif archive_conn is not None:
        contacts_query_conn = archive_conn
        contacts_sql = "SELECT number, folder, display_name FROM contacts"
    else:
        contacts_query_conn = None
        contacts_sql = ""

    contact_rows = []
    if contacts_query_conn is not None:
        try:
            contact_rows = contacts_query_conn.execute(contacts_sql).fetchall()
        except sqlite3.OperationalError:
            contact_rows = []

    contacts_to_insert = []
    if contact_rows:
        for r in contact_rows:
            raw_num = str(r[0] or "").strip()
            folder = str(r[1] or "")
            disp = str(r[2] or "").strip()
            if not disp and folder:
                disp = re.sub(r"\s*\(\+?\d+\)$", "", folder).strip()
            if not raw_num:
                continue
            digits = re.sub(r"\D", "", raw_num)
            if digits:
                contacts_to_insert.append((digits, disp, folder))
                clean_stripped = raw_num.lstrip("+").split(":")[0]
                if clean_stripped != digits:
                    contacts_to_insert.append((clean_stripped, disp, folder))
                if digits.startswith("00"):
                    contacts_to_insert.append((digits[2:], disp, folder))
                elif digits.startswith("0") and len(digits) > 8:
                    contacts_to_insert.append((digits[1:], disp, folder))

        try:
            # Fast in-memory suffix bridging: match international E.164 JIDs to national contact numbers and vice-versa
            jid_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid'"
            ).fetchone()
            if jid_table_exists:
                jid_cols = {
                    r["name"] if isinstance(r, sqlite3.Row) else r[1]
                    for r in conn.execute("PRAGMA table_info(jid)").fetchall()
                }
                phone_filter = (
                    "WHERE (server = 's.whatsapp.net' OR server IS NULL)"
                    if "server" in jid_cols
                    else "WHERE 1=1"
                )
                jid_users = {
                    str(row[0]).split(":")[0].split(".")[0]
                    for row in conn.execute(
                        f"SELECT user FROM jid {phone_filter} AND user IS NOT NULL AND user != ''"
                    ).fetchall()
                }
                # Index JID users by suffixes (lengths 8..15) for O(1) matching
                jid_by_suffix = {}
                for ju in jid_users:
                    if ju.isdigit() and len(ju) >= 8:
                        for length in range(8, len(ju) + 1):
                            suf = ju[-length:]
                            jid_by_suffix.setdefault(suf, []).append(ju)

                bridged = []
                for digits, d_name, fld in list(contacts_to_insert):
                    if len(digits) >= 8:
                        if digits in jid_by_suffix:
                            for ju in jid_by_suffix[digits]:
                                if ju != digits:
                                    bridged.append((ju, d_name, fld))
                        if digits.isdigit():
                            for length in range(8, len(digits)):
                                suf = digits[-length:]
                                if suf in jid_users:
                                    bridged.append((suf, d_name, fld))
                if bridged:
                    contacts_to_insert.extend(bridged)
        except sqlite3.OperationalError:
            pass

        if contacts_to_insert:
            conn.executemany(
                "INSERT OR REPLACE INTO _contacts_resolved (clean_number, display_name, folder) VALUES (?, ?, ?)",
                contacts_to_insert,
            )

    # 2. Inspect available schema tables in conn
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    except sqlite3.OperationalError:
        return

    jid_cols = (
        {
            r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(jid)").fetchall()
        }
        if "jid" in tables
        else set()
    )
    has_jid_server = "server" in jid_cols
    has_jid_raw_string = "raw_string" in jid_cols
    has_jid_map = "jid_map" in tables
    has_chat = "chat" in tables
    has_ldn = "lid_display_name" in tables
    has_message = "message" in tables

    # 3. Populate from jid_map
    if has_jid_map:
        try:
            if has_jid_server:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM jid_map jm
                    JOIN jid j1 ON j1._id = jm.lid_row_id
                    JOIN jid j2 ON j2._id = jm.jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.server = 'lid'
                    JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.server = 's.whatsapp.net'
                """)
            elif has_jid_raw_string:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM jid_map jm
                    JOIN jid j1 ON j1._id = jm.lid_row_id
                    JOIN jid j2 ON j2._id = jm.jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.raw_string LIKE '%@lid'
                    JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.raw_string LIKE '%@s.whatsapp.net'
                """)
            else:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                    FROM jid_map GROUP BY lid_row_id
                """)
        except sqlite3.OperationalError:
            pass

    # 4. Populate from chat.account_jid_row_id
    if has_chat:
        try:
            chat_cols = {
                r["name"] if isinstance(r, sqlite3.Row) else r[1]
                for r in conn.execute("PRAGMA table_info(chat)").fetchall()
            }
            if "account_jid_row_id" in chat_cols:
                if has_jid_server:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT j_lid._id, j_phone._id
                        FROM chat c
                        JOIN jid j1 ON j1._id = c.jid_row_id
                        JOIN jid j2 ON j2._id = c.account_jid_row_id
                        JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.server = 'lid'
                        JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.server = 's.whatsapp.net'
                        WHERE c.account_jid_row_id IS NOT NULL AND c.account_jid_row_id != 0
                    """)
                elif has_jid_raw_string:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT j_lid._id, j_phone._id
                        FROM chat c
                        JOIN jid j1 ON j1._id = c.jid_row_id
                        JOIN jid j2 ON j2._id = c.account_jid_row_id
                        JOIN jid j_lid ON j_lid._id IN (j1._id, j2._id) AND j_lid.raw_string LIKE '%@lid'
                        JOIN jid j_phone ON j_phone._id IN (j1._id, j2._id) AND j_phone.raw_string LIKE '%@s.whatsapp.net'
                        WHERE c.account_jid_row_id IS NOT NULL AND c.account_jid_row_id != 0
                    """)
                else:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT jid_row_id, account_jid_row_id
                        FROM chat
                        WHERE account_jid_row_id IS NOT NULL AND account_jid_row_id != 0
                    """)
        except sqlite3.OperationalError:
            pass

    # 5. Populate from lid_display_name matching phone numbers or contacts
    if has_ldn and "jid" in tables:
        try:
            phone_filter = (
                "WHERE server = 's.whatsapp.net'"
                if has_jid_server
                else ("WHERE raw_string LIKE '%@s.whatsapp.net'" if has_jid_raw_string else "")
            )
            phone_jids = {
                str(r[0]).split(":")[0]: r[1]
                for r in conn.execute(
                    f"SELECT user, _id FROM jid {phone_filter} WHERE user IS NOT NULL AND user != ''"
                ).fetchall()
            }
            ldn_rows = conn.execute(
                "SELECT lid_row_id, display_name FROM lid_display_name WHERE display_name IS NOT NULL AND display_name != ''"
            ).fetchall()
            ldn_mappings = []
            for lid_id, d_name in ldn_rows:
                d_digits = re.sub(r"\D", "", d_name)
                if not d_digits:
                    continue
                if d_digits in phone_jids:
                    ldn_mappings.append((lid_id, phone_jids[d_digits]))
                elif len(d_digits) >= 8:
                    for pu, p_id in phone_jids.items():
                        if pu == d_digits or (
                            len(pu) >= 8 and (pu.endswith(d_digits) or d_digits.endswith(pu))
                        ):
                            ldn_mappings.append((lid_id, p_id))
                            break
            if ldn_mappings:
                conn.executemany(
                    "INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id) VALUES (?, ?)",
                    ldn_mappings,
                )
        except sqlite3.OperationalError:
            pass

    # 6. Populate from 1-to-1 message history
    if has_message and has_chat and "jid" in tables:
        try:
            if has_jid_server:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM message m
                    JOIN chat c ON c._id = m.chat_row_id AND c.subject IS NULL
                    JOIN jid j_chat ON j_chat._id = c.jid_row_id
                    JOIN jid j_sender ON j_sender._id = m.sender_jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j_chat._id, j_sender._id) AND j_lid.server = 'lid'
                    JOIN jid j_phone ON j_phone._id IN (j_chat._id, j_sender._id) AND j_phone.server = 's.whatsapp.net'
                    WHERE m.from_me = 0
                """)
            elif has_jid_raw_string:
                conn.execute("""
                    INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                    SELECT j_lid._id, j_phone._id
                    FROM message m
                    JOIN chat c ON c._id = m.chat_row_id AND c.subject IS NULL
                    JOIN jid j_chat ON j_chat._id = c.jid_row_id
                    JOIN jid j_sender ON j_sender._id = m.sender_jid_row_id
                    JOIN jid j_lid ON j_lid._id IN (j_chat._id, j_sender._id) AND j_lid.raw_string LIKE '%@lid'
                    JOIN jid j_phone ON j_phone._id IN (j_chat._id, j_sender._id) AND j_phone.raw_string LIKE '%@s.whatsapp.net'
                    WHERE m.from_me = 0
                """)
        except sqlite3.OperationalError:
            pass

    # 7. Bridge any companion device LIDs sharing user identity with resolved primary LIDs
    if "jid" in tables and has_jid_server:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                SELECT j_comp._id, jm.jid_row_id
                FROM _jid_map_resolved jm
                JOIN jid j_pri ON j_pri._id = jm.lid_row_id
                JOIN jid j_comp ON j_comp.user = j_pri.user AND j_comp.server = 'lid'
                WHERE j_comp._id != jm.lid_row_id
            """)
        except sqlite3.OperationalError:
            pass

    # 8. Mirror into _lid_map_resolved
    try:
        conn.execute("""
            INSERT OR IGNORE INTO _lid_map_resolved (jid_row_id, lid_row_id)
            SELECT jid_row_id, lid_row_id
            FROM _jid_map_resolved
        """)
    except sqlite3.OperationalError:
        pass


def _fts_android_chat(
    wa_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    on_progress,
    hd_clause: str,
):
    _ensure_jid_map_resolved(wa_conn, None)

    if chat_type == "group":
        where = "WHERE CAST(m.chat_row_id AS TEXT) = ?"
        params = (chat_id,)
    else:
        clean_id = chat_id.split(":")[0] if ":" in chat_id else chat_id
        where = (
            "WHERE m.chat_row_id IN ("
            "SELECT c._id FROM chat c "
            "LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id "
            "LEFT JOIN jid j_real ON j_real._id = jm_c.jid_row_id "
            "LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id "
            "WHERE ? IN ("
            f"j_real.user, j_chat.user, {_clean_user_sql('j_real.user')}, {_clean_user_sql('j_chat.user')}, CAST(c._id AS TEXT)"
            "))"
        )
        params = (clean_id,)
    has_poll_opt = wa_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_poll_option'"
    ).fetchone()
    poll_opt_sql = (
        " || ' ' || COALESCE((SELECT GROUP_CONCAT(option_name, ' ') FROM message_poll_option WHERE message_row_id = m._id), '')"
        if has_poll_opt
        else ""
    )
    jid_cols = {
        r["name"] if isinstance(r, sqlite3.Row) else r[1]
        for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()
    }
    has_j_server = "server" in jid_cols
    has_j_raw = "raw_string" in jid_cols
    fts_group_parts = ["c.subject IS NOT NULL"]
    if has_j_server:
        fts_group_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'g.us')")
    if has_j_raw:
        fts_group_parts.append(
            "(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@g.us')"
        )
    fts_is_group = " OR ".join(fts_group_parts)

    cursor = wa_conn.execute(
        f"""
        SELECT
            m._id                                                    AS rowid,
            CASE
                WHEN ({fts_is_group}) THEN CAST(m.chat_row_id AS TEXT)
                ELSE COALESCE(
                    {_clean_user_sql("j_chat_real.user")},
                    {_clean_user_sql("j_chat.user")},
                    CAST(m.chat_row_id AS TEXT)
                )
            END                                                      AS chat_id,
            CASE WHEN ({fts_is_group}) THEN 'group' ELSE 'contact' END AS chat_type,
            COALESCE(m.timestamp, 0)                                AS timestamp_ms,
            COALESCE(
                CASE WHEN m.message_type = 66 THEN m.text_data {poll_opt_sql}
                     ELSE m.text_data
                END,
                ''
            )                                                        AS text_body,
            m.message_type,
            mm.file_path                                            AS media_file
        FROM message m
        LEFT JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
        LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
        LEFT JOIN message_media mm ON mm.message_row_id = m._id
        {where}
        {hd_clause}
        ORDER BY m.timestamp ASC
    """,
        params,
    )
    _stream_fts_rows(cursor, cache_conn, on_progress=on_progress)


_ANDROID_HD_DEDUP_CLAUSE = """
    AND NOT EXISTS (
        SELECT 1 FROM message_association ma
        JOIN message_media mm_hd ON mm_hd.message_row_id = ma.child_message_row_id
        WHERE ma.parent_message_row_id = m._id
          AND ma.association_type IN (12, 7)
          AND mm_hd.file_path IS NOT NULL
    )
"""


def _check_android_hd_association(wa_conn: sqlite3.Connection) -> bool:
    try:
        has_table = wa_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_association'"
        ).fetchone()
        if not has_table:
            return False
        has_hd = wa_conn.execute(
            "SELECT 1 FROM message_association WHERE association_type IN (12, 7) LIMIT 1"
        ).fetchone()
        return bool(has_hd)
    except sqlite3.OperationalError:
        return False


_IOS_HD_DEDUP_CLAUSE = """
    AND NOT EXISTS (
        SELECT 1 FROM ext.message_parent_association mpa
        JOIN ZWAMESSAGE m_hd ON m_hd.ZSTANZAID = mpa.stanza_id
        JOIN ZWAMEDIAITEM mi_hd ON mi_hd.Z_PK = m_hd.ZMEDIAITEM
        WHERE mpa.parent_stanza_id = m.ZSTANZAID
          AND mpa.type IN (10, 5)
          AND mi_hd.ZMEDIALOCALPATH IS NOT NULL
    )
"""


def _find_ios_db(wa_db_path: str, rel_path: str, output_root: Path | None) -> Path | None:
    db_p = Path(wa_db_path)
    candidates = [
        db_p.parent / rel_path,
        db_p.parent / "Whatsapp Databases" / rel_path,
        db_p.parent.parent / "Whatsapp Databases" / rel_path,
        db_p.parent.parent / rel_path,
        db_p.parent / "ExtChatDB" / rel_path,
    ]
    if output_root is not None:
        candidates.extend(
            [
                output_root / "Whatsapp Databases" / rel_path,
                output_root / rel_path,
                output_root / "ExtChatDB" / rel_path,
            ]
        )
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _check_ios_hd_association(wa_db_path: str, ext_db_path: Path | None) -> bool:
    if ext_db_path is None or not ext_db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(wa_db_path)
        conn.execute("ATTACH DATABASE ? AS ext", (str(ext_db_path),))
        has_table = conn.execute(
            "SELECT 1 FROM ext.sqlite_master WHERE type='table' AND name='message_parent_association'"
        ).fetchone()
        if not has_table:
            conn.close()
            return False
        has_hd = conn.execute(
            "SELECT 1 FROM ext.message_parent_association WHERE type IN (10, 5) LIMIT 1"
        ).fetchone()
        conn.close()
        return bool(has_hd)
    except sqlite3.OperationalError:
        return False


def _fts_ios_chat(
    wa_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    chat_id: str,
    on_progress,
    hd_clause: str,
):
    mi_cols = {row[1] for row in wa_conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
    vcard_name_col = "mi.ZVCARDNAME" if "ZVCARDNAME" in mi_cols else "NULL"
    has_msg_info = wa_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGEINFO'"
    ).fetchone()
    msg_info_join = (
        "LEFT JOIN ZWAMESSAGEINFO mi_info ON mi_info.ZMESSAGE = m.Z_PK" if has_msg_info else ""
    )
    poll_receipt_col = (
        "mi_info.ZRECEIPTINFO AS poll_receipt_info,"
        if has_msg_info
        else "NULL AS poll_receipt_info,"
    )
    sql = f"""
        SELECT
            m.Z_PK                                                      AS rowid,
            CAST(m.ZCHATSESSION AS TEXT)                               AS chat_id,
            CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                 ELSE 'contact' END                                     AS chat_type,
            CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)       AS timestamp_ms,
            COALESCE(m.ZTEXT, CASE WHEN m.ZMESSAGETYPE = 4 THEN {vcard_name_col} END, '') AS text_body,
            {poll_receipt_col}
            m.ZMESSAGETYPE                                              AS message_type,
            mi.ZMEDIALOCALPATH                                          AS media_file
        FROM ZWAMESSAGE m
        LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
        {msg_info_join}
        WHERE CAST(m.ZCHATSESSION AS TEXT) = ?
          {hd_clause}
        ORDER BY m.ZMESSAGEDATE ASC
    """
    cursor = wa_conn.execute(sql, (chat_id,))
    _stream_fts_rows(cursor, cache_conn, on_progress=on_progress)


def _build_fts_chat(
    cache_conn: sqlite3.Connection,
    source_type: str,
    wa_db_path: str,
    chat_id: str,
    chat_type: str,
    on_progress=None,
):
    if wa_db_path:
        wa_conn = sqlite3.connect(wa_db_path)
        wa_conn.row_factory = sqlite3.Row
        if source_type == "android":
            has_android_hd = _check_android_hd_association(wa_conn)
            hd_clause = _ANDROID_HD_DEDUP_CLAUSE if has_android_hd else ""
            _fts_android_chat(wa_conn, cache_conn, chat_id, chat_type, on_progress, hd_clause)
        else:
            ext_db = _find_ios_db(wa_db_path, "ExtChatDatabase.sqlite", None)
            if not ext_db:
                ext_db = _find_ios_db(wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", None)
            has_ios_hd = _check_ios_hd_association(wa_db_path, ext_db)
            hd_clause = _IOS_HD_DEDUP_CLAUSE if has_ios_hd else ""
            if has_ios_hd and ext_db:
                try:
                    wa_conn.execute("ATTACH DATABASE ? AS ext", (str(ext_db),))
                except sqlite3.OperationalError:
                    hd_clause = ""
            _fts_ios_chat(wa_conn, cache_conn, chat_id, on_progress, hd_clause)
        wa_conn.close()

    pres_path = None
    if wa_db_path:
        pres_path = _detect_preserved_db(Path(wa_db_path).parent)
        if not pres_path:
            pres_path = _detect_preserved_db(Path(wa_db_path).parent.parent)
    if pres_path and pres_path.exists():
        try:
            with sqlite3.connect(
                f"file:{pres_path.resolve().as_posix()}?mode=ro", uri=True
            ) as p_conn:
                p_conn.row_factory = sqlite3.Row
                _fts_preserved_chat(p_conn, cache_conn, chat_id, chat_type, on_progress)
        except Exception:
            pass

    cache_conn.execute(
        "INSERT OR IGNORE INTO indexed_chats (chat_id, chat_type) VALUES (?, ?)",
        (chat_id, chat_type),
    )
    cache_conn.commit()


def _ensure_chat_indexed(
    cache_conn: sqlite3.Connection, source_type: str, wa_db_path: str, chat_id: str, chat_type: str
):
    row = cache_conn.execute(
        "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?",
        (chat_id, chat_type),
    ).fetchone()
    if row is None:
        _build_fts_chat(cache_conn, source_type, wa_db_path, chat_id, chat_type)


def _maybe_start_indexing(
    chat_id: str,
    chat_type: str,
    cache_conn,
    source_type: str,
    wa_db_path: str,
    output_root: Path,
    indexing_state: dict,
    indexing_lock,
):
    key = (chat_id, chat_type)
    already = cache_conn.execute(
        "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?", key
    ).fetchone()
    if already:
        return
    with indexing_lock:
        if key in indexing_state:
            return
        indexing_state[key] = "indexing"
    threading.Thread(
        target=_bg_index_chat,
        args=(
            str(get_cache_db_path(output_root)),
            source_type,
            wa_db_path,
            chat_id,
            chat_type,
            indexing_state,
            indexing_lock,
        ),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_ANDROID_JID_MAP = """
    LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
    LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
"""

_ANDROID_SELECT = None

_SUPPORTED_ANDROID_SYSTEM_ACTIONS = (
    1,
    4,
    5,
    6,
    7,
    11,
    12,
    13,
    14,
    15,
    18,
    19,
    20,
    27,
    28,
    56,
    58,
    61,
    67,
    68,
    69,
    70,
    79,
    123,
    124,
    127,
    129,
    136,
    144,
    146,
)

_ANDROID_FILTER = """
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND m.message_type != 7 AND (mm.file_path IS NOT NULL OR mm.message_row_id IS NOT NULL OR m.message_type IN (1, 2, 3, 5, 9, 13, 16, 20, 28)))
        OR (m.message_type IN (4, 14))
        OR (m.message_type IN (15, 64))
        OR (m.message_type IN (42, 43, 82))
        OR (m.message_type = 7)
        OR (m.message_type = 66)
    )
"""

_IOS_CHAT_ID = "CAST(m.ZCHATSESSION AS TEXT)"
_IOS_CHAT_TYPE = "CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END"

_IOS_SENDER_RAW = """COALESCE(
    gm.ZMEMBERJID,
    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN m.ZFROMJID END,
    ''
)"""

_IOS_SENDER_JID = f"""NULLIF(SUBSTR({_IOS_SENDER_RAW}, 1,
                    INSTR({_IOS_SENDER_RAW} || '@', '@') - 1), '')"""

quoted_stanza_id_ios = "NULL"

_IOS_SELECT = f"""
    SELECT
        m.Z_PK                                                       AS msg_id,
        m.Z_PK                                                       AS sort_id,
        {_IOS_CHAT_ID}                                               AS chat_id,
        {_IOS_CHAT_TYPE}                                             AS chat_type,
        CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)        AS timestamp_ms,
        COALESCE(
            CASE WHEN ({_IOS_SENDER_JID}) = '0' THEN 'WhatsApp' END,
            CASE WHEN ic_s.full_name NOT LIKE '+%' THEN NULLIF(ic_s.full_name, '') END,
            NULLIF(con_s.display_name, ''),
            NULLIF(con_lid_s.display_name, ''),
            NULLIF(pp_lid.ZPUSHNAME, ''),
            NULLIF(pp_phone.ZPUSHNAME, ''),
            NULLIF(cs_lid.ZPARTNERNAME, ''),
            NULLIF(cs_phone.ZPARTNERNAME, ''),
            CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN NULLIF(m.ZPUSHNAME, '') END,
            CASE WHEN COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            ) IS NOT NULL
            THEN '+' || COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            )
            END,
            ''
        )                                                            AS sender,
        m.ZISFROMME                                                  AS from_me,
        ac.archive_path,
        CASE
            WHEN m.ZMESSAGETYPE = 14 THEN 'deleted'
            WHEN m.ZMESSAGETYPE IN (6, 10) THEN 'service'
            WHEN m.ZMESSAGETYPE = 4 THEN 'vcard'
            WHEN m.ZMESSAGETYPE = 5 THEN 'location'
            WHEN m.ZMESSAGETYPE = 15 THEN 'sticker'
            WHEN m.ZMESSAGETYPE IN (1, 38) THEN 'image'
            WHEN m.ZMESSAGETYPE IN (3, 39) THEN 'video'
            WHEN m.ZMESSAGETYPE IN (2, 53) THEN 'audio'
            WHEN m.ZMESSAGETYPE = 8 THEN 'document'
            WHEN m.ZMESSAGETYPE = 11 THEN 'gif'
            WHEN m.ZMESSAGETYPE = 46 THEN 'poll'
            WHEN m.ZMESSAGETYPE = 7 THEN 'link'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="('Message/' || COALESCE(mi.ZMEDIALOCALPATH,''))")}
        END                                                           AS media_type,
        COALESCE(
            CASE WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(m.ZTEXT, '') END,
            mi.ZMEDIALOCALPATH,
            ''
        )                                                             AS media_name,
        COALESCE(
            CASE
                WHEN m.ZISFROMME = 1 AND m.ZMESSAGETYPE = 14 THEN 'You deleted this message'
                WHEN m.ZMESSAGETYPE = 14 THEN 'This message was deleted'
                WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(mi.ZTITLE, '')
                WHEN m.ZMESSAGETYPE IN (1, 3, 11, 38, 39, 54) THEN COALESCE(NULLIF(m.ZTEXT, ''), NULLIF(mi.ZTITLE, ''))
                ELSE m.ZTEXT
            END,
            ''
        )                                                             AS text_body,
        COALESCE(
            qm.ZTEXT,
            CASE
                WHEN qm.ZMESSAGETYPE = 46 THEN 'Poll'
                WHEN qm.ZMESSAGETYPE = 1 THEN '📷 Photo'
                WHEN qm.ZMESSAGETYPE = 2 THEN '🎵 Audio'
                WHEN qm.ZMESSAGETYPE = 3 THEN '🎥 Video'
                WHEN qm.ZMESSAGETYPE = 15 THEN 'Sticker'
            END,
            ''
        )                                                             AS quoted_text,
        CASE WHEN qm.ZISFROMME = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 NULLIF(qm.ZPUSHNAME, ''),
                 CASE WHEN SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                   INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1) != ''
                      THEN '+' || SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                         INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
                 END,
                 '')
        END                                                          AS quoted_sender,
        COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0) AS quoted_ts,
        qm.Z_PK                                                      AS quoted_msg_id,
        {quoted_stanza_id_ios}                                       AS quoted_stanza_id,
        NULL                                                          AS reactions,
        0                                                            AS is_edited,
        NULL                                                          AS edited_ts,
        CASE WHEN m.ZMESSAGETYPE IN (38, 39, 53) THEN 1 ELSE 0 END   AS is_view_once,
        m.ZMESSAGETYPE                                               AS raw_msg_type,
        0                                                            AS group_event_type,
        gm.ZMEMBERJID                                                AS member_jid,
        0                                                            AS media_width,
        0                                                            AS media_height,
        0                                                            AS media_size,
        0                                                            AS media_duration,
        0.0                                                          AS aspect_ratio,
        0                                                            AS has_thumbnail,
        CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZTITLE, '') END  AS link_title,
        NULL                                                         AS link_desc,
        CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZMEDIAURL, '') END AS link_url,
        0                                                            AS link_preview_type,
        NULL                                                         AS _raw_metadata
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
    LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = ({_IOS_SENDER_RAW})
    LEFT JOIN _ios_contacts ic_s ON ic_s.jid = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE
    LEFT JOIN arch.contacts con_s
          ON con_s.number = COALESCE(
              CASE WHEN ({_IOS_SENDER_RAW}) LIKE '%@lid' THEN ic_s.phone_number END,
              ({_IOS_SENDER_JID})
          )
    LEFT JOIN arch.contacts con_lid_s
          ON con_lid_s.number = SUBSTR(({_IOS_SENDER_RAW}), 1,
                                       INSTR(({_IOS_SENDER_RAW}) || '@', '@') - 1)
    LEFT JOIN arch.contacts con_sq
          ON con_sq.number = SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                    INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
    LEFT JOIN arch.archive_copies ac
          ON ac.original_path = COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)
"""

_BASE_IOS_FILTER = """
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND m.ZMESSAGETYPE NOT IN (6, 10) AND (mi.ZMEDIALOCALPATH IS NOT NULL OR m.ZMESSAGETYPE IN (1, 2, 3, 5, 8, 11, 15, 23, 38, 39, 54) OR mi.ZTITLE IS NOT NULL))
        OR (m.ZMESSAGETYPE = 4)
        OR (m.ZMESSAGETYPE = 14)
        OR (m.ZMESSAGETYPE IN (38, 39, 53))
        OR (m.ZMESSAGETYPE = 46)
        OR (m.ZMESSAGETYPE = 7)
        OR (m.ZMESSAGETYPE IN (6, 10))
    )
"""

_BASE_IOS_FILTER_TS = _BASE_IOS_FILTER

_IOS_FILTER = _BASE_IOS_FILTER
_IOS_FILTER_TS = _BASE_IOS_FILTER_TS

_ANDROID_TS = "COALESCE(m.timestamp, 0)"
_IOS_TS = "CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)"

_ANDROID_FILTER_TS = _ANDROID_FILTER

_ANDROID_IS_MEDIA = "NOT (m.message_type IS NULL OR m.message_type IN (0, 7, 15, 64, 66))"
_IOS_IS_MEDIA = "NOT (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE IN (0, 6, 10, 14, 46))"

_ANDROID_IS_LINK = (
    "(m.message_type IS NULL OR m.message_type = 0)"
    " AND m.text_data IS NOT NULL"
    " AND (INSTR(LOWER(m.text_data), 'http://') > 0"
    "      OR INSTR(LOWER(m.text_data), 'https://') > 0)"
)
_IOS_IS_LINK = (
    "(m.ZMESSAGETYPE = 7 OR ((m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"
    " AND m.ZTEXT IS NOT NULL"
    " AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0"
    "      OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)))"
)

_ANDROID_IS_DOCUMENT_UNDOWNLOADED = (
    "NOT (m.message_type IS NULL OR m.message_type = 0)"
    " AND mm.file_path IS NULL"
    " AND mm.media_name IS NOT NULL AND mm.media_name != ''"
)
_IOS_IS_DOCUMENT_UNDOWNLOADED = (
    "NOT (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"
    " AND mi.ZMEDIALOCALPATH IS NULL"
    " AND mi.ZTITLE IS NOT NULL AND mi.ZTITLE != ''"
)


def _android_chat_filter(chat_id: str, chat_type: str) -> tuple[str, list]:
    if chat_type == "group":
        return "m.chat_row_id = CAST(? AS INTEGER)", [chat_id]
    clean_id = chat_id.split(":")[0] if ":" in chat_id else chat_id
    pred = (
        "m.chat_row_id IN ("
        "SELECT c._id FROM chat c "
        "LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id "
        "LEFT JOIN jid j_real ON j_real._id = jm_c.jid_row_id "
        "LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id "
        "WHERE ? IN ("
        f"j_real.user, j_chat.user, {_clean_user_sql('j_real.user')}, {_clean_user_sql('j_chat.user')}, CAST(c._id AS TEXT)"
        "))"
    )
    return pred, [clean_id]


def _ios_chat_filter(chat_id: str) -> tuple[str, list]:
    return "m.ZCHATSESSION = CAST(? AS INTEGER)", [chat_id]


def _bg_index_chat(
    cache_db_path: str,
    source_type: str,
    wa_db_path: str,
    chat_id: str,
    chat_type: str,
    state: dict,
    lock: threading.Lock,
):
    try:
        cache_conn = sqlite3.connect(cache_db_path)
        try:
            cache_conn.execute("PRAGMA journal_mode = WAL")
            cache_conn.execute("PRAGMA synchronous = NORMAL")
            cache_conn.row_factory = sqlite3.Row
            _build_fts_chat(cache_conn, source_type, wa_db_path, chat_id, chat_type)
        finally:
            cache_conn.close()
    except Exception:
        print(f"[wab_viewer] Background indexing error for {chat_id}:\n{traceback.format_exc()}")
    finally:
        with lock:
            state[(chat_id, chat_type)] = "done"


def _parse_ios_receipt_blob(blob: bytes, conn, is_group: bool = False, msg_ts_s: int = 0) -> list:
    """Decode iOS ZRECEIPTINFO into per-recipient delivered/read timestamps.

    Blob layout (validated against real DB + phone screenshots):
      top field 3 = base_ts (unix seconds = send time)
      top field 2 = repeated recipient entry
    Recipient entry:
      field 1  = LID bytes (value[1:] hex, strip a trailing 'f')
      field 5  = read delta in seconds (present => read at base + field5)
      field 10 = repeated delivered event {1: delta_seconds}; delivered = base + min(deltas)
      field 9  = repeated event {1: delta_seconds}; delivered fallback when field 10 absent
                 (compact format: no base_ts, no field 5 => delivered-only, read blank)
      field 4  = unrelated large value; NOT a delivered/read gate => ignored
    """

    def _read_varint(data, pos):
        result = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    def _event_delta(value):
        """Return sub-field 1 (delta in seconds) of a field-9/field-10 event, or None."""
        pos = 0
        delta = None
        while pos < len(value):
            tag_byte, pos = _read_varint(value, pos)
            field = tag_byte >> 3
            wire = tag_byte & 0x07
            if wire == 0:
                v, pos = _read_varint(value, pos)
                if field == 1:
                    delta = v
            elif wire == 2:
                length, pos = _read_varint(value, pos)
                pos += length
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        return delta

    def _parse_member_entry(data):
        """Return (lid, delivered_delta, read_delta). Deltas are seconds; None when absent."""
        pos = 0
        lid = ""
        read_delta = None
        delivered_deltas = []
        fallback_deltas = []
        while pos < len(data):
            tag_byte, pos = _read_varint(data, pos)
            field = tag_byte >> 3
            wire = tag_byte & 0x07
            if wire == 2:
                length, pos = _read_varint(data, pos)
                value = data[pos : pos + length]
                pos += length
                if field == 1:
                    if len(value) > 1:
                        s = value[1:].hex()
                        lid = s[:-1] if s.endswith("f") else s
                elif field == 10:
                    d = _event_delta(value)
                    if d is not None:
                        delivered_deltas.append(d)
                elif field == 9:
                    d = _event_delta(value)
                    if d is not None:
                        fallback_deltas.append(d)
            elif wire == 0:
                value, pos = _read_varint(data, pos)
                if field == 5:
                    read_delta = value
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        if delivered_deltas:
            delivered_delta = min(delivered_deltas)
        elif fallback_deltas:
            delivered_delta = min(fallback_deltas)
        else:
            delivered_delta = None
        return lid, delivered_delta, read_delta

    base_ts = None
    entries = []
    pos = 0
    while pos < len(blob):
        try:
            tag_byte, pos = _read_varint(blob, pos)
        except Exception:
            break
        field = tag_byte >> 3
        wire = tag_byte & 0x07
        if wire == 2:
            try:
                length, pos = _read_varint(blob, pos)
            except Exception:
                break
            value = blob[pos : pos + length]
            pos += length
            if field == 2:
                entries.append(value)
        elif wire == 0:
            try:
                value, pos = _read_varint(blob, pos)
            except Exception:
                break
            if field == 3:
                base_ts = value
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            break

    anchor = base_ts if base_ts is not None else msg_ts_s
    if anchor is None or not entries:
        return []

    parsed = []
    for entry_bytes in entries:
        try:
            parsed.append(_parse_member_entry(entry_bytes))
        except Exception:
            continue

    if not is_group:
        delivered_deltas = [dd for _, dd, _ in parsed if dd is not None]
        read_deltas = [rd for _, _, rd in parsed if rd is not None]
        if not delivered_deltas and not read_deltas:
            return []
        return [
            {
                "name": "",
                "jid": "",
                "delivered_ts": (anchor + min(delivered_deltas)) * 1000
                if delivered_deltas
                else None,
                "read_ts": (anchor + min(read_deltas)) * 1000 if read_deltas else None,
                "played_ts": None,
            }
        ]

    has_phone_col = False
    try:
        ic_cols = {
            r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(_ios_contacts)").fetchall()
        }
        has_phone_col = "phone_number" in ic_cols
    except Exception:
        pass
    phone_col_expr = ", phone_number" if has_phone_col else ", NULL AS phone_number"

    members = []
    for lid, delivered_delta, read_delta in parsed:
        name = lid
        if lid:
            row = conn.execute(
                f"SELECT full_name {phone_col_expr} FROM _ios_contacts WHERE jid = ?",
                (f"{lid}@lid",),
            ).fetchone()
            if row and row[0]:
                name = row[0]
            elif row and row[1]:
                c_row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (row[1],)
                ).fetchone()
                if c_row and c_row[0]:
                    name = c_row[0]
                else:
                    name = f"+{row[1]}"
            else:
                row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (lid,)
                ).fetchone()
                if row and row[0]:
                    name = row[0]
        members.append(
            {
                "name": name or lid or "",
                "jid": lid or "",
                "delivered_ts": (anchor + delivered_delta) * 1000
                if delivered_delta is not None
                else None,
                "read_ts": (anchor + read_delta) * 1000 if read_delta is not None else None,
                "played_ts": None,
            }
        )
    return members


def _ios_read_without_timestamp(msg_status: int, read_ts) -> bool:
    """iOS aggregate ZMESSAGESTATUS 8 = read (in groups WhatsApp only sets it once
    every recipient has read). Recent messages (~mid-2026 onward) keep this flag but
    no longer persist the per-recipient read timestamp. Return True when the message
    is confirmed read yet no timestamp is stored, so the UI can show that state
    instead of a blank."""
    return msg_status == 8 and read_ts is None


def _parse_ios_receipt_device_rows(infra_rows: list, conn, is_group: bool) -> list[dict]:
    """Group per-device receipt rows from infra.receipt_device by user_jid."""
    by_user: dict[str, dict] = {}
    for row in infra_rows:
        ujid = row["user_jid"]
        if not ujid:
            continue
        deliv = row["delivered_timestamp"]
        read = row["read_timestamp"]
        played = row["played_timestamp"]

        if ujid not in by_user:
            by_user[ujid] = {
                "delivered_sec": deliv,
                "read_sec": read,
                "played_sec": played,
            }
        else:
            entry = by_user[ujid]
            if deliv is not None:
                entry["delivered_sec"] = (
                    min(entry["delivered_sec"], deliv)
                    if entry["delivered_sec"] is not None
                    else deliv
                )
            if read is not None:
                entry["read_sec"] = (
                    min(entry["read_sec"], read) if entry["read_sec"] is not None else read
                )
            if played is not None:
                entry["played_sec"] = (
                    min(entry["played_sec"], played) if entry["played_sec"] is not None else played
                )

    if not is_group:
        delivered_secs = [
            d["delivered_sec"] for d in by_user.values() if d["delivered_sec"] is not None
        ]
        read_secs = [d["read_sec"] for d in by_user.values() if d["read_sec"] is not None]
        played_secs = [d["played_sec"] for d in by_user.values() if d["played_sec"] is not None]
        if not delivered_secs and not read_secs and not played_secs:
            return []
        deliv_ts = min(delivered_secs) * 1000 if delivered_secs else None
        read_ts = min(read_secs) * 1000 if read_secs else None
        played_ts = min(played_secs) * 1000 if played_secs else None
        return [
            {
                "name": "",
                "jid": "",
                "delivered_ts": deliv_ts,
                "read_ts": read_ts,
                "played_ts": played_ts,
                "read_known": read_ts is not None,
            }
        ]

    has_phone_col = False
    try:
        ic_cols = {
            r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute("PRAGMA table_info(_ios_contacts)").fetchall()
        }
        has_phone_col = "phone_number" in ic_cols
    except Exception:
        pass
    phone_col_expr = ", phone_number" if has_phone_col else ", NULL AS phone_number"

    members = []
    for ujid, data in by_user.items():
        name = None
        c_row = conn.execute(
            f"SELECT full_name {phone_col_expr} FROM _ios_contacts WHERE jid = ?", (ujid,)
        ).fetchone()
        if c_row and c_row[0]:
            name = c_row[0]
        elif c_row and c_row[1]:
            con_row = conn.execute(
                "SELECT display_name FROM arch.contacts WHERE number = ?", (c_row[1],)
            ).fetchone()
            if con_row and con_row[0]:
                name = con_row[0]
            else:
                name = f"+{c_row[1]}"
        else:
            prefix = ujid.split("@")[0]
            c_row2 = conn.execute(
                f"SELECT full_name {phone_col_expr} FROM _ios_contacts WHERE jid = ?", (prefix,)
            ).fetchone()
            if c_row2 and c_row2[0]:
                name = c_row2[0]
            elif c_row2 and c_row2[1]:
                con_row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (c_row2[1],)
                ).fetchone()
                if con_row and con_row[0]:
                    name = con_row[0]
                else:
                    name = f"+{c_row2[1]}"
            else:
                p_row = conn.execute(
                    "SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ?", (ujid,)
                ).fetchone()
                if p_row and p_row[0]:
                    name = p_row[0]
                else:
                    name = prefix

        jid_clean = ujid.split("@")[0]
        deliv_ts = data["delivered_sec"] * 1000 if data["delivered_sec"] is not None else None
        read_ts = data["read_sec"] * 1000 if data["read_sec"] is not None else None
        played_ts = data["played_sec"] * 1000 if data["played_sec"] is not None else None

        members.append(
            {
                "name": name or jid_clean or "",
                "jid": jid_clean,
                "delivered_ts": deliv_ts,
                "read_ts": read_ts,
                "played_ts": played_ts,
                "read_known": read_ts is not None,
            }
        )
    return members


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------


def create_app(output_root: Path, rescan: bool = False):
    app = Flask(__name__)

    archive_db_path = get_archive_db_path(output_root)
    source_type, wa_db_path = _detect_source_db(output_root)
    if not wa_db_path:
        raise FileNotFoundError(
            f"WhatsApp database not found in {output_root} (msgstore.db or ChatStorage.sqlite)"
        )
    preserved_db_path = _detect_preserved_db(output_root)
    _ensure_writable(archive_db_path)
    _ensure_writable(Path(wa_db_path))
    cache_conn = _open_cache_db(output_root)
    _indexing_state: dict = {}
    _indexing_lock = threading.Lock()
    _bulk_index_state: dict = {"running": False, "indexed_msgs": 0, "total_msgs": 0}
    _wa_local = threading.local()

    app._indexing_lock = _indexing_lock
    app._bulk_index_state = _bulk_index_state

    schema_alert = None
    source_is_changed = False
    with contextlib.closing(sqlite3.connect(str(wa_db_path))) as chk_conn:
        schema_res = validate_viewer_schema(chk_conn.cursor(), source_type, _get_version())
        if not schema_res.is_valid:
            print(schema_res.banner)
            schema_alert = {
                "summary": schema_res.summary,
                "issue_url": schema_res.issue_url,
                "platform": source_type,
                "is_legacy": schema_res.is_legacy,
            }
            source_type = None

    app.config["SCHEMA_ALERT"] = schema_alert
    has_android_hd = False
    has_ios_hd = False

    if source_type is None:
        print(
            "[wab_viewer] Notice: WhatsApp schema incompatibility detected. Chats cannot be loaded."
        )
    else:
        print(f"[wab_viewer] Source DB: {source_type} at {wa_db_path}")

        _ensure_writable(Path(wa_db_path))
        _ensure_wa_indexes(wa_db_path)

        has_prior_stamp = bool(
            cache_conn.execute("SELECT 1 FROM sync_meta WHERE key = 'source_mtime'").fetchone()
        )
        source_is_changed = bool(rescan or _source_changed(cache_conn, wa_db_path))
        if source_is_changed:
            _clear_fts_index(cache_conn)
            cache_conn.execute("VACUUM")
            _save_source_stamp(cache_conn, wa_db_path)
            print("[wab_viewer] FTS cache cleared — chats will be indexed on first open")
        else:
            count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            print(f"[wab_viewer] {count} chat(s) already indexed")

        if source_type == "android" and wa_db_path is not None:
            try:
                tmp_chk = sqlite3.connect(wa_db_path)
                has_android_hd = _check_android_hd_association(tmp_chk)
                tmp_chk.close()
            except sqlite3.OperationalError:
                has_android_hd = False
        if source_type == "ios" and wa_db_path is not None:
            ext_db_init = _find_ios_db(wa_db_path, "ExtChatDatabase.sqlite", output_root)
            if not ext_db_init:
                ext_db_init = _find_ios_db(
                    wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", output_root
                )
            has_ios_hd = _check_ios_hd_association(wa_db_path, ext_db_init)

        if has_ios_hd:
            globals()["_IOS_FILTER"] = _BASE_IOS_FILTER + _IOS_HD_DEDUP_CLAUSE
            globals()["_IOS_FILTER_TS"] = _BASE_IOS_FILTER_TS + _IOS_HD_DEDUP_CLAUSE
        else:
            globals()["_IOS_FILTER"] = _BASE_IOS_FILTER
            globals()["_IOS_FILTER_TS"] = _BASE_IOS_FILTER_TS

    def get_wa():
        if source_type is None:
            return None
        conn = getattr(_wa_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(wa_db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA cache_size = -32000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("ATTACH DATABASE ? AS arch", (str(archive_db_path),))
            if source_type == "android":
                _ensure_jid_map_resolved(conn, _archive_conn)
            if source_type == "ios":
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT)"
                )

                contacts_v2 = _find_ios_db(wa_db_path, "ContactsV2.sqlite", output_root)
                if contacts_v2:
                    conn.execute("ATTACH DATABASE ? AS cv", (str(contacts_v2),))
                    cv_cols = {
                        r["name"]
                        for r in conn.execute(
                            "PRAGMA cv.table_info(ZWAADDRESSBOOKCONTACT)"
                        ).fetchall()
                    }
                    if "ZLID" in cv_cols:
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                ZLID AS jid,
                                ZFULLNAME AS full_name,
                                CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                                     THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                                END AS phone_number
                            FROM cv.ZWAADDRESSBOOKCONTACT
                            WHERE ZLID IS NOT NULL
                              AND ((ZFULLNAME IS NOT NULL AND ZFULLNAME != '') OR (ZWHATSAPPID LIKE '%@s.whatsapp.net'))
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                                phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                        """)
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                SUBSTR(ZLID, 1, INSTR(ZLID, '@') - 1) AS jid,
                                ZFULLNAME AS full_name,
                                CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                                     THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                                END AS phone_number
                            FROM cv.ZWAADDRESSBOOKCONTACT
                            WHERE ZLID LIKE '%@lid'
                              AND ((ZFULLNAME IS NOT NULL AND ZFULLNAME != '') OR (ZWHATSAPPID LIKE '%@s.whatsapp.net'))
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                                phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                        """)
                    if "ZWHATSAPPID" in cv_cols:
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                ZWHATSAPPID AS jid,
                                ZFULLNAME AS full_name,
                                CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                                     THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                                END AS phone_number
                            FROM cv.ZWAADDRESSBOOKCONTACT
                            WHERE ZWHATSAPPID IS NOT NULL
                              AND ((ZFULLNAME IS NOT NULL AND ZFULLNAME != '') OR (ZWHATSAPPID LIKE '%@s.whatsapp.net'))
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                                phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                        """)
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1) AS jid,
                                ZFULLNAME AS full_name,
                                CASE WHEN ZWHATSAPPID LIKE '%@s.whatsapp.net'
                                     THEN SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                                END AS phone_number
                            FROM cv.ZWAADDRESSBOOKCONTACT
                            WHERE ZWHATSAPPID LIKE '%@s.whatsapp.net'
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(excluded.full_name, _ios_contacts.full_name),
                                phone_number = COALESCE(excluded.phone_number, _ios_contacts.phone_number)
                        """)
                lid_db = _find_ios_db(wa_db_path, "LID.sqlite", output_root)
                if lid_db:
                    conn.execute("ATTACH DATABASE ? AS lid_db", (str(lid_db),))
                    lid_cols = {
                        r["name"]
                        for r in conn.execute("PRAGMA lid_db.table_info(ZWAZACCOUNT)").fetchall()
                    }
                    if "ZIDENTIFIER" in lid_cols and "ZPHONENUMBER" in lid_cols:
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                ZIDENTIFIER AS jid,
                                NULL AS full_name,
                                ZPHONENUMBER AS phone_number
                            FROM lid_db.ZWAZACCOUNT
                            WHERE ZIDENTIFIER IS NOT NULL
                              AND ZPHONENUMBER IS NOT NULL
                              AND ZPHONENUMBER != ''
                            ON CONFLICT(jid) DO UPDATE SET
                                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                        """)
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                SUBSTR(ZIDENTIFIER, 1, INSTR(ZIDENTIFIER, '@') - 1) AS jid,
                                NULL AS full_name,
                                ZPHONENUMBER AS phone_number
                            FROM lid_db.ZWAZACCOUNT
                            WHERE ZIDENTIFIER LIKE '%@lid'
                              AND ZPHONENUMBER IS NOT NULL
                              AND ZPHONENUMBER != ''
                            ON CONFLICT(jid) DO UPDATE SET
                                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                        """)
                try:
                    conn.execute("""
                        INSERT INTO _ios_contacts (jid, full_name, phone_number)
                        SELECT
                            ZJID AS jid,
                            ZPUSHNAME AS full_name,
                            CASE WHEN ZJID LIKE '%@s.whatsapp.net'
                                 THEN SUBSTR(ZJID, 1, INSTR(ZJID, '@') - 1)
                            END AS phone_number
                        FROM ZWAPROFILEPUSHNAME
                        WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != ''
                        ON CONFLICT(jid) DO UPDATE SET
                            full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                            phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                    """)
                    conn.execute("""
                        INSERT INTO _ios_contacts (jid, full_name, phone_number)
                        SELECT
                            SUBSTR(ZJID, 1, INSTR(ZJID, '@') - 1) AS jid,
                            ZPUSHNAME AS full_name,
                            CASE WHEN ZJID LIKE '%@s.whatsapp.net'
                                 THEN SUBSTR(ZJID, 1, INSTR(ZJID, '@') - 1)
                            END AS phone_number
                        FROM ZWAPROFILEPUSHNAME
                        WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != ''
                        ON CONFLICT(jid) DO UPDATE SET
                            full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                            phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                    """)
                except sqlite3.OperationalError:
                    pass
                try:
                    cs_cols = {
                        r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()
                    }
                    if "ZCONTACTIDENTIFIER" in cs_cols:
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                cs.ZCONTACTJID AS jid,
                                NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                                CASE WHEN cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                                     THEN SUBSTR(cs.ZCONTACTIDENTIFIER, 1, INSTR(cs.ZCONTACTIDENTIFIER, '@') - 1)
                                END AS phone_number
                            FROM ZWACHATSESSION cs
                            WHERE cs.ZCONTACTJID LIKE '%@lid'
                              AND cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                        """)
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS jid,
                                NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                                CASE WHEN cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                                     THEN SUBSTR(cs.ZCONTACTIDENTIFIER, 1, INSTR(cs.ZCONTACTIDENTIFIER, '@') - 1)
                                END AS phone_number
                            FROM ZWACHATSESSION cs
                            WHERE cs.ZCONTACTJID LIKE '%@lid'
                              AND cs.ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                        """)
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                cs.ZCONTACTIDENTIFIER AS jid,
                                NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                                SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                            FROM ZWACHATSESSION cs
                            WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                              AND cs.ZCONTACTIDENTIFIER LIKE '%@lid'
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                        """)
                        conn.execute("""
                            INSERT INTO _ios_contacts (jid, full_name, phone_number)
                            SELECT
                                SUBSTR(cs.ZCONTACTIDENTIFIER, 1, INSTR(cs.ZCONTACTIDENTIFIER, '@') - 1) AS jid,
                                NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                                SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                            FROM ZWACHATSESSION cs
                            WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                              AND cs.ZCONTACTIDENTIFIER LIKE '%@lid'
                            ON CONFLICT(jid) DO UPDATE SET
                                full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                                phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                        """)
                    conn.execute("""
                        INSERT INTO _ios_contacts (jid, full_name, phone_number)
                        SELECT
                            cs.ZCONTACTJID AS jid,
                            NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                            SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                        FROM ZWACHATSESSION cs
                        WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                          AND cs.ZPARTNERNAME IS NOT NULL AND cs.ZPARTNERNAME != ''
                        ON CONFLICT(jid) DO UPDATE SET
                            full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                            phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                    """)
                    conn.execute("""
                        INSERT INTO _ios_contacts (jid, full_name, phone_number)
                        SELECT
                            SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS jid,
                            NULLIF(cs.ZPARTNERNAME, '') AS full_name,
                            SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) AS phone_number
                        FROM ZWACHATSESSION cs
                        WHERE cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                          AND cs.ZPARTNERNAME IS NOT NULL AND cs.ZPARTNERNAME != ''
                        ON CONFLICT(jid) DO UPDATE SET
                            full_name = COALESCE(NULLIF(_ios_contacts.full_name, ''), excluded.full_name),
                            phone_number = COALESCE(_ios_contacts.phone_number, excluded.phone_number)
                    """)
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("""
                        UPDATE _ios_contacts
                        SET full_name = (
                            SELECT con.display_name
                            FROM arch.contacts con
                            WHERE (con.number = _ios_contacts.phone_number
                               OR con.number = _ios_contacts.jid
                               OR con.number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid || '@', '@') - 1))
                              AND con.display_name IS NOT NULL
                              AND con.display_name != ''
                              AND con.display_name NOT LIKE 'Unknown%'
                            LIMIT 1
                        )
                        WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
                          AND EXISTS (
                              SELECT 1 FROM arch.contacts con
                              WHERE (con.number = _ios_contacts.phone_number
                                 OR con.number = _ios_contacts.jid
                                 OR con.number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid || '@', '@') - 1))
                                AND con.display_name IS NOT NULL
                                AND con.display_name != ''
                                AND con.display_name NOT LIKE 'Unknown%'
                          )
                    """)
                except sqlite3.OperationalError:
                    pass
                try:
                    conn.execute("""
                        UPDATE _ios_contacts
                        SET full_name = (
                            SELECT p.full_name FROM _ios_contacts p
                            WHERE p.jid = (_ios_contacts.phone_number || '@s.whatsapp.net')
                              AND p.full_name IS NOT NULL AND p.full_name != ''
                              AND p.full_name NOT LIKE '+%'
                            LIMIT 1
                        )
                        WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
                          AND phone_number IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM _ios_contacts p
                              WHERE p.jid = (_ios_contacts.phone_number || '@s.whatsapp.net')
                                AND p.full_name IS NOT NULL AND p.full_name != ''
                                AND p.full_name NOT LIKE '+%'
                          )
                    """)
                    conn.execute("""
                        UPDATE _ios_contacts
                        SET full_name = (
                            SELECT l.full_name FROM _ios_contacts l
                            WHERE l.phone_number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid, '@') - 1)
                              AND l.full_name IS NOT NULL AND l.full_name != ''
                              AND l.full_name NOT LIKE '+%'
                            LIMIT 1
                        )
                        WHERE (full_name IS NULL OR full_name = '' OR full_name LIKE '+%')
                          AND _ios_contacts.jid LIKE '%@s.whatsapp.net'
                          AND EXISTS (
                              SELECT 1 FROM _ios_contacts l
                              WHERE l.phone_number = SUBSTR(_ios_contacts.jid, 1, INSTR(_ios_contacts.jid, '@') - 1)
                                AND l.full_name IS NOT NULL AND l.full_name != ''
                                AND l.full_name NOT LIKE '+%'
                          )
                    """)
                except sqlite3.OperationalError:
                    pass
                try:
                    user_row = conn.execute(
                        "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZFROMJID LIKE '%@g.us' AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                    ).fetchone()
                    if not user_row:
                        user_row = conn.execute(
                            "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZISFROMME = 0 AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                        ).fetchone()
                    if user_row and user_row[0]:
                        u_jid = user_row[0]
                        u_phone = u_jid.split("@")[0]
                        conn.execute(
                            "INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)",
                            (u_jid, u_phone),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)",
                            (u_phone, u_phone),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)",
                            (f"+{u_phone}", u_phone),
                        )
                except sqlite3.OperationalError:
                    pass
                conn.execute("CREATE INDEX IF NOT EXISTS _ios_contacts_jid ON _ios_contacts(jid)")
                infra_db = _find_ios_db(wa_db_path, "MessagingInfraDatabase.sqlite", output_root)
                if not infra_db:
                    infra_db = _find_ios_db(
                        wa_db_path, "MessagingInfraDB_v2/MessagingInfraDatabase.sqlite", output_root
                    )
                if infra_db:
                    conn.execute("ATTACH DATABASE ? AS infra", (str(infra_db),))
                ext_db = _find_ios_db(wa_db_path, "ExtChatDatabase.sqlite", output_root)
                if not ext_db:
                    ext_db = _find_ios_db(
                        wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", output_root
                    )
                if ext_db:
                    try:
                        conn.execute("ATTACH DATABASE ? AS ext", (str(ext_db),))
                    except sqlite3.OperationalError:
                        pass
            _wa_local.conn = conn
        return conn

    def get_preserved():
        if preserved_db_path is None or not preserved_db_path.exists():
            return None
        conn = getattr(_wa_local, "pres_conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{preserved_db_path.resolve().as_posix()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            _wa_local.pres_conn = conn
        return conn

    @app.teardown_appcontext
    def _close_wa_conn(exc):
        if source_type is not None:
            conn = getattr(_wa_local, "conn", None)
            if conn is not None:
                try:
                    conn.commit()
                except sqlite3.Error:
                    pass
        pres_conn = getattr(_wa_local, "pres_conn", None)
        if pres_conn is not None:
            try:
                pres_conn.commit()
            except sqlite3.Error:
                pass

    def get_cache():
        return cache_conn

    _ensure_writable(archive_db_path)
    _archive_conn = sqlite3.connect(str(archive_db_path), check_same_thread=False)
    _archive_conn.execute("PRAGMA journal_mode = WAL")
    _archive_conn.execute("PRAGMA busy_timeout = 5000")
    _archive_conn.row_factory = sqlite3.Row
    _archive_conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number TEXT PRIMARY KEY,
            folder TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_row_id TEXT PRIMARY KEY,
            folder TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path TEXT PRIMARY KEY,
            archive_path TEXT NOT NULL,
            sha256 TEXT,
            is_hardlink INTEGER NOT NULL DEFAULT 0,
            timestamp_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS recent_messages (
            chat_id        TEXT NOT NULL,
            chat_type      TEXT NOT NULL,
            msg_id         INTEGER NOT NULL,
            timestamp_ms   INTEGER NOT NULL,
            sender         TEXT NOT NULL,
            from_me        INTEGER NOT NULL,
            archive_path   TEXT,
            media_type     TEXT NOT NULL DEFAULT 'text',
            media_name     TEXT,
            text_body      TEXT NOT NULL,
            quoted_text    TEXT,
            quoted_sender  TEXT,
            quoted_ts      INTEGER,
            quoted_msg_id  INTEGER,
            reactions      TEXT,
            is_edited      INTEGER NOT NULL DEFAULT 0,
            edited_ts      INTEGER,
            is_view_once   INTEGER NOT NULL DEFAULT 0,
            link_title     TEXT,
            link_desc      TEXT,
            link_url       TEXT,
            link_preview_type INTEGER NOT NULL DEFAULT 0,
            has_thumbnail  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, chat_type, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recent_chat_ts
            ON recent_messages(chat_id, chat_type, timestamp_ms DESC);

        CREATE TABLE IF NOT EXISTS reactions_cache (
            chat_id    TEXT NOT NULL,
            msg_id     INTEGER NOT NULL,
            reactions  TEXT NOT NULL,
            PRIMARY KEY (chat_id, msg_id)
        );
    """)
    copy_cols = [
        r[1] for r in _archive_conn.execute("PRAGMA table_info(archive_copies)").fetchall()
    ]
    if "is_hardlink" not in copy_cols:
        _archive_conn.execute(
            "ALTER TABLE archive_copies ADD COLUMN is_hardlink INTEGER NOT NULL DEFAULT 0"
        )
    if "timestamp_ms" not in copy_cols:
        _archive_conn.execute("ALTER TABLE archive_copies ADD COLUMN timestamp_ms INTEGER")

    cols = [r[1] for r in _archive_conn.execute("PRAGMA table_info(recent_messages)").fetchall()]
    if "reactions" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN reactions TEXT")
    if "is_edited" not in cols:
        _archive_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN is_edited INTEGER NOT NULL DEFAULT 0"
        )
    if "edited_ts" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN edited_ts INTEGER")
    if "is_view_once" not in cols:
        _archive_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN is_view_once INTEGER NOT NULL DEFAULT 0"
        )
    if "quoted_msg_id" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN quoted_msg_id INTEGER")
    if "link_title" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN link_title TEXT")
    if "link_desc" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN link_desc TEXT")
    if "link_url" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN link_url TEXT")
    if "link_preview_type" not in cols:
        _archive_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN link_preview_type INTEGER NOT NULL DEFAULT 0"
        )
    if "has_thumbnail" not in cols:
        _archive_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN has_thumbnail INTEGER NOT NULL DEFAULT 0"
        )
    if "quoted_has_thumb" not in cols:
        _archive_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN quoted_has_thumb INTEGER NOT NULL DEFAULT 0"
        )
    if rescan or (source_is_changed and has_prior_stamp):
        _archive_conn.execute("DELETE FROM recent_messages")
        try:
            _archive_conn.execute("DELETE FROM reactions_cache")
        except sqlite3.OperationalError:
            pass
    try:
        _archive_conn.execute(
            """UPDATE recent_messages
               SET media_type = 'deleted',
                   text_body = CASE WHEN from_me = 1 THEN 'You deleted this message' ELSE 'This message was deleted' END
               WHERE chat_type = 'contact'
                 AND (
                     media_type = 'service'
                     OR text_body LIKE '%group subject%'
                     OR text_body LIKE '%group description%'
                 )"""
        )
    except sqlite3.OperationalError:
        pass
    _archive_conn.commit()

    _run_db_maintenance(cache_conn, _archive_conn)

    # Build _ANDROID_SELECT and _IOS_SELECT based on available tables
    if not has_android_hd and source_type == "android" and wa_db_path is not None:
        try:
            tmp_chk = sqlite3.connect(wa_db_path)
            has_android_hd = _check_android_hd_association(tmp_chk)
            tmp_chk.close()
        except sqlite3.OperationalError:
            has_android_hd = False
    has_reactions = False
    has_ios_reactions = False
    has_lid_dn = False
    has_message_system = False
    has_mcp = False
    has_ios_group_event = False
    has_jid_server = False
    has_jid_raw_string = False
    has_android_edit_info = False
    has_android_add_on = False
    has_mei_ts = False
    has_mao_ts = False
    has_android_key_id = False
    has_ios_stanza_id = False
    has_message_thumbnail = False
    has_media_hash_thumbnail = False
    has_message_quoted_media = False
    has_mm_width = False
    has_mm_height = False
    has_mm_size = False
    has_mm_duration = False
    has_mm_file_hash = False
    has_ios_aspect_ratio = False
    has_ios_filesize = False
    has_ios_duration = False
    has_mq_key_id = False
    has_mq_msg_type = False
    has_mq_pm_chat = False
    has_message_view_once_media = False
    has_message_revoked = False
    _mi_cols = set()
    if wa_db_path is not None:
        _tmp_wa = sqlite3.connect(str(wa_db_path))
        _wa_tables = {
            r[0]
            for r in _tmp_wa.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        has_message_view_once_media = "message_view_once_media" in _wa_tables
        has_message_revoked = "message_revoked" in _wa_tables
        has_reactions = "message_add_on" in _wa_tables and "message_add_on_reaction" in _wa_tables
        has_message_poll = "message_poll" in _wa_tables
        _mao_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_add_on)").fetchall()}
            if "message_add_on" in _wa_tables
            else set()
        )
        has_message_add_on = "message_add_on" in _wa_tables and "message_add_on_type" in _mao_cols
        has_ios_reactions = "ZWAMESSAGEINFO" in _wa_tables
        has_lid_dn = "lid_display_name" in _wa_tables
        has_message_system = "message_system" in _wa_tables
        has_mcp = "message_system_chat_participant" in _wa_tables
        has_message_thumbnail = "message_thumbnail" in _wa_tables
        has_media_hash_thumbnail = "media_hash_thumbnail" in _wa_tables
        has_message_quoted_media = "message_quoted_media" in _wa_tables
        _mq_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_quoted)").fetchall()}
            if "message_quoted" in _wa_tables
            else set()
        )
        has_mq_key_id = "key_id" in _mq_cols
        has_mq_msg_type = "message_type" in _mq_cols
        has_mq_pm_chat = "parent_message_chat_row_id" in _mq_cols
        _mm_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_media)").fetchall()}
            if "message_media" in _wa_tables
            else set()
        )
        has_mm_width = "width" in _mm_cols
        has_mm_height = "height" in _mm_cols
        has_mm_size = "file_size" in _mm_cols
        has_mm_duration = "media_duration" in _mm_cols
        has_mm_file_hash = "file_hash" in _mm_cols
        _msg_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message)").fetchall()}
            if "message" in _wa_tables
            else set()
        )
        has_android_key_id = "key_id" in _msg_cols
        _mei_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_edit_info)").fetchall()}
            if "message_edit_info" in _wa_tables
            else set()
        )
        has_android_edit_info = "message_edit_info" in _wa_tables and "message_row_id" in _mei_cols
        has_mei_ts = "edited_timestamp" in _mei_cols

        _mao_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_add_on)").fetchall()}
            if "message_add_on" in _wa_tables
            else set()
        )
        has_android_add_on = (
            "message_add_on" in _wa_tables
            and "message_add_on_type" in _mao_cols
            and "parent_message_row_id" in _mao_cols
        )
        has_mao_ts = "timestamp" in _mao_cols
        _jid_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(jid)").fetchall()}
            if "jid" in _wa_tables
            else set()
        )
        has_jid_server = "server" in _jid_cols
        has_jid_raw_string = "raw_string" in _jid_cols
        _mi_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
            if "ZWAMEDIAITEM" in _wa_tables
            else set()
        )
        has_ios_vcard_name = "ZVCARDNAME" in _mi_cols
        has_ios_aspect_ratio = "ZASPECTRATIO" in _mi_cols
        has_ios_filesize = "ZFILESIZE" in _mi_cols
        has_ios_duration = "ZMOVIEDURATION" in _mi_cols
        has_ios_title = "ZTITLE" in _mi_cols
        has_ios_mediaurl = "ZMEDIAURL" in _mi_cols
        has_ios_metadata = "ZMETADATA" in _mi_cols
        has_message_text = "message_text" in _wa_tables
        _mt_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_text)").fetchall()}
            if has_message_text
            else set()
        )
        has_mt_title = "page_title" in _mt_cols
        has_mt_desc = "description" in _mt_cols
        has_mt_url = "url" in _mt_cols
        has_mt_preview_type = "preview_type" in _mt_cols
        _zwa_cols = (
            {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
            if "ZWAMESSAGE" in _wa_tables
            else set()
        )
        has_ios_group_event = "ZGROUPEVENTTYPE" in _zwa_cols
        has_ios_stanza_id = "ZSTANZAID" in _zwa_cols
        _tmp_wa.close()

    if source_type == "ios":
        vcard_name_ios = "mi.ZVCARDNAME" if has_ios_vcard_name else "NULL"
        group_ev_col = "m.ZGROUPEVENTTYPE" if has_ios_group_event else "0"
        has_ios_sort = "ZSORT" in _zwa_cols
        sort_col_ios = "COALESCE(m.ZSORT, m.Z_PK)" if has_ios_sort else "m.Z_PK"
        stanza_col_ios = (
            "COALESCE(NULLIF(m.ZSTANZAID, ''), 'msg_' || m.Z_PK)"
            if has_ios_stanza_id
            else "'msg_' || m.Z_PK"
        )
        quoted_stanza_id_ios = "qm.ZSTANZAID" if has_ios_stanza_id else "NULL"
        ios_filesize_col = "COALESCE(mi.ZFILESIZE, 0)" if has_ios_filesize else "0"
        ios_duration_col = "COALESCE(mi.ZMOVIEDURATION, 0)" if has_ios_duration else "0"
        ios_aspect_ratio_col = "COALESCE(mi.ZASPECTRATIO, 0.0)" if has_ios_aspect_ratio else "0.0"
        ios_link_title = (
            "CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZTITLE, '') END"
            if has_ios_title
            else "NULL"
        )
        ios_link_url = (
            "CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZMEDIAURL, '') END"
            if has_ios_mediaurl
            else "NULL"
        )
        ios_raw_metadata = "mi.ZMETADATA" if has_ios_metadata else "NULL"
        ios_thumb_arms = []
        if "ZXMPPTHUMBPATH" in _mi_cols:
            ios_thumb_arms.append("mi.ZXMPPTHUMBPATH IS NOT NULL")
        if "ZTHUMBNAILLOCALPATH" in _mi_cols:
            ios_thumb_arms.append("mi.ZTHUMBNAILLOCALPATH IS NOT NULL")
        if "ZMETADATA" in _mi_cols:
            ios_thumb_arms.append(
                "(m.ZMESSAGETYPE != 0 AND mi.ZMETADATA IS NOT NULL AND INSTR(mi.ZMETADATA, X'ffd8ff') > 0)"
            )
        ios_has_thumb_expr = (
            "CASE WHEN " + " OR ".join(ios_thumb_arms) + " THEN 1 ELSE 0 END"
            if ios_thumb_arms
            else "0"
        )
        ios_service_filter = (
            "OR (m.ZMESSAGETYPE IN (6, 10) AND m.ZGROUPEVENTTYPE IN (1, 2, 3, 4, 5, 6, 7, 9, 10, 12, 15, 21, 26, 36, 37, 38, 44, 50, 56, 58))"
            if has_ios_group_event
            else "OR (m.ZMESSAGETYPE IN (6, 10))"
        )
        ios_service_case = (
            "WHEN m.ZMESSAGETYPE = 6 OR (m.ZMESSAGETYPE = 10 AND m.ZGROUPEVENTTYPE IS NOT NULL AND m.ZGROUPEVENTTYPE > 0) THEN 'service'"
            if has_ios_group_event
            else "WHEN m.ZMESSAGETYPE IN (6, 10) THEN 'service'"
        )
        globals()["_BASE_IOS_FILTER"] = f"""
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND m.ZMESSAGETYPE NOT IN (6, 10) AND (mi.ZMEDIALOCALPATH IS NOT NULL OR m.ZMESSAGETYPE IN (1, 2, 3, 5, 8, 11, 15, 23, 38, 39, 54) OR mi.ZTITLE IS NOT NULL))
        OR (m.ZMESSAGETYPE = 4)
        OR (m.ZMESSAGETYPE = 14)
        OR (m.ZMESSAGETYPE IN (38, 39, 53))
        OR (m.ZMESSAGETYPE = 46)
        OR (m.ZMESSAGETYPE = 7)
        {ios_service_filter}
    )
"""
        globals()["_BASE_IOS_FILTER_TS"] = globals()["_BASE_IOS_FILTER"]
        if has_ios_hd:
            globals()["_IOS_FILTER"] = globals()["_BASE_IOS_FILTER"] + _IOS_HD_DEDUP_CLAUSE
            globals()["_IOS_FILTER_TS"] = globals()["_BASE_IOS_FILTER_TS"] + _IOS_HD_DEDUP_CLAUSE
        else:
            globals()["_IOS_FILTER"] = globals()["_BASE_IOS_FILTER"]
            globals()["_IOS_FILTER_TS"] = globals()["_BASE_IOS_FILTER_TS"]

        globals()["_IOS_SELECT"] = f"""
    SELECT
        m.Z_PK                                                       AS msg_id,
        {stanza_col_ios}                                             AS stanza_id,
        {sort_col_ios}                                               AS sort_id,
        {_IOS_CHAT_ID}                                               AS chat_id,
        {_IOS_CHAT_TYPE}                                             AS chat_type,
        CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)        AS timestamp_ms,
        COALESCE(
            CASE WHEN ({_IOS_SENDER_JID}) = '0' THEN 'WhatsApp' END,
            CASE WHEN ic_s.full_name NOT LIKE '+%' THEN NULLIF(ic_s.full_name, '') END,
            NULLIF(con_s.display_name, ''),
            NULLIF(con_lid_s.display_name, ''),
            NULLIF(pp_lid.ZPUSHNAME, ''),
            NULLIF(pp_phone.ZPUSHNAME, ''),
            NULLIF(cs_lid.ZPARTNERNAME, ''),
            NULLIF(cs_phone.ZPARTNERNAME, ''),
            CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN NULLIF(m.ZPUSHNAME, '') END,
            CASE WHEN COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            ) IS NOT NULL
            THEN '+' || COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            )
            END,
            ''
        )                                                            AS sender,
        m.ZISFROMME                                                  AS from_me,
        ac.archive_path,
        CASE
            WHEN m.ZMESSAGETYPE = 14 THEN 'deleted'
            {ios_service_case}
            WHEN m.ZMESSAGETYPE = 4 THEN 'vcard'
            WHEN m.ZMESSAGETYPE = 5 THEN 'location'
            WHEN m.ZMESSAGETYPE = 15 THEN 'sticker'
            WHEN m.ZMESSAGETYPE IN (1, 38) THEN 'image'
            WHEN m.ZMESSAGETYPE IN (3, 39) THEN 'video'
            WHEN m.ZMESSAGETYPE IN (2, 53) THEN 'audio'
            WHEN m.ZMESSAGETYPE = 8 THEN 'document'
            WHEN m.ZMESSAGETYPE = 11 THEN 'gif'
            WHEN m.ZMESSAGETYPE = 46 THEN 'poll'
            WHEN m.ZMESSAGETYPE = 7 THEN 'link'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="COALESCE('Message/' || mi.ZMEDIALOCALPATH, mi.ZTITLE)")}
        END                                                           AS media_type,
        COALESCE(
            CASE WHEN m.ZMESSAGETYPE = 4 THEN {vcard_name_ios} END,
            CASE WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(m.ZTEXT, '') END,
            mi.ZMEDIALOCALPATH,
            ''
        )                                                             AS media_name,
        COALESCE(
            CASE
                WHEN m.ZISFROMME = 1 AND m.ZMESSAGETYPE = 14 THEN 'You deleted this message'
                WHEN m.ZMESSAGETYPE = 14 THEN 'This message was deleted'
                WHEN m.ZMESSAGETYPE = 4 THEN {vcard_name_ios}
                WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(mi.ZTITLE, '')
                WHEN m.ZMESSAGETYPE IN (1, 3, 11, 38, 39, 54) THEN COALESCE(NULLIF(m.ZTEXT, ''), NULLIF(mi.ZTITLE, ''))
                ELSE m.ZTEXT
            END,
            ''
        )                                                             AS text_body,
        COALESCE(
            qm.ZTEXT,
            CASE
                WHEN qm.ZMESSAGETYPE = 46 THEN 'Poll'
                WHEN qm.ZMESSAGETYPE = 1 THEN '📷 Photo'
                WHEN qm.ZMESSAGETYPE = 2 THEN '🎵 Audio'
                WHEN qm.ZMESSAGETYPE = 3 THEN '🎥 Video'
                WHEN qm.ZMESSAGETYPE = 15 THEN 'Sticker'
            END,
            ''
        )                                                             AS quoted_text,
        CASE WHEN qm.ZISFROMME = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 NULLIF(qm.ZPUSHNAME, ''),
                 CASE WHEN SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                   INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1) != ''
                      THEN '+' || SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                         INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
                 END,
                 '')
        END                                                          AS quoted_sender,
        COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0) AS quoted_ts,
        qm.Z_PK                                                      AS quoted_msg_id,
        {quoted_stanza_id_ios}                                       AS quoted_stanza_id,
        NULL                                                          AS reactions,
        0                                                            AS is_edited,
        NULL                                                          AS edited_ts,
        CASE WHEN m.ZMESSAGETYPE IN (38, 39, 53) THEN 1 ELSE 0 END   AS is_view_once,
        m.ZMESSAGETYPE                                               AS raw_msg_type,
        {group_ev_col}                                               AS group_event_type,
        gm.ZMEMBERJID                                                AS member_jid,
        0                                                            AS media_width,
        0                                                            AS media_height,
        {ios_filesize_col}                                           AS media_size,
        {ios_duration_col}                                           AS media_duration,
        {ios_aspect_ratio_col}                                       AS aspect_ratio,
        {ios_has_thumb_expr}                                         AS has_thumbnail,
        {ios_link_title}                                             AS link_title,
        NULL                                                         AS link_desc,
        {ios_link_url}                                               AS link_url,
        0                                                            AS link_preview_type,
        {ios_raw_metadata}                                           AS _raw_metadata
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
    LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = ({_IOS_SENDER_RAW})
    LEFT JOIN _ios_contacts ic_s ON ic_s.jid = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE
    LEFT JOIN arch.contacts con_s
          ON con_s.number = COALESCE(
              CASE WHEN ({_IOS_SENDER_RAW}) LIKE '%@lid' THEN ic_s.phone_number END,
              ({_IOS_SENDER_JID})
          )
    LEFT JOIN arch.contacts con_lid_s
          ON con_lid_s.number = SUBSTR(({_IOS_SENDER_RAW}), 1,
                                       INSTR(({_IOS_SENDER_RAW}) || '@', '@') - 1)
    LEFT JOIN arch.contacts con_sq
          ON con_sq.number = SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                    INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
    LEFT JOIN arch.archive_copies ac
          ON ac.original_path = COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)
"""

    ms_join = (
        "LEFT JOIN message_system ms ON ms.message_row_id = m._id" if has_message_system else ""
    )
    ms_col = "ms.action_type" if has_message_system else "NULL"
    mrev_join = (
        "    LEFT JOIN message_revoked mrev ON mrev.message_row_id = m._id\n"
        if has_message_revoked
        else ""
    )
    mrev_pred = "mrev.message_row_id IS NOT NULL" if has_message_revoked else "0"

    ms_action_clause = (
        f"ms.action_type IN {tuple(_SUPPORTED_ANDROID_SYSTEM_ACTIONS)}"
        if has_message_system
        else "(m.text_data IS NOT NULL AND m.text_data != '')"
    )
    android_hd_clause = _ANDROID_HD_DEDUP_CLAUSE if has_android_hd else ""

    poll_subquery_parts = []
    if has_message_poll:
        poll_subquery_parts.append(
            "EXISTS (SELECT 1 FROM message_poll mp WHERE mp.message_row_id = m._id)"
        )
    if has_message_add_on:
        poll_subquery_parts.append(
            "EXISTS (SELECT 1 FROM message_add_on mao WHERE mao.parent_message_row_id = m._id AND mao.message_add_on_type = 67)"
        )
    extra_poll_filter = (
        f"\n        OR ({' OR '.join(poll_subquery_parts)})" if poll_subquery_parts else ""
    )
    extra_poll_select = f" OR ({' OR '.join(poll_subquery_parts)})" if poll_subquery_parts else ""
    mvom_filter_or = " OR (mvom.message_row_id IS NOT NULL)" if has_message_view_once_media else ""
    extra_mrev_filter = (
        "\n        OR (EXISTS (SELECT 1 FROM message_revoked mrev_f WHERE mrev_f.message_row_id = m._id))"
        if has_message_revoked
        else ""
    )

    globals()["_ANDROID_FILTER"] = f"""
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND m.message_type != 7 AND (mm.file_path IS NOT NULL OR mm.message_row_id IS NOT NULL OR m.message_type IN (1, 2, 3, 5, 9, 13, 16, 20, 28)))
        OR (m.message_type IN (4, 14))
        OR (m.message_type IN (15, 64){extra_mrev_filter})
        OR (m.message_type IN (42, 43, 82){mvom_filter_or})
        OR (m.message_type = 7 AND {ms_action_clause})
        OR (m.message_type = 66){extra_poll_filter}
    )
    {android_hd_clause}
    """
    globals()["_ANDROID_FILTER_TS"] = globals()["_ANDROID_FILTER"]

    ldn_select_joins = (
        """
    LEFT JOIN _lid_map_resolved jm_lid_s ON jm_lid_s.jid_row_id = m.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_lid_s.lid_row_id

    LEFT JOIN _lid_map_resolved jm_lid_sq ON jm_lid_sq.jid_row_id = mq.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_sq_direct ON ldn_sq_direct.lid_row_id = mq.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_sq_mapped ON ldn_sq_mapped.lid_row_id = jm_lid_sq.lid_row_id
"""
        if has_lid_dn
        else ""
    )

    ldn_sender_arm = (
        """
            NULLIF(ldn_s_direct.display_name, ''),
            NULLIF(ldn_s_mapped.display_name, ''),
"""
        if has_lid_dn
        else ""
    )

    ldn_quoted_sender_arm = (
        """
                 NULLIF(ldn_sq_direct.display_name, ''),
                 NULLIF(ldn_sq_mapped.display_name, ''),
"""
        if has_lid_dn
        else ""
    )

    j_is_lid = "j.server = 'lid'" if has_jid_server else "0"
    jq_is_lid = "jq.server = 'lid'" if has_jid_server else "0"

    if has_android_edit_info:
        edit_join = "LEFT JOIN message_edit_info mei ON mei.message_row_id = m._id"
        edit_is_edited_col = "CASE WHEN mei.message_row_id IS NOT NULL THEN 1 ELSE 0 END"
        edit_ts_col = "mei.edited_timestamp" if has_mei_ts else "NULL"
    elif has_android_add_on:
        edit_join = (
            "LEFT JOIN message_add_on mao_edit "
            "ON mao_edit.parent_message_row_id = m._id AND mao_edit.message_add_on_type = 74"
        )
        edit_is_edited_col = "CASE WHEN mao_edit._id IS NOT NULL THEN 1 ELSE 0 END"
        edit_ts_col = "mao_edit.timestamp" if has_mao_ts else "NULL"
    else:
        edit_join = ""
        edit_is_edited_col = "0"
        edit_ts_col = "NULL"

    stanza_col_android = (
        "COALESCE(NULLIF(m.key_id, ''), 'msg_' || m._id)"
        if has_android_key_id
        else "'msg_' || m._id"
    )

    mm_width_col = "COALESCE(mm.width, 0)" if has_mm_width else "0"
    mm_height_col = "COALESCE(mm.height, 0)" if has_mm_height else "0"
    mm_size_col = "COALESCE(mm.file_size, 0)" if has_mm_size else "0"
    mm_duration_col = "COALESCE(mm.media_duration, 0)" if has_mm_duration else "0"
    mm_aspect_col = (
        "CASE WHEN mm.height > 0 AND mm.width > 0 "
        "THEN ROUND(CAST(mm.width AS REAL) / CAST(mm.height AS REAL), 3) "
        "ELSE 0.0 END"
        if (has_mm_width and has_mm_height)
        else "0.0"
    )

    android_thumb_joins = ""
    android_has_thumb_arms = []
    if has_message_thumbnail:
        android_thumb_joins += "    LEFT JOIN message_thumbnail mt ON mt.message_row_id = m._id\n"
        android_has_thumb_arms.append("mt.message_row_id IS NOT NULL")
    if has_media_hash_thumbnail and has_mm_file_hash:
        android_thumb_joins += (
            "    LEFT JOIN media_hash_thumbnail mht ON mht.media_hash = mm.file_hash\n"
        )
        android_has_thumb_arms.append("mht.media_hash IS NOT NULL")
    if has_message_quoted_media:
        android_thumb_joins += (
            "    LEFT JOIN message_quoted_media mqm ON mqm.message_row_id = m._id\n"
        )
        android_has_thumb_arms.append(
            "(mqm.message_row_id IS NOT NULL AND mqm.thumbnail IS NOT NULL)"
        )
    if has_message_quoted_media and has_mq_key_id and has_android_key_id:
        android_has_thumb_arms.append(
            "(m.message_type IN (1, 3, 9, 20, 26, 42, 43, 82) AND EXISTS ("
            "    SELECT 1 FROM message_quoted mq_orig "
            "    JOIN message_quoted_media mqm_orig ON mqm_orig.message_row_id = mq_orig.message_row_id "
            "    WHERE mq_orig.key_id = m.key_id AND mqm_orig.thumbnail IS NOT NULL"
            "))"
        )

    android_has_thumb_expr = (
        "CASE WHEN " + " OR ".join(android_has_thumb_arms) + " THEN 1 ELSE 0 END"
        if android_has_thumb_arms
        else "0"
    )

    android_quoted_has_thumb_expr = (
        "CASE WHEN (mqm.message_row_id IS NOT NULL AND mqm.thumbnail IS NOT NULL) THEN 1 ELSE 0 END"
        if has_message_quoted_media
        else "0"
    )

    quoted_msg_id_col = "NULL"
    quoted_stanza_id_col = "NULL"
    quoted_ts_col = "COALESCE(mq.timestamp, 0)"
    m_orig_join = ""
    if has_android_key_id and has_mq_key_id:
        pm_clause = (
            "AND (m_orig.chat_row_id = mq.parent_message_chat_row_id OR mq.parent_message_chat_row_id IS NULL) "
            if has_mq_pm_chat
            else ""
        )
        m_orig_join = f"    LEFT JOIN message m_orig ON m_orig.key_id = mq.key_id {pm_clause}\n"
        quoted_msg_id_col = "m_orig._id"
        quoted_stanza_id_col = "mq.key_id"
        quoted_ts_col = "COALESCE(NULLIF(mq.timestamp, 0), m_orig.timestamp, 0)"

    orig_text_arm = (
        "NULLIF(m_orig.text_data, ''),\n            "
        if (has_android_key_id and has_mq_key_id)
        else ""
    )
    if has_mq_msg_type:
        quoted_text_expr = f"""COALESCE(
            NULLIF(mq.text_data, ''),
            {orig_text_arm}CASE
                WHEN mq.message_type IN (1, 42) THEN '📷 Photo'
                WHEN mq.message_type IN (2, 82) THEN '🎵 Audio'
                WHEN mq.message_type IN (3, 43) THEN '🎥 Video'
                WHEN mq.message_type = 20 THEN 'Sticker'
                WHEN mq.message_type = 66 THEN 'Poll'
            END,
            ''
        )"""
    else:
        quoted_text_expr = f"COALESCE(NULLIF(mq.text_data, ''), {orig_text_arm}'')"

    group_cond_parts = ["c.subject IS NOT NULL"]
    if has_jid_server:
        group_cond_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'g.us')")
    if has_jid_raw_string:
        group_cond_parts.append(
            "(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@g.us')"
        )
    is_group_chat_select = " OR ".join(group_cond_parts)

    android_chat_id = f"""
        CASE
            WHEN ({is_group_chat_select}) THEN CAST(m.chat_row_id AS TEXT)
            ELSE COALESCE(
                {_clean_user_sql("j_chat_real.user")},
                {_clean_user_sql("j_chat.user")},
                CAST(m.chat_row_id AS TEXT)
            )
        END
    """
    android_chat_type = f"CASE WHEN ({is_group_chat_select}) THEN 'group' ELSE 'contact' END"

    android_text_join = (
        "    LEFT JOIN message_text mtxt ON mtxt.message_row_id = m._id\n"
        if has_message_text
        else ""
    )
    mt_title_col = "NULLIF(mtxt.page_title, '')" if has_mt_title else "NULL"
    mt_desc_col = "NULLIF(mtxt.description, '')" if has_mt_desc else "NULL"
    mt_url_col = "NULLIF(mtxt.url, '')" if has_mt_url else "NULL"
    mt_preview_type_col = "COALESCE(mtxt.preview_type, 0)" if has_mt_preview_type else "0"
    mvom_join = (
        "    LEFT JOIN message_view_once_media mvom ON mvom.message_row_id = m._id\n"
        if has_message_view_once_media
        else ""
    )
    mvom_or = " OR mvom.message_row_id IS NOT NULL" if has_message_view_once_media else ""

    globals()["_ANDROID_SELECT"] = f"""
    SELECT
        m._id                                                        AS msg_id,
        {stanza_col_android}                                         AS stanza_id,
        {android_chat_id}                                           AS chat_id,
        {android_chat_type}                                         AS chat_type,
        COALESCE(m.timestamp, 0)                                     AS timestamp_ms,
        COALESCE(
            NULLIF(con_s.display_name, ''),
            NULLIF(con_s.folder, ''),
            CASE WHEN COALESCE(j2.user, CASE WHEN NOT ({j_is_lid}) THEN j.user END) = '0' THEN 'WhatsApp' END,
            CASE WHEN j2.user IS NOT NULL THEN '+' || {_clean_user_sql("j2.user")} END,
            {ldn_sender_arm}
            CASE WHEN NOT ({j_is_lid}) AND j.user IS NOT NULL THEN '+' || {_clean_user_sql("j.user")} END,
            ''
        )                                                            AS sender,
        m.from_me,
        ac.archive_path,
        CASE
            WHEN m.message_type IN (15, 64) OR {mrev_pred} THEN 'deleted'
            WHEN m.message_type = 7 AND ({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select}) THEN 'deleted'
            WHEN m.message_type = 7 THEN 'service'
            WHEN m.message_type IN (4, 14) THEN 'vcard'
            WHEN m.message_type = 20 THEN 'sticker'
            WHEN m.message_type = 42 THEN 'image'
            WHEN m.message_type = 43 THEN 'video'
            WHEN m.message_type = 82 THEN 'audio'
            WHEN m.message_type = 66{extra_poll_select} THEN 'poll'
            WHEN m.message_type IS NULL OR m.message_type = 0 THEN
                CASE WHEN m.text_data IS NOT NULL
                      AND (INSTR(LOWER(m.text_data), 'http://') > 0
                           OR INSTR(LOWER(m.text_data), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            WHEN m.message_type IN (5, 16) THEN 'link'
            WHEN mm.file_path IS NULL AND m.message_type = 1 THEN 'image'
            WHEN mm.file_path IS NULL AND m.message_type = 2 THEN 'audio'
            WHEN mm.file_path IS NULL AND m.message_type IN (3, 13, 28) THEN 'video'
            WHEN mm.file_path IS NULL AND m.message_type = 9 THEN 'document'
            ELSE {_MEDIA_TYPE_EXPR.format(col="COALESCE(mm.file_path, mm.media_name)")}
        END                                                          AS media_type,
        COALESCE(mm.media_name, CASE WHEN m.message_type IN (4, 14) THEN m.text_data END, '') AS media_name,
        COALESCE(
            m.text_data,
            CASE
                WHEN m.message_type = 64 THEN 'This message was deleted by an admin'
                WHEN (m.from_me = 1 AND (m.message_type = 15 OR {mrev_pred})) OR (m.from_me = 1 AND m.message_type = 7 AND ({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select})) THEN 'You deleted this message'
                WHEN m.message_type = 15 OR {mrev_pred} OR (m.message_type = 7 AND ({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select})) THEN 'This message was deleted'
                WHEN m.message_type IN (4, 14) THEN m.text_data
                WHEN m.message_type = 66{extra_poll_select} THEN 'Poll'
            END,
            ''
        )                                                            AS text_body,
        {quoted_text_expr}                                           AS quoted_text,
        CASE WHEN mq.from_me = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 NULLIF(con_sq.folder, ''),
                 CASE WHEN jq2.user IS NOT NULL THEN '+' || {_clean_user_sql("jq2.user")} END,
                 {ldn_quoted_sender_arm}
                 CASE WHEN NOT ({jq_is_lid}) AND jq.user IS NOT NULL THEN '+' || {_clean_user_sql("jq.user")} END,
                 '') END                                             AS quoted_sender,
        {quoted_ts_col}                                              AS quoted_ts,
        {quoted_msg_id_col}                                          AS quoted_msg_id,
        {quoted_stanza_id_col}                                       AS quoted_stanza_id,
        {android_quoted_has_thumb_expr}                              AS quoted_has_thumb,
        {edit_is_edited_col}                                         AS is_edited,
        {edit_ts_col}                                                AS edited_ts,
        CASE WHEN m.message_type IN (42, 43, 82){mvom_or} THEN 1 ELSE 0 END   AS is_view_once,
        {ms_col}                                                     AS action_type,
        ''                                                           AS participant_name,
        {mm_width_col}                                               AS media_width,
        {mm_height_col}                                              AS media_height,
        {mm_size_col}                                                AS media_size,
        {mm_duration_col}                                            AS media_duration,
        {mm_aspect_col}                                              AS aspect_ratio,
        {android_has_thumb_expr}                                     AS has_thumbnail,
        {mt_title_col}                                               AS link_title,
        {mt_desc_col}                                                AS link_desc,
        {mt_url_col}                                                 AS link_url,
        {mt_preview_type_col}                                        AS link_preview_type
    FROM message m
    LEFT JOIN message_media mm ON mm.message_row_id = m._id
{android_thumb_joins}{android_text_join}{mvom_join}    LEFT JOIN chat c ON c._id = m.chat_row_id
    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
    LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
    LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
    LEFT JOIN jid j ON j._id = m.sender_jid_row_id
    {_ANDROID_JID_MAP}
    LEFT JOIN message_quoted mq ON mq.message_row_id = m._id
{m_orig_join}    LEFT JOIN jid jq ON jq._id = mq.sender_jid_row_id
    LEFT JOIN _jid_map_resolved jmq ON jmq.lid_row_id = mq.sender_jid_row_id
    LEFT JOIN jid jq2 ON jq2._id = jmq.jid_row_id
    {ldn_select_joins}
    {ms_join}
    {mrev_join}
    {edit_join}
    LEFT JOIN _contacts_resolved con_s  ON con_s.clean_number  = {_clean_user_sql("COALESCE(j2.user, j.user)")}
    LEFT JOIN _contacts_resolved con_sq ON con_sq.clean_number = {_clean_user_sql("COALESCE(jq2.user, jq.user)")}
    LEFT JOIN arch.archive_copies ac ON ac.original_path IN (
        COALESCE(mm.file_path, 'vcard:' || m._id),
        REPLACE(COALESCE(mm.file_path, ''), '\\', '/'),
        REPLACE(COALESCE(mm.file_path, ''), '/', '\\'),
        CASE WHEN mm.file_path LIKE 'WhatsApp/%' OR mm.file_path LIKE 'WhatsApp\\%'
             THEN SUBSTR(REPLACE(mm.file_path, '\\', '/'), 10) END,
        CASE WHEN mm.file_path LIKE 'WhatsApp/%' OR mm.file_path LIKE 'WhatsApp\\%'
             THEN REPLACE(SUBSTR(REPLACE(mm.file_path, '\\', '/'), 10), '/', '\\') END,
        CASE WHEN mm.file_path NOT LIKE 'WhatsApp/%' AND mm.file_path NOT LIKE 'WhatsApp\\%' AND mm.file_path IS NOT NULL
             THEN 'WhatsApp/' || REPLACE(mm.file_path, '\\', '/') END,
        CASE WHEN mm.file_path NOT LIKE 'WhatsApp/%' AND mm.file_path NOT LIKE 'WhatsApp\\%' AND mm.file_path IS NOT NULL
             THEN 'WhatsApp\\' || REPLACE(mm.file_path, '/', '\\') END
    )
"""

    def get_archive():
        return _archive_conn

    # ---- API: list chats ---------------------------------------------------

    @app.route("/api/chats")
    def api_chats():
        conn = get_wa()
        if conn is None:
            result = []
            pres = get_preserved()
            if pres is not None:
                result = _merge_preserved_chats(result, pres, get_archive())
            return jsonify(result)

        if source_type == "android":
            j_s_is_lid = (
                "(j_s.server IS NOT NULL AND j_s.server = 'lid')" if has_jid_server else "0"
            )
            lid_c_parts = []
            if has_jid_server:
                lid_c_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'lid')")
            if has_jid_raw_string:
                lid_c_parts.append(
                    "(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@lid')"
                )
            j_c_is_lid = " OR ".join(lid_c_parts) if lid_c_parts else "0"

            group_c_parts = ["c.subject IS NOT NULL"]
            if has_jid_server:
                group_c_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'g.us')")
            if has_jid_raw_string:
                group_c_parts.append(
                    "(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@g.us')"
                )
            is_group_chat = " OR ".join(group_c_parts)
            ms_chat_col = "ms.action_type" if has_message_system else "NULL"
            ms_chat_join = (
                "LEFT JOIN message_system ms ON ms.message_row_id = m._id"
                if has_message_system
                else ""
            )
            ms_allowed_actions_tuple = tuple(ALLOWED_GROUP_SYSTEM_ACTIONS)
            ms_disp_join = (
                "LEFT JOIN message_system ms_disp ON ms_disp.message_row_id = m_disp._id"
                if has_message_system
                else ""
            )
            ms_sub_join = (
                "LEFT JOIN message_system ms_sub ON ms_sub.message_row_id = m_sub._id"
                if has_message_system
                else ""
            )
            ms_disp_col = "ms_disp.action_type" if has_message_system else "NULL"
            ms_sub_col = "ms_sub.action_type" if has_message_system else "NULL"
            ms_ts_col = "ms_ts.action_type" if has_message_system else "NULL"
            ms_disp_allowed_clause = (
                f"AND ms_disp.action_type IN {ms_allowed_actions_tuple}"
                if has_message_system
                else "AND 0"
            )
            ms_sub_allowed_clause = (
                f"AND ms_sub.action_type IN {ms_allowed_actions_tuple}"
                if has_message_system
                else "AND 0"
            )
            ms_ts_join = (
                "LEFT JOIN message_system ms_ts ON ms_ts.message_row_id = m_ts._id"
                if has_message_system
                else ""
            )
            ms_ts_allowed_clause = (
                f"AND ms_ts.action_type IN {ms_allowed_actions_tuple}"
                if has_message_system
                else "AND 0"
            )
            mrev_ts_filter = (
                "OR EXISTS (SELECT 1 FROM message_revoked mrev_ts WHERE mrev_ts.message_row_id = m_ts._id)"
                if has_message_revoked
                else ""
            )
            mrev_disp_filter = (
                "OR EXISTS (SELECT 1 FROM message_revoked mrev_disp WHERE mrev_disp.message_row_id = m_disp._id)"
                if has_message_revoked
                else ""
            )
            mrev_sub_filter = (
                "OR EXISTS (SELECT 1 FROM message_revoked mrev_sub WHERE mrev_sub.message_row_id = m_sub._id)"
                if has_message_revoked
                else ""
            )
            mrev_exist_filter = (
                "OR EXISTS (SELECT 1 FROM message_revoked mrev_ex WHERE mrev_ex.message_row_id = m._id)"
                if has_message_revoked
                else ""
            )
            ldn_s_join = (
                """
                LEFT JOIN _lid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_s.lid_row_id
            """
                if has_lid_dn
                else "LEFT JOIN _jid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id"
            )
            ldn_s_arm = (
                """
                NULLIF(ldn_s_direct.display_name, ''),
                NULLIF(ldn_s_mapped.display_name, ''),
            """
                if has_lid_dn
                else ""
            )
            ldn_c_join = (
                "LEFT JOIN lid_display_name ldn_c ON ldn_c.lid_row_id = c.jid_row_id"
                if has_lid_dn
                else ""
            )
            con_ldn_join = (
                f"LEFT JOIN _contacts_resolved con_ldn ON con_ldn.clean_number = {_clean_user_sql('ldn_c.display_name')}"
                if has_lid_dn
                else ""
            )
            con_ldn_arm = "NULLIF(con_ldn.display_name, '')," if has_lid_dn else ""
            con_ldn_folder_arm = "con_ldn.folder," if has_lid_dn else ""
            ldn_chat_name_arm = (
                "CASE WHEN ldn_c.display_name NOT LIKE '%∙%' AND ldn_c.display_name NOT LIKE '%\u2219%' THEN NULLIF(ldn_c.display_name, '') END,"
                if has_lid_dn
                else ""
            )
            ldn_chat_user_arm = "NULLIF(ldn_c.username, '')," if has_lid_dn else ""
            rows = conn.execute(f"""
                SELECT
                    CASE WHEN {is_group_chat} THEN CAST(c._id AS TEXT)
                         ELSE COALESCE(
                             {_clean_user_sql("j_chat_real.user")},
                             {_clean_user_sql("j_chat.user")},
                             CAST(c._id AS TEXT)
                         )
                    END                                                 AS id,
                    CASE WHEN {is_group_chat} THEN 'group'
                         ELSE 'contact' END                            AS type,
                    COALESCE(
                        NULLIF(c.subject, ''),
                        NULLIF(grp.subject, ''),
                        NULLIF(con.display_name, ''),
                        {con_ldn_arm}
                        CASE WHEN COALESCE(j_chat_real.user, j_chat.user) = '0' THEN 'WhatsApp' END,
                        con.folder,
                        {con_ldn_folder_arm}
                        {ldn_chat_name_arm}
                        {ldn_chat_user_arm}
                        CASE WHEN {is_group_chat} THEN 'Unknown Group (' || c._id || ')' END,
                        CASE WHEN j_chat_real.user IS NOT NULL THEN '+' || {_clean_user_sql("j_chat_real.user")} END,
                        CASE WHEN NOT ({j_c_is_lid}) AND j_chat.user IS NOT NULL THEN '+' || {_clean_user_sql("j_chat.user")} END,
                        'Unknown Contact (' || c._id || ')'
                    )                                                   AS display_name,
                    COALESCE(
                        (
                            SELECT MAX(m_ts.timestamp)
                            FROM message m_ts
                            {ms_ts_join}
                            WHERE m_ts.chat_row_id = c._id
                              AND m_ts.message_type NOT IN (25, 67, 74, 79, 90)
                              AND (
                                  (m_ts.text_data IS NOT NULL AND m_ts.text_data != '' AND (m_ts.message_type IS NULL OR m_ts.message_type = 0 OR m_ts.message_type IN (5, 16)))
                                  OR (m_ts.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82) {mrev_ts_filter})
                                  OR (m_ts.message_type = 7 AND {ms_ts_col} = 1 AND NOT ({is_group_chat}))
                                  OR ({is_group_chat} AND m_ts.message_type = 7 {ms_ts_allowed_clause})
                              )
                        ),
                        NULLIF(c.sort_timestamp, 0),
                        0
                    ) AS newest_ts,
                    COALESCE(m.text_data, '')                          AS last_msg_preview,
                    COALESCE(m.message_type, 0)                        AS last_msg_type,
                    COALESCE(m.from_me, 0)                             AS last_msg_from_me,
                    m._id                                               AS last_msg_id,
                    mm.file_path                                        AS last_msg_media_path,
                    {ms_chat_col}                                       AS last_msg_action_type,
                    ''                                                  AS last_msg_participant_name,
                    COALESCE(
                        NULLIF(con_s.display_name, ''),
                        CASE WHEN COALESCE(j_s_real.user, CASE WHEN NOT ({j_s_is_lid}) THEN j_s.user END) = '0' THEN 'WhatsApp' END,
                        CASE WHEN j_s_real.user IS NOT NULL THEN '+' || {_clean_user_sql("j_s_real.user")} END,
                        {ldn_s_arm}
                        CASE WHEN NOT ({j_s_is_lid}) AND j_s.user IS NOT NULL THEN '+' || {_clean_user_sql("j_s.user")} END,
                        ''
                    )                                                   AS last_msg_sender
                FROM chat c
                LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                {ldn_c_join}
                LEFT JOIN _contacts_resolved con ON con.clean_number = {_clean_user_sql("COALESCE(j_chat_real.user, j_chat.user)")}
                {con_ldn_join}
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(c._id AS TEXT)
                LEFT JOIN message m ON m._id = COALESCE(
                    (
                        SELECT m_disp._id
                        FROM message m_disp
                        {ms_disp_join}
                        WHERE m_disp._id = c.display_message_row_id
                          AND m_disp.chat_row_id = c._id
                          AND m_disp.message_type NOT IN (25, 67, 74, 79, 90)
                          AND (
                              (m_disp.text_data IS NOT NULL AND m_disp.text_data != '' AND (m_disp.message_type IS NULL OR m_disp.message_type = 0 OR m_disp.message_type IN (5, 16)))
                              OR (m_disp.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82) {mrev_disp_filter})
                              OR (m_disp.message_type = 7 AND {ms_disp_col} = 1 AND NOT ({is_group_chat}))
                              OR ({is_group_chat} AND m_disp.message_type = 7 {ms_disp_allowed_clause})
                          )
                    ),
                    (
                        SELECT m_sub._id
                        FROM message m_sub
                        {ms_sub_join}
                        WHERE m_sub.chat_row_id = c._id
                          AND m_sub.message_type NOT IN (25, 67, 74, 79, 90)
                          AND (
                              (m_sub.text_data IS NOT NULL AND m_sub.text_data != '' AND (m_sub.message_type IS NULL OR m_sub.message_type = 0 OR m_sub.message_type IN (5, 16)))
                              OR (m_sub.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82) {mrev_sub_filter})
                              OR (m_sub.message_type = 7 AND {ms_sub_col} = 1 AND NOT ({is_group_chat}))
                              OR ({is_group_chat} AND m_sub.message_type = 7 {ms_sub_allowed_clause})
                          )
                        ORDER BY m_sub.timestamp DESC, m_sub._id DESC
                        LIMIT 1
                    )
                )
                LEFT JOIN message_media mm ON mm.message_row_id = m._id
                LEFT JOIN jid j_s ON j_s._id = m.sender_jid_row_id
                {ldn_s_join}
                LEFT JOIN jid j_s_real ON j_s_real._id = jm_s.jid_row_id
                LEFT JOIN _contacts_resolved con_s ON con_s.clean_number = {_clean_user_sql("COALESCE(j_s_real.user, j_s.user)")}
                {ms_chat_join}
                WHERE (
                    ({is_group_chat} AND (c.hidden = 0 OR c.hidden IS NULL OR EXISTS (SELECT 1 FROM message m WHERE m.chat_row_id = c._id AND m.message_type NOT IN (7, 90))))
                    OR
                    (NOT ({is_group_chat}) AND (
                        EXISTS (
                            SELECT 1 FROM message m
                            WHERE m.chat_row_id = c._id
                              AND m.message_type NOT IN (25, 67, 74, 79, 90)
                              AND (
                                  (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
                                  OR (m.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82) {mrev_exist_filter})
                              )
                        )
                    ))
                )
                ORDER BY newest_ts DESC
            """).fetchall()
            seen_ids = set()
            deduped_rows = []
            for r in rows:
                cid = r["id"]
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                deduped_rows.append(r)
            rows = deduped_rows
        else:
            group_ev_col = "m.ZGROUPEVENTTYPE" if has_ios_group_event else "0"
            # Exclude internal system events (2: E2E encryption notice, 56: security code notice)
            # from preview text and sorting so only actual user interactions or group actions are shown
            ios_service_filter_last_real = (
                "OR (msg.ZMESSAGETYPE IN (6, 10) AND msg.ZGROUPEVENTTYPE IN (1, 3, 4, 5, 6, 7, 9, 10, 12, 15, 21, 26, 36, 37, 38, 44, 50, 58))"
                if has_ios_group_event
                else ""
            )
            cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
            has_cs_ident = "ZCONTACTIDENTIFIER" in cs_cols
            cs_ident_col = "cs.ZCONTACTIDENTIFIER" if has_cs_ident else "NULL"
            ios_phone_expr = f"""
                COALESCE(
                    CASE WHEN cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                         THEN SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) END,
                    CASE WHEN {cs_ident_col} LIKE '%@s.whatsapp.net'
                         THEN SUBSTR({cs_ident_col}, 1, INSTR({cs_ident_col}, '@') - 1) END,
                    ic.phone_number
                )
            """
            rows = conn.execute(f"""
                SELECT
                    CAST(cs.Z_PK AS TEXT)                               AS id,
                    CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                         ELSE 'contact' END                            AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        NULLIF(con_lid.display_name, ''),
                        CASE WHEN ({ios_phone_expr}) = '0' OR cs.ZCONTACTJID = '0@s.whatsapp.net' THEN 'WhatsApp' END,
                        CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN COALESCE(NULLIF(grp.subject, ''), NULLIF(cs.ZPARTNERNAME, ''), 'Unknown Group (' || cs.Z_PK || ')') END,
                        CASE WHEN ic.full_name IS NOT NULL AND ic.full_name != '' AND ic.full_name NOT LIKE '+%' AND ic.full_name NOT LIKE 'Unknown%' THEN ic.full_name END,
                        NULLIF(cs.ZPARTNERNAME, ''),
                        CASE WHEN con.folder IS NOT NULL AND con.folder != '' AND con.folder NOT LIKE 'Unknown%'
                             THEN RTRIM(SUBSTR(con.folder, 1, INSTR(con.folder || ' (', ' (') - 1)) END,
                        CASE WHEN ({ios_phone_expr}) IS NOT NULL AND ({ios_phone_expr}) != '' THEN '+' || ({ios_phone_expr}) END,
                        'Unknown Contact (' || cs.Z_PK || ')'
                    )                                                   AS display_name,
                    CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER) AS newest_ts,
                    COALESCE(m.ZTEXT, '')                              AS last_msg_preview,
                    COALESCE(m.ZMESSAGETYPE, 0)                        AS last_msg_type,
                    {group_ev_col}                                     AS last_msg_group_event_type,
                    COALESCE(m.ZISFROMME, 0)                           AS last_msg_from_me,
                    mi.ZMEDIALOCALPATH                                  AS last_msg_media_path,
                    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN m.ZFROMJID END AS last_msg_sender_jid,
                    gm.ZMEMBERJID                                       AS last_msg_member_jid
                FROM ZWACHATSESSION cs
                LEFT JOIN _ios_contacts ic ON ic.jid = cs.ZCONTACTJID
                LEFT JOIN arch.contacts con
                      ON con.number = ({ios_phone_expr})
                LEFT JOIN arch.contacts con_lid
                      ON con_lid.number = SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                                 INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1)
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(cs.Z_PK AS TEXT)
                JOIN (
                    SELECT msg.ZCHATSESSION, MAX(msg.Z_PK) AS last_pk
                    FROM ZWAMESSAGE msg
                    LEFT JOIN ZWAMEDIAITEM mi2 ON mi2.Z_PK = msg.ZMEDIAITEM
                    WHERE (msg.ZTEXT IS NOT NULL AND msg.ZTEXT != '' AND (msg.ZMESSAGETYPE IS NULL OR msg.ZMESSAGETYPE = 0))
                       OR (msg.ZMESSAGETYPE IS NOT NULL AND msg.ZMESSAGETYPE NOT IN (0, 6, 10, 14, 38, 39, 53)
                           AND mi2.ZMEDIALOCALPATH IS NOT NULL)
                       OR (msg.ZMESSAGETYPE IN (4, 14, 38, 39, 53))
                       {ios_service_filter_last_real}
                    GROUP BY msg.ZCHATSESSION
                ) last_real ON last_real.ZCHATSESSION = cs.Z_PK
                JOIN ZWAMESSAGE m ON m.Z_PK = last_real.last_pk
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                WHERE cs.ZHIDDEN = 0
                  AND (
                      cs.ZGROUPINFO IS NOT NULL
                      OR EXISTS (
                          SELECT 1 FROM ZWAMESSAGE msg_real
                          WHERE msg_real.ZCHATSESSION = cs.Z_PK
                            AND (msg_real.ZMESSAGETYPE NOT IN (6, 10) OR msg_real.ZMESSAGETYPE IS NULL)
                      )
                  )
                ORDER BY m.ZMESSAGEDATE DESC
            """).fetchall()

        result = []
        ios_contacts_map = {}
        if source_type == "ios":
            needed_jids = set()
            for r in rows:
                if r["last_msg_type"] in (6, 10):
                    for s_jid in (r["last_msg_sender_jid"], r["last_msg_member_jid"]):
                        if s_jid:
                            needed_jids.add(s_jid)
                            if not s_jid.endswith("@s.whatsapp.net") and not s_jid.endswith("@lid"):
                                needed_jids.add(f"{s_jid}@s.whatsapp.net")
                                needed_jids.add(f"{s_jid}@lid")
                            elif s_jid.endswith("@s.whatsapp.net"):
                                needed_jids.add(s_jid.split("@")[0])
                    txt = (r["last_msg_preview"] or "").strip()
                    if txt.startswith("{"):
                        try:
                            data = json.loads(txt)
                            author = data.get("author")
                            if author:
                                needed_jids.add(author)
                                if not author.endswith("@s.whatsapp.net") and not author.endswith(
                                    "@lid"
                                ):
                                    needed_jids.add(f"{author}@s.whatsapp.net")
                                    needed_jids.add(f"{author}@lid")
                                elif author.endswith("@s.whatsapp.net"):
                                    needed_jids.add(author.split("@")[0])
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    for part in re.split(r"[;,]", txt):
                        part = part.strip()
                        if part:
                            needed_jids.add(part)
                            if not part.endswith("@s.whatsapp.net") and not part.endswith("@lid"):
                                needed_jids.add(f"{part}@s.whatsapp.net")
                                needed_jids.add(f"{part}@lid")
                            elif part.endswith("@s.whatsapp.net"):
                                needed_jids.add(part.split("@")[0])
            if needed_jids:
                ph = ",".join("?" * len(needed_jids))
                try:
                    c_rows = conn.execute(
                        f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
                        list(needed_jids),
                    ).fetchall()
                    for cr in c_rows:
                        ios_contacts_map[cr["jid"]] = (cr["full_name"], cr["phone_number"])
                except sqlite3.OperationalError:
                    pass
                try:
                    arch_rows = conn.execute(
                        f"SELECT number, display_name FROM arch.contacts WHERE number IN ({ph})",
                        list(needed_jids),
                    ).fetchall()
                    for ar in arch_rows:
                        if ar["display_name"]:
                            ios_contacts_map[ar["number"]] = (ar["display_name"], ar["number"])
                            ios_contacts_map[f"{ar['number']}@s.whatsapp.net"] = (
                                ar["display_name"],
                                ar["number"],
                            )
                except sqlite3.OperationalError:
                    pass
                try:
                    push_rows = conn.execute(
                        f"SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID IN ({ph})",
                        list(needed_jids),
                    ).fetchall()
                    for pr in push_rows:
                        if pr["ZPUSHNAME"]:
                            if pr["ZJID"] not in ios_contacts_map:
                                ios_contacts_map[pr["ZJID"]] = (pr["ZPUSHNAME"], None)
                            bare_p = pr["ZJID"].split("@")[0]
                            if bare_p not in ios_contacts_map:
                                ios_contacts_map[bare_p] = (pr["ZPUSHNAME"], None)
                except sqlite3.OperationalError:
                    pass

            try:
                user_row = conn.execute(
                    "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZFROMJID LIKE '%@g.us' AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                ).fetchone()
                if not user_row:
                    user_row = conn.execute(
                        "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZISFROMME = 0 AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                    ).fetchone()
                if user_row and user_row[0]:
                    u_jid = user_row[0]
                    u_phone = u_jid.split("@")[0]
                    ios_contacts_map[u_jid] = ("You", u_phone)
                    ios_contacts_map[u_phone] = ("You", u_phone)
                    ios_contacts_map[f"+{u_phone}"] = ("You", u_phone)
            except sqlite3.OperationalError:
                pass

        android_p_map = {}
        android_poll_mids = set()
        if source_type == "android":
            candidate_last_mids = [r["last_msg_id"] for r in rows if r["last_msg_id"]]
            if candidate_last_mids:
                c_ph = ",".join("?" * len(candidate_last_mids))
                try:
                    android_poll_mids.update(
                        r[0]
                        for r in conn.execute(
                            f"SELECT message_row_id FROM message_poll WHERE message_row_id IN ({c_ph})",
                            candidate_last_mids,
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    pass
                mao_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()
                }
                if "message_add_on_type" in mao_cols:
                    try:
                        android_poll_mids.update(
                            r[0]
                            for r in conn.execute(
                                f"SELECT parent_message_row_id FROM message_add_on WHERE parent_message_row_id IN ({c_ph}) AND message_add_on_type = 67",
                                candidate_last_mids,
                            ).fetchall()
                        )
                    except sqlite3.OperationalError:
                        pass
            if has_mcp:
                svc_mids = [
                    r["last_msg_id"]
                    for r in rows
                    if r["last_msg_type"] == 7
                    and r["last_msg_action_type"] in _SUPPORTED_ANDROID_SYSTEM_ACTIONS
                    and r["last_msg_id"]
                ]
                if svc_mids:
                    android_p_map = _hydrate_android_service_participants(
                        conn,
                        svc_mids,
                        has_mcp,
                        has_lid_dn,
                        has_jid_server,
                        has_jid_raw_string,
                    )
            android_vom_mids = set()
            if has_message_view_once_media:
                candidate_mids = [r["last_msg_id"] for r in rows if r["last_msg_id"]]
                if candidate_mids:
                    c_ph = ",".join("?" * len(candidate_mids))
                    try:
                        android_vom_mids = {
                            r[0]
                            for r in conn.execute(
                                f"SELECT message_row_id FROM message_view_once_media WHERE message_row_id IN ({c_ph})",
                                candidate_mids,
                            ).fetchall()
                        }
                    except sqlite3.OperationalError:
                        pass
            android_revoked_mids = set()
            if has_message_revoked:
                candidate_mids = [r["last_msg_id"] for r in rows if r["last_msg_id"]]
                if candidate_mids:
                    c_ph = ",".join("?" * len(candidate_mids))
                    try:
                        android_revoked_mids = {
                            r[0]
                            for r in conn.execute(
                                f"SELECT message_row_id FROM message_revoked WHERE message_row_id IN ({c_ph})",
                                candidate_mids,
                            ).fetchall()
                        }
                    except sqlite3.OperationalError:
                        pass

        for r in rows:
            d = dict(r)
            raw_type = d.pop("last_msg_type", 0)
            media_path = d.pop("last_msg_media_path", None)
            last_msg_id = d.pop("last_msg_id", None)
            last_msg_action_type = d.pop("last_msg_action_type", None)
            last_msg_participant_name = d.pop("last_msg_participant_name", None)
            last_msg_participant_names = None
            if last_msg_id:
                last_msg_participant_names = android_p_map.get(last_msg_id)
            if not last_msg_participant_names and last_msg_participant_name:
                last_msg_participant_names = [last_msg_participant_name]
            last_msg_group_event_type = d.pop("last_msg_group_event_type", None)
            last_msg_sender = d.pop("last_msg_sender", None)
            last_msg_sender_jid = d.pop("last_msg_sender_jid", None)
            last_msg_member_jid = d.pop("last_msg_member_jid", None)

            if (
                source_type == "android"
                and (raw_type in (15, 64) or last_msg_id in android_revoked_mids)
            ) or (source_type == "ios" and raw_type == 14):
                d["last_msg_type"] = "deleted"
                if source_type == "android" and raw_type == 64:
                    d["last_msg_preview"] = "This message was deleted by an admin"
                elif d.get("last_msg_from_me"):
                    d["last_msg_preview"] = "You deleted this message"
                else:
                    d["last_msg_preview"] = "This message was deleted"
                d["is_view_once"] = 0
            elif (
                source_type == "android"
                and (raw_type in (42, 43, 82) or last_msg_id in android_vom_mids)
            ) or (source_type == "ios" and raw_type in (38, 39, 53)):
                d["is_view_once"] = 1
                if media_path:
                    path = media_path if source_type == "android" else f"Message/{media_path}"
                    d["last_msg_type"] = _media_type_from_path(path)
                else:
                    if (source_type == "android" and raw_type in (3, 43)) or (
                        source_type == "ios" and raw_type == 39
                    ):
                        d["last_msg_type"] = "video"
                    elif (source_type == "android" and raw_type in (2, 82)) or (
                        source_type == "ios" and raw_type == 53
                    ):
                        d["last_msg_type"] = "audio"
                    else:
                        d["last_msg_type"] = "image"
            elif source_type == "android" and raw_type == 7:
                d["is_view_once"] = 0
                if d.get("type") == "contact" and last_msg_action_type == 1:
                    d["last_msg_type"] = "deleted"
                    if d.get("last_msg_from_me"):
                        d["last_msg_preview"] = "You deleted this message"
                    else:
                        d["last_msg_preview"] = "This message was deleted"
                elif last_msg_action_type in _SUPPORTED_ANDROID_SYSTEM_ACTIONS:
                    s_row = {
                        "action_type": last_msg_action_type,
                        "text_body": d.get("last_msg_preview", ""),
                        "from_me": d.get("last_msg_from_me", 0),
                        "sender": last_msg_sender,
                        "participant_names": last_msg_participant_names,
                        "participant_name": (
                            format_participant_names(last_msg_participant_names)
                            if last_msg_participant_names
                            else (last_msg_participant_name or "")
                        ),
                    }
                    d["last_msg_preview"] = _format_android_service_row(s_row)
                    d["last_msg_type"] = "service"
                else:
                    d["last_msg_preview"] = ""
                    d["last_msg_type"] = "text"
            elif source_type == "ios" and raw_type in (6, 10):
                d["is_view_once"] = 0
                if not has_ios_group_event or (
                    last_msg_group_event_type and last_msg_group_event_type > 0
                ):
                    sender_name = (
                        _resolve_ios_jid(last_msg_sender_jid, ios_contacts_map)
                        if last_msg_sender_jid
                        else ""
                    )
                    s_row = {
                        "group_event_type": last_msg_group_event_type,
                        "text_body": d.get("last_msg_preview", ""),
                        "from_me": d.get("last_msg_from_me", 0),
                        "sender": sender_name,
                        "member_jid": last_msg_member_jid or "",
                        "chat_type": d.get("type", "contact"),
                    }
                    d["last_msg_preview"] = _format_ios_service_row(s_row, ios_contacts_map)
                    d["last_msg_type"] = "service"
                else:
                    d["last_msg_preview"] = ""
                    d["last_msg_type"] = "text"
            elif (
                source_type == "android"
                and (raw_type == 66 or (last_msg_id and last_msg_id in android_poll_mids))
            ) or (source_type == "ios" and raw_type == 46):
                d["is_view_once"] = 0
                d["last_msg_type"] = "poll"
                if source_type == "android":
                    d["last_msg_preview"] = d.get("last_msg_preview") or "Poll"
                else:
                    poll_q = ""
                    if last_msg_id:
                        try:
                            info_r = conn.execute(
                                "SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZMESSAGE = ? AND ZRECEIPTINFO IS NOT NULL",
                                (last_msg_id,),
                            ).fetchone()
                            if info_r and info_r[0]:
                                parsed = _parse_ios_poll_blob(bytes(info_r[0]))
                                if parsed and parsed["question"]:
                                    poll_q = parsed["question"]
                        except sqlite3.OperationalError:
                            pass
                    d["last_msg_preview"] = poll_q or "Poll"
            elif (source_type == "android" and raw_type in (4, 14)) or (
                source_type == "ios" and raw_type == 4
            ):
                d["is_view_once"] = 0
                d["last_msg_type"] = "vcard"
            elif (source_type == "android" and raw_type == 20) or (
                source_type == "ios" and raw_type == 15
            ):
                d["is_view_once"] = 0
                d["last_msg_type"] = "sticker"
            elif raw_type != 0 and media_path:
                d["is_view_once"] = 0
                path = media_path if source_type == "android" else f"Message/{media_path}"
                d["last_msg_type"] = _media_type_from_path(path)
            else:
                d["is_view_once"] = 0
                d["last_msg_type"] = "text"
            result.append(d)
        pres = get_preserved()
        if pres is not None:
            result = _merge_preserved_chats(result, pres, get_archive())
        else:
            for c in result:
                c.setdefault("is_preserved_only", False)
        return jsonify(result)

    # ---- API: paginated messages -------------------------------------------

    @app.route("/api/messages")
    def api_messages():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        before = request.args.get("before")
        after = request.args.get("after")
        limit = min(int(request.args.get("limit", 50)), 200)

        if not before and not after and source_type == "android":
            rows = (
                get_archive()
                .execute(
                    """SELECT * FROM recent_messages
                   WHERE chat_id = ? AND chat_type = ?
                   ORDER BY timestamp_ms DESC LIMIT ?""",
                    (chat_id, chat_type, limit),
                )
                .fetchall()
            )
            if rows:
                _maybe_start_indexing(
                    chat_id,
                    chat_type,
                    get_cache(),
                    source_type,
                    wa_db_path,
                    output_root,
                    _indexing_state,
                    _indexing_lock,
                )
                if source_type == "android" and rows:
                    rows = [dict(r) for r in rows]
                    for r in rows:
                        if r.get("chat_type") == "contact" and r.get("media_type") == "service":
                            r["media_type"] = "deleted"
                            r["text_body"] = (
                                "You deleted this message"
                                if r.get("from_me") == 1
                                else "This message was deleted"
                            )
                    needs_refresh = False
                    if get_wa() is not None and has_message_revoked and rows:
                        cached_mids = {r["msg_id"] for r in rows}
                        min_ts = rows[-1]["timestamp_ms"]
                        max_ts = rows[0]["timestamp_ms"]
                        chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
                        try:
                            missing_rev = (
                                get_wa()
                                .execute(
                                    f"""SELECT m._id FROM message m
                                       JOIN message_revoked mr ON mr.message_row_id = m._id
                                       WHERE {chat_pred} AND m.timestamp >= ? AND m.timestamp <= ?
                                       LIMIT 1""",
                                    chat_params + [min_ts, max_ts],
                                )
                                .fetchone()
                            )
                            if missing_rev and missing_rev[0] not in cached_mids:
                                needs_refresh = True
                        except sqlite3.OperationalError:
                            pass
                    if not needs_refresh:
                        if has_android_hd and get_wa() is not None:
                            mids = [r["msg_id"] for r in rows]
                            placeholders = ",".join("?" * len(mids))
                            try:
                                hd_parents = {
                                    r[0]
                                    for r in get_wa()
                                    .execute(
                                        f"""SELECT ma.parent_message_row_id FROM message_association ma
                                           JOIN message_media mm_hd ON mm_hd.message_row_id = ma.child_message_row_id
                                           WHERE ma.parent_message_row_id IN ({placeholders})
                                             AND ma.association_type IN (12, 7)
                                             AND mm_hd.file_path IS NOT NULL""",
                                        mids,
                                    )
                                    .fetchall()
                                }
                                if hd_parents:
                                    rows = [r for r in rows if r["msg_id"] not in hd_parents]
                            except sqlite3.OperationalError:
                                pass
                        unresolved_media = [
                            r
                            for r in rows
                            if r.get("media_type") not in ("text", "link", "deleted", "service")
                            and not r.get("archive_path")
                        ]
                        if unresolved_media and get_wa() is not None:
                            wa = get_wa()
                            mids = [r["msg_id"] for r in unresolved_media]
                            placeholders = ",".join("?" * len(mids))
                            try:
                                found_copies = wa.execute(
                                    f"""SELECT m._id, ac.archive_path
                                       FROM message m
                                       JOIN message_media mm ON mm.message_row_id = m._id
                                       JOIN arch.archive_copies ac ON ac.original_path IN (
                                           COALESCE(mm.file_path, 'vcard:' || m._id),
                                           REPLACE(COALESCE(mm.file_path, ''), '\\', '/'),
                                           REPLACE(COALESCE(mm.file_path, ''), '/', '\\'),
                                           CASE WHEN mm.file_path LIKE 'WhatsApp/%' OR mm.file_path LIKE 'WhatsApp\\%'
                                                THEN SUBSTR(REPLACE(mm.file_path, '\\', '/'), 10) END,
                                           CASE WHEN mm.file_path LIKE 'WhatsApp/%' OR mm.file_path LIKE 'WhatsApp\\%'
                                                THEN REPLACE(SUBSTR(REPLACE(mm.file_path, '\\', '/'), 10), '/', '\\') END,
                                           CASE WHEN mm.file_path NOT LIKE 'WhatsApp/%' AND mm.file_path NOT LIKE 'WhatsApp\\%' AND mm.file_path IS NOT NULL
                                                THEN 'WhatsApp/' || REPLACE(mm.file_path, '\\', '/') END,
                                           CASE WHEN mm.file_path NOT LIKE 'WhatsApp/%' AND mm.file_path NOT LIKE 'WhatsApp\\%' AND mm.file_path IS NOT NULL
                                                THEN 'WhatsApp\\' || REPLACE(mm.file_path, '/', '\\') END
                                       )
                                       WHERE m._id IN ({placeholders}) AND ac.archive_path IS NOT NULL""",
                                    mids,
                                ).fetchall()
                                if found_copies:
                                    copy_map = {r[0]: r[1] for r in found_copies}
                                    for r in rows:
                                        if r["msg_id"] in copy_map:
                                            r["archive_path"] = copy_map[r["msg_id"]]
                                            get_archive().execute(
                                                "UPDATE recent_messages SET archive_path = ? WHERE msg_id = ?",
                                                (copy_map[r["msg_id"]], r["msg_id"]),
                                            )
                                    get_archive().commit()
                            except sqlite3.OperationalError:
                                pass
                        missing_rx = [r["msg_id"] for r in rows if r.get("reactions") is None]
                        if missing_rx:
                            rx_cache = (
                                get_archive()
                                .execute(
                                    """SELECT msg_id, reactions FROM reactions_cache
                                   WHERE chat_id = ? AND msg_id IN ("""
                                    + ",".join("?" * len(missing_rx))
                                    + """)""",
                                    [chat_id] + missing_rx,
                                )
                                .fetchall()
                            )
                            rx_map = {r["msg_id"]: r["reactions"] for r in rx_cache}
                            still_missing = [mid for mid in missing_rx if mid not in rx_map]
                            if still_missing and get_wa() is not None and has_reactions:
                                wa_rows = (
                                    get_wa()
                                    .execute(
                                        f"""
                                        SELECT ao.parent_message_row_id AS msg_id,
                                               GROUP_CONCAT(r.reaction) AS reactions
                                        FROM message_add_on ao
                                        JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                                        WHERE ao.parent_message_row_id IN ({",".join("?" * len(still_missing))})
                                        GROUP BY ao.parent_message_row_id
                                    """,
                                        still_missing,
                                    )
                                    .fetchall()
                                )
                                found_ids = set()
                                cache_inserts = []
                                for wr in wa_rows:
                                    rx_map[wr["msg_id"]] = wr["reactions"]
                                    found_ids.add(wr["msg_id"])
                                    cache_inserts.append((chat_id, wr["msg_id"], wr["reactions"]))
                                for mid in still_missing:
                                    if mid not in found_ids:
                                        rx_map[mid] = ""
                                        cache_inserts.append((chat_id, mid, ""))
                                if cache_inserts:
                                    get_archive().executemany(
                                        "INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions) VALUES (?, ?, ?)",
                                        cache_inserts,
                                    )
                            for row in rows:
                                if row["msg_id"] in rx_map:
                                    val = rx_map[row["msg_id"]]
                                    row["reactions"] = val if val else None
                                    get_archive().execute(
                                        "UPDATE recent_messages SET reactions = ? WHERE chat_id = ? AND msg_id = ?",
                                        (row["reactions"], chat_id, row["msg_id"]),
                                    )
                            get_archive().commit()
                        _hydrate_poll_rows(rows, source_type, get_wa())
                        _hydrate_quoted_rows(rows, source_type, get_wa())
                        _hydrate_link_preview_rows(rows, source_type)
                        pres = get_preserved()
                        if pres is not None:
                            pres_rows = _query_preserved_messages(
                                pres, chat_id, chat_type, None, None, limit
                            )
                            if pres_rows:
                                rows = _merge_messages(rows, pres_rows, True, limit)
                        _resolve_quoted_msg_ids(rows, source_type, get_wa(), pres)
                        return jsonify(rows)

        conn = get_wa()
        if conn is None:
            pres = get_preserved()
            if pres is not None:
                b_val = int(before) if before else None
                a_val = int(after) if after else None
                pres_rows = _query_preserved_messages(pres, chat_id, chat_type, b_val, a_val, limit)
                _resolve_quoted_msg_ids(pres_rows, source_type, None, pres)
                return jsonify(pres_rows)
            return jsonify([])

        if not before and not after:
            _maybe_start_indexing(
                chat_id,
                chat_type,
                get_cache(),
                source_type,
                wa_db_path,
                output_root,
                _indexing_state,
                _indexing_lock,
            )

        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER

        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            ts_col = _ANDROID_TS
            sort_phys_col = "m._id"
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            ts_col = _IOS_TS
            sort_phys_col = "COALESCE(m.ZSORT, m.Z_PK)" if has_ios_sort else "m.Z_PK"

        if before:
            sql = f"{select} WHERE {chat_pred} AND {ts_col} < ? {extra} ORDER BY {ts_col} DESC, {sort_phys_col} DESC LIMIT ?"
            rows = conn.execute(sql, chat_params + [int(before), limit]).fetchall()
        elif after:
            sql = f"{select} WHERE {chat_pred} AND {ts_col} > ? {extra} ORDER BY {ts_col} ASC, {sort_phys_col} ASC LIMIT ?"
            rows = conn.execute(sql, chat_params + [int(after), limit]).fetchall()
        else:
            sql = f"{select} WHERE {chat_pred} {extra} ORDER BY {ts_col} DESC, {sort_phys_col} DESC LIMIT ?"
            rows = conn.execute(sql, chat_params + [limit]).fetchall()

        rows = [dict(r) for r in rows]
        _format_service_rows(
            rows, source_type, conn, has_mcp, has_lid_dn, has_jid_server, has_jid_raw_string
        )

        if source_type == "android" and rows and has_reactions:
            msg_ids = [r["msg_id"] for r in rows]
            placeholders = ",".join("?" * len(msg_ids))
            cached = (
                get_archive()
                .execute(
                    f"SELECT msg_id, reactions FROM reactions_cache "
                    f"WHERE chat_id = ? AND msg_id IN ({placeholders})",
                    [chat_id] + msg_ids,
                )
                .fetchall()
            )
            cached_map = {r["msg_id"]: r["reactions"] for r in cached}
            missing = [mid for mid in msg_ids if mid not in cached_map]
            if missing:
                missing_ph = ",".join("?" * len(missing))
                from_wa = conn.execute(
                    f"""
                    SELECT ao.parent_message_row_id AS msg_id,
                           GROUP_CONCAT(r.reaction) AS reactions
                    FROM message_add_on ao
                    JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                    WHERE ao.parent_message_row_id IN ({missing_ph})
                    GROUP BY ao.parent_message_row_id
                """,
                    missing,
                ).fetchall()
                wa_map = {r["msg_id"]: r["reactions"] for r in from_wa}
                cache_inserts = [(chat_id, mid, wa_map.get(mid, "")) for mid in missing]
                if cache_inserts:
                    get_archive().executemany(
                        "INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions) "
                        "VALUES (?, ?, ?)",
                        cache_inserts,
                    )
                    get_archive().commit()
                cached_map.update({mid: wa_map.get(mid, "") for mid in missing})
            for row in rows:
                rx_val = cached_map.get(row["msg_id"])
                row["reactions"] = rx_val if rx_val else None
        elif source_type == "ios" and rows and has_ios_reactions:
            msg_ids = [r["msg_id"] for r in rows]
            ios_rx = _ios_reactions(conn, chat_id, msg_ids)
            for row in rows:
                reactors = ios_rx.get(row["msg_id"])
                if reactors:
                    row["reactions"] = ",".join(e for _, e, _ in reactors)
                    row["reactions_from_me"] = ",".join(str(f) for _, _, f in reactors)
                else:
                    row["reactions"] = None
        else:
            for row in rows:
                row.setdefault("reactions", None)

        for row in rows:
            row.pop("action_type", None)
            row.pop("participant_name", None)
            row.pop("group_event_type", None)
            row.pop("raw_msg_type", None)
            row.pop("member_jid", None)

        _hydrate_poll_rows(rows, source_type, conn)
        _hydrate_quoted_rows(rows, source_type, conn)
        _hydrate_link_preview_rows(rows, source_type)
        pres = get_preserved()
        if pres is not None:
            b_val = int(before) if before else None
            a_val = int(after) if after else None
            pres_rows = _query_preserved_messages(pres, chat_id, chat_type, b_val, a_val, limit)
            if pres_rows:
                descending = after is None
                rows = _merge_messages(rows, pres_rows, descending, limit)
        _resolve_quoted_msg_ids(rows, source_type, conn, pres)

        if not before and not after:
            threading.Thread(
                target=_backfill_recent_messages,
                args=(archive_db_path, [dict(r) for r in rows]),
                daemon=True,
            ).start()

        return jsonify(rows)

    # ---- API: messages around a timestamp ----------------------------------

    @app.route("/api/messages/at")
    def api_messages_at():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        ts_raw = request.args.get("ts")
        limit = min(int(request.args.get("limit", 50)), 200)
        if not ts_raw:
            return jsonify({"error": "ts is required"}), 400
        try:
            ts = int(ts_raw)
        except ValueError:
            return jsonify({"error": "ts must be an integer"}), 400

        half = limit // 2
        conn = get_wa()
        if conn is None:
            pres = get_preserved()
            if pres is not None:
                pres_before = _query_preserved_messages(
                    pres, chat_id, chat_type, ts + 1, None, half
                )
                pres_after = _query_preserved_messages(pres, chat_id, chat_type, None, ts, half)
                combined = list(reversed(pres_before)) + pres_after
                _resolve_quoted_msg_ids(combined, source_type, None, pres)
                return jsonify(combined)
            return jsonify([])

        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER

        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            ts_col = _ANDROID_TS
            sort_phys_col = "m._id"
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            ts_col = _IOS_TS
            sort_phys_col = "COALESCE(m.ZSORT, m.Z_PK)" if has_ios_sort else "m.Z_PK"

        before_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {ts_col} <= ? {extra} ORDER BY {ts_col} DESC, {sort_phys_col} DESC LIMIT ?",
            chat_params + [ts, half],
        ).fetchall()
        after_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {ts_col} > ? {extra} ORDER BY {ts_col} ASC, {sort_phys_col} ASC LIMIT ?",
            chat_params + [ts, half],
        ).fetchall()

        combined = [dict(r) for r in reversed(before_rows)] + [dict(r) for r in after_rows]
        _format_service_rows(
            combined, source_type, conn, has_mcp, has_lid_dn, has_jid_server, has_jid_raw_string
        )
        if source_type == "android" and combined and has_reactions:
            _resolve_and_cache_reactions(source_type, conn, get_archive(), combined)
        elif source_type == "ios" and combined and has_ios_reactions:
            msg_ids = [r["msg_id"] for r in combined]
            ios_rx = _ios_reactions(conn, chat_id, msg_ids)
            for row in combined:
                reactors = ios_rx.get(row["msg_id"])
                if reactors:
                    row["reactions"] = ",".join(e for _, e, _ in reactors)
                    row["reactions_from_me"] = ",".join(str(f) for _, _, f in reactors)
                else:
                    row["reactions"] = None
        for row in combined:
            row.pop("action_type", None)
            row.pop("participant_name", None)
            row.pop("group_event_type", None)
            row.pop("raw_msg_type", None)
            row.pop("member_jid", None)
        _hydrate_poll_rows(combined, source_type, conn)
        _hydrate_quoted_rows(combined, source_type, conn)
        _hydrate_link_preview_rows(combined, source_type)
        pres = get_preserved()
        if pres is not None:
            pres_before = _query_preserved_messages(pres, chat_id, chat_type, ts + 1, None, half)
            pres_after = _query_preserved_messages(pres, chat_id, chat_type, None, ts, half)
            live_before = [r for r in combined if r["timestamp_ms"] <= ts]
            live_after = [r for r in combined if r["timestamp_ms"] > ts]
            merged_before = _merge_messages(live_before, pres_before, True, half)
            merged_after = _merge_messages(live_after, pres_after, False, half)
            combined = list(reversed(merged_before)) + merged_after
        _resolve_quoted_msg_ids(combined, source_type, conn, pres)
        return jsonify(_strip_none_reactions(combined))

    # ---- API: media gallery ------------------------------------------------

    @app.route("/api/media")
    def api_media():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        before = request.args.get("before")
        types_raw = request.args.get("types")
        conn = get_wa()
        if conn is None:
            return jsonify([])
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER
        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            is_media, ts_col = _ANDROID_IS_MEDIA, _ANDROID_TS
            is_link = _ANDROID_IS_LINK
            is_doc = _ANDROID_IS_DOCUMENT_UNDOWNLOADED
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            is_media, ts_col = _IOS_IS_MEDIA, _IOS_TS
            is_link = _IOS_IS_LINK
            is_doc = _IOS_IS_DOCUMENT_UNDOWNLOADED

        if not types_raw:
            if before:
                sql = f"{select} WHERE {chat_pred} {extra} AND {is_media} AND ac.archive_path IS NOT NULL AND {ts_col} < ? ORDER BY {ts_col} DESC LIMIT ?"
                rows = [
                    dict(r)
                    for r in conn.execute(
                        sql, chat_params + [int(before), GALLERY_PAGE_SIZE]
                    ).fetchall()
                ]
            else:
                sql = f"{select} WHERE {chat_pred} {extra} AND {is_media} AND ac.archive_path IS NOT NULL ORDER BY {ts_col} DESC LIMIT ?"
                rows = [
                    dict(r) for r in conn.execute(sql, chat_params + [GALLERY_PAGE_SIZE]).fetchall()
                ]
            _hydrate_link_preview_rows(rows, source_type)
            return jsonify(rows)

        types_set = {t.strip().lower() for t in types_raw.split(",") if t.strip()}
        has_all = "all" in types_set
        has_link = has_all or "link" in types_set
        has_doc = has_all or "document" in types_set
        media_types = (types_set - {"link", "document", "all"}) if not has_all else set()
        has_archived_media = has_all or bool(media_types) or has_doc

        _EXT_PATTERNS = {
            "image": ["%.jpg", "%.jpeg", "%.png", "%.heic", "%.heif"],
            "video": ["%.mp4", "%.mov", "%.avi", "%.mkv", "%.3gp"],
            "audio": ["%.mp3", "%.ogg", "%.aac", "%.opus", "%.m4a"],
            "gif": ["%.gif"],
            "sticker": ["%sticker%", "%.webp", "%.was"],
            "vcard": ["%.vcf"],
        }

        cand_clauses = []
        if has_archived_media:
            if not has_all and not has_doc and media_types:
                ext_conds = []
                for mt in media_types:
                    for ext in _EXT_PATTERNS.get(mt, []):
                        ext_conds.append(f"LOWER(ac.archive_path) LIKE '{ext}'")
                    if mt == "vcard":
                        if source_type == "android":
                            ext_conds.append("m.message_type IN (4, 14)")
                        else:
                            ext_conds.append("m.ZMESSAGETYPE = 4")
                    if mt == "sticker":
                        if source_type == "android":
                            ext_conds.append(
                                "(m.message_type = 20 OR mm.file_path LIKE '%sticker%')"
                            )
                        else:
                            ext_conds.append("m.ZMESSAGETYPE = 15")
                if ext_conds:
                    prune_sql = " OR ".join(ext_conds)
                    cand_clauses.append(
                        f"({is_media} AND ac.archive_path IS NOT NULL AND ({prune_sql}))"
                    )
                else:
                    cand_clauses.append(f"({is_media} AND ac.archive_path IS NOT NULL)")
            else:
                cand_clauses.append(f"({is_media} AND ac.archive_path IS NOT NULL)")
        if has_link:
            cand_clauses.append(f"({is_link})")
        if has_doc:
            cand_clauses.append(f"({is_doc})")

        if not cand_clauses:
            return jsonify([])

        cand_pred = " OR ".join(cand_clauses)
        params = list(chat_params)
        ts_clause = ""
        if before:
            ts_clause = f"AND {ts_col} < ?"
            params.append(int(before))

        if has_all:
            type_filter = "media_type NOT IN ('text', 'service')"
            type_params = []
        else:
            placeholders = ", ".join("?" for _ in types_set)
            type_filter = f"media_type IN ({placeholders})"
            type_params = list(types_set)

        if source_type == "android" and has_android_hd:
            hd_dedup_clause = _ANDROID_HD_DEDUP_CLAUSE
        elif source_type == "ios" and has_ios_hd:
            hd_dedup_clause = _IOS_HD_DEDUP_CLAUSE
        else:
            hd_dedup_clause = ""

        sql = f"""
            SELECT * FROM (
                {select} WHERE {chat_pred} {hd_dedup_clause} AND ({cand_pred}) {ts_clause}
                ORDER BY {ts_col} DESC
            )
            WHERE {type_filter}
            ORDER BY timestamp_ms DESC
            LIMIT ?
        """
        rows = [
            dict(r)
            for r in conn.execute(sql, params + type_params + [GALLERY_PAGE_SIZE]).fetchall()
        ]
        _hydrate_quoted_rows(rows, source_type, conn)
        _hydrate_link_preview_rows(rows, source_type)
        return jsonify(rows)

    @app.route("/api/media/links")
    def api_media_links():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        conn = get_wa()
        if conn is None:
            return jsonify([])
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            is_link = _ANDROID_IS_LINK
            ts_col = _ANDROID_TS
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            is_link = _IOS_IS_LINK
            ts_col = _IOS_TS
        sql = f"{select} WHERE {chat_pred} AND {is_link} ORDER BY {ts_col} DESC"
        rows = [dict(r) for r in conn.execute(sql, chat_params).fetchall()]
        _hydrate_quoted_rows(rows, source_type, conn)
        _hydrate_link_preview_rows(rows, source_type)
        return jsonify(rows)

    @app.route("/api/media/documents")
    def api_media_documents():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        conn = get_wa()
        if conn is None:
            return jsonify([])
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            is_doc = _ANDROID_IS_DOCUMENT_UNDOWNLOADED
            ts_col = _ANDROID_TS
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            is_doc = _IOS_IS_DOCUMENT_UNDOWNLOADED
            ts_col = _IOS_TS
        sql = f"{select} WHERE {chat_pred} AND {is_doc} ORDER BY {ts_col} DESC"
        rows = [dict(r) for r in conn.execute(sql, chat_params).fetchall()]
        _hydrate_quoted_rows(rows, source_type, conn)
        _hydrate_link_preview_rows(rows, source_type)
        return jsonify(rows)

    # ---- API: media count --------------------------------------------------

    @app.route("/api/media/count")
    def api_media_count():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        conn = get_wa()
        if conn is None:
            return jsonify({})
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER
        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            is_media = _ANDROID_IS_MEDIA
            is_link = _ANDROID_IS_LINK
            is_doc_undownloaded = _ANDROID_IS_DOCUMENT_UNDOWNLOADED
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            is_media = _IOS_IS_MEDIA
            is_link = _IOS_IS_LINK
            is_doc_undownloaded = _IOS_IS_DOCUMENT_UNDOWNLOADED
        rows = conn.execute(
            f"{select} WHERE {chat_pred} {extra} AND {is_media}", chat_params
        ).fetchall()
        link_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {is_link}", chat_params
        ).fetchall()
        doc_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {is_doc_undownloaded}", chat_params
        ).fetchall()
        all_rows = list(rows) + list(doc_rows) + list(link_rows)
        total = len(all_rows)
        archived = sum(1 for r in rows if r["archive_path"])
        by_type: dict = {}
        for r in rows:
            t = r["media_type"]
            by_type.setdefault(t, {"count": 0, "missing": 0})
            by_type[t]["count"] += 1
            is_vo = r["is_view_once"] if "is_view_once" in r.keys() else 0
            if not r["archive_path"] and not is_vo:
                by_type[t]["missing"] += 1
        for r in list(doc_rows) + list(link_rows):
            t = r["media_type"]
            by_type.setdefault(t, {"count": 0, "missing": 0})
            by_type[t]["count"] += 1
        return jsonify({"total": total, "archived": archived, "by_type": by_type})

    # ---- API: search -------------------------------------------------------

    @app.route("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        chat_id = request.args.get("chat_id")
        chat_type = request.args.get("chat_type")
        if not q:
            return jsonify({"results": [], "indexed_count": 0})

        fts_conn = get_cache()
        wa = get_wa()

        try:
            if chat_id and chat_type:
                idx_rows = fts_conn.execute(
                    """
                    SELECT mi.rowid, mi.chat_id, mi.chat_type, mi.timestamp_ms
                    FROM message_index_fts fts
                    JOIN message_index mi ON mi.rowid = fts.rowid
                    WHERE message_index_fts MATCH ?
                      AND mi.chat_id = ? AND mi.chat_type = ?
                    ORDER BY mi.timestamp_ms ASC LIMIT 500
                """,
                    (q, chat_id, chat_type),
                ).fetchall()
            elif chat_id:
                idx_rows = fts_conn.execute(
                    """
                    SELECT mi.rowid, mi.chat_id, mi.chat_type, mi.timestamp_ms
                    FROM message_index_fts fts
                    JOIN message_index mi ON mi.rowid = fts.rowid
                    WHERE message_index_fts MATCH ? AND mi.chat_id = ?
                    ORDER BY mi.timestamp_ms ASC LIMIT 500
                """,
                    (q, chat_id),
                ).fetchall()
            else:
                idx_rows = fts_conn.execute(
                    """
                    SELECT mi.rowid, mi.chat_id, mi.chat_type, mi.timestamp_ms
                    FROM message_index_fts fts
                    JOIN message_index mi ON mi.rowid = fts.rowid
                    WHERE message_index_fts MATCH ?
                    ORDER BY mi.timestamp_ms ASC LIMIT 100
                """,
                    (q,),
                ).fetchall()
        except sqlite3.OperationalError as e:
            return jsonify({"error": f"Invalid search query: {e}"}), 400

        if not idx_rows:
            indexed_count = get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            return jsonify({"results": [], "indexed_count": indexed_count})

        rowids = [r["rowid"] for r in idx_rows]
        live_rowids = [r for r in rowids if r > 0]
        pres_rowids = [-r for r in rowids if r < 0]

        results = []
        if live_rowids and wa is not None:
            placeholders = ",".join("?" * len(live_rowids))
            id_col = "m._id" if source_type == "android" else "m.Z_PK"
            select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
            extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER
            rows = [
                dict(r)
                for r in wa.execute(
                    f"{select} WHERE {id_col} IN ({placeholders}) {extra} ORDER BY timestamp_ms ASC",
                    live_rowids,
                ).fetchall()
            ]
            _hydrate_link_preview_rows(rows, source_type)
            results.extend(rows)
        elif live_rowids and wa is None:
            results.extend([dict(r) for r in idx_rows if r["rowid"] > 0])

        if pres_rowids:
            pres = get_preserved()
            if pres is not None:
                p_placeholders = ",".join("?" * len(pres_rowids))
                p_rows = pres.execute(
                    f"SELECT rowid, * FROM preserved_messages WHERE rowid IN ({p_placeholders})",
                    pres_rowids,
                ).fetchall()
                for pr in p_rows:
                    d = dict(pr)
                    d["msg_id"] = -int(d["rowid"])
                    d["sort_id"] = -int(d["rowid"])
                    d["is_preserved"] = 1
                    d["is_view_once"] = int(d.get("is_view_once") or 0)
                    d.pop("rowid", None)
                    d.pop("action_type", None)
                    d.pop("participant_name", None)
                    d.pop("source_platform", None)
                    results.append(d)

        results.sort(key=lambda r: (r.get("timestamp_ms", 0), r.get("msg_id", 0)))
        indexed_count = get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
        return jsonify({"results": results, "indexed_count": indexed_count})

    # ---- API: message receipts -----------------------------------------------

    @app.route("/api/message_receipts/<int:message_id>")
    def api_message_receipts(message_id):
        conn = get_wa()
        if conn is None:
            return jsonify({"available": False})

        if source_type == "android":
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_user'"
            ).fetchone()
            if not table_exists:
                return jsonify({"available": False})

            has_jid_map = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid_map'"
                ).fetchone()
            )
            jm_join = (
                """
                LEFT JOIN (
                    SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                    FROM jid_map GROUP BY lid_row_id
                ) jm ON jm.lid_row_id = ru.receipt_user_jid_row_id
                LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
            """
                if has_jid_map
                else """
                LEFT JOIN (SELECT NULL AS lid_row_id, NULL AS jid_row_id) jm ON 1=0
                LEFT JOIN (SELECT NULL AS _id, NULL AS raw_string, NULL AS user) j_real ON 1=0
            """
            )
            rows = conn.execute(
                f"""
                SELECT ru.receipt_timestamp, ru.read_timestamp, ru.played_timestamp,
                       COALESCE(j_real.raw_string, j.raw_string) AS jid,
                       COALESCE(j_real.user, j.user)             AS phone,
                       COALESCE(con.display_name, con2.display_name,
                                CASE WHEN COALESCE(j_real.user, j.user) = '0' THEN 'WhatsApp' END,
                                CASE WHEN COALESCE(j_real.user, j.user) IS NOT NULL
                                     THEN '+' || COALESCE(j_real.user, j.user)
                                END,
                                COALESCE(j_real.raw_string, j.raw_string)) AS name
                FROM receipt_user ru
                LEFT JOIN jid j ON j._id = ru.receipt_user_jid_row_id
                {jm_join}
                LEFT JOIN arch.contacts con  ON con.number  = j_real.user
                LEFT JOIN arch.contacts con2 ON con2.number = j.user
                WHERE ru.message_row_id = ?
            """,
                (message_id,),
            ).fetchall()

            if not rows:
                cols = {r[1] for r in conn.execute("PRAGMA table_info('message')").fetchall()}
                status_expr = "status" if "status" in cols else "0"
                msg_row = conn.execute(
                    f"SELECT from_me, {status_expr} AS status, timestamp FROM message WHERE _id = ?",
                    (message_id,),
                ).fetchone()
                if msg_row and msg_row["from_me"] == 1:
                    st = msg_row["status"] or 0
                    ts = msg_row["timestamp"]
                    read_known = st == 13
                    deliv_ts = ts if st in (5, 6, 13) else None
                    read_ts = None
                    played_ts = ts if st == 6 else None
                    return jsonify(
                        {
                            "available": True,
                            "members": [
                                {
                                    "name": "",
                                    "jid": "",
                                    "delivered_ts": deliv_ts,
                                    "read_ts": read_ts,
                                    "played_ts": played_ts,
                                    "read_known": read_known,
                                }
                            ],
                            "delivered_ts": deliv_ts,
                            "read_ts": read_ts,
                            "played_ts": played_ts,
                            "read_known": read_known,
                        }
                    )
                return jsonify({"available": True, "members": []})

            members = [
                {
                    "name": r["name"] or r["jid"] or "",
                    "jid": r["jid"] or "",
                    "delivered_ts": r["receipt_timestamp"] or None,
                    "read_ts": r["read_timestamp"] or None,
                    "played_ts": r["played_timestamp"] or None,
                }
                for r in rows
            ]
            result = {"available": True, "members": members}
            if len(members) == 1:
                result["delivered_ts"] = members[0]["delivered_ts"]
                result["read_ts"] = members[0]["read_ts"]
                result["played_ts"] = members[0]["played_ts"]
                result["read_known"] = members[0]["read_ts"] is not None
            return jsonify(result)

        else:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
            ).fetchone()
            if not table_exists:
                return jsonify({"available": False})

            cols = {r[1] for r in conn.execute("PRAGMA table_info('ZWAMESSAGE')").fetchall()}
            status_col = "m.ZMESSAGESTATUS" if "ZMESSAGESTATUS" in cols else "NULL"
            stanza_col = "m.ZSTANZAID" if "ZSTANZAID" in cols else "NULL"

            row = conn.execute(
                f"""SELECT mi.ZRECEIPTINFO,
                          cs.ZGROUPINFO IS NOT NULL AS is_group,
                          CAST(m.ZMESSAGEDATE + 978307200 AS INTEGER) AS msg_ts_s,
                          m.ZCHATSESSION AS chat_session_pk,
                          {status_col} AS msg_status,
                          {stanza_col} AS stanza_id
                   FROM ZWAMESSAGE m
                   LEFT JOIN ZWAMESSAGEINFO mi ON mi.ZMESSAGE = m.Z_PK
                   JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
                   WHERE m.Z_PK = ?""",
                (message_id,),
            ).fetchone()
            if not row:
                return jsonify({"available": False})

            is_group = bool(row["is_group"])
            members = []

            has_receipt_device = False
            try:
                conn.execute("SELECT 1 FROM infra.receipt_device LIMIT 1")
                has_receipt_device = True
            except sqlite3.OperationalError:
                has_receipt_device = False

            if has_receipt_device and row["stanza_id"]:
                infra_rows = conn.execute(
                    """SELECT user_jid, device_id, delivered_timestamp, read_timestamp, played_timestamp
                       FROM infra.receipt_device
                       WHERE stanza_id = ?""",
                    (row["stanza_id"],),
                ).fetchall()
                if infra_rows:
                    members = _parse_ios_receipt_device_rows(infra_rows, conn, is_group)

            if not members and row["ZRECEIPTINFO"]:
                blob = bytes(row["ZRECEIPTINFO"])
                members = _parse_ios_receipt_blob(
                    blob, conn, is_group=is_group, msg_ts_s=row["msg_ts_s"] or 0
                )

            if not members and not row["ZRECEIPTINFO"]:
                return jsonify({"available": False})

            if is_group and members:
                # 1. Try active or all group members directly from ZWAGROUPMEMBER
                member_jids = set()
                try:
                    for r in conn.execute(
                        """SELECT DISTINCT gm.ZMEMBERJID
                           FROM ZWAGROUPMEMBER gm
                           WHERE gm.ZCHATSESSION = CAST(? AS INTEGER)
                             AND gm.ZMEMBERJID IS NOT NULL""",
                        (row["chat_session_pk"],),
                    ).fetchall():
                        if r[0]:
                            member_jids.add(r[0])
                except sqlite3.OperationalError:
                    pass

                # 2. Fallback to message history senders via ZGROUPMEMBER if direct query yielded nothing
                if not member_jids:
                    try:
                        for r in conn.execute(
                            """SELECT DISTINCT gm.ZMEMBERJID
                               FROM ZWAMESSAGE m
                               JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                               WHERE m.ZCHATSESSION = ?
                                 AND gm.ZMEMBERJID IS NOT NULL""",
                            (row["chat_session_pk"],),
                        ).fetchall():
                            if r[0]:
                                member_jids.add(r[0])
                    except sqlite3.OperationalError:
                        pass

                if member_jids:
                    known_jids = set()
                    for jid in member_jids:
                        prefix = jid.split("@")[0]
                        known_jids.add(prefix)
                        known_jids.add(jid)
                        # Also resolve corresponding LID or phone number via _ios_contacts
                        if jid.endswith("@s.whatsapp.net"):
                            try:
                                lids = conn.execute(
                                    "SELECT jid FROM _ios_contacts WHERE phone_number = ? AND jid LIKE '%@lid'",
                                    (prefix,),
                                ).fetchall()
                                for l_row in lids:
                                    known_jids.add(l_row[0].split("@")[0])
                                    known_jids.add(l_row[0])
                            except sqlite3.OperationalError:
                                pass
                        elif jid.endswith("@lid"):
                            try:
                                phones = conn.execute(
                                    "SELECT phone_number FROM _ios_contacts WHERE jid = ?",
                                    (jid,),
                                ).fetchall()
                                for p_row in phones:
                                    if p_row[0]:
                                        known_jids.add(p_row[0])
                                        known_jids.add(f"{p_row[0]}@s.whatsapp.net")
                            except sqlite3.OperationalError:
                                pass
                    members = [
                        m
                        for m in members
                        if m.get("jid") in known_jids
                        or f"{m.get('jid')}@lid" in known_jids
                        or f"{m.get('jid')}@s.whatsapp.net" in known_jids
                    ]

            for m in members:
                if m.get("read_known") is None or not m["read_known"]:
                    m["read_known"] = bool(
                        m.get("read_ts") is not None
                        or _ios_read_without_timestamp(row["msg_status"], m.get("read_ts"))
                    )

            result = {"available": True, "members": members}
            if len(members) == 1:
                result["delivered_ts"] = members[0].get("delivered_ts")
                result["read_ts"] = members[0].get("read_ts")
                result["played_ts"] = members[0].get("played_ts")
                result["read_known"] = members[0].get("read_known")
            return jsonify(result)

    # ---- API: reaction details -----------------------------------------------

    @app.route("/api/reaction_details/<int:message_id>")
    def api_reaction_details(message_id):
        conn = get_wa()
        if conn is None:
            return jsonify({"available": False})

        if source_type == "android":
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "message_add_on" not in tables:
                return jsonify({"available": False})
            _ensure_jid_map_resolved(conn, _archive_conn)
            mao_cols = {r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()}
            chat_join_expr = (
                "COALESCE(ao.chat_row_id, m.chat_row_id)"
                if "chat_row_id" in mao_cols
                else "m.chat_row_id"
            )
            has_lid_dn = "lid_display_name" in tables
            ldn_rx_join = (
                """
                LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = ao.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = ao.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
            """
                if has_lid_dn
                else ""
            )
            ldn_rx_arm = (
                """
                           NULLIF(ldn_direct.display_name, ''),
                           NULLIF(ldn_mapped.display_name, ''),
            """
                if has_lid_dn
                else ""
            )
            ldn_c_join = (
                """
                LEFT JOIN _lid_map_resolved jm_lid_c ON jm_lid_c.jid_row_id = c.jid_row_id
                LEFT JOIN lid_display_name ldn_c_direct ON ldn_c_direct.lid_row_id = c.jid_row_id
                LEFT JOIN lid_display_name ldn_c_mapped ON ldn_c_mapped.lid_row_id = jm_lid_c.lid_row_id
            """
                if has_lid_dn
                else ""
            )
            ldn_c_arm = (
                """
                                   NULLIF(ldn_c_direct.display_name, ''),
                                   NULLIF(ldn_c_mapped.display_name, ''),
            """
                if has_lid_dn
                else ""
            )
            rows = conn.execute(
                f"""
                SELECT r.reaction AS emoji,
                       ao.from_me,
                       COALESCE(
                           NULLIF(con_s.display_name, ''),
                           CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                           CASE WHEN COALESCE(j2.user, j.user) IS NOT NULL
                                THEN '+' || {_clean_user_sql("COALESCE(j2.user, j.user)")} END,
                           {ldn_rx_arm}
                           CASE WHEN ao.from_me = 0 AND (ao.sender_jid_row_id <= 0 OR ao.sender_jid_row_id IS NULL)
                                THEN COALESCE(
                                    NULLIF(con_c.display_name, ''),
                                    NULLIF(c.subject, ''),
                                    CASE WHEN COALESCE(j2_c.user, j_c.user) = '0' THEN 'WhatsApp' END,
                                    CASE WHEN COALESCE(j2_c.user, j_c.user) IS NOT NULL
                                         THEN '+' || {_clean_user_sql("COALESCE(j2_c.user, j_c.user)")} END,
                                    {ldn_c_arm}
                                    ''
                                )
                           END,
                           ''
                       ) AS name
                FROM message_add_on ao
                JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                LEFT JOIN jid j ON j._id = ao.sender_jid_row_id
                LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = ao.sender_jid_row_id
                LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
                {ldn_rx_join}
                LEFT JOIN arch.contacts con_s ON con_s.number = {_clean_user_sql("COALESCE(j2.user, j.user)")}
                LEFT JOIN message m ON m._id = ao.parent_message_row_id
                LEFT JOIN chat c ON c._id = {chat_join_expr}
                LEFT JOIN jid j_c ON j_c._id = c.jid_row_id
                LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id
                LEFT JOIN jid j2_c ON j2_c._id = jm_c.jid_row_id
                {ldn_c_join}
                LEFT JOIN arch.contacts con_c ON con_c.number = {_clean_user_sql("COALESCE(j2_c.user, j_c.user)")}
                WHERE ao.parent_message_row_id = ?
            """,
                (message_id,),
            ).fetchall()
            reactors = [
                {"name": r["name"] or "", "emoji": r["emoji"] or "", "from_me": r["from_me"] or 0}
                for r in rows
            ]
            return jsonify({"available": True, "total": len(reactors), "reactors": reactors})

        else:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGEINFO'"
            ).fetchone()
            if not table_exists:
                return jsonify({"available": False})

            row = conn.execute(
                """
                SELECT mi.ZRECEIPTINFO, m.ZCHATSESSION AS chat_session_pk
                FROM ZWAMESSAGEINFO mi
                JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
                WHERE mi.ZMESSAGE = ?
            """,
                (message_id,),
            ).fetchone()
            if not row or not row[0]:
                return jsonify({"available": False})

            blob = bytes(row[0])
            raw_reactors = _extract_ios_reaction_reactors(blob)
            if not raw_reactors:
                return jsonify({"available": True, "total": 0, "reactors": []})

            # Resolve "me" from a sent message in this chat, and the 1-to-1 partner.
            my_row = conn.execute(
                """
                SELECT COALESCE(gm.ZMEMBERJID, m.ZFROMJID) AS me_jid
                FROM ZWAMESSAGE m
                LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                WHERE m.ZCHATSESSION = ? AND m.ZISFROMME = 1
                LIMIT 1
            """,
                (row["chat_session_pk"],),
            ).fetchone()
            my_phone = None
            if my_row and my_row["me_jid"]:
                at = my_row["me_jid"].find("@")
                if at > 0:
                    my_phone = my_row["me_jid"][:at]

            def _resolve_number(num):
                r = conn.execute(
                    "SELECT full_name FROM _ios_contacts WHERE jid = ?", (f"{num}@s.whatsapp.net",)
                ).fetchone()
                if r and r[0]:
                    return r[0]
                r = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (num,)
                ).fetchone()
                if r and r[0]:
                    return r[0]
                return None

            def _resolve_lid(lid_jid):
                # Same chain the message-sender query uses for @lid group members:
                # ContactsV2 full name, then chat-session partner name, then the
                # broadcast push name keyed by the LID.
                r = conn.execute(
                    "SELECT full_name, phone_number FROM _ios_contacts WHERE jid = ?", (lid_jid,)
                ).fetchone()
                if r and r[0] and not r[0].startswith("+"):
                    return r[0]
                try:
                    r_cs = conn.execute(
                        "SELECT ZPARTNERNAME FROM ZWACHATSESSION WHERE ZCONTACTJID = ? "
                        "AND ZPARTNERNAME IS NOT NULL AND ZPARTNERNAME != '' LIMIT 1",
                        (lid_jid,),
                    ).fetchone()
                    if r_cs and r_cs[0]:
                        return r_cs[0]
                    r_pp = conn.execute(
                        "SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ? "
                        "AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != '' LIMIT 1",
                        (lid_jid,),
                    ).fetchone()
                    if r_pp and r_pp[0]:
                        return r_pp[0]
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e).lower():
                        raise
                if r and r[1]:
                    return f"+{r[1]}"
                if r and r[0]:
                    return r[0]
                return None

            reactors = []
            for rr in raw_reactors:
                emoji_char = rr["emoji"]
                phone = rr["phone"]
                lid = rr["lid"]
                if (not phone and not lid) or (phone and my_phone and phone == my_phone):
                    # Own reaction: WhatsApp stores no reactor JID for the DB owner.
                    reactors.append({"name": "", "emoji": emoji_char, "from_me": 1})
                elif phone:
                    name = _resolve_number(phone) or f"+{phone}"
                    reactors.append({"name": name, "emoji": emoji_char, "from_me": 0})
                else:
                    # @lid reactor (recent group reactions). Resolves via the same
                    # push-name chain as group message senders; "Unknown" only when
                    # the LID is absent from every name source in the export.
                    name = _resolve_lid(lid) or "Unknown"
                    reactors.append({"name": name, "emoji": emoji_char, "from_me": 0})

            return jsonify({"available": True, "total": len(reactors), "reactors": reactors})

    # ---- API: poll details ----------------------------------------------------

    @app.route("/api/poll_details/<int:message_id>")
    def api_poll_details(message_id):
        conn = get_wa()
        if conn is None:
            return jsonify({"available": False})

        if source_type == "android":
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "message_poll" not in tables or "message_poll_option" not in tables:
                return jsonify({"available": False})

            q_row = conn.execute(
                "SELECT text_data FROM message WHERE _id = ?", (message_id,)
            ).fetchone()
            question = q_row[0] if q_row and q_row[0] else ""

            meta_row = conn.execute(
                "SELECT selectable_options_count FROM message_poll WHERE message_row_id = ?",
                (message_id,),
            ).fetchone()
            selectable_count = meta_row[0] if meta_row else 0

            opt_rows = conn.execute(
                "SELECT _id, option_name, vote_total FROM message_poll_option WHERE message_row_id = ? ORDER BY _id ASC",
                (message_id,),
            ).fetchall()

            voters_by_opt = {opt["_id"]: [] for opt in opt_rows}
            total_voters_set = set()
            if "message_add_on" in tables and "message_add_on_poll_vote_selected_option" in tables:
                has_lid_dn = "lid_display_name" in tables
                ldn_rx_join = (
                    """
                    LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = mao.sender_jid_row_id
                    LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = mao.sender_jid_row_id
                    LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
                    """
                    if has_lid_dn
                    else ""
                )
                ldn_rx_arm = (
                    """
                    NULLIF(ldn_direct.display_name, ''),
                    NULLIF(ldn_mapped.display_name, ''),
                    """
                    if has_lid_dn
                    else ""
                )
                mao_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()
                }
                has_pv = "message_add_on_poll_vote" in tables
                pv_join = (
                    "LEFT JOIN message_add_on_poll_vote pv ON pv.message_add_on_row_id = mao._id"
                    if has_pv
                    else ""
                )
                if "timestamp" in mao_cols and has_pv:
                    ts_expr = "COALESCE(mao.timestamp, pv.sender_timestamp, 0)"
                    order_expr = "COALESCE(mao.timestamp, pv.sender_timestamp, 0) ASC"
                elif "timestamp" in mao_cols:
                    ts_expr = "COALESCE(mao.timestamp, 0)"
                    order_expr = "mao.timestamp ASC"
                elif has_pv:
                    ts_expr = "COALESCE(pv.sender_timestamp, 0)"
                    order_expr = "pv.sender_timestamp ASC"
                else:
                    ts_expr = "0"
                    order_expr = "mao._id ASC"

                try:
                    vote_rows = conn.execute(
                        f"""
                        SELECT
                            ps.message_poll_option_id,
                            mao._id AS add_on_id,
                            mao.from_me,
                            {ts_expr} AS timestamp_ms,
                            COALESCE(
                                NULLIF(con_s.display_name, ''),
                                CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                                CASE WHEN COALESCE(j2.user, j.user) IS NOT NULL
                                     THEN '+' || {_clean_user_sql("COALESCE(j2.user, j.user)")} END,
                                {ldn_rx_arm}
                                ''
                            ) AS sender_name
                        FROM message_add_on mao
                        JOIN message_add_on_poll_vote_selected_option ps ON ps.message_add_on_row_id = mao._id
                        {pv_join}
                        LEFT JOIN jid j ON j._id = mao.sender_jid_row_id
                        LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = mao.sender_jid_row_id
                        LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
                        {ldn_rx_join}
                        LEFT JOIN arch.contacts con_s ON con_s.number = {_clean_user_sql("COALESCE(j2.user, j.user)")}
                        WHERE mao.parent_message_row_id = ? AND mao.message_add_on_type = 67
                        ORDER BY {order_expr}
                        """,
                        (message_id,),
                    ).fetchall()

                    for vr in vote_rows:
                        total_voters_set.add(vr["add_on_id"])
                        opt_id = vr["message_poll_option_id"]
                        if opt_id in voters_by_opt:
                            voter_name = (
                                "You" if vr["from_me"] == 1 else (vr["sender_name"] or "Unknown")
                            )
                            voters_by_opt[opt_id].append(
                                {
                                    "name": voter_name,
                                    "timestamp_ms": vr["timestamp_ms"],
                                }
                            )
                except sqlite3.OperationalError:
                    pass

            return jsonify(
                {
                    "available": True,
                    "msg_id": message_id,
                    "question": question,
                    "multiple_answers": (selectable_count == 0),
                    "total_voters": len(total_voters_set),
                    "options": [
                        {
                            "id": opt["_id"],
                            "name": opt["option_name"] or "",
                            "vote_count": opt["vote_total"] or len(voters_by_opt[opt["_id"]]),
                            "voters": voters_by_opt[opt["_id"]],
                        }
                        for opt in opt_rows
                    ],
                }
            )

        else:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGEINFO'"
            ).fetchone()
            if not table_exists:
                return jsonify({"available": False})

            row = conn.execute(
                """
                SELECT mi.ZRECEIPTINFO, m.ZCHATSESSION AS chat_session_pk
                FROM ZWAMESSAGEINFO mi
                JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
                WHERE mi.ZMESSAGE = ?
                """,
                (message_id,),
            ).fetchone()
            if not row or not row[0]:
                return jsonify({"available": False})

            parsed = _parse_ios_poll_blob(bytes(row[0]))
            if not parsed:
                return jsonify({"available": False})

            my_row = conn.execute(
                """
                SELECT COALESCE(gm.ZMEMBERJID, m.ZFROMJID) AS me_jid
                FROM ZWAMESSAGE m
                LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                WHERE m.ZCHATSESSION = ? AND m.ZISFROMME = 1
                LIMIT 1
                """,
                (row["chat_session_pk"],),
            ).fetchone()
            my_phone = None
            if my_row and my_row["me_jid"]:
                at = my_row["me_jid"].find("@")
                if at > 0:
                    my_phone = my_row["me_jid"][:at]

            def _resolve_number(num: str) -> str | None:
                r = conn.execute(
                    "SELECT full_name FROM _ios_contacts WHERE jid = ?", (f"{num}@s.whatsapp.net",)
                ).fetchone()
                if r and r[0]:
                    return r[0]
                r = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (num,)
                ).fetchone()
                if r and r[0]:
                    return r[0]
                return None

            def _resolve_lid(lid_jid: str) -> str | None:
                r = conn.execute(
                    "SELECT full_name, phone_number FROM _ios_contacts WHERE jid = ?", (lid_jid,)
                ).fetchone()
                if r and r[0] and not r[0].startswith("+"):
                    return r[0]
                try:
                    r_cs = conn.execute(
                        "SELECT ZPARTNERNAME FROM ZWACHATSESSION WHERE ZCONTACTJID = ? "
                        "AND ZPARTNERNAME IS NOT NULL AND ZPARTNERNAME != '' LIMIT 1",
                        (lid_jid,),
                    ).fetchone()
                    if r_cs and r_cs[0]:
                        return r_cs[0]
                    r_pp = conn.execute(
                        "SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ? "
                        "AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != '' LIMIT 1",
                        (lid_jid,),
                    ).fetchone()
                    if r_pp and r_pp[0]:
                        return r_pp[0]
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e).lower():
                        raise
                if r and r[1]:
                    return f"+{r[1]}"
                if r and r[0]:
                    return r[0]
                return None

            voters_by_opt = {idx: [] for idx in range(len(parsed["options"]))}
            for v in parsed["votes"]:
                v_jid = v["voter_jid"]
                v_ts = v["server_ts"] or v["client_ts"] or 0
                if not v_jid or v_jid == "You" or (my_phone and v_jid.startswith(my_phone)):
                    v_name = "You"
                elif v_jid.endswith("@s.whatsapp.net"):
                    phone = v_jid.split("@")[0]
                    v_name = _resolve_number(phone) or f"+{phone}"
                elif v_jid.endswith("@lid"):
                    v_name = _resolve_lid(v_jid) or "Unknown"
                elif v_jid.isdigit():
                    v_name = _resolve_number(v_jid) or f"+{v_jid}"
                else:
                    v_name = v_jid

                for o_idx in v["option_indices"]:
                    if o_idx in voters_by_opt:
                        voters_by_opt[o_idx].append(
                            {
                                "name": v_name,
                                "timestamp_ms": v_ts,
                            }
                        )

            return jsonify(
                {
                    "available": True,
                    "msg_id": message_id,
                    "question": parsed["question"],
                    "multiple_answers": parsed["multiple_answers"],
                    "total_voters": len(parsed["votes"]),
                    "options": [
                        {
                            "id": idx,
                            "name": opt_name,
                            "vote_count": len(voters_by_opt[idx]),
                            "voters": voters_by_opt[idx],
                        }
                        for idx, opt_name in enumerate(parsed["options"])
                    ],
                }
            )

    # ---- API: chat info -------------------------------------------------------

    def _group_members_android(conn, chat_id: str) -> list:
        # Note on Android WhatsApp: User-chosen friendly push names (wa_name) reside
        # in /data/data/com.whatsapp/databases/wa.db (table wa_contacts). Extracting wa.db
        # requires root privileges on the device, which is not implemented in the Viewer.
        # In standard msgstore.db backups, lid_display_name only contains Meta's privacy-masked
        # phone strings (e.g. +39••••••••04), so unsaved members resolve to their real phone number
        # when mapped via jid_map, falling back to lid_display_name only if completely unmapped.
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        jid_cols = (
            {r["name"] for r in conn.execute("PRAGMA table_info(jid)").fetchall()}
            if "jid" in tables
            else set()
        )
        has_server = "server" in jid_cols
        has_raw_string = "raw_string" in jid_cols
        has_lid_dn = "lid_display_name" in tables
        has_gpu = "group_participant_user" in tables
        j_phone_fallback = (
            "CASE WHEN j.server = 's.whatsapp.net' THEN j.user END" if has_server else "j.user"
        )

        ldn_arm = (
            """
            NULLIF(ldn_direct.display_name, ''),
            NULLIF(ldn_mapped.display_name, ''),
        """
            if has_lid_dn
            else ""
        )

        # 1. Modern WhatsApp: group_participant_user
        if has_gpu:
            try:
                phone_expr_gpu = (
                    """
                    COALESCE(
                        j_phone.user,
                        CASE WHEN j_user.server = 's.whatsapp.net' THEN j_user.user END
                    )
                """
                    if has_server
                    else "COALESCE(j_phone.user, j_user.user)"
                )
                ldn_gpu_join = (
                    """
                    LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = gpu.user_jid_row_id
                    LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = gpu.user_jid_row_id
                    LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
                """
                    if has_lid_dn
                    else ""
                )

                rows = conn.execute(
                    f"""
                    SELECT
                        COALESCE({phone_expr_gpu}, '') AS number,
                        CASE WHEN j_user.raw_string = 'lid_me' THEN 'You'
                             ELSE COALESCE(
                                 NULLIF(con.display_name, ''),
                                 NULLIF(con_lid.display_name, ''),
                                 CASE WHEN {phone_expr_gpu} IS NOT NULL
                                      THEN '+' || {phone_expr_gpu}
                                 END,
                                 {ldn_arm}
                                 'Unknown'
                             )
                        END AS name
                    FROM group_participant_user gpu
                    JOIN chat c ON c._id = CAST(? AS INTEGER)
                    JOIN jid j_user ON j_user._id = gpu.user_jid_row_id
                    LEFT JOIN _jid_map_resolved jm_phone ON jm_phone.lid_row_id = gpu.user_jid_row_id
                    LEFT JOIN jid j_phone ON j_phone._id = jm_phone.jid_row_id
                    {ldn_gpu_join}
                    LEFT JOIN arch.contacts con ON con.number = {phone_expr_gpu}
                    LEFT JOIN arch.contacts con_lid ON con_lid.number = j_user.user
                    WHERE gpu.group_jid_row_id = c.jid_row_id
                    ORDER BY name
                """,
                    (chat_id,),
                ).fetchall()
                if rows:
                    return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                    raise

        # 2. Legacy WhatsApp: group_participants
        join_gp_jid = (
            "j_gp.raw_string = gp.jid"
            if has_raw_string
            else "j_gp.user = NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '')"
        )
        gjid_pred = (
            "gp.gjid = (SELECT j.raw_string FROM jid j WHERE j._id = c.jid_row_id)"
            if has_raw_string
            else "1=1"
        )
        try:
            phone_expr_gp = """
                COALESCE(
                    j_phone.user,
                    CASE WHEN gp.jid NOT LIKE '%@lid'
                         THEN NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '')
                    END
                )
            """
            ldn_gp_join = (
                """
                LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = j_gp._id
                LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = j_gp._id
                LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
            """
                if has_lid_dn
                else ""
            )

            rows = conn.execute(
                f"""
                SELECT
                    COALESCE({phone_expr_gp}, '') AS number,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        NULLIF(con_lid.display_name, ''),
                        CASE WHEN {phone_expr_gp} IS NOT NULL
                             THEN '+' || {phone_expr_gp}
                        END,
                        {ldn_arm}
                        'Unknown'
                    ) AS name
                FROM group_participants gp
                JOIN chat c ON c._id = CAST(? AS INTEGER)
                LEFT JOIN jid j_gp ON {join_gp_jid}
                LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = j_gp._id
                LEFT JOIN jid j_phone ON j_phone._id = jm.jid_row_id
                {ldn_gp_join}
                LEFT JOIN arch.contacts con ON con.number = {phone_expr_gp}
                LEFT JOIN arch.contacts con_lid ON con_lid.number = j_gp.user
                WHERE {gjid_pred}
                  AND gp.jid != ''
                ORDER BY name
            """,
                (chat_id,),
            ).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                raise

        # 3. Fallback: message history
        ldn_msg_join = (
            """
            LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
        """
            if has_lid_dn
            else ""
        )
        phone_expr_msg = f"COALESCE(j2.user, {j_phone_fallback})"

        rows = conn.execute(
            f"""
            SELECT DISTINCT
                COALESCE({phone_expr_msg}, '') AS number,
                COALESCE(
                    NULLIF(con.display_name, ''),
                    NULLIF(con_lid.display_name, ''),
                    CASE WHEN {phone_expr_msg} IS NOT NULL
                         THEN '+' || {phone_expr_msg}
                    END,
                    {ldn_arm}
                    'Unknown'
                ) AS name
            FROM message m
            JOIN chat c ON c._id = m.chat_row_id
            LEFT JOIN jid j ON j._id = m.sender_jid_row_id
            LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
            LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
            {ldn_msg_join}
            LEFT JOIN arch.contacts con ON con.number = {phone_expr_msg}
            LEFT JOIN arch.contacts con_lid ON con_lid.number = j.user
            WHERE m.chat_row_id = CAST(? AS INTEGER)
              AND m.from_me = 0
              AND m.sender_jid_row_id IS NOT NULL
            ORDER BY name
        """,
            (chat_id,),
        ).fetchall()
        return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]

    def _group_members_ios(conn, chat_id: str) -> list:
        phone_expr = """
            COALESCE(
                CASE WHEN gm.ZMEMBERJID LIKE '%@s.whatsapp.net'
                     THEN SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID || '@', '@') - 1)
                END,
                ic.phone_number
            )
        """
        raw_prefix = "SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID || '@', '@') - 1)"
        phone_jid = f"({phone_expr} || '@s.whatsapp.net')"

        query_template = f"""
            SELECT DISTINCT
                COALESCE(
                    {phone_expr},
                    ''
                ) AS number,
                COALESCE(
                    CASE WHEN ic.full_name NOT LIKE '+%' THEN NULLIF(ic.full_name, '') END,
                    NULLIF(con.display_name, ''),
                    NULLIF(con_lid.display_name, ''),
                    NULLIF(pp_lid.ZPUSHNAME, ''),
                    NULLIF(pp_phone.ZPUSHNAME, ''),
                    NULLIF(cs_lid.ZPARTNERNAME, ''),
                    NULLIF(cs_phone.ZPARTNERNAME, ''),
                    CASE WHEN {phone_expr} IS NOT NULL
                         THEN '+' || {phone_expr}
                    END,
                    'Unknown'
                ) AS name
            FROM {{source_table}}
            LEFT JOIN _ios_contacts ic ON ic.jid = gm.ZMEMBERJID
            LEFT JOIN arch.contacts con ON con.number = {phone_expr}
            LEFT JOIN arch.contacts con_lid ON con_lid.number = {raw_prefix}
            LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = gm.ZMEMBERJID
            LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = {phone_jid}
            LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = gm.ZMEMBERJID
            LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = {phone_jid}
            WHERE {{where_clause}}
              AND gm.ZMEMBERJID IS NOT NULL
              AND gm.ZMEMBERJID != ''
            ORDER BY name
        """
        try:
            # 1. Try active members from ZWAGROUPMEMBER
            rows = conn.execute(
                query_template.format(
                    source_table="ZWAGROUPMEMBER gm",
                    where_clause="gm.ZCHATSESSION = CAST(? AS INTEGER) AND gm.ZISACTIVE = 1",
                ),
                (chat_id,),
            ).fetchall()
            if not rows:
                # 2. Try all members from ZWAGROUPMEMBER
                rows = conn.execute(
                    query_template.format(
                        source_table="ZWAGROUPMEMBER gm",
                        where_clause="gm.ZCHATSESSION = CAST(? AS INTEGER)",
                    ),
                    (chat_id,),
                ).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                raise

        # 3. Fallback to message history senders via ZWAGROUPMEMBER
        try:
            rows = conn.execute(
                query_template.format(
                    source_table="ZWAMESSAGE m JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER",
                    where_clause="m.ZCHATSESSION = CAST(? AS INTEGER)",
                ),
                (chat_id,),
            ).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise

        # 4. Fallback to message history senders via ZFROMJID
        phone_expr_m = """
            COALESCE(
                CASE WHEN m.ZFROMJID LIKE '%@s.whatsapp.net'
                     THEN SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1)
                END,
                ic.phone_number
            )
        """
        raw_prefix_m = "SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1)"
        phone_jid_m = f"({phone_expr_m} || '@s.whatsapp.net')"

        try:
            rows = conn.execute(
                f"""
                SELECT DISTINCT
                    COALESCE(
                        {phone_expr_m},
                        ''
                    ) AS number,
                    COALESCE(
                        CASE WHEN ic.full_name NOT LIKE '+%' THEN NULLIF(ic.full_name, '') END,
                        NULLIF(con.display_name, ''),
                        NULLIF(con_lid.display_name, ''),
                        NULLIF(pp_lid.ZPUSHNAME, ''),
                        NULLIF(pp_phone.ZPUSHNAME, ''),
                        NULLIF(cs_lid.ZPARTNERNAME, ''),
                        NULLIF(cs_phone.ZPARTNERNAME, ''),
                        CASE WHEN {phone_expr_m} IS NOT NULL
                             THEN '+' || {phone_expr_m}
                        END,
                        'Unknown'
                    ) AS name
                FROM ZWAMESSAGE m
                LEFT JOIN _ios_contacts ic ON ic.jid = m.ZFROMJID
                LEFT JOIN arch.contacts con ON con.number = {phone_expr_m}
                LEFT JOIN arch.contacts con_lid ON con_lid.number = {raw_prefix_m}
                LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = m.ZFROMJID
                LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = {phone_jid_m}
                LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = m.ZFROMJID
                LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = {phone_jid_m}
                WHERE m.ZCHATSESSION = CAST(? AS INTEGER)
                  AND m.ZFROMJID IS NOT NULL
                  AND m.ZFROMJID != ''
                ORDER BY name
            """,
                (chat_id,),
            ).fetchall()
            return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            return []

    @app.route("/api/chat-info")
    @app.route("/api/chat_info")
    def api_chat_info():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")

        cache = get_cache()
        ts_row = cache.execute(
            "SELECT MIN(timestamp_ms) AS first_ts, MAX(timestamp_ms) AS last_ts "
            "FROM message_index WHERE chat_id = ? AND chat_type = ?",
            (chat_id, chat_type),
        ).fetchone()
        last_ts = ts_row["last_ts"] if ts_row else None
        first_ts = None

        conn = get_wa()

        if conn is not None:
            if source_type == "android":
                chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
                live = conn.execute(
                    f"SELECT MIN(timestamp_ms) AS first_ts, MAX(timestamp_ms) AS last_ts"
                    f" FROM ({_ANDROID_SELECT} WHERE {chat_pred} {_ANDROID_FILTER_TS})",
                    chat_params,
                ).fetchone()
            else:
                chat_pred, chat_params = _ios_chat_filter(chat_id)
                live = conn.execute(
                    f"SELECT MIN(timestamp_ms) AS first_ts, MAX(timestamp_ms) AS last_ts"
                    f" FROM ({_IOS_SELECT} WHERE {chat_pred} {_IOS_FILTER_TS})",
                    chat_params,
                ).fetchone()
            if live:
                first_ts = live["first_ts"]
                last_ts = last_ts or live["last_ts"]
        sent = received = total = None

        display_name = None
        number = None
        description = None
        members = []
        top_senders = []
        created_ts = None
        creator_number = None
        creator_name = None

        if conn is not None:
            if source_type == "android":
                chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
                extra = _ANDROID_FILTER
                select = _ANDROID_SELECT

                count_row = conn.execute(
                    f"SELECT COUNT(*) AS total,"
                    f" SUM(CASE WHEN from_me=1 THEN 1 ELSE 0 END) AS sent,"
                    f" SUM(CASE WHEN from_me=0 THEN 1 ELSE 0 END) AS received"
                    f" FROM ({select} WHERE {chat_pred} {extra}) WHERE media_type != 'service'",
                    chat_params,
                ).fetchone()
                total = count_row["total"] or 0 if count_row else 0
                sent = count_row["sent"] or 0 if count_row else 0
                received = count_row["received"] or 0 if count_row else 0

                if chat_type == "contact":
                    row = conn.execute(
                        """
                        SELECT COALESCE(j_real.user, j.user) AS user
                        FROM chat c
                        LEFT JOIN jid j ON j._id = c.jid_row_id
                        LEFT JOIN (
                            SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                            FROM jid_map GROUP BY lid_row_id
                        ) jm ON jm.lid_row_id = c.jid_row_id
                        LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                        WHERE c.subject IS NULL
                          AND COALESCE(j_real.user, j.user) = ?
                        LIMIT 1
                    """,
                        (chat_id,),
                    ).fetchone()
                    number = row["user"] if row else chat_id

                    name_row = conn.execute(
                        """
                        SELECT COALESCE(NULLIF(con.display_name,''), con.folder) AS name
                        FROM arch.contacts con
                        WHERE con.number = ?
                    """,
                        (chat_id,),
                    ).fetchone()
                    if chat_id == "0":
                        display_name = "WhatsApp"
                    elif name_row and name_row["name"]:
                        display_name = name_row["name"]
                    else:
                        display_name = None
                else:
                    members = _group_members_android(conn, chat_id)

                    top_rows = conn.execute(
                        f"SELECT CASE WHEN from_me=1 THEN 'You' ELSE sender END AS sndr,"
                        f" COUNT(*) AS cnt FROM ({select} WHERE {chat_pred} {extra})"
                        f" WHERE (from_me=1 OR sender != '') AND media_type != 'service' GROUP BY sndr ORDER BY cnt DESC LIMIT 5",
                        chat_params,
                    ).fetchall()
                    top_senders = [{"name": r["sndr"], "count": r["cnt"]} for r in top_rows]

                    grp_row = conn.execute(
                        "SELECT COALESCE(NULLIF(grp.subject,''), grp.folder) AS name"
                        " FROM arch.groups grp WHERE grp.chat_row_id = ?",
                        (chat_id,),
                    ).fetchone()
                    display_name = grp_row["name"] if grp_row else None

                    try:
                        tables = {
                            r[0]
                            for r in conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            ).fetchall()
                        }
                        if "message_system" in tables:
                            desc_row = conn.execute(
                                """
                                SELECT m.text_data
                                FROM message m
                                JOIN message_system ms ON ms.message_row_id = m._id
                                WHERE m.chat_row_id = CAST(? AS INTEGER)
                                  AND ms.action_type = 27
                                ORDER BY m.timestamp DESC, m._id DESC
                                LIMIT 1
                            """,
                                (chat_id,),
                            ).fetchone()
                            if desc_row and desc_row["text_data"]:
                                description = desc_row["text_data"].strip() or None

                        if not description:
                            arch_grp_cols = {
                                c[1]
                                for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()
                            }
                            if "description" in arch_grp_cols:
                                arch_desc_row = conn.execute(
                                    "SELECT NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id = ?",
                                    (str(chat_id),),
                                ).fetchone()
                                if arch_desc_row and arch_desc_row["description"]:
                                    description = arch_desc_row["description"].strip() or None

                        jid_cols = (
                            {c[1] for c in conn.execute("PRAGMA table_info(jid)").fetchall()}
                            if "jid" in tables
                            else set()
                        )
                        j_fallback = (
                            "CASE WHEN j.server = 's.whatsapp.net' THEN j.user END"
                            if "server" in jid_cols
                            else "j.user"
                        )

                        has_jid_map = "jid_map" in tables
                        has_lid_dn = "lid_display_name" in tables

                        jm_join = (
                            """
                            LEFT JOIN (
                                SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                                FROM jid_map GROUP BY lid_row_id
                            ) jm ON jm.lid_row_id = m.sender_jid_row_id
                            LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                        """
                            if has_jid_map
                            else ""
                        )
                        creator_expr = (
                            f"COALESCE(j_real.user, {j_fallback})" if has_jid_map else j_fallback
                        )

                        ldn_creator_join = (
                            """
                            LEFT JOIN (
                                SELECT jid_row_id, MIN(lid_row_id) AS lid_row_id
                                FROM jid_map GROUP BY jid_row_id
                            ) jm_lid ON jm_lid.jid_row_id = m.sender_jid_row_id
                            LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id
                            LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
                        """
                            if has_lid_dn and has_jid_map
                            else (
                                "LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id"
                                if has_lid_dn
                                else ""
                            )
                        )
                        ldn_creator_arm = (
                            """
                            NULLIF(ldn_direct.display_name, ''),
                            NULLIF(ldn_mapped.display_name, ''),
                        """
                            if has_lid_dn and has_jid_map
                            else ("NULLIF(ldn_direct.display_name, '')," if has_lid_dn else "")
                        )

                        cre_row = conn.execute(
                            f"""
                            SELECT c.created_timestamp,
                                   {creator_expr} AS creator,
                                   COALESCE(
                                       NULLIF(con.display_name, ''),
                                       CASE WHEN {creator_expr} IS NOT NULL THEN '+' || {creator_expr} END,
                                       {ldn_creator_arm}
                                       ''
                                   ) AS creator_name
                            FROM chat c
                            LEFT JOIN message m
                                   ON m.chat_row_id = c._id
                                  AND m.timestamp = c.created_timestamp
                                  AND m.message_type = 7
                            LEFT JOIN jid j ON j._id = m.sender_jid_row_id
                            {jm_join}
                            {ldn_creator_join}
                            LEFT JOIN arch.contacts con ON con.number = {creator_expr}
                            WHERE c._id = CAST(? AS INTEGER)
                            LIMIT 1
                        """,
                            (chat_id,),
                        ).fetchone()
                        if cre_row:
                            created_ts = cre_row["created_timestamp"]
                            creator_number = cre_row["creator"]
                            creator_name = cre_row["creator_name"]
                    except sqlite3.OperationalError:
                        pass

            else:
                chat_pred, chat_params = _ios_chat_filter(chat_id)
                extra = _IOS_FILTER
                select = _IOS_SELECT

                count_row = conn.execute(
                    f"SELECT COUNT(*) AS total,"
                    f" SUM(CASE WHEN from_me=1 THEN 1 ELSE 0 END) AS sent,"
                    f" SUM(CASE WHEN from_me=0 THEN 1 ELSE 0 END) AS received"
                    f" FROM ({select} WHERE {chat_pred} {extra}) WHERE media_type != 'service'",
                    chat_params,
                ).fetchone()
                total = count_row["total"] or 0 if count_row else 0
                sent = count_row["sent"] or 0 if count_row else 0
                received = count_row["received"] or 0 if count_row else 0

                if chat_type == "contact":
                    cs_cols = {
                        r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()
                    }
                    has_cs_ident = "ZCONTACTIDENTIFIER" in cs_cols
                    cs_ident_col = "cs.ZCONTACTIDENTIFIER" if has_cs_ident else "NULL"
                    phone_expr = f"""
                        COALESCE(
                            CASE WHEN cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                                 THEN SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) END,
                            CASE WHEN {cs_ident_col} LIKE '%@s.whatsapp.net'
                                 THEN SUBSTR({cs_ident_col}, 1, INSTR({cs_ident_col}, '@') - 1) END,
                            ic.phone_number
                        )
                    """
                    row = conn.execute(
                        f"""
                        SELECT
                            ({phone_expr}) AS user,
                            COALESCE(
                                NULLIF(con.display_name,''),
                                NULLIF(con_lid.display_name,''),
                                CASE WHEN ({phone_expr}) = '0' OR cs.ZCONTACTJID = '0@s.whatsapp.net' THEN 'WhatsApp' END,
                                CASE WHEN ic.full_name IS NOT NULL AND ic.full_name != '' AND ic.full_name NOT LIKE '+%' AND ic.full_name NOT LIKE 'Unknown%' THEN ic.full_name END,
                                NULLIF(cs.ZPARTNERNAME, ''),
                                CASE WHEN con.folder IS NOT NULL AND con.folder != '' AND con.folder NOT LIKE 'Unknown%'
                                     THEN RTRIM(SUBSTR(con.folder, 1, INSTR(con.folder || ' (', ' (') - 1)) END,
                                CASE WHEN ({phone_expr}) IS NOT NULL AND ({phone_expr}) != '' THEN '+' || ({phone_expr}) END,
                                'Unknown Contact (' || cs.Z_PK || ')'
                            ) AS name
                        FROM ZWACHATSESSION cs
                        LEFT JOIN _ios_contacts ic ON ic.jid = cs.ZCONTACTJID
                        LEFT JOIN arch.contacts con ON con.number = ({phone_expr})
                        LEFT JOIN arch.contacts con_lid ON con_lid.number = NULLIF(
                            SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                   INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '')
                        WHERE cs.Z_PK = CAST(? AS INTEGER) AND cs.ZGROUPINFO IS NULL
                    """,
                        (chat_id,),
                    ).fetchone()
                    number = row["user"] if row else None
                    display_name = row["name"] if row else None
                else:
                    members = _group_members_ios(conn, chat_id)

                    top_rows = conn.execute(
                        f"SELECT CASE WHEN from_me=1 THEN 'You' ELSE sender END AS sndr,"
                        f" COUNT(*) AS cnt FROM ({select} WHERE {chat_pred} {extra})"
                        f" WHERE (from_me=1 OR sender != '') AND media_type != 'service' GROUP BY sndr ORDER BY cnt DESC LIMIT 5",
                        chat_params,
                    ).fetchall()
                    top_senders = [{"name": r["sndr"], "count": r["cnt"]} for r in top_rows]

                    grp_row = conn.execute(
                        "SELECT COALESCE(NULLIF(grp.subject,''), grp.folder) AS name"
                        " FROM arch.groups grp WHERE grp.chat_row_id = ?",
                        (chat_id,),
                    ).fetchone()
                    phone_expr_c = """
                        COALESCE(
                            CASE WHEN gi.ZCREATORJID LIKE '%@s.whatsapp.net'
                                 THEN SUBSTR(gi.ZCREATORJID, 1, INSTR(gi.ZCREATORJID || '@', '@') - 1)
                            END,
                            ic.phone_number
                        )
                    """
                    raw_prefix_c = (
                        "SUBSTR(gi.ZCREATORJID, 1, INSTR(gi.ZCREATORJID || '@', '@') - 1)"
                    )
                    phone_jid_c = f"({phone_expr_c} || '@s.whatsapp.net')"

                    gi_cols = {
                        c[1] for c in conn.execute("PRAGMA table_info(ZWAGROUPINFO)").fetchall()
                    }
                    extra_gi_cols = []
                    if "ZPICTUREID" in gi_cols:
                        extra_gi_cols.append("gi.ZPICTUREID AS picture_id")
                    if "ZDESCRIPTION" in gi_cols:
                        extra_gi_cols.append("gi.ZDESCRIPTION AS direct_desc")
                    elif "ZGROUPDESCRIPTION" in gi_cols:
                        extra_gi_cols.append("gi.ZGROUPDESCRIPTION AS direct_desc")
                    extra_gi_select = (", " + ", ".join(extra_gi_cols)) if extra_gi_cols else ""

                    cre_row = conn.execute(
                        f"""
                        SELECT CAST((gi.ZCREATIONDATE + 978307200) * 1000 AS INTEGER) AS created_ms,
                               COALESCE({phone_expr_c}, '') AS creator_number,
                               COALESCE(
                                   CASE WHEN ic.full_name NOT LIKE '+%' THEN NULLIF(ic.full_name, '') END,
                                   NULLIF(con.display_name, ''),
                                   NULLIF(con_lid.display_name, ''),
                                   NULLIF(pp_lid.ZPUSHNAME, ''),
                                   NULLIF(pp_phone.ZPUSHNAME, ''),
                                   NULLIF(cs_lid.ZPARTNERNAME, ''),
                                   NULLIF(cs_phone.ZPARTNERNAME, ''),
                                   ''
                               ) AS creator_name
                               {extra_gi_select}
                        FROM ZWACHATSESSION cs
                        JOIN ZWAGROUPINFO gi ON gi.Z_PK = cs.ZGROUPINFO
                        LEFT JOIN _ios_contacts ic ON ic.jid = gi.ZCREATORJID
                        LEFT JOIN arch.contacts con ON con.number = {phone_expr_c}
                        LEFT JOIN arch.contacts con_lid ON con_lid.number = {raw_prefix_c}
                        LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = gi.ZCREATORJID
                        LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = {phone_jid_c}
                        LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = gi.ZCREATORJID
                        LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = {phone_jid_c}
                        WHERE cs.Z_PK = CAST(? AS INTEGER)
                    """,
                        (chat_id,),
                    ).fetchone()
                    if cre_row:
                        created_ts = cre_row["created_ms"]
                        creator_number = cre_row["creator_number"] or None
                        creator_name = cre_row["creator_name"] or None
                        cre_keys = cre_row.keys()
                        if "direct_desc" in cre_keys and cre_row["direct_desc"]:
                            description = str(cre_row["direct_desc"]).strip() or None
                        elif "picture_id" in cre_keys and cre_row["picture_id"]:
                            description = _extract_ios_group_description(cre_row["picture_id"])

                    if not description:
                        arch_grp_cols = {
                            c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()
                        }
                        if "description" in arch_grp_cols:
                            arch_desc_row = conn.execute(
                                "SELECT NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id = ?",
                                (str(chat_id),),
                            ).fetchone()
                            if arch_desc_row and arch_desc_row["description"]:
                                description = arch_desc_row["description"].strip() or None

        if total is None or total == 0:
            pres = get_preserved()
            if pres is not None:
                p_stat = pres.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN from_me=1 THEN 1 ELSE 0 END) AS sent,
                           SUM(CASE WHEN from_me=0 THEN 1 ELSE 0 END) AS received,
                           MIN(timestamp_ms) AS first_ts,
                           MAX(timestamp_ms) AS last_ts
                    FROM preserved_messages
                    WHERE chat_id = ? AND chat_type = ?
                    """,
                    (chat_id, chat_type),
                ).fetchone()
                if p_stat and p_stat["total"]:
                    total = p_stat["total"]
                    sent = p_stat["sent"] or 0
                    received = p_stat["received"] or 0
                    first_ts = first_ts or p_stat["first_ts"]
                    last_ts = last_ts or p_stat["last_ts"]
                    if not display_name:
                        arch = get_archive()
                        if chat_type == "group":
                            g_row = arch.execute(
                                "SELECT subject, folder FROM groups WHERE chat_row_id = ?",
                                (chat_id,),
                            ).fetchone()
                            if g_row:
                                display_name = g_row["subject"] or g_row["folder"]
                        else:
                            c_row = arch.execute(
                                "SELECT display_name, folder FROM contacts WHERE number = ?",
                                (chat_id,),
                            ).fetchone()
                            if c_row:
                                display_name = c_row["display_name"] or c_row["folder"]
                            elif chat_id == "0":
                                display_name = "WhatsApp"
                            else:
                                display_name = f"+{chat_id}"
                            number = chat_id

        calls_count = 0
        pres = get_preserved()
        if pres is not None:
            try:
                pc_row = pres.execute(
                    """
                    SELECT subject, description, created_ts, creator_jid, oldest_ts, newest_ts, msg_count, members_json
                    FROM preserved_chats
                    WHERE chat_id = ? AND chat_type = ?
                    """,
                    (chat_id, chat_type),
                ).fetchone()
                if pc_row:
                    if not display_name and pc_row["subject"]:
                        display_name = pc_row["subject"]
                    if not description and pc_row["description"]:
                        description = pc_row["description"]
                    if created_ts is None and pc_row["created_ts"]:
                        created_ts = pc_row["created_ts"]
                    if first_ts is None and pc_row["oldest_ts"]:
                        first_ts = pc_row["oldest_ts"]
                    if last_ts is None and pc_row["newest_ts"]:
                        last_ts = pc_row["newest_ts"]
                    if (total is None or total == 0) and pc_row["msg_count"]:
                        total = pc_row["msg_count"]
                    if chat_type == "group" and not members and pc_row["members_json"]:
                        try:
                            members = json.loads(pc_row["members_json"])
                        except Exception:
                            pass
            except sqlite3.OperationalError:
                pass

            try:
                c_row = pres.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM preserved_calls
                    WHERE chat_id = ? OR chat_jid = ?
                    """,
                    (chat_id, chat_id),
                ).fetchone()
                if c_row and c_row["cnt"]:
                    calls_count = int(c_row["cnt"])
            except sqlite3.OperationalError:
                pass

        result = {
            "display_name": display_name,
            "number": number,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "sent": sent,
            "received": received,
            "total": total,
            "calls_count": calls_count,
        }
        if chat_type == "group":
            result["description"] = description
            result["members"] = members
            result["top_senders"] = top_senders
            result["created_ts"] = created_ts
            result["creator_number"] = creator_number
            result["creator_name"] = creator_name
        return jsonify(result)

    @app.route("/api/chat-info/media-size")
    def api_chat_info_media_size():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")

        conn = get_wa()
        prefix = None

        archive_conn = sqlite3.connect(str(archive_db_path), timeout=5.0)
        archive_conn.row_factory = sqlite3.Row
        try:
            if chat_type == "contact":
                folder_row = archive_conn.execute(
                    "SELECT folder FROM contacts WHERE number = ? OR number = ?",
                    (chat_id, chat_id.lstrip("+")),
                ).fetchone()
                if folder_row and folder_row["folder"]:
                    prefix = f"Contacts/{folder_row['folder']}/"
                elif conn is not None:
                    if source_type == "ios":
                        row = conn.execute(
                            """
                            SELECT COALESCE(con1.folder, con2.folder) AS folder
                            FROM ZWACHATSESSION cs
                            LEFT JOIN _ios_contacts ic ON ic.jid = cs.ZCONTACTJID
                            LEFT JOIN arch.contacts con1 ON con1.number = NULLIF(
                                SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                       INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '')
                            LEFT JOIN arch.contacts con2 ON con2.number = ic.phone_number
                            WHERE cs.Z_PK = CAST(? AS INTEGER)
                            """,
                            (chat_id,),
                        ).fetchone()
                        if row and row["folder"]:
                            prefix = f"Contacts/{row['folder']}/"
                        else:
                            sample = conn.execute(
                                """
                                SELECT ac.archive_path
                                FROM ZWAMESSAGE m
                                JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                                JOIN arch.archive_copies ac ON ac.original_path = 'Message/' || mi.ZMEDIALOCALPATH
                                WHERE m.ZCHATSESSION = CAST(? AS INTEGER) AND ac.archive_path IS NOT NULL
                                LIMIT 1
                                """,
                                (chat_id,),
                            ).fetchone()
                            if sample and sample["archive_path"]:
                                parts = sample["archive_path"].replace("\\", "/").split("/")
                                if len(parts) >= 2:
                                    prefix = f"{parts[0]}/{parts[1]}/"
                    else:
                        row = conn.execute(
                            f"""
                            SELECT con.folder
                            FROM chat c
                            LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                            LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                            LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                            LEFT JOIN arch.contacts con ON (
                                con.number = {_clean_user_sql("j_chat_real.user")}
                                OR con.number = {_clean_user_sql("j_chat.user")}
                                OR con.number = ?
                                OR con.number = ?
                            )
                            WHERE c.subject IS NULL
                              AND (
                                  c._id = CAST(? AS INTEGER)
                                  OR COALESCE(j_chat_real.user, j_chat.user) = ?
                                  OR j_chat.user = ?
                                  OR j_chat_real.user = ?
                                  OR {_clean_user_sql("COALESCE(j_chat_real.user, j_chat.user)")} = ?
                              )
                            LIMIT 1
                            """,
                            (
                                chat_id,
                                chat_id.lstrip("+"),
                                chat_id,
                                chat_id,
                                chat_id,
                                chat_id,
                                chat_id.split(":")[0],
                            ),
                        ).fetchone()
                        if row and row["folder"]:
                            prefix = f"Contacts/{row['folder']}/"
                        else:
                            clean_cid = chat_id.split(":")[0]
                            sample = conn.execute(
                                f"""
                                SELECT ac.archive_path
                                FROM message m
                                JOIN message_media mm ON mm.message_row_id = m._id
                                JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
                                WHERE m.chat_row_id IN (
                                    SELECT c._id FROM chat c
                                    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                                    LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                                    LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                                    WHERE c.subject IS NULL
                                      AND (
                                          c._id = CAST(? AS INTEGER)
                                          OR ? IN (
                                              j_chat_real.user, j_chat.user,
                                              {_clean_user_sql("j_chat_real.user")},
                                              {_clean_user_sql("j_chat.user")}
                                          )
                                          OR ? IN (
                                              j_chat_real.user, j_chat.user,
                                              {_clean_user_sql("j_chat_real.user")},
                                              {_clean_user_sql("j_chat.user")}
                                          )
                                      )
                                )
                                AND ac.archive_path IS NOT NULL
                                LIMIT 1
                                """,
                                (chat_id, chat_id, clean_cid),
                            ).fetchone()
                            if sample and sample["archive_path"]:
                                parts = sample["archive_path"].replace("\\", "/").split("/")
                                if len(parts) >= 2:
                                    prefix = f"{parts[0]}/{parts[1]}/"
            else:
                folder_row = archive_conn.execute(
                    "SELECT folder FROM groups WHERE chat_row_id = ?", (chat_id,)
                ).fetchone()
                prefix = (
                    f"Groups/{folder_row['folder']}/"
                    if folder_row and folder_row["folder"]
                    else None
                )
                if prefix is None and conn is not None:
                    if source_type == "ios":
                        sample = conn.execute(
                            """
                            SELECT ac.archive_path
                            FROM ZWAMESSAGE m
                            JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                            JOIN arch.archive_copies ac ON ac.original_path = 'Message/' || mi.ZMEDIALOCALPATH
                            WHERE m.ZCHATSESSION = CAST(? AS INTEGER) AND ac.archive_path IS NOT NULL
                            LIMIT 1
                            """,
                            (chat_id,),
                        ).fetchone()
                    else:
                        sample = conn.execute(
                            """
                            SELECT ac.archive_path
                            FROM message m
                            JOIN message_media mm ON mm.message_row_id = m._id
                            JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
                            WHERE m.chat_row_id = CAST(? AS INTEGER) AND ac.archive_path IS NOT NULL
                            LIMIT 1
                            """,
                            (chat_id,),
                        ).fetchone()
                    if sample and sample["archive_path"]:
                        parts = sample["archive_path"].replace("\\", "/").split("/")
                        if len(parts) >= 2:
                            prefix = f"{parts[0]}/{parts[1]}/"

            if prefix is None:
                return jsonify({"bytes": 0})

            safe_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            paths = [
                r["archive_path"]
                for r in archive_conn.execute(
                    "SELECT archive_path FROM archive_copies WHERE archive_path LIKE ? ESCAPE '\\'",
                    (safe_prefix + "%",),
                ).fetchall()
            ]
        finally:
            archive_conn.close()

        total_bytes = 0
        for p in paths:
            full = output_root / p
            try:
                total_bytes += full.stat().st_size
            except OSError:
                pass
        return jsonify({"bytes": total_bytes})

    # ---- API: index status -------------------------------------------------

    @app.route("/api/index-status")
    def api_index_status():
        cache = get_cache()
        indexed = cache.execute("SELECT COUNT(*) FROM message_index").fetchone()[0]
        return jsonify({"indexed": indexed})

    @app.route("/api/chat-index-status")
    def api_chat_index_status():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        key = (chat_id, chat_type)
        with _indexing_lock:
            if (
                get_cache()
                .execute("SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?", key)
                .fetchone()
            ):
                return jsonify({"status": "done"})
            state = _indexing_state.get(key, "idle")
            if state == "done":
                _indexing_state.pop(key, None)
                state = "idle"
        return jsonify({"status": state})

    @app.route("/api/index/source-size")
    def api_index_source_size():
        row = (
            get_cache().execute("SELECT value FROM sync_meta WHERE key = 'source_size'").fetchone()
        )
        size = int(row["value"]) if row and row["value"] else 0
        return jsonify({"bytes": size})

    @app.route("/api/index/progress")
    def api_index_progress():
        with _indexing_lock:
            state = dict(_bulk_index_state)
        return jsonify(state)

    def _bg_index_all():
        try:
            wa_conn_bulk = sqlite3.connect(wa_db_path)
            wa_conn_bulk.row_factory = sqlite3.Row
            if source_type == "android":
                _ensure_jid_map_resolved(wa_conn_bulk, None)
                bulk_jid_cols = {
                    r["name"] if isinstance(r, sqlite3.Row) else r[1]
                    for r in wa_conn_bulk.execute("PRAGMA table_info(jid)").fetchall()
                }
                has_b_server = "server" in bulk_jid_cols
                has_b_raw = "raw_string" in bulk_jid_cols
                bulk_g_parts = ["c.subject IS NOT NULL"]
                if has_b_server:
                    bulk_g_parts.append("(j.server IS NOT NULL AND j.server = 'g.us')")
                if has_b_raw:
                    bulk_g_parts.append("(j.raw_string IS NOT NULL AND j.raw_string LIKE '%@g.us')")
                bulk_is_group = " OR ".join(bulk_g_parts)

                chat_rows = wa_conn_bulk.execute(f"""
                    SELECT
                        CASE WHEN ({bulk_is_group}) THEN CAST(c._id AS TEXT)
                             ELSE COALESCE(
                                 {_clean_user_sql("j_real.user")},
                                 {_clean_user_sql("j.user")},
                                 CAST(c._id AS TEXT)
                             )
                        END AS id,
                        CASE WHEN ({bulk_is_group}) THEN 'group' ELSE 'contact' END AS type
                    FROM chat c
                    LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id
                    LEFT JOIN jid j_real ON j_real._id = jm_c.jid_row_id
                    LEFT JOIN jid j ON j._id = c.jid_row_id
                    WHERE (
                        ({bulk_is_group} AND (c.hidden = 0 OR c.hidden IS NULL OR EXISTS (SELECT 1 FROM message m WHERE m.chat_row_id = c._id AND m.message_type NOT IN (7, 90))))
                        OR
                        (NOT ({bulk_is_group}) AND (
                            EXISTS (
                                SELECT 1 FROM message m
                                WHERE m.chat_row_id = c._id
                                  AND m.message_type NOT IN (25, 67, 74, 79, 90)
                                  AND (
                                      (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
                                      OR (m.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82))
                                  )
                            )
                        ))
                    )
                """).fetchall()
                total = wa_conn_bulk.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            else:
                chat_rows = wa_conn_bulk.execute("""
                    SELECT CAST(cs.Z_PK AS TEXT) AS id,
                           CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END AS type
                    FROM ZWACHATSESSION cs
                    WHERE cs.ZHIDDEN = 0
                      AND (
                          cs.ZGROUPINFO IS NOT NULL
                          OR EXISTS (
                              SELECT 1 FROM ZWAMESSAGE msg_real
                              WHERE msg_real.ZCHATSESSION = cs.Z_PK
                                AND (msg_real.ZMESSAGETYPE NOT IN (6, 10) OR msg_real.ZMESSAGETYPE IS NULL)
                          )
                      )
                """).fetchall()
                total = wa_conn_bulk.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
            wa_conn_bulk.close()

            with _indexing_lock:
                _bulk_index_state["total_msgs"] = total
                _bulk_index_state["indexed_msgs"] = 0

            cache_bulk = sqlite3.connect(str(get_cache_db_path(output_root)))
            cache_bulk.execute("PRAGMA journal_mode = WAL")
            cache_bulk.execute("PRAGMA synchronous = NORMAL")
            cache_bulk.row_factory = sqlite3.Row

            def _on_progress(n):
                with _indexing_lock:
                    _bulk_index_state["indexed_msgs"] += n

            for row in chat_rows:
                already = cache_bulk.execute(
                    "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?",
                    (row["id"], row["type"]),
                ).fetchone()
                if not already:
                    _build_fts_chat(
                        cache_bulk,
                        source_type,
                        wa_db_path,
                        row["id"],
                        row["type"],
                        on_progress=_on_progress,
                    )

            cache_bulk.execute(
                "INSERT INTO message_index_fts(message_index_fts) VALUES('optimize')"
            )
            cache_bulk.execute("PRAGMA optimize")
            cache_bulk.execute("PRAGMA wal_checkpoint(PASSIVE)")
            cache_bulk.commit()
            cache_bulk.close()
        except Exception:
            print(f"[wab_viewer] Bulk index error:\n{traceback.format_exc()}")
        finally:
            with _indexing_lock:
                _bulk_index_state["running"] = False
                _bulk_index_state["indexed_msgs"] = _bulk_index_state["total_msgs"]

    @app.route("/api/index/all", methods=["POST"])
    def api_index_all():
        if source_type is None:
            return jsonify({"error": "no source database"}), 400
        with _indexing_lock:
            if _bulk_index_state["running"]:
                return jsonify({"error": "already running"})
            _bulk_index_state["running"] = True
            _bulk_index_state["indexed_msgs"] = 0
            _bulk_index_state["total_msgs"] = 0
        threading.Thread(target=_bg_index_all, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/api/index/clear", methods=["POST"])
    def api_index_clear():
        with _indexing_lock:
            if _bulk_index_state["running"]:
                return jsonify({"error": "indexing in progress"}), 409
        _clear_fts_index(get_cache())
        get_cache().execute("VACUUM")
        return jsonify({"ok": True})

    @app.route("/api/index/optimize", methods=["POST"])
    def api_index_optimize():
        with _indexing_lock:
            if _bulk_index_state["running"]:
                return jsonify({"error": "indexing in progress"}), 409
        cache_path = get_cache_db_path(output_root)
        wal_path = cache_path.with_name(cache_path.name + "-wal")

        def _calc_size():
            sz = cache_path.stat().st_size if cache_path.exists() else 0
            if wal_path.exists():
                sz += wal_path.stat().st_size
            return sz

        size_before = _calc_size()
        c = get_cache()
        c.execute("INSERT INTO message_index_fts(message_index_fts) VALUES('optimize')")
        c.commit()
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("PRAGMA optimize")
        size_after = _calc_size()
        return jsonify(
            {
                "ok": True,
                "size_before": size_before,
                "size_after": size_after,
                "reclaimed": max(0, size_before - size_after),
            }
        )

    # ---- API: preferences --------------------------------------------------

    @app.route("/api/preferences", methods=["GET"])
    def api_preferences_get():
        rows = get_cache().execute("SELECT key, value FROM user_preferences").fetchall()
        return jsonify({r["key"]: r["value"] for r in rows})

    @app.route("/api/preferences", methods=["POST"])
    def api_preferences_post():
        data = request.get_json(force=True)
        key = data.get("key", "")
        value = data.get("value", "")
        if key not in VALID_PREF_VALUES:
            return jsonify({"error": "unknown preference key"}), 400
        if value not in VALID_PREF_VALUES[key]:
            return jsonify({"error": f"invalid value for {key}"}), 400
        get_cache().execute(
            "INSERT OR REPLACE INTO user_preferences (key, value) VALUES (?, ?)",
            (key, value),
        )
        get_cache().commit()
        return jsonify({"ok": True})

    # ---- API: media file serving -------------------------------------------

    @app.route("/media/<path:p>")
    def serve_media(p):
        try:
            safe = (output_root / p).resolve()
            root = output_root.resolve()
            safe.relative_to(root)
        except (OSError, ValueError):
            return "Forbidden", 403
        if not safe.exists():
            return "Not found", 404

        range_hdr = request.headers.get("Range")
        file_size = safe.stat().st_size
        content_type, _ = mimetypes.guess_type(str(safe))

        if range_hdr:
            try:
                units, range_spec = range_hdr.split("=", 1)
                if units.strip() != "bytes":
                    return "416 Range Not Satisfiable", 416
                start_b, end_b = range_spec.strip().split("-")
                start = int(start_b) if start_b else 0
                end = int(end_b) if end_b else file_size - 1
            except (ValueError, AttributeError):
                return "416 Range Not Satisfiable", 416

            length = end - start + 1
            with open(safe, "rb") as f:
                f.seek(start)
                data = f.read(length)
            resp = Response(data, 206)
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Content-Length"] = length
            resp.headers["Content-Type"] = content_type or "application/octet-stream"
            return resp
        else:
            with open(safe, "rb") as f:
                data = f.read()
            resp = Response(data, 200)
            resp.headers["Content-Length"] = file_size
            resp.headers["Content-Type"] = content_type or "application/octet-stream"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

    # ---- API: thumbnail serving --------------------------------------------

    @app.route("/api/thumbnail/<int:msg_id>")
    def api_thumbnail(msg_id):
        conn = get_wa()
        if conn is None:
            return "Not found", 404

        thumb_bytes = None
        if source_type == "android":
            if has_message_thumbnail:
                row = conn.execute(
                    "SELECT thumbnail FROM message_thumbnail WHERE message_row_id = ?",
                    (msg_id,),
                ).fetchone()
                if row and row["thumbnail"]:
                    thumb_bytes = row["thumbnail"]

            if not thumb_bytes and has_media_hash_thumbnail and has_mm_file_hash:
                row = conn.execute(
                    """
                    SELECT mht.thumbnail
                    FROM media_hash_thumbnail mht
                    JOIN message_media mm ON mm.file_hash = mht.media_hash
                    WHERE mm.message_row_id = ?
                    """,
                    (msg_id,),
                ).fetchone()
                if row and row["thumbnail"]:
                    thumb_bytes = row["thumbnail"]

            if not thumb_bytes and has_message_quoted_media:
                row = conn.execute(
                    "SELECT thumbnail FROM message_quoted_media WHERE message_row_id = ?",
                    (msg_id,),
                ).fetchone()
                if row and row["thumbnail"]:
                    thumb_bytes = row["thumbnail"]

            if (
                not thumb_bytes
                and has_message_quoted_media
                and has_mq_key_id
                and has_android_key_id
            ):
                row = conn.execute(
                    """
                    SELECT mqm.thumbnail
                    FROM message m
                    JOIN message_quoted mq ON mq.key_id = m.key_id
                    JOIN message_quoted_media mqm ON mqm.message_row_id = mq.message_row_id
                    WHERE m._id = ? AND mqm.thumbnail IS NOT NULL
                    LIMIT 1
                    """,
                    (msg_id,),
                ).fetchone()
                if row and row["thumbnail"]:
                    thumb_bytes = row["thumbnail"]
        elif source_type == "ios":
            thumb_cols = []
            if "ZXMPPTHUMBPATH" in _mi_cols:
                thumb_cols.append("mi.ZXMPPTHUMBPATH")
            if "ZTHUMBNAILLOCALPATH" in _mi_cols:
                thumb_cols.append("mi.ZTHUMBNAILLOCALPATH")
            if thumb_cols:
                row = conn.execute(
                    f"""
                    SELECT {", ".join(thumb_cols)}
                    FROM ZWAMESSAGE m
                    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                    WHERE m.Z_PK = ?
                    """,
                    (msg_id,),
                ).fetchone()
                if row:
                    thumb_path = (
                        row["ZXMPPTHUMBPATH"]
                        if "ZXMPPTHUMBPATH" in _mi_cols and row["ZXMPPTHUMBPATH"]
                        else (
                            row["ZTHUMBNAILLOCALPATH"]
                            if "ZTHUMBNAILLOCALPATH" in _mi_cols and row["ZTHUMBNAILLOCALPATH"]
                            else None
                        )
                    )
                    if thumb_path:
                        cand_thumb = (
                            output_root
                            / "Whatsapp Databases"
                            / ".thumbnails"
                            / Path(thumb_path).name
                        ).resolve()
                        if cand_thumb.exists():
                            try:
                                thumb_bytes = cand_thumb.read_bytes()
                            except OSError:
                                pass
                        if not thumb_bytes:
                            try:
                                ac_row = conn.execute(
                                    "SELECT archive_path FROM arch.archive_copies WHERE original_path IN (?, ?)",
                                    (thumb_path, "Message/" + thumb_path),
                                ).fetchone()
                                if ac_row and ac_row["archive_path"]:
                                    cand_ac = (output_root / ac_row["archive_path"]).resolve()
                                    if cand_ac.exists():
                                        try:
                                            thumb_bytes = cand_ac.read_bytes()
                                        except OSError:
                                            pass
                            except sqlite3.OperationalError:
                                pass
                        if not thumb_bytes:
                            cand = (output_root / thumb_path).resolve()
                            if cand.exists():
                                try:
                                    thumb_bytes = cand.read_bytes()
                                except OSError:
                                    pass

            if not thumb_bytes and "ZMETADATA" in _mi_cols:
                meta_row = conn.execute(
                    """
                    SELECT mi.ZMETADATA
                    FROM ZWAMESSAGE m
                    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                    WHERE m.Z_PK = ?
                    """,
                    (msg_id,),
                ).fetchone()
                if meta_row and meta_row["ZMETADATA"]:
                    blob = meta_row["ZMETADATA"]
                    if isinstance(blob, bytes):
                        jpeg_idx = blob.find(b"\xff\xd8\xff")
                        if jpeg_idx != -1:
                            eoi_idx = blob.find(b"\xff\xd9", jpeg_idx)
                            if eoi_idx != -1:
                                thumb_bytes = blob[jpeg_idx : eoi_idx + 2]
                if not thumb_bytes:
                    try:
                        reply_row = conn.execute(
                            """
                            SELECT reply_mi.ZMETADATA
                            FROM ZWAMESSAGE m
                            JOIN ZWAMESSAGE reply_m ON INSTR(reply_mi.ZMETADATA, m.ZSTANZAID) > 0
                            JOIN ZWAMEDIAITEM reply_mi ON reply_mi.Z_PK = reply_m.ZMEDIAITEM
                            WHERE m.Z_PK = ? AND INSTR(reply_mi.ZMETADATA, X'ffd8ff') > 0
                            LIMIT 1
                            """,
                            (msg_id,),
                        ).fetchone()
                        if reply_row and reply_row[0]:
                            rblob = reply_row[0]
                            if isinstance(rblob, bytes):
                                j_idx = rblob.find(b"\xff\xd8\xff")
                                if j_idx != -1:
                                    e_idx = rblob.find(b"\xff\xd9", j_idx)
                                    if e_idx != -1:
                                        thumb_bytes = rblob[j_idx : e_idx + 2]
                    except sqlite3.OperationalError:
                        pass

        if not thumb_bytes or len(thumb_bytes) < 16:
            return "Thumbnail not found", 404

        mime = "image/jpeg"
        if thumb_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif thumb_bytes.startswith(b"RIFF") and b"WEBP" in thumb_bytes[:16]:
            mime = "image/webp"

        resp = Response(thumb_bytes, 200, mimetype=mime)
        resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
        resp.headers["Content-Length"] = len(thumb_bytes)
        return resp

    # ---- static assets ------------------------------------------------------

    @app.route("/static/app.css")
    def serve_app_css():
        resp = make_response(send_from_directory(_CHAT_VIEWER_DIR, "app.css"))
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.route("/static/app.js")
    def serve_app_js():
        resp = make_response(send_from_directory(_CHAT_VIEWER_DIR, "app.js"))
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.route("/static/favicon.svg")
    @app.route("/favicon.ico")
    def serve_favicon():
        return send_from_directory(_CHAT_VIEWER_DIR, "favicon.svg", mimetype="image/svg+xml")

    # ---- UI ----------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template_string(
            HTML_TEMPLATE,
            output_root=str(output_root),
            version=_get_version(),
            schema_alert=app.config.get("SCHEMA_ALERT"),
        )

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_output_root(output_root: Path) -> None:
    hint = (
        "Run 'wab-archiver' first to create an archive, or pass a different path as the argument."
    )

    if not output_root.exists():
        print(f"Error: folder does not exist: {output_root}", file=sys.stderr)
        print(f"Hint:  {hint}", file=sys.stderr)
        sys.exit(1)

    if not output_root.is_dir():
        print(f"Error: path is not a directory: {output_root}", file=sys.stderr)
        print(f"Hint:  {hint}", file=sys.stderr)
        sys.exit(1)

    archive_db = get_archive_db_path(output_root)
    if not archive_db.exists():
        print(f"Error: archive database not found: {archive_db}", file=sys.stderr)
        print(f"Hint:  {hint}", file=sys.stderr)
        sys.exit(1)

    source_type, wa_db_path = _detect_source_db(output_root)
    if not wa_db_path:
        print(f"Error: WhatsApp database not found in {output_root}", file=sys.stderr)
        print(
            "Hint:  Ensure 'msgstore.db' (Android) or 'ChatStorage.sqlite' (iOS) is present "
            "in the archive (e.g. in 'Whatsapp Databases' subfolder or root).",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    args = parse_args()
    raw_path = args.output_root
    output_root = Path(raw_path).expanduser().resolve()

    validate_output_root(output_root)

    log_level = logging.INFO if args.verbose else logging.ERROR
    logging.getLogger("werkzeug").setLevel(log_level)

    print(f"[wab_viewer] Opening archive: {output_root}")

    app = create_app(output_root, rescan=args.rescan)

    url = f"http://{args.host}:{args.port}"
    print(f"[wab_viewer] Starting server at {url}")
    print("[wab_viewer] Press Ctrl+C to stop")

    if not args.no_browser:
        browser_url = resolve_browser_url(args.host, args.port)
        webbrowser.open(browser_url)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
