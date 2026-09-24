#!/usr/bin/env python3
"""
wab_viewer — Browse archived WhatsApp chats via a local Flask web UI.

Usage:
    python -m wab_viewer <output_root> [--port PORT] [--host HOST] [--rescan]

Dependencies:
    pip install flask
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

try:
    from flask import Flask, Response, g, jsonify, render_template_string, request, send_from_directory
except ImportError:
    sys.exit("Flask is not installed. Run: pip install flask")

def _get_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("wabtools")
    except Exception:
        import tomllib
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]

from wab_viewer.chat_viewer.template import HTML_TEMPLATE

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

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Browse archived WhatsApp chats")
    p.add_argument("output_root", nargs="?", default=None,
                   help="Path to the archive output directory")
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    p.add_argument("--rescan", action="store_true", help="Force rebuild of the FTS index")
    p.add_argument('--version', action='version',
                   version=f'WhatsApp Backup Tools — Viewer v{_get_version()}')
    args = p.parse_args()
    if not args.output_root:
        p.error("output_root is required")
    return args


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_cache_db_path(output_root: Path) -> Path:
    return output_root / ".wa_viewer.db"


def get_archive_db_path(output_root: Path) -> Path:
    return output_root / ".wa_media_archiver.db"


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

CREATE VIRTUAL TABLE IF NOT EXISTS message_index_fts USING fts5(
    text_body,
    content='',
    contentless_delete=1
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
    "theme":       {"dark", "light"},
    "date_format": {"DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"},
    "font_size":   {"small", "medium", "large"},
}

GALLERY_PAGE_SIZE = 100

_MEDIA_TYPE_EXPR = """
    CASE
        WHEN {col} IS NULL THEN 'text'
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
    conn = sqlite3.connect(str(wa_db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        if "idx_message_chat_ts" not in existing:
            has_android = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
            ).fetchone()
            if has_android:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_message_chat_ts "
                    "ON message(chat_row_id, timestamp)")
        if "idx_zwamessage_chat_ts" not in existing:
            has_ios = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
            ).fetchone()
            if has_ios:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_zwamessage_chat_ts "
                    "ON ZWAMESSAGE(ZCHATSESSION, ZMESSAGEDATE)")
        conn.commit()
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

    if ev == 12:  # Group created
        subj = txt
        creator = None
        if txt.startswith("{"):
            try:
                data = json.loads(txt)
                subj = data.get("subject", "")
                author_jid = data.get("author")
                if author_jid:
                    creator = _resolve_ios_jid(author_jid, contacts_map)
            except (json.JSONDecodeError, AttributeError):
                pass
        if not creator:
            if from_me == 1:
                creator = "You"
            elif member_jid:
                creator = _resolve_ios_jid(member_jid, contacts_map)
            elif sender and sender != "Someone":
                creator = sender
            else:
                creator = "Someone"
        if subj:
            return f'{creator} created group "{subj}"'
        return f'{creator} created this group'

    if ev == 1:  # Subject changed
        actor = "You" if from_me == 1 else (_resolve_ios_jid(member_jid, contacts_map) if member_jid else (sender or "Someone"))
        if txt:
            return f'{actor} changed the subject to "{txt}"'
        return f'{actor} changed the group subject'

    if ev == 3:  # Group icon changed
        actor = "You" if from_me == 1 else (_resolve_ios_jid(member_jid, contacts_map) if member_jid else (sender or "Someone"))
        return f"{actor} changed this group's icon"

    if ev == 4:  # Participant left
        target = _resolve_ios_jid(member_jid, contacts_map) if member_jid else ("You" if from_me == 1 else "A participant")
        if target in ("You", "you"):
            return "You left"
        return f"{target} left"

    if ev == 15:  # Joined via invite link
        target = _resolve_ios_jid(member_jid, contacts_map) if member_jid else ("You" if from_me == 1 else "A participant")
        if target in ("You", "you"):
            return "You joined using this group's invite link"
        return f"{target} joined using this group's invite link"

    if ev == 2:  # Participant added or joined
        if member_jid:
            actor = "You" if from_me == 1 else (_resolve_ios_jid(txt, contacts_map) if txt else None)
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

        if target in ("You", "you"):
            if actor and actor != "Someone":
                return f"{actor} added you"
            return "You joined"
        if actor == "You":
            return f"You added {target}"
        if actor and actor != "Someone" and actor != target:
            return f"{actor} added {target}"
        return f"{target} joined"

    if ev == 50:  # Multiple participants added
        if ";" in txt:
            actor_part, targets_part = txt.split(";", 1)
            actor = "You" if from_me == 1 else _resolve_ios_jid(actor_part.strip(), contacts_map)
            target_jids = [j.strip() for j in targets_part.split(",") if j.strip()]
        else:
            actor = "You" if from_me == 1 else None
            target_jids = [j.strip() for j in txt.split(",") if j.strip()]
        names = [_resolve_ios_jid(j, contacts_map) for j in target_jids]
        target_str = ", ".join("you" if n in ("You", "you") else n for n in names) if names else "participants"
        if actor and actor != "Someone":
            return f"{actor} added {target_str}"
        return f"{target_str} joined"

    if ev == 7:  # Participant removed or left
        if member_jid:
            actor = "You" if from_me == 1 else (_resolve_ios_jid(txt, contacts_map) if txt else None)
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

        if target in ("You", "you"):
            if actor and actor != "Someone":
                return f"{actor} removed you"
            return "You left"
        if actor == "You":
            return f"You removed {target}"
        if actor and actor != "Someone" and actor != target:
            return f"{actor} removed {target}"
        return f"{target} was removed"

    if ev in (5, 9):  # Admin changed
        target = _resolve_ios_jid(member_jid, contacts_map) if member_jid else (
            _resolve_ios_jid(txt, contacts_map) if txt else "You"
        )
        if target in ("You", "you"):
            return "You're now an admin" if ev == 9 else "You're no longer an admin"
        return f"{target} is now an admin" if ev == 9 else f"{target} is no longer an admin"

    if ev == 26:  # Disappearing messages
        secs = int(txt) if txt.isdigit() else None
        if secs and secs > 0:
            days = secs // 86400
            if days >= 1:
                return f"Disappearing messages set to {days} days"
            hours = secs // 3600
            return f"Disappearing messages set to {hours} hours"
        return "Disappearing messages turned off"

    if ev in (36, 37):
        return "Group settings changed"

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

    actor = sender if sender else ("You" if from_me == 1 else "Someone")
    target = part_name or (sender if act in (4, 5, 13, 79) else None) or ("You" if from_me == 1 and act in (4, 5, 13, 79) else "Someone")

    if act == 11:  # Group created
        if txt:
            return f'{actor} created group "{txt}"'
        return f'{actor} created this group'

    if act == 1:  # Subject changed
        if txt:
            return f'{actor} changed the subject to "{txt}"'
        return f'{actor} changed the group subject'

    if act in (12, 4):  # Participant added / joined
        if target in ("You", "you") and (not actor or actor == "Someone" or actor == "You"):
            return "You joined"
        if actor and actor != "Someone" and actor != target:
            return f"{actor} added {target}"
        return f"{target} joined"

    if act == 79:  # Joined via invite link
        if target in ("You", "you"):
            return "You joined using this group's invite link"
        return f"{target} joined using this group's invite link"

    if act in (13, 5):  # Participant left
        if target in ("You", "you"):
            return "You left"
        return f"{target} left"

    if act == 14:  # Participant removed
        if target in ("You", "you"):
            if actor and actor != "Someone" and actor != "You":
                return f"{actor} removed you"
            return "You were removed"
        if actor and actor != "Someone" and actor != target:
            return f"{actor} removed {target}"
        return f"{target} was removed"

    if act == 6:  # Photo / icon changed
        return f"{actor} changed this group's icon"

    if act == 27:  # Description changed
        return f"{actor} changed the group description"

    if act == 15:  # Admin promoted
        if target in ("You", "you"):
            return "You're now an admin"
        return f"{target} is now an admin"

    if act == 20:  # Admin demoted
        if target in ("You", "you"):
            return "You're no longer an admin"
        return f"{target} is no longer an admin"

    if act == 58:  # Announcement mode
        if txt.lower() == "true":
            return "Only admins can send messages in this group"
        return "All participants can send messages in this group"

    if txt and not txt.startswith("{") and "@lid" not in txt and txt.lower() not in ("true", "false") and not txt.isdigit():
        return txt
    return "Group event"


def _hydrate_android_service_participants(
    conn: sqlite3.Connection,
    msg_ids: list,
    has_mcp: bool,
    has_lid_dn: bool,
    has_jid_server: bool,
    has_jid_raw_string: bool,
) -> dict:
    """Hydrate participant names for Android service messages on demand."""
    if not has_mcp or not msg_ids:
        return {}
    ph = ",".join("?" * len(msg_ids))
    ldn_col = "ldn.display_name AS lid_name," if has_lid_dn else "NULL AS lid_name,"
    ldn_join = "LEFT JOIN lid_display_name ldn ON ldn.lid_row_id = mcp.user_jid_row_id" if has_lid_dn else ""
    server_col = "j_part_raw.server AS raw_server," if has_jid_server else "NULL AS raw_server,"
    raw_str_col = "j_part_raw.raw_string AS raw_string," if has_jid_raw_string else "NULL AS raw_string,"
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

    p_map: dict = {}
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

    return {mid: ", ".join(names) for mid, names in p_map.items()}


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
                        contacts_map[f"{ar['number']}@s.whatsapp.net"] = (ar["display_name"], ar["number"])
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
        for r in service_rows:
            if not r.get("participant_name"):
                r["participant_name"] = p_map.get(r["msg_id"], "")
            r["text_body"] = _format_android_service_row(r)


# ---------------------------------------------------------------------------
# iOS reactions — ZRECEIPTINFO protobuf extraction
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple:
    """Read a base-128 varint from data at pos. Returns (value, new_pos).

    Each byte contributes 7 bits; the high bit (0x80) signals continuation.
    """
    val, shift = 0, 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, pos


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
                val = int.from_bytes(data[pos:pos + 8], "little")
                pos += 8
                results.append((f, "fixed64", val))
            elif w == 2:  # length-delimited
                length, pos = _read_varint(data, pos)
                val = data[pos:pos + length]
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
            emojis.append(data[i:i + 3].hex())
            i += 3
        elif b == 0xF0 and i + 3 < len(data):
            # 4-byte emoji (e.g. 😂 = f0 9f 98 82)
            emojis.append(data[i:i + 4].hex())
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


def _ios_reactions(wa_conn: sqlite3.Connection,
                   chat_id: str,
                   msg_ids: list) -> dict:
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
    my_row = wa_conn.execute(f"""
        SELECT COALESCE(gm.ZMEMBERJID, m.ZFROMJID) AS me_jid
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        WHERE m.ZCHATSESSION = CAST(? AS INTEGER)
          AND m.ZISFROMME = 1
        LIMIT 1
    """, (chat_id,)).fetchone()

    my_phone = None
    if my_row and my_row["me_jid"]:
        jid = my_row["me_jid"]
        at = jid.find("@")
        if at > 0:
            my_phone = jid[:at]

    rows = wa_conn.execute(f"""
        SELECT m.Z_PK AS msg_id,
               mi.ZRECEIPTINFO AS receipt_info
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAMESSAGEINFO mi ON mi.Z_PK = m.ZMESSAGEINFO
        WHERE m.Z_PK IN ({placeholders})
          AND mi.ZRECEIPTINFO IS NOT NULL
    """, msg_ids).fetchall()

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


def _resolve_and_cache_reactions(source_type: str,
                                 wa_conn: sqlite3.Connection,
                                 archive_conn: sqlite3.Connection,
                                 rows: list) -> None:
    if source_type != "android" or not rows:
        return

    msg_ids = [r["msg_id"] for r in rows if r.get("msg_id") is not None]
    if not msg_ids:
        return

    placeholders = ",".join("?" * len(msg_ids))
    reaction_rows = wa_conn.execute(f"""
        SELECT ao.parent_message_row_id AS msg_id,
               GROUP_CONCAT(r.reaction) AS reactions
        FROM message_add_on ao
        JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
        WHERE ao.parent_message_row_id IN ({placeholders})
        GROUP BY ao.parent_message_row_id
    """, msg_ids).fetchall()

    if not reaction_rows:
        return

    reaction_map = {row["msg_id"]: row["reactions"] for row in reaction_rows}

    for row in rows:
        row["reactions"] = reaction_map.get(row["msg_id"])

    cache_sql = """
        INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions)
        VALUES (?, ?, ?)
    """
    archive_conn.executemany(cache_sql, [
        (row["chat_id"], row["msg_id"], row["reactions"])
        for row in rows
        if row["msg_id"] in reaction_map
    ])
    archive_conn.commit()


def _backfill_recent_messages(archive_conn: sqlite3.Connection, rows: list) -> None:
    if not rows:
        return
    sql = """
        INSERT OR REPLACE INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me,
             archive_path, media_type, media_name, text_body,
             quoted_text, quoted_sender, quoted_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    keys = rows[0].keys()
    archive_conn.executemany(sql, [
        (r["chat_id"], r["chat_type"], r["msg_id"], r["timestamp_ms"],
         r["sender"], r["from_me"], r["archive_path"] if "archive_path" in keys else None,
         r["media_type"], r["media_name"] if "media_name" in keys else None,
         r["text_body"], r["quoted_text"] if "quoted_text" in keys else None,
         r["quoted_sender"] if "quoted_sender" in keys else None,
         r["quoted_ts"] if "quoted_ts" in keys else None)
        for r in rows
    ])
    archive_conn.commit()


def _media_type_from_path(path: str) -> str:
    if path is None:
        return "text"
    p = path.lower()
    if "sticker" in p or "stickers" in p:
        return "sticker"
    ext = os.path.splitext(p)[1]
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
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
    conn = sqlite3.connect(str(cache_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -8000")
    conn.executescript(CACHE_SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('theme', 'dark')")
    conn.execute("INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('date_format', 'DD/MM/YYYY')")
    conn.execute("INSERT OR IGNORE INTO user_preferences (key, value) VALUES ('font_size', 'medium')")
    conn.commit()
    return conn


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
    return (
        rows.get("source_mtime") != current_mtime
        or rows.get("source_size") != current_size
    )


def _save_source_stamp(cache_conn: sqlite3.Connection, source_path: str):
    st = os.stat(source_path)
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('source_mtime', ?)",
        (str(st.st_mtime),)
    )
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('source_size', ?)",
        (str(st.st_size),)
    )
    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_scanned_at', ?)",
        (str(int(time.time())),)
    )
    cache_conn.commit()


def _stream_fts_rows(cursor, cache_conn: sqlite3.Connection, chunk_size: int = 2000,
                     on_progress=None):
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
        if msg_type in (6, 7) and ("@lid" in text_body or text_body.startswith("{") or text_body.isdigit() or text_body in ("true", "false")):
            continue
        is_media = row["message_type"] is not None and row["message_type"] != 0 and row["media_file"] is not None
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
    cache_conn.execute("DELETE FROM message_index_fts")
    cache_conn.execute("DELETE FROM indexed_chats")
    cache_conn.commit()


def _fts_android_chat(wa_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                      chat_id: str, chat_type: str, on_progress=None):
    if chat_type == "group":
        where = "WHERE CAST(m.chat_row_id AS TEXT) = ?"
        params = (chat_id,)
    else:
        where = "WHERE c.subject IS NULL AND COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT)) = ?"
        params = (chat_id,)
    cursor = wa_conn.execute(f"""
        SELECT
            m._id                                                    AS rowid,
            CASE
                WHEN c.subject IS NOT NULL THEN CAST(m.chat_row_id AS TEXT)
                ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
            END                                                      AS chat_id,
            CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
            COALESCE(m.timestamp, 0)                                AS timestamp_ms,
            COALESCE(m.text_data, '')                                AS text_body,
            m.message_type,
            mm.file_path                                            AS media_file
        FROM message m
        LEFT JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
        LEFT JOIN message_media mm ON mm.message_row_id = m._id
        {where}
        ORDER BY m.timestamp ASC
    """, params)
    _stream_fts_rows(cursor, cache_conn, on_progress=on_progress)


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
        candidates.extend([
            output_root / "Whatsapp Databases" / rel_path,
            output_root / rel_path,
            output_root / "ExtChatDB" / rel_path,
        ])
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


def _fts_ios_chat(wa_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                  chat_id: str, on_progress, hd_clause: str):
    sql = f"""
        SELECT
            m.Z_PK                                                      AS rowid,
            CAST(m.ZCHATSESSION AS TEXT)                               AS chat_id,
            CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                 ELSE 'contact' END                                     AS chat_type,
            CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)       AS timestamp_ms,
            COALESCE(m.ZTEXT, '')                                       AS text_body,
            m.ZMESSAGETYPE                                              AS message_type,
            mi.ZMEDIALOCALPATH                                          AS media_file
        FROM ZWAMESSAGE m
        LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
        WHERE CAST(m.ZCHATSESSION AS TEXT) = ?
          {hd_clause}
        ORDER BY m.ZMESSAGEDATE ASC
    """
    cursor = wa_conn.execute(sql, (chat_id,))
    _stream_fts_rows(cursor, cache_conn, on_progress=on_progress)


def _build_fts_chat(cache_conn: sqlite3.Connection, source_type: str, wa_db_path: str,
                    chat_id: str, chat_type: str, on_progress=None):
    wa_conn = sqlite3.connect(wa_db_path)
    wa_conn.row_factory = sqlite3.Row
    if source_type == "android":
        _fts_android_chat(wa_conn, cache_conn, chat_id, chat_type, on_progress=on_progress)
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
    cache_conn.execute(
        "INSERT OR IGNORE INTO indexed_chats (chat_id, chat_type) VALUES (?, ?)",
        (chat_id, chat_type),
    )
    cache_conn.commit()


def _ensure_chat_indexed(cache_conn: sqlite3.Connection, source_type: str, wa_db_path: str,
                         chat_id: str, chat_type: str):
    row = cache_conn.execute(
        "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?",
        (chat_id, chat_type),
    ).fetchone()
    if row is None:
        _build_fts_chat(cache_conn, source_type, wa_db_path, chat_id, chat_type)


def _maybe_start_indexing(chat_id: str, chat_type: str,
                          cache_conn, source_type: str, wa_db_path: str,
                          output_root: Path,
                          indexing_state: dict, indexing_lock):
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
        args=(str(get_cache_db_path(output_root)), source_type,
              wa_db_path, chat_id, chat_type,
              indexing_state, indexing_lock),
        daemon=True,
    ).start()


def _build_media_only(archive_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                      output_root: Path):
    archive_map = {
        row["original_path"]: row["archive_path"]
        for row in archive_conn.execute(
            "SELECT original_path, archive_path FROM archive_copies"
        ).fetchall()
    }

    group_folders = {
        r["folder"]: ("group", str(r["chat_row_id"]))
        for r in archive_conn.execute(
            "SELECT folder, chat_row_id FROM groups"
        ).fetchall()
    }
    folder_map = {**{r["folder"]: ("contact", str(r["number"]))
                     for r in archive_conn.execute(
                         "SELECT folder, number FROM contacts"
                     ).fetchall()},
                  **group_folders}

    cache_conn.execute("DELETE FROM message_index")
    cache_conn.execute("DELETE FROM message_index_fts")
    cache_conn.commit()

    insert_sql = """
        INSERT INTO message_index (chat_id, chat_type, timestamp_ms)
        VALUES (?, ?, ?)
    """
    batch = []
    for original_path, archive_path in archive_map.items():
        if not archive_path:
            continue
        parts = archive_path.split("/")
        if len(parts) < 2:
            continue
        folder = parts[1]
        chat_key = folder_map.get(folder) or folder_map.get(parts[0])
        if chat_key is None:
            chat_key = ("contact", parts[1] if len(parts) > 1 else folder)
        chat_type, chat_id = chat_key

        full_path = output_root / archive_path
        if not full_path.exists():
            continue
        mtime_ms = int(os.path.getmtime(full_path) * 1000)
        batch.append((chat_id, chat_type, mtime_ms))

    cache_conn.executemany(insert_sql, batch)
    cache_conn.commit()


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_ANDROID_JID_MAP = """
    LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
    LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
"""

_ANDROID_CHAT_ID = """
    CASE
        WHEN c.subject IS NOT NULL THEN CAST(m.chat_row_id AS TEXT)
        ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
    END
"""

_ANDROID_CHAT_TYPE = "CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END"

_ANDROID_SELECT = None

_ANDROID_FILTER = """
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND m.message_type != 7 AND mm.file_path IS NOT NULL)
        OR (m.message_type = 7)
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
            WHEN m.ZMESSAGETYPE = 6 THEN 'service'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="('Message/' || COALESCE(mi.ZMEDIALOCALPATH,''))")}
        END                                                           AS media_type,
        COALESCE(mi.ZTITLE, '')                                      AS media_name,
        COALESCE(m.ZTEXT, '')                                        AS text_body,
        COALESCE(qm.ZTEXT, '')                                       AS quoted_text,
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
        NULL                                                          AS reactions,
        m.ZMESSAGETYPE                                               AS raw_msg_type,
        0                                                            AS group_event_type,
        gm.ZMEMBERJID                                                AS member_jid
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
          ON ac.original_path = 'Message/' || COALESCE(mi.ZMEDIALOCALPATH, '')
"""

_BASE_IOS_FILTER = """
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND m.ZMESSAGETYPE != 6 AND mi.ZMEDIALOCALPATH IS NOT NULL)
        OR (m.ZMESSAGETYPE = 6)
    )
"""

_BASE_IOS_FILTER_TS = _BASE_IOS_FILTER

_IOS_FILTER = _BASE_IOS_FILTER
_IOS_FILTER_TS = _BASE_IOS_FILTER_TS

_ANDROID_TS = "COALESCE(m.timestamp, 0)"
_IOS_TS = "CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)"

_ANDROID_FILTER_TS = _ANDROID_FILTER

_ANDROID_IS_MEDIA = "NOT (m.message_type IS NULL OR m.message_type = 0)"
_IOS_IS_MEDIA = "NOT (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"

_ANDROID_IS_LINK = (
    "(m.message_type IS NULL OR m.message_type = 0)"
    " AND m.text_data IS NOT NULL"
    " AND (INSTR(LOWER(m.text_data), 'http://') > 0"
    "      OR INSTR(LOWER(m.text_data), 'https://') > 0)"
)
_IOS_IS_LINK = (
    "(m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"
    " AND m.ZTEXT IS NOT NULL"
    " AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0"
    "      OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)"
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
    return "j_chat.user = ?", [chat_id]


def _ios_chat_filter(chat_id: str) -> tuple[str, list]:
    return "m.ZCHATSESSION = CAST(? AS INTEGER)", [chat_id]


def _bg_index_chat(cache_db_path: str, source_type: str, wa_db_path: str,
                   chat_id: str, chat_type: str,
                   state: dict, lock: threading.Lock):
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
                value = data[pos:pos + length]
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
            value = blob[pos:pos + length]
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
        return [{
            "name": "",
            "jid": "",
            "delivered_ts": (anchor + min(delivered_deltas)) * 1000 if delivered_deltas else None,
            "read_ts": (anchor + min(read_deltas)) * 1000 if read_deltas else None,
            "played_ts": None,
        }]

    members = []
    for lid, delivered_delta, read_delta in parsed:
        name = lid
        if lid:
            row = conn.execute(
                "SELECT full_name FROM _ios_contacts WHERE jid = ?", (f"{lid}@lid",)
            ).fetchone()
            if row and row[0]:
                name = row[0]
            else:
                row = conn.execute(
                    "SELECT display_name FROM arch.contacts WHERE number = ?", (lid,)
                ).fetchone()
                if row and row[0]:
                    name = row[0]
        members.append({
            "name": name or lid or "",
            "jid": lid or "",
            "delivered_ts": (anchor + delivered_delta) * 1000 if delivered_delta is not None else None,
            "read_ts": (anchor + read_delta) * 1000 if read_delta is not None else None,
            "played_ts": None,
        })
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
                entry["delivered_sec"] = min(entry["delivered_sec"], deliv) if entry["delivered_sec"] is not None else deliv
            if read is not None:
                entry["read_sec"] = min(entry["read_sec"], read) if entry["read_sec"] is not None else read
            if played is not None:
                entry["played_sec"] = min(entry["played_sec"], played) if entry["played_sec"] is not None else played

    if not is_group:
        delivered_secs = [d["delivered_sec"] for d in by_user.values() if d["delivered_sec"] is not None]
        read_secs = [d["read_sec"] for d in by_user.values() if d["read_sec"] is not None]
        played_secs = [d["played_sec"] for d in by_user.values() if d["played_sec"] is not None]
        if not delivered_secs and not read_secs and not played_secs:
            return []
        deliv_ts = min(delivered_secs) * 1000 if delivered_secs else None
        read_ts = min(read_secs) * 1000 if read_secs else None
        played_ts = min(played_secs) * 1000 if played_secs else None
        return [{
            "name": "",
            "jid": "",
            "delivered_ts": deliv_ts,
            "read_ts": read_ts,
            "played_ts": played_ts,
            "read_known": read_ts is not None,
        }]

    members = []
    for ujid, data in by_user.items():
        name = None
        c_row = conn.execute("SELECT full_name FROM _ios_contacts WHERE jid = ?", (ujid,)).fetchone()
        if c_row and c_row[0]:
            name = c_row[0]
        else:
            prefix = ujid.split("@")[0]
            c_row2 = conn.execute("SELECT full_name FROM _ios_contacts WHERE jid = ?", (prefix,)).fetchone()
            if c_row2 and c_row2[0]:
                name = c_row2[0]
            else:
                p_row = conn.execute("SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ?", (ujid,)).fetchone()
                if p_row and p_row[0]:
                    name = p_row[0]
                else:
                    name = prefix

        jid_clean = ujid.split("@")[0]
        deliv_ts = data["delivered_sec"] * 1000 if data["delivered_sec"] is not None else None
        read_ts = data["read_sec"] * 1000 if data["read_sec"] is not None else None
        played_ts = data["played_sec"] * 1000 if data["played_sec"] is not None else None

        members.append({
            "name": name or jid_clean or "",
            "jid": jid_clean,
            "delivered_ts": deliv_ts,
            "read_ts": read_ts,
            "played_ts": played_ts,
            "read_known": read_ts is not None,
        })
    return members


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app(output_root: Path, rescan: bool = False):
    app = Flask(__name__)

    archive_db_path = get_archive_db_path(output_root)
    source_type, wa_db_path = _detect_source_db(output_root)
    cache_conn = _open_cache_db(output_root)
    _indexing_state: dict = {}
    _indexing_lock = threading.Lock()
    _bulk_index_state: dict = {"running": False, "indexed_msgs": 0, "total_msgs": 0}
    _wa_local = threading.local()

    if source_type is None:
        print("[wab_viewer] Warning: No source WA DB found. Media-only mode.")
        archive_conn_tmp = sqlite3.connect(str(archive_db_path), check_same_thread=False)
        archive_conn_tmp.row_factory = sqlite3.Row
        _build_media_only(archive_conn_tmp, cache_conn, output_root)
        archive_conn_tmp.close()
        wa_conn = None
    else:
        print(f"[wab_viewer] Source DB: {source_type} at {wa_db_path}")

        _ensure_wa_indexes(wa_db_path)

        if rescan or _source_changed(cache_conn, wa_db_path):
            _clear_fts_index(cache_conn)
            _save_source_stamp(cache_conn, wa_db_path)
            print("[wab_viewer] FTS cache cleared — chats will be indexed on first open")
        else:
            count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            print(f"[wab_viewer] {count} chat(s) already indexed")

        has_ios_hd = False
        if source_type == "ios" and wa_db_path is not None:
            ext_db_init = _find_ios_db(wa_db_path, "ExtChatDatabase.sqlite", output_root)
            if not ext_db_init:
                ext_db_init = _find_ios_db(wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", output_root)
            has_ios_hd = _check_ios_hd_association(wa_db_path, ext_db_init)

        if has_ios_hd:
            globals()['_IOS_FILTER'] = _BASE_IOS_FILTER + _IOS_HD_DEDUP_CLAUSE
            globals()['_IOS_FILTER_TS'] = _BASE_IOS_FILTER_TS + _IOS_HD_DEDUP_CLAUSE
        else:
            globals()['_IOS_FILTER'] = _BASE_IOS_FILTER
            globals()['_IOS_FILTER_TS'] = _BASE_IOS_FILTER_TS

    def get_wa():
        if source_type is None:
            return None
        conn = getattr(_wa_local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(wa_db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA cache_size = -32000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("ATTACH DATABASE ? AS arch", (str(archive_db_path),))
            if source_type == "android":
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)")
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _lid_map_resolved (jid_row_id INTEGER PRIMARY KEY, lid_row_id INTEGER)")
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO _jid_map_resolved (lid_row_id, jid_row_id)
                        SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                        FROM jid_map GROUP BY lid_row_id
                    """)
                    conn.execute("""
                        INSERT OR IGNORE INTO _lid_map_resolved (jid_row_id, lid_row_id)
                        SELECT jid_row_id, MIN(lid_row_id) AS lid_row_id
                        FROM jid_map GROUP BY jid_row_id
                    """)
                except sqlite3.OperationalError:
                    pass
            if source_type == "ios":
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ios_contacts (jid TEXT PRIMARY KEY, full_name TEXT, phone_number TEXT)")

                contacts_v2 = _find_ios_db(wa_db_path, "ContactsV2.sqlite", output_root)
                if contacts_v2:
                    conn.execute("ATTACH DATABASE ? AS cv", (str(contacts_v2),))
                    cv_cols = {r["name"] for r in conn.execute("PRAGMA cv.table_info(ZWAADDRESSBOOKCONTACT)").fetchall()}
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
                    lid_cols = {r["name"] for r in conn.execute("PRAGMA lid_db.table_info(ZWAZACCOUNT)").fetchall()}
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
                        conn.execute("INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)", (u_jid, u_phone))
                        conn.execute("INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)", (u_phone, u_phone))
                        conn.execute("INSERT OR REPLACE INTO _ios_contacts (jid, full_name, phone_number) VALUES (?, 'You', ?)", (f"+{u_phone}", u_phone))
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS _ios_contacts_jid ON _ios_contacts(jid)"
                )
                infra_db = _find_ios_db(wa_db_path, "MessagingInfraDatabase.sqlite", output_root)
                if not infra_db:
                    infra_db = _find_ios_db(wa_db_path, "MessagingInfraDB_v2/MessagingInfraDatabase.sqlite", output_root)
                if infra_db:
                    conn.execute("ATTACH DATABASE ? AS infra", (str(infra_db),))
                ext_db = _find_ios_db(wa_db_path, "ExtChatDatabase.sqlite", output_root)
                if not ext_db:
                    ext_db = _find_ios_db(wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", output_root)
                if ext_db:
                    try:
                        conn.execute("ATTACH DATABASE ? AS ext", (str(ext_db),))
                    except sqlite3.OperationalError:
                        pass
            _wa_local.conn = conn
        return conn

    @app.teardown_appcontext
    def _close_wa_conn(exc):
        if source_type is None:
            return
        conn = getattr(_wa_local, 'conn', None)
        if conn is not None:
            try:
                conn.commit()
            except sqlite3.Error:
                pass

    def get_cache():
        return cache_conn

    _archive_conn = sqlite3.connect(str(archive_db_path), check_same_thread=False)
    _archive_conn.execute("PRAGMA journal_mode = WAL")
    _archive_conn.execute("PRAGMA busy_timeout = 5000")
    _archive_conn.row_factory = sqlite3.Row
    _archive_conn.executescript(f"""
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
            sha256 TEXT
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
            reactions      TEXT,
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
    cols = [r[1] for r in _archive_conn.execute(
        "PRAGMA table_info(recent_messages)").fetchall()]
    if "reactions" not in cols:
        _archive_conn.execute("ALTER TABLE recent_messages ADD COLUMN reactions TEXT")

    # Build _ANDROID_SELECT and _IOS_SELECT based on available tables
    has_reactions = False
    has_ios_reactions = False
    has_lid_dn = False
    has_message_system = False
    has_mcp = False
    has_ios_group_event = False
    has_jid_server = False
    has_jid_raw_string = False
    if wa_db_path is not None:
        _tmp_wa = sqlite3.connect(str(wa_db_path))
        _wa_tables = {r[0] for r in _tmp_wa.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        has_reactions = "message_add_on" in _wa_tables
        has_ios_reactions = "ZWAMESSAGEINFO" in _wa_tables
        has_lid_dn = "lid_display_name" in _wa_tables
        has_message_system = "message_system" in _wa_tables
        has_mcp = "message_system_chat_participant" in _wa_tables
        _jid_cols = {r[1] for r in _tmp_wa.execute("PRAGMA table_info(jid)").fetchall()} if "jid" in _wa_tables else set()
        has_jid_server = "server" in _jid_cols
        has_jid_raw_string = "raw_string" in _jid_cols
        _zwa_cols = {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()} if "ZWAMESSAGE" in _wa_tables else set()
        has_ios_group_event = "ZGROUPEVENTTYPE" in _zwa_cols
        _tmp_wa.close()

    if source_type == "ios":
        group_ev_col = "m.ZGROUPEVENTTYPE" if has_ios_group_event else "0"
        has_ios_sort = "ZSORT" in _zwa_cols
        sort_col_ios = "COALESCE(m.ZSORT, m.Z_PK)" if has_ios_sort else "m.Z_PK"
        ios_service_filter = "OR (m.ZMESSAGETYPE = 6 AND m.ZGROUPEVENTTYPE IN (1, 2, 3, 4, 5, 7, 9, 12, 15, 26, 36, 37, 50))" if has_ios_group_event else "OR (m.ZMESSAGETYPE = 6)"
        globals()['_BASE_IOS_FILTER'] = f"""
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND m.ZMESSAGETYPE != 6 AND mi.ZMEDIALOCALPATH IS NOT NULL)
        {ios_service_filter}
    )
"""
        globals()['_BASE_IOS_FILTER_TS'] = globals()['_BASE_IOS_FILTER']
        if has_ios_hd:
            globals()['_IOS_FILTER'] = globals()['_BASE_IOS_FILTER'] + _IOS_HD_DEDUP_CLAUSE
            globals()['_IOS_FILTER_TS'] = globals()['_BASE_IOS_FILTER_TS'] + _IOS_HD_DEDUP_CLAUSE
        else:
            globals()['_IOS_FILTER'] = globals()['_BASE_IOS_FILTER']
            globals()['_IOS_FILTER_TS'] = globals()['_BASE_IOS_FILTER_TS']

        globals()['_IOS_SELECT'] = f"""
    SELECT
        m.Z_PK                                                       AS msg_id,
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
            WHEN m.ZMESSAGETYPE = 6 THEN 'service'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="('Message/' || COALESCE(mi.ZMEDIALOCALPATH,''))")}
        END                                                           AS media_type,
        COALESCE(mi.ZTITLE, '')                                      AS media_name,
        COALESCE(m.ZTEXT, '')                                        AS text_body,
        COALESCE(qm.ZTEXT, '')                                       AS quoted_text,
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
        NULL                                                          AS reactions,
        m.ZMESSAGETYPE                                               AS raw_msg_type,
        {group_ev_col}                                               AS group_event_type,
        gm.ZMEMBERJID                                                AS member_jid
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
          ON ac.original_path = 'Message/' || COALESCE(mi.ZMEDIALOCALPATH, '')
"""

    ms_join = "LEFT JOIN message_system ms ON ms.message_row_id = m._id" if has_message_system else ""
    ms_col = "ms.action_type" if has_message_system else "NULL"

    ms_action_clause = "ms.action_type IN (1, 4, 5, 6, 11, 12, 13, 14, 15, 20, 27, 58, 79)" if has_message_system else "(m.text_data IS NOT NULL AND m.text_data != '')"
    globals()['_ANDROID_FILTER'] = f"""
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND m.message_type != 7 AND mm.file_path IS NOT NULL)
        OR (m.message_type = 7 AND {ms_action_clause})
    )
    """
    globals()['_ANDROID_FILTER_TS'] = globals()['_ANDROID_FILTER']

    ldn_select_joins = """
    LEFT JOIN _lid_map_resolved jm_lid_s ON jm_lid_s.jid_row_id = m.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_lid_s.lid_row_id

    LEFT JOIN _lid_map_resolved jm_lid_sq ON jm_lid_sq.jid_row_id = mq.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_sq_direct ON ldn_sq_direct.lid_row_id = mq.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_sq_mapped ON ldn_sq_mapped.lid_row_id = jm_lid_sq.lid_row_id
""" if has_lid_dn else ""

    ldn_sender_arm = """
            NULLIF(ldn_s_direct.display_name, ''),
            NULLIF(ldn_s_mapped.display_name, ''),
""" if has_lid_dn else ""

    ldn_quoted_sender_arm = """
                 NULLIF(ldn_sq_direct.display_name, ''),
                 NULLIF(ldn_sq_mapped.display_name, ''),
""" if has_lid_dn else ""

    j_is_lid = "j.server = 'lid'" if has_jid_server else "0"
    jq_is_lid = "jq.server = 'lid'" if has_jid_server else "0"

    globals()['_ANDROID_SELECT'] = f"""
    SELECT
        m._id                                                        AS msg_id,
        {_ANDROID_CHAT_ID}                                           AS chat_id,
        {_ANDROID_CHAT_TYPE}                                         AS chat_type,
        COALESCE(m.timestamp, 0)                                     AS timestamp_ms,
        COALESCE(
            NULLIF(con_s.display_name, ''),
            CASE WHEN COALESCE(j2.user, CASE WHEN NOT ({j_is_lid}) THEN j.user END) = '0' THEN 'WhatsApp' END,
            CASE WHEN j2.user IS NOT NULL THEN '+' || j2.user END,
            {ldn_sender_arm}
            CASE WHEN NOT ({j_is_lid}) AND j.user IS NOT NULL THEN '+' || j.user END,
            ''
        )                                                            AS sender,
        m.from_me,
        ac.archive_path,
        CASE
            WHEN m.message_type = 7 THEN 'service'
            WHEN m.message_type IS NULL OR m.message_type = 0 THEN
                CASE WHEN m.text_data IS NOT NULL
                      AND (INSTR(LOWER(m.text_data), 'http://') > 0
                           OR INSTR(LOWER(m.text_data), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col='mm.file_path')}
        END                                                          AS media_type,
        COALESCE(mm.media_name, '')                                  AS media_name,
        COALESCE(m.text_data, '')                                    AS text_body,
        COALESCE(mq.text_data, '')                                   AS quoted_text,
        CASE WHEN mq.from_me = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 CASE WHEN jq2.user IS NOT NULL THEN '+' || jq2.user END,
                 {ldn_quoted_sender_arm}
                 CASE WHEN NOT ({jq_is_lid}) AND jq.user IS NOT NULL THEN '+' || jq.user END,
                 '') END                                             AS quoted_sender,
        COALESCE(mq.timestamp, 0)                                    AS quoted_ts,
        {ms_col}                                                     AS action_type,
        ''                                                           AS participant_name
    FROM message m
    LEFT JOIN message_media mm ON mm.message_row_id = m._id
    LEFT JOIN chat c ON c._id = m.chat_row_id
    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
    LEFT JOIN jid j ON j._id = m.sender_jid_row_id
    {_ANDROID_JID_MAP}
    LEFT JOIN message_quoted mq ON mq.message_row_id = m._id
    LEFT JOIN jid jq ON jq._id = mq.sender_jid_row_id
    LEFT JOIN _jid_map_resolved jmq ON jmq.lid_row_id = mq.sender_jid_row_id
    LEFT JOIN jid jq2 ON jq2._id = jmq.jid_row_id
    {ldn_select_joins}
    {ms_join}
    LEFT JOIN arch.contacts con_s  ON con_s.number  = COALESCE(j2.user, j.user)
    LEFT JOIN arch.contacts con_sq ON con_sq.number = COALESCE(jq2.user, jq.user)
    LEFT JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
"""

    def get_archive():
        return _archive_conn

    # ---- API: list chats ---------------------------------------------------

    @app.route("/api/chats")
    def api_chats():
        conn = get_wa()
        if conn is None:
            rows = get_cache().execute("""
                SELECT chat_id AS id, chat_type AS type,
                       MAX(NULLIF(timestamp_ms, 0)) AS newest_ts
                FROM message_index
                GROUP BY chat_id, chat_type
                ORDER BY newest_ts DESC
            """).fetchall()
            return jsonify([{
                "id": r["id"], "type": r["type"],
                "display_name": r["id"],
                "newest_ts": r["newest_ts"],
                "last_msg_preview": "",
                "last_msg_type": "text",
                "last_msg_from_me": 0,
            } for r in rows])

        if source_type == "android":
            j_s_is_lid = "j_s.server = 'lid'" if has_jid_server else "0"
            ms_chat_col = "ms.action_type" if has_message_system else "NULL"
            ms_chat_join = "LEFT JOIN message_system ms ON ms.message_row_id = m._id" if has_message_system else ""
            ldn_s_join = """
                LEFT JOIN _lid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_s.lid_row_id
            """ if has_lid_dn else "LEFT JOIN _jid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id"
            ldn_s_arm = """
                NULLIF(ldn_s_direct.display_name, ''),
                NULLIF(ldn_s_mapped.display_name, ''),
            """ if has_lid_dn else ""
            rows = conn.execute(f"""
                SELECT
                    CASE WHEN c.subject IS NOT NULL THEN CAST(c._id AS TEXT)
                         ELSE COALESCE(j_chat_real.user, j_chat.user, CAST(c._id AS TEXT))
                    END                                                 AS id,
                    CASE WHEN c.subject IS NOT NULL THEN 'group'
                         ELSE 'contact' END                            AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        CASE WHEN COALESCE(j_chat_real.user, j_chat.user) = '0' THEN 'WhatsApp' END,
                        con.folder,
                        grp.subject,
                        CASE WHEN COALESCE(j_chat_real.user, j_chat.user) IS NOT NULL
                             THEN '+' || COALESCE(j_chat_real.user, j_chat.user)
                             ELSE CAST(c._id AS TEXT) END
                    )                                                   AS display_name,
                    c.sort_timestamp                                    AS newest_ts,
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
                        CASE WHEN j_s_real.user IS NOT NULL THEN '+' || j_s_real.user END,
                        {ldn_s_arm}
                        CASE WHEN NOT ({j_s_is_lid}) AND j_s.user IS NOT NULL THEN '+' || j_s.user END,
                        ''
                    )                                                   AS last_msg_sender
                FROM chat c
                LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                LEFT JOIN arch.contacts con ON con.number = COALESCE(j_chat_real.user, j_chat.user)
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(c._id AS TEXT)
                LEFT JOIN message m ON m._id = c.display_message_row_id
                LEFT JOIN message_media mm ON mm.message_row_id = m._id
                LEFT JOIN jid j_s ON j_s._id = m.sender_jid_row_id
                {ldn_s_join}
                LEFT JOIN jid j_s_real ON j_s_real._id = jm_s.jid_row_id
                LEFT JOIN arch.contacts con_s ON con_s.number = COALESCE(j_s_real.user, j_s.user)
                {ms_chat_join}
                WHERE c.hidden = 0
                ORDER BY c.sort_timestamp DESC
            """).fetchall()
        else:
            group_ev_col = "m.ZGROUPEVENTTYPE" if has_ios_group_event else "0"
            ios_service_filter_last_real = (
                "OR (msg.ZMESSAGETYPE = 6 AND msg.ZGROUPEVENTTYPE IN (1, 2, 3, 4, 5, 7, 9, 12, 15, 26, 36, 37, 50))"
                if has_ios_group_event else "OR (msg.ZMESSAGETYPE = 6)"
            )
            rows = conn.execute(f"""
                SELECT
                    CAST(cs.Z_PK AS TEXT)                               AS id,
                    CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                         ELSE 'contact' END                            AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        CASE WHEN SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                         INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1) = '0'
                            THEN 'WhatsApp' END,
                        con.folder,
                        grp.subject,
                        cs.ZPARTNERNAME,
                        CASE WHEN SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                         INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1) != ''
                            THEN '+' || SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                                INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1)
                        END,
                        CAST(cs.Z_PK AS TEXT)
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
                LEFT JOIN arch.contacts con
                      ON con.number = SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                             INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1)
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(cs.Z_PK AS TEXT)
                JOIN (
                    SELECT msg.ZCHATSESSION, MAX(msg.Z_PK) AS last_pk
                    FROM ZWAMESSAGE msg
                    LEFT JOIN ZWAMEDIAITEM mi2 ON mi2.Z_PK = msg.ZMEDIAITEM
                    WHERE (msg.ZTEXT IS NOT NULL AND msg.ZTEXT != '' AND (msg.ZMESSAGETYPE IS NULL OR msg.ZMESSAGETYPE = 0))
                       OR (msg.ZMESSAGETYPE IS NOT NULL AND msg.ZMESSAGETYPE != 0 AND msg.ZMESSAGETYPE != 6
                           AND mi2.ZMEDIALOCALPATH IS NOT NULL)
                       {ios_service_filter_last_real}
                    GROUP BY msg.ZCHATSESSION
                ) last_real ON last_real.ZCHATSESSION = cs.Z_PK
                JOIN ZWAMESSAGE m ON m.Z_PK = last_real.last_pk
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                WHERE cs.ZHIDDEN = 0
                ORDER BY m.ZMESSAGEDATE DESC
            """).fetchall()

        result = []
        ios_contacts_map = {}
        if source_type == "ios":
            needed_jids = set()
            for r in rows:
                if r["last_msg_type"] == 6:
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
                            ios_contacts_map[f"{ar['number']}@s.whatsapp.net"] = (ar["display_name"], ar["number"])
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
        if source_type == "android" and has_mcp:
            svc_mids = [
                r["last_msg_id"] for r in rows
                if r["last_msg_type"] == 7
                and r["last_msg_action_type"] in (1, 4, 5, 6, 11, 12, 13, 14, 15, 20, 27, 58, 79)
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

        for r in rows:
            d = dict(r)
            raw_type = d.pop("last_msg_type", 0)
            media_path = d.pop("last_msg_media_path", None)
            last_msg_id = d.pop("last_msg_id", None)
            last_msg_action_type = d.pop("last_msg_action_type", None)
            last_msg_participant_name = d.pop("last_msg_participant_name", None)
            if not last_msg_participant_name and last_msg_id:
                last_msg_participant_name = android_p_map.get(last_msg_id, "")
            last_msg_group_event_type = d.pop("last_msg_group_event_type", None)
            last_msg_sender = d.pop("last_msg_sender", None)
            last_msg_sender_jid = d.pop("last_msg_sender_jid", None)
            last_msg_member_jid = d.pop("last_msg_member_jid", None)

            if source_type == "android" and raw_type == 7:
                if last_msg_action_type in (1, 4, 5, 6, 11, 12, 13, 14, 15, 20, 27, 58, 79):
                    s_row = {
                        "action_type": last_msg_action_type,
                        "text_body": d.get("last_msg_preview", ""),
                        "from_me": d.get("last_msg_from_me", 0),
                        "sender": last_msg_sender,
                        "participant_name": last_msg_participant_name,
                    }
                    d["last_msg_preview"] = _format_android_service_row(s_row)
                    d["last_msg_type"] = "service"
                else:
                    d["last_msg_preview"] = ""
                    d["last_msg_type"] = "text"
            elif source_type == "ios" and raw_type == 6:
                if not has_ios_group_event or last_msg_group_event_type in (1, 2, 3, 4, 5, 7, 9, 12, 15, 26, 36, 37, 50):
                    sender_name = _resolve_ios_jid(last_msg_sender_jid, ios_contacts_map) if last_msg_sender_jid else ""
                    s_row = {
                        "group_event_type": last_msg_group_event_type,
                        "text_body": d.get("last_msg_preview", ""),
                        "from_me": d.get("last_msg_from_me", 0),
                        "sender": sender_name,
                        "member_jid": last_msg_member_jid or "",
                    }
                    d["last_msg_preview"] = _format_ios_service_row(s_row, ios_contacts_map)
                    d["last_msg_type"] = "service"
                else:
                    d["last_msg_preview"] = ""
                    d["last_msg_type"] = "text"
            elif raw_type != 0 and media_path:
                path = media_path if source_type == "android" else f"Message/{media_path}"
                d["last_msg_type"] = _media_type_from_path(path)
            else:
                d["last_msg_type"] = "text"
            result.append(d)
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
            rows = get_archive().execute(
                """SELECT * FROM recent_messages
                   WHERE chat_id = ? AND chat_type = ?
                   ORDER BY timestamp_ms DESC LIMIT ?""",
                (chat_id, chat_type, limit)
            ).fetchall()
            if rows:
                _maybe_start_indexing(chat_id, chat_type, get_cache(), source_type,
                                     wa_db_path, output_root,
                                     _indexing_state, _indexing_lock)
                if source_type == "android" and rows:
                    rows = [dict(r) for r in rows]
                    rx_cache = get_archive().execute(
                        """SELECT msg_id, reactions FROM reactions_cache
                           WHERE chat_id = ? AND msg_id IN (""" + ",".join("?" * len(rows)) + """)""",
                        [chat_id] + [r["msg_id"] for r in rows]
                    ).fetchall()
                    rx_map = {r["msg_id"]: r["reactions"] for r in rx_cache}
                    for row in rows:
                        row["reactions"] = rx_map.get(row["msg_id"])
                return jsonify(rows)

        conn = get_wa()
        if conn is None:
            return jsonify([])

        if not before and not after:
            _maybe_start_indexing(chat_id, chat_type, get_cache(), source_type,
                                 wa_db_path, output_root,
                                 _indexing_state, _indexing_lock)

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
        _format_service_rows(rows, source_type, conn, has_mcp, has_lid_dn, has_jid_server, has_jid_raw_string)

        if source_type == "android" and rows and has_reactions:
            msg_ids = [r["msg_id"] for r in rows]
            placeholders = ",".join("?" * len(msg_ids))
            cached = get_archive().execute(
                f"SELECT msg_id, reactions FROM reactions_cache "
                f"WHERE chat_id = ? AND msg_id IN ({placeholders})",
                [chat_id] + msg_ids
            ).fetchall()
            cached_map = {r["msg_id"]: r["reactions"] for r in cached}
            missing = [mid for mid in msg_ids if mid not in cached_map]
            if missing:
                missing_ph = ",".join("?" * len(missing))
                from_wa = conn.execute(f"""
                    SELECT ao.parent_message_row_id AS msg_id,
                           GROUP_CONCAT(r.reaction) AS reactions
                    FROM message_add_on ao
                    JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                    WHERE ao.parent_message_row_id IN ({missing_ph})
                    GROUP BY ao.parent_message_row_id
                """, missing).fetchall()
                wa_map = {r["msg_id"]: r["reactions"] for r in from_wa}
                if from_wa:
                    get_archive().executemany(
                        "INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions) "
                        "VALUES (?, ?, ?)",
                        [(chat_id, r["msg_id"], r["reactions"]) for r in from_wa]
                    )
                    get_archive().commit()
                for row in rows:
                    row["reactions"] = wa_map.get(row["msg_id"])
            else:
                for row in rows:
                    row["reactions"] = cached_map.get(row["msg_id"])
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

        if not before and not after:
            threading.Thread(
                target=_backfill_recent_messages,
                args=(get_archive(), rows),
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

        conn = get_wa()
        if conn is None:
            return jsonify([])

        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER
        half = limit // 2

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
            chat_params + [ts, half]
        ).fetchall()
        after_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {ts_col} > ? {extra} ORDER BY {ts_col} ASC, {sort_phys_col} ASC LIMIT ?",
            chat_params + [ts, half]
        ).fetchall()

        combined = [dict(r) for r in reversed(before_rows)] + [dict(r) for r in after_rows]
        _format_service_rows(combined, source_type, conn, has_mcp, has_lid_dn, has_jid_server, has_jid_raw_string)
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
        return jsonify(_strip_none_reactions(combined))

    # ---- API: media gallery ------------------------------------------------

    @app.route("/api/media")
    def api_media():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        before = request.args.get("before")
        conn = get_wa()
        if conn is None:
            return jsonify([])
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER
        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            is_media, ts_col = _ANDROID_IS_MEDIA, _ANDROID_TS
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            is_media, ts_col = _IOS_IS_MEDIA, _IOS_TS
        if before:
            sql = f"{select} WHERE {chat_pred} {extra} AND {is_media} AND ac.archive_path IS NOT NULL AND {ts_col} < ? ORDER BY {ts_col} DESC LIMIT ?"
            rows = conn.execute(sql, chat_params + [int(before), GALLERY_PAGE_SIZE]).fetchall()
        else:
            sql = f"{select} WHERE {chat_pred} {extra} AND {is_media} AND ac.archive_path IS NOT NULL ORDER BY {ts_col} DESC LIMIT ?"
            rows = conn.execute(sql, chat_params + [GALLERY_PAGE_SIZE]).fetchall()
        return jsonify([dict(r) for r in rows])

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
        rows = conn.execute(sql, chat_params).fetchall()
        return jsonify([dict(r) for r in rows])

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
        rows = conn.execute(sql, chat_params).fetchall()
        return jsonify([dict(r) for r in rows])

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
            f"{select} WHERE {chat_pred} {extra} AND {is_media}",
            chat_params
        ).fetchall()
        link_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {is_link}",
            chat_params
        ).fetchall()
        doc_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {is_doc_undownloaded}",
            chat_params
        ).fetchall()
        all_rows = list(rows) + list(doc_rows) + list(link_rows)
        total = len(all_rows)
        archived = sum(1 for r in rows if r["archive_path"])
        by_type: dict = {}
        for r in rows:
            t = r["media_type"]
            by_type.setdefault(t, {"count": 0, "missing": 0})
            by_type[t]["count"] += 1
            if not r["archive_path"]:
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
                idx_rows = fts_conn.execute("""
                    SELECT mi.rowid, mi.chat_id, mi.chat_type, mi.timestamp_ms
                    FROM message_index mi
                    JOIN message_index_fts fts ON fts.rowid = mi.rowid
                    WHERE message_index_fts MATCH ?
                      AND mi.chat_id = ? AND mi.chat_type = ?
                    ORDER BY mi.timestamp_ms ASC LIMIT 500
                """, (q, chat_id, chat_type)).fetchall()
            elif chat_id:
                idx_rows = fts_conn.execute("""
                    SELECT mi.rowid, mi.chat_id, mi.chat_type, mi.timestamp_ms
                    FROM message_index mi
                    JOIN message_index_fts fts ON fts.rowid = mi.rowid
                    WHERE message_index_fts MATCH ? AND mi.chat_id = ?
                    ORDER BY mi.timestamp_ms ASC LIMIT 500
                """, (q, chat_id)).fetchall()
            else:
                idx_rows = fts_conn.execute("""
                    SELECT mi.rowid, mi.chat_id, mi.chat_type, mi.timestamp_ms
                    FROM message_index mi
                    JOIN message_index_fts fts ON fts.rowid = mi.rowid
                    WHERE message_index_fts MATCH ?
                    ORDER BY mi.timestamp_ms ASC LIMIT 100
                """, (q,)).fetchall()
        except sqlite3.OperationalError as e:
            return jsonify({"error": f"Invalid search query: {e}"}), 400

        if not idx_rows:
            indexed_count = get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            return jsonify({"results": [], "indexed_count": indexed_count})

        if wa is None:
            indexed_count = get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            return jsonify({"results": [dict(r) for r in idx_rows], "indexed_count": indexed_count})

        rowids = [r["rowid"] for r in idx_rows]
        placeholders = ",".join("?" * len(rowids))
        id_col = "m._id" if source_type == "android" else "m.Z_PK"
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER

        rows = wa.execute(
            f"{select} WHERE {id_col} IN ({placeholders}) {extra} ORDER BY timestamp_ms ASC",
            rowids
        ).fetchall()

        indexed_count = get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
        return jsonify({"results": [dict(r) for r in rows], "indexed_count": indexed_count})

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
            rows = conn.execute("""
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
                LEFT JOIN (
                    SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                    FROM jid_map GROUP BY lid_row_id
                ) jm ON jm.lid_row_id = ru.receipt_user_jid_row_id
                LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                LEFT JOIN arch.contacts con  ON con.number  = j_real.user
                LEFT JOIN arch.contacts con2 ON con2.number = j.user
                WHERE ru.message_row_id = ?
            """, (message_id,)).fetchall()

            if not rows:
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
                (message_id,)
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
                    (row["stanza_id"],)
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
                known_jids = {
                    r[0].split("@")[0]
                    for r in conn.execute(
                        """SELECT DISTINCT gm.ZMEMBERJID
                           FROM ZWAMESSAGE m
                           JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                           WHERE m.ZCHATSESSION = ?
                             AND gm.ZMEMBERJID IS NOT NULL""",
                        (row["chat_session_pk"],)
                    ).fetchall()
                }
                members = [m for m in members if m["jid"] in known_jids]

            for m in members:
                if m.get("read_known") is None:
                    m["read_known"] = _ios_read_without_timestamp(row["msg_status"], m.get("read_ts"))

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
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "message_add_on" not in tables:
                return jsonify({"available": False})
            has_lid_dn = "lid_display_name" in tables
            ldn_rx_join = """
                LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = ao.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = ao.sender_jid_row_id
                LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
            """ if has_lid_dn else ""
            ldn_rx_arm = """
                           NULLIF(ldn_direct.display_name, ''),
                           NULLIF(ldn_mapped.display_name, ''),
            """ if has_lid_dn else ""
            rows = conn.execute(f"""
                SELECT r.reaction AS emoji,
                       ao.from_me,
                       COALESCE(
                           NULLIF(con_s.display_name, ''),
                           CASE WHEN COALESCE(j2.user, j.user) = '0' THEN 'WhatsApp' END,
                           CASE WHEN COALESCE(j2.user, j.user) IS NOT NULL
                                THEN '+' || COALESCE(j2.user, j.user) END,
                           {ldn_rx_arm}
                           ''
                       ) AS name
                FROM message_add_on ao
                JOIN message_add_on_reaction r ON r.message_add_on_row_id = ao._id
                LEFT JOIN jid j ON j._id = ao.sender_jid_row_id
                LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = ao.sender_jid_row_id
                LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
                {ldn_rx_join}
                LEFT JOIN arch.contacts con_s ON con_s.number = COALESCE(j2.user, j.user)
                WHERE ao.parent_message_row_id = ?
            """, (message_id,)).fetchall()
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

            row = conn.execute("""
                SELECT mi.ZRECEIPTINFO, m.ZCHATSESSION AS chat_session_pk
                FROM ZWAMESSAGEINFO mi
                JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
                WHERE mi.ZMESSAGE = ?
            """, (message_id,)).fetchone()
            if not row or not row[0]:
                return jsonify({"available": False})

            blob = bytes(row[0])
            raw_reactors = _extract_ios_reaction_reactors(blob)
            if not raw_reactors:
                return jsonify({"available": True, "total": 0, "reactors": []})

            # Resolve "me" from a sent message in this chat, and the 1-to-1 partner.
            my_row = conn.execute("""
                SELECT COALESCE(gm.ZMEMBERJID, m.ZFROMJID) AS me_jid
                FROM ZWAMESSAGE m
                LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                WHERE m.ZCHATSESSION = ? AND m.ZISFROMME = 1
                LIMIT 1
            """, (row["chat_session_pk"],)).fetchone()
            my_phone = None
            if my_row and my_row["me_jid"]:
                at = my_row["me_jid"].find("@")
                if at > 0:
                    my_phone = my_row["me_jid"][:at]

            def _resolve_number(num):
                r = conn.execute(
                    "SELECT full_name FROM _ios_contacts WHERE jid = ?",
                    (f"{num}@s.whatsapp.net",)
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
                        (lid_jid,)
                    ).fetchone()
                    if r_cs and r_cs[0]:
                        return r_cs[0]
                    r_pp = conn.execute(
                        "SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ? "
                        "AND ZPUSHNAME IS NOT NULL AND ZPUSHNAME != '' LIMIT 1",
                        (lid_jid,)
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

    # ---- API: chat info -------------------------------------------------------

    def _group_members_android(conn, chat_id: str) -> list:
        # Note on Android WhatsApp: User-chosen friendly push names (wa_name) reside
        # in /data/data/com.whatsapp/databases/wa.db (table wa_contacts). Extracting wa.db
        # requires root privileges on the device, which is not implemented in the Viewer.
        # In standard msgstore.db backups, lid_display_name only contains Meta's privacy-masked
        # phone strings (e.g. +39••••••••04), so unsaved members resolve to their real phone number
        # when mapped via jid_map, falling back to lid_display_name only if completely unmapped.
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        jid_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jid)").fetchall()} if "jid" in tables else set()
        has_server = "server" in jid_cols
        has_raw_string = "raw_string" in jid_cols
        has_lid_dn = "lid_display_name" in tables
        has_gpu = "group_participant_user" in tables
        j_phone_fallback = "CASE WHEN j.server = 's.whatsapp.net' THEN j.user END" if has_server else "j.user"

        ldn_arm = """
            NULLIF(ldn_direct.display_name, ''),
            NULLIF(ldn_mapped.display_name, ''),
        """ if has_lid_dn else ""

        # 1. Modern WhatsApp: group_participant_user
        if has_gpu:
            try:
                phone_expr_gpu = """
                    COALESCE(
                        j_phone.user,
                        CASE WHEN j_user.server = 's.whatsapp.net' THEN j_user.user END
                    )
                """ if has_server else "COALESCE(j_phone.user, j_user.user)"
                ldn_gpu_join = """
                    LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = gpu.user_jid_row_id
                    LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = gpu.user_jid_row_id
                    LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
                """ if has_lid_dn else ""

                rows = conn.execute(f"""
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
                """, (chat_id,)).fetchall()
                if rows:
                    return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                    raise

        # 2. Legacy WhatsApp: group_participants
        join_gp_jid = "j_gp.raw_string = gp.jid" if has_raw_string else "j_gp.user = NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '')"
        gjid_pred = "gp.gjid = (SELECT j.raw_string FROM jid j WHERE j._id = c.jid_row_id)" if has_raw_string else "1=1"
        try:
            phone_expr_gp = """
                COALESCE(
                    j_phone.user,
                    CASE WHEN gp.jid NOT LIKE '%@lid'
                         THEN NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '')
                    END
                )
            """
            ldn_gp_join = """
                LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = j_gp._id
                LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = j_gp._id
                LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
            """ if has_lid_dn else ""

            rows = conn.execute(f"""
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
            """, (chat_id,)).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                raise

        # 3. Fallback: message history
        ldn_msg_join = """
            LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
        """ if has_lid_dn else ""
        phone_expr_msg = f"COALESCE(j2.user, {j_phone_fallback})"

        rows = conn.execute(f"""
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
        """, (chat_id,)).fetchall()
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
            rows = conn.execute(query_template.format(
                source_table="ZWAGROUPMEMBER gm",
                where_clause="gm.ZCHATSESSION = CAST(? AS INTEGER) AND gm.ZISACTIVE = 1",
            ), (chat_id,)).fetchall()
            if not rows:
                # 2. Try all members from ZWAGROUPMEMBER
                rows = conn.execute(query_template.format(
                    source_table="ZWAGROUPMEMBER gm",
                    where_clause="gm.ZCHATSESSION = CAST(? AS INTEGER)",
                ), (chat_id,)).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                raise

        # 3. Fallback to message history senders via ZWAGROUPMEMBER
        try:
            rows = conn.execute(query_template.format(
                source_table="ZWAMESSAGE m JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER",
                where_clause="m.ZCHATSESSION = CAST(? AS INTEGER)",
            ), (chat_id,)).fetchall()
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
            rows = conn.execute(f"""
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
            """, (chat_id,)).fetchall()
            return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            return []

    @app.route("/api/chat-info")
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
                    row = conn.execute("""
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
                    """, (chat_id,)).fetchone()
                    number = row["user"] if row else chat_id

                    name_row = conn.execute("""
                        SELECT COALESCE(NULLIF(con.display_name,''), con.folder) AS name
                        FROM arch.contacts con
                        WHERE con.number = ?
                    """, (chat_id,)).fetchone()
                    if chat_id == '0':
                        display_name = 'WhatsApp'
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
                        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                        if "message_system" in tables:
                            desc_row = conn.execute("""
                                SELECT m.text_data
                                FROM message m
                                JOIN message_system ms ON ms.message_row_id = m._id
                                WHERE m.chat_row_id = CAST(? AS INTEGER)
                                  AND ms.action_type = 27
                                ORDER BY m.timestamp DESC, m._id DESC
                                LIMIT 1
                            """, (chat_id,)).fetchone()
                            if desc_row and desc_row["text_data"]:
                                description = desc_row["text_data"].strip() or None

                        if not description:
                            arch_grp_cols = {c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()}
                            if "description" in arch_grp_cols:
                                arch_desc_row = conn.execute(
                                    "SELECT NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id = ?",
                                    (str(chat_id),),
                                ).fetchone()
                                if arch_desc_row and arch_desc_row["description"]:
                                    description = arch_desc_row["description"].strip() or None

                        jid_cols = {c[1] for c in conn.execute("PRAGMA table_info(jid)").fetchall()} if "jid" in tables else set()
                        j_fallback = "CASE WHEN j.server = 's.whatsapp.net' THEN j.user END" if "server" in jid_cols else "j.user"

                        has_jid_map = "jid_map" in tables
                        has_lid_dn = "lid_display_name" in tables

                        jm_join = """
                            LEFT JOIN (
                                SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                                FROM jid_map GROUP BY lid_row_id
                            ) jm ON jm.lid_row_id = m.sender_jid_row_id
                            LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                        """ if has_jid_map else ""
                        creator_expr = f"COALESCE(j_real.user, {j_fallback})" if has_jid_map else j_fallback

                        ldn_creator_join = """
                            LEFT JOIN (
                                SELECT jid_row_id, MIN(lid_row_id) AS lid_row_id
                                FROM jid_map GROUP BY jid_row_id
                            ) jm_lid ON jm_lid.jid_row_id = m.sender_jid_row_id
                            LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id
                            LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
                        """ if has_lid_dn and has_jid_map else (
                            "LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id" if has_lid_dn else ""
                        )
                        ldn_creator_arm = """
                            NULLIF(ldn_direct.display_name, ''),
                            NULLIF(ldn_mapped.display_name, ''),
                        """ if has_lid_dn and has_jid_map else (
                            "NULLIF(ldn_direct.display_name, '')," if has_lid_dn else ""
                        )

                        cre_row = conn.execute(f"""
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
                        """, (chat_id,)).fetchone()
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
                    row = conn.execute("""
                        SELECT
                            NULLIF(SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                         INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '') AS user,
                            COALESCE(
                                NULLIF(con.display_name,''),
                                CASE WHEN NULLIF(SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                         INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '') = '0'
                                     THEN 'WhatsApp' END,
                                con.folder,
                                cs.ZPARTNERNAME
                            ) AS name
                        FROM ZWACHATSESSION cs
                        LEFT JOIN arch.contacts con ON con.number = NULLIF(
                            SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                   INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '')
                        WHERE cs.Z_PK = CAST(? AS INTEGER) AND cs.ZGROUPINFO IS NULL
                    """, (chat_id,)).fetchone()
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
                    raw_prefix_c = "SUBSTR(gi.ZCREATORJID, 1, INSTR(gi.ZCREATORJID || '@', '@') - 1)"
                    phone_jid_c = f"({phone_expr_c} || '@s.whatsapp.net')"

                    gi_cols = {c[1] for c in conn.execute("PRAGMA table_info(ZWAGROUPINFO)").fetchall()}
                    extra_gi_cols = []
                    if "ZPICTUREID" in gi_cols:
                        extra_gi_cols.append("gi.ZPICTUREID AS picture_id")
                    if "ZDESCRIPTION" in gi_cols:
                        extra_gi_cols.append("gi.ZDESCRIPTION AS direct_desc")
                    elif "ZGROUPDESCRIPTION" in gi_cols:
                        extra_gi_cols.append("gi.ZGROUPDESCRIPTION AS direct_desc")
                    extra_gi_select = (", " + ", ".join(extra_gi_cols)) if extra_gi_cols else ""

                    cre_row = conn.execute(f"""
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
                    """, (chat_id,)).fetchone()
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
                        arch_grp_cols = {c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()}
                        if "description" in arch_grp_cols:
                            arch_desc_row = conn.execute(
                                "SELECT NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id = ?",
                                (str(chat_id),),
                            ).fetchone()
                            if arch_desc_row and arch_desc_row["description"]:
                                description = arch_desc_row["description"].strip() or None

        result = {
            "display_name": display_name,
            "number": number,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "sent": sent,
            "received": received,
            "total": total,
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

        archive_conn = sqlite3.connect(str(archive_db_path), timeout=5.0)
        archive_conn.row_factory = sqlite3.Row
        try:
            if chat_type == "contact":
                folder_row = archive_conn.execute(
                    "SELECT folder FROM contacts WHERE number = ?", (chat_id,)
                ).fetchone()
                prefix = f"Contacts/{folder_row['folder']}/" if folder_row else None
            else:
                folder_row = archive_conn.execute(
                    "SELECT folder FROM groups WHERE chat_row_id = ?", (chat_id,)
                ).fetchone()
                prefix = f"Groups/{folder_row['folder']}/" if folder_row else None

            if prefix is None:
                return jsonify({"bytes": 0})

            safe_prefix = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            paths = [
                r["archive_path"]
                for r in archive_conn.execute(
                    "SELECT archive_path FROM archive_copies"
                    " WHERE archive_path LIKE ? ESCAPE '\\'",
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
            if get_cache().execute(
                "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?", key
            ).fetchone():
                return jsonify({"status": "done"})
            state = _indexing_state.get(key, "idle")
            if state == "done":
                _indexing_state.pop(key, None)
                state = "idle"
        return jsonify({"status": state})

    @app.route("/api/index/source-size")
    def api_index_source_size():
        row = get_cache().execute(
            "SELECT value FROM sync_meta WHERE key = 'source_size'"
        ).fetchone()
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
                chat_rows = wa_conn_bulk.execute("""
                    SELECT
                        CASE WHEN c.subject IS NOT NULL THEN CAST(c._id AS TEXT)
                             ELSE COALESCE(j.user, CAST(c._id AS TEXT))
                        END AS id,
                        CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END AS type
                    FROM chat c
                    LEFT JOIN jid j ON j._id = c.jid_row_id
                    WHERE c.hidden = 0
                """).fetchall()
                total = wa_conn_bulk.execute(
                    "SELECT COUNT(*) FROM message"
                ).fetchone()[0]
            else:
                chat_rows = wa_conn_bulk.execute("""
                    SELECT CAST(cs.Z_PK AS TEXT) AS id,
                           CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END AS type
                    FROM ZWACHATSESSION cs
                """).fetchall()
                total = wa_conn_bulk.execute(
                    "SELECT COUNT(*) FROM ZWAMESSAGE"
                ).fetchone()[0]
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
                    (row["id"], row["type"])
                ).fetchone()
                if not already:
                    _build_fts_chat(cache_bulk, source_type, wa_db_path,
                                    row["id"], row["type"], on_progress=_on_progress)

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

    # ---- static assets ------------------------------------------------------

    @app.route("/static/app.css")
    def serve_app_css():
        return send_from_directory(_CHAT_VIEWER_DIR, "app.css")

    @app.route("/static/app.js")
    def serve_app_js():
        return send_from_directory(_CHAT_VIEWER_DIR, "app.js")

    # ---- UI ----------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template_string(HTML_TEMPLATE, output_root=str(output_root), version=_get_version())

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_output_root(output_root: Path) -> None:
    hint = (
        "Run 'wab-archiver' first to create an archive, "
        "or pass a different path as the argument."
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


def main():
    args = parse_args()
    raw_path = args.output_root
    output_root = Path(raw_path).expanduser().resolve()

    validate_output_root(output_root)

    print(f"[wab_viewer] Opening archive: {output_root}")

    app = create_app(output_root, rescan=args.rescan)

    url = f"http://{args.host}:{args.port}"
    print(f"[wab_viewer] Starting server at {url}")
    print(f"[wab_viewer] Press Ctrl+C to stop")

    browser_url = f"http://127.0.0.1:{args.port}"
    webbrowser.open(browser_url)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
