#!/usr/bin/env python3
"""
wa_chat_viewer.py — Browse archived WhatsApp chats via a local Flask web UI.

Usage:
    python wa_chat_viewer.py <output_root> [--port PORT] [--host HOST] [--rescan]
    python wa_chat_viewer.py --output_root <PATH> [--port PORT] [--host HOST] [--rescan]

Dependencies:
    pip install flask
"""

from __future__ import annotations

import argparse
import mimetypes
import os
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

from chat_viewer.template import HTML_TEMPLATE

_CHAT_VIEWER_DIR = Path(__file__).parent / "chat_viewer"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Browse archived WhatsApp chats")
    p.add_argument("output_root", nargs="?", default=None,
                   help="Path to the archive output directory")
    p.add_argument("--output_root", dest="output_root_flag", default=None, metavar="PATH",
                   help="Path to the archive output directory (alternative to positional argument)")
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    p.add_argument("--rescan", action="store_true", help="Force rebuild of the FTS index")
    args = p.parse_args()
    if not args.output_root_flag and not args.output_root:
        p.error("output_root is required (positional or --output_root)")
    return args


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_cache_db_path(output_root: Path) -> Path:
    return output_root / ".wa_viewer.db"


def get_archive_db_path(output_root: Path) -> Path:
    return output_root / ".wa_media_archiver.db"


# ---------------------------------------------------------------------------
# Cache schema  (FTS index only — no full message copy)
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

# SQL expression that maps a file path (or NULL) to a media_type string.
# Used in live queries so Python's _media_type_from_path is not needed at serve time.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    msgstore = output_root / "msgstore.db"
    chat_storage = output_root / "ChatStorage.sqlite"
    if msgstore.exists():
        return ("android", str(msgstore))
    if chat_storage.exists():
        return ("ios", str(chat_storage))
    return (None, None)


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# FTS index helpers
# ---------------------------------------------------------------------------

def _stream_fts_rows(cursor, cache_conn: sqlite3.Connection, chunk_size: int = 2000):
    idx_sql = """
        INSERT INTO message_index (rowid, chat_id, chat_type, timestamp_ms)
        VALUES (?, ?, ?, ?)
    """
    fts_sql = """
        INSERT INTO message_index_fts (rowid, text_body)
        VALUES (?, ?)
    """
    idx_batch = []
    fts_batch = []
    for row in cursor:
        text_body = row["text_body"] or ""
        is_media = row["message_type"] is not None and row["message_type"] != 0 and row["media_file"] is not None
        if not text_body and not is_media:
            continue
        idx_batch.append((row["rowid"], row["chat_id"], row["chat_type"], row["timestamp_ms"]))
        if text_body:
            fts_batch.append((row["rowid"], text_body))
        if len(idx_batch) >= chunk_size:  # fts_batch <= idx_batch (media-only rows skip FTS)
            cache_conn.executemany(idx_sql, idx_batch)
            if fts_batch:
                cache_conn.executemany(fts_sql, fts_batch)
            cache_conn.commit()
            idx_batch.clear()
            fts_batch.clear()
    if idx_batch:
        cache_conn.executemany(idx_sql, idx_batch)
        if fts_batch:
            cache_conn.executemany(fts_sql, fts_batch)
        cache_conn.commit()


# ---------------------------------------------------------------------------
# Lazy per-chat FTS indexing
# ---------------------------------------------------------------------------

def _clear_fts_index(cache_conn: sqlite3.Connection):
    cache_conn.execute("DELETE FROM message_index")
    cache_conn.execute("DELETE FROM message_index_fts")
    cache_conn.execute("DELETE FROM indexed_chats")
    cache_conn.commit()


def _fts_android_chat(wa_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                      chat_id: str, chat_type: str):
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
            COALESCE(m.text_data, '')                               AS text_body,
            m.message_type,
            mm.file_path                                            AS media_file
        FROM message m
        LEFT JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
        LEFT JOIN message_media mm ON mm.message_row_id = m._id
        {where}
        ORDER BY m.timestamp ASC
    """, params)
    _stream_fts_rows(cursor, cache_conn)


def _fts_ios_chat(wa_conn: sqlite3.Connection, cache_conn: sqlite3.Connection, chat_id: str):
    cursor = wa_conn.execute("""
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
        ORDER BY m.ZMESSAGEDATE ASC
    """, (chat_id,))
    _stream_fts_rows(cursor, cache_conn)


def _build_fts_chat(cache_conn: sqlite3.Connection, source_type: str, wa_db_path: str,
                    chat_id: str, chat_type: str):
    wa_conn = sqlite3.connect(wa_db_path)
    wa_conn.row_factory = sqlite3.Row
    if source_type == "android":
        _fts_android_chat(wa_conn, cache_conn, chat_id, chat_type)
    else:
        _fts_ios_chat(wa_conn, cache_conn, chat_id)
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


# ---------------------------------------------------------------------------
# Media-only mode  (no source WA DB)
# ---------------------------------------------------------------------------

def _build_media_only(archive_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                      output_root: Path):
    """Populate message_index from archive_copies file mtimes when no WA DB is present."""
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
# Live query helpers (Android + iOS)
# ---------------------------------------------------------------------------

# Shared JID subquery for Android sender resolution via jid_map.
# At query time this references the _jid_map_resolved TEMP TABLE created in get_wa().
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

_ANDROID_SELECT = f"""
    SELECT
        m._id                                                        AS msg_id,
        {_ANDROID_CHAT_ID}                                           AS chat_id,
        {_ANDROID_CHAT_TYPE}                                         AS chat_type,
        COALESCE(m.timestamp, 0)                                     AS timestamp_ms,
        COALESCE(
            NULLIF(con_s.display_name, ''),
            CASE WHEN COALESCE(j2.user, j.user) IS NOT NULL
                 THEN '+' || COALESCE(j2.user, j.user) END,
            ''
        )                                                            AS sender,
        m.from_me,
        ac.archive_path,
        CASE
            WHEN m.message_type IS NULL OR m.message_type = 0 THEN
                CASE WHEN m.text_data IS NOT NULL
                      AND (INSTR(LOWER(m.text_data), 'http://') > 0
                           OR INSTR(LOWER(m.text_data), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col='mm.file_path')}
        END                                                          AS media_type,
        COALESCE(mm.media_name, '')                                 AS media_name,
        COALESCE(m.text_data, '')                                   AS text_body,
        COALESCE(mq.text_data, '')                                  AS quoted_text,
        CASE WHEN mq.from_me = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 CASE WHEN jq.user IS NOT NULL THEN '+' || jq.user END,
                 '') END                                             AS quoted_sender,
        COALESCE(mq.timestamp, 0)                                   AS quoted_ts
    FROM message m
    LEFT JOIN message_media mm ON mm.message_row_id = m._id
    LEFT JOIN chat c ON c._id = m.chat_row_id
    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
    LEFT JOIN jid j ON j._id = m.sender_jid_row_id
    {_ANDROID_JID_MAP}
    LEFT JOIN message_quoted mq ON mq.message_row_id = m._id
    LEFT JOIN jid jq ON jq._id = mq.sender_jid_row_id
    LEFT JOIN arch.contacts con_s  ON con_s.number  = COALESCE(j2.user, j.user)
    LEFT JOIN arch.contacts con_sq ON con_sq.number = jq.user
    LEFT JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
"""

_ANDROID_FILTER = """
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '')
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND mm.file_path IS NOT NULL)
    )
"""

_IOS_CHAT_ID = "CAST(m.ZCHATSESSION AS TEXT)"
_IOS_CHAT_TYPE = "CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END"

# Raw phone extraction from group-member JID or sender JID — NULL when empty.
_IOS_SENDER_JID = """CASE WHEN cs.ZGROUPINFO IS NOT NULL
         THEN NULLIF(SUBSTR(COALESCE(gm.ZMEMBERJID,''), 1,
                            INSTR(COALESCE(gm.ZMEMBERJID,'') || '@', '@') - 1), '')
         ELSE NULLIF(SUBSTR(COALESCE(m.ZFROMJID,''), 1,
                            INSTR(COALESCE(m.ZFROMJID,'') || '@', '@') - 1), '')
    END"""

_IOS_SELECT = f"""
    SELECT
        m.Z_PK                                                       AS msg_id,
        {_IOS_CHAT_ID}                                               AS chat_id,
        {_IOS_CHAT_TYPE}                                             AS chat_type,
        CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)         AS timestamp_ms,
        COALESCE(
            CASE WHEN ({_IOS_SENDER_JID}) = '0' THEN 'WhatsApp' END,
            NULLIF(ic_s.full_name, ''),
            NULLIF(con_s.display_name, ''),
            NULLIF(cs_lid.ZPARTNERNAME, ''),
            NULLIF(pp_lid.ZPUSHNAME, ''),
            CASE WHEN gm.ZMEMBERJID NOT LIKE '%@lid' THEN NULLIF(m.ZPUSHNAME, '') END,
            CASE WHEN SUBSTR(COALESCE(({_IOS_SENDER_JID}), ''), 1, 1) != ''
                 THEN '+' || ({_IOS_SENDER_JID})
            END,
            ''
        )                                                            AS sender,
        m.ZISFROMME                                                  AS from_me,
        ac.archive_path,
        CASE
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="('Message/' || COALESCE(mi.ZMEDIALOCALPATH,''))")}
        END                                                          AS media_type,
        COALESCE(mi.ZTITLE, '')                                     AS media_name,
        COALESCE(m.ZTEXT, '')                                       AS text_body,
        COALESCE(qm.ZTEXT, '')                                      AS quoted_text,
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
        END                                                         AS quoted_sender,
        COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0) AS quoted_ts
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
    LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = gm.ZMEMBERJID
    LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = gm.ZMEMBERJID
    LEFT JOIN _ios_contacts ic_s ON ic_s.jid = COALESCE(gm.ZMEMBERJID, m.ZFROMJID)
    LEFT JOIN ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE
    LEFT JOIN arch.contacts con_s
          ON con_s.number = ({_IOS_SENDER_JID})
    LEFT JOIN arch.contacts con_sq
          ON con_sq.number = SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                    INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
    LEFT JOIN arch.archive_copies ac
          ON ac.original_path = 'Message/' || COALESCE(mi.ZMEDIALOCALPATH, '')
"""

_IOS_FILTER = """
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '')
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND mi.ZMEDIALOCALPATH IS NOT NULL)
    )
"""

_ANDROID_TS = "COALESCE(m.timestamp, 0)"
_IOS_TS = "CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)"

# Stricter variants used only for first_ts: text arm requires message_type=0 so that
# group-creation system messages (type=7 with text_data = group name) are excluded.
_ANDROID_FILTER_TS = """
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND mm.file_path IS NOT NULL)
    )
"""
_IOS_FILTER_TS = """
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND mi.ZMEDIALOCALPATH IS NOT NULL)
    )
"""
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

# Documents that were never downloaded to the phone (no file_path / no archive_path).
# These are non-text messages whose media_type resolves to 'document' in _MEDIA_TYPE_EXPR,
# but lack a local file path so they are excluded from _ANDROID_FILTER and api_media.
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
        print(f"[wa_chat_viewer] Background indexing error for {chat_id}:\n{traceback.format_exc()}")
    finally:
        with lock:
            state[(chat_id, chat_type)] = "done"


def _parse_ios_receipt_blob(blob: bytes, conn, is_group: bool = False, msg_ts_s: int = 0) -> list:
    """Parse ZWAMESSAGEINFO.ZRECEIPTINFO protobuf into a list of member receipt dicts.

    Two blob formats exist:

    Old format (pre-2026): top-level field 3 = base_ts (Unix seconds).
      - 1-to-1: per-entry status always 0; base_ts = delivered, base_ts + max(delta) = read.
      - Group: per-entry field 4 = status (1=delivered, 2=read), field 5 = delta seconds.

    New format (2026+): no top-level field 3; field 2 entries contain field 9 sub-entries
    encoding (delta_minutes, event_type) offsets from message send time (msg_ts_s).
      - event_type 1 = delivered, 3 = read.
      - Collapse across all device entries: take the delta for each event type.
      - Group: same structure but one field-2 entry per member; field 2 sub-field = JID string.
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

    def _skip_field(data, pos, wire):
        if wire == 2:
            length, pos = _read_varint(data, pos)
            return pos + length
        if wire == 0:
            _, pos = _read_varint(data, pos)
            return pos
        if wire == 1:
            return pos + 8
        if wire == 5:
            return pos + 4
        return pos  # group wire types: cannot skip, caller should stop

    def _parse_member_entry(data):
        """Old-format entry: field1=phone bytes, field4=status, field5=delta_s."""
        pos = 0
        phone_raw = None
        status = 0
        delta = 0
        while pos < len(data):
            tag_byte, pos = _read_varint(data, pos)
            field = tag_byte >> 3
            wire = tag_byte & 0x07
            if wire == 2:
                length, pos = _read_varint(data, pos)
                value = data[pos:pos + length]
                pos += length
                if field == 1:
                    phone_raw = value
            elif wire == 0:
                value, pos = _read_varint(data, pos)
                if field == 4:
                    status = value
                elif field == 5:
                    delta = value
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        phone = ""
        if phone_raw and len(phone_raw) > 1:
            phone = "".join(f"{b:02x}" for b in phone_raw[1:])
        return phone, status, delta

    def _parse_new_entry(data):
        """New-format entry: field1=phone bytes (same encoding as old format),
        field9 sub-entries = (delta_minutes, event_type) offsets from msg_ts_s.
        Returns (phone_str, delivered_delta_s, read_delta_s) where deltas are in seconds.
        event_type 1=delivered, 3=read.
        """
        pos = 0
        phone = ""
        delivered_delta = None
        read_delta = None
        while pos < len(data):
            tag_byte, pos = _read_varint(data, pos)
            field = tag_byte >> 3
            wire = tag_byte & 0x07
            if wire == 2:
                length, pos = _read_varint(data, pos)
                value = data[pos:pos + length]
                pos += length
                if field == 1:
                    # phone bytes: hex-encode bytes after the 0x8C prefix byte,
                    # strip trailing 'f' padding nibble for odd-length numbers
                    if len(value) > 1:
                        s = "".join(f"{b:02x}" for b in value[1:])
                        phone = s[:-1] if s.endswith("f") else s
                elif field == 9:
                    # (delta_seconds, event_type) pair
                    # event_type >= 1 = delivered; event_type == 3 = read
                    try:
                        p2 = 0
                        ds, ev = 0, 0
                        while p2 < len(value):
                            tb2, p2 = _read_varint(value, p2)
                            f2, w2 = tb2 >> 3, tb2 & 0x07
                            if w2 == 0:
                                v2, p2 = _read_varint(value, p2)
                                if f2 == 1:
                                    ds = v2
                                elif f2 == 2:
                                    ev = v2
                            else:
                                p2 = _skip_field(value, p2, w2)
                        ds_s = ds * 60
                        if ev >= 1 and ev != 3 and (delivered_delta is None or ds_s < delivered_delta):
                            delivered_delta = ds_s
                        elif ev == 3 and (read_delta is None or ds_s < read_delta):
                            read_delta = ds_s
                    except Exception:
                        pass
            elif wire == 0:
                _, pos = _read_varint(data, pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        return phone, delivered_delta, read_delta

    # --- parse top-level blob ---
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

    # --- new format: no base_ts, entries use field9 delta_seconds offsets ---
    if base_ts is None and entries and msg_ts_s:
        if not is_group:
            # Collapse all device entries — take min delivered delta, min read delta
            delivered_delta = None
            read_delta = None
            for entry_bytes in entries:
                try:
                    _, dd, rd = _parse_new_entry(entry_bytes)
                    if dd is not None and (delivered_delta is None or dd < delivered_delta):
                        delivered_delta = dd
                    if rd is not None and (read_delta is None or rd < read_delta):
                        read_delta = rd
                except Exception:
                    continue
            if delivered_delta is None and read_delta is None:
                return []
            return [{
                "name": "",
                "jid": "",
                "delivered_ts": (msg_ts_s + delivered_delta) * 1000 if delivered_delta is not None else None,
                "read_ts": (msg_ts_s + read_delta) * 1000 if read_delta is not None else None,
                "played_ts": None,
            }]
        else:
            members = []
            for entry_bytes in entries:
                try:
                    phone, dd, rd = _parse_new_entry(entry_bytes)
                except Exception:
                    continue
                name = phone
                if phone:
                    # Try _ios_contacts first (covers @lid numbers from address book)
                    lid_jid = f"{phone}@lid"
                    row = conn.execute(
                        "SELECT full_name FROM _ios_contacts WHERE jid = ?", (lid_jid,)
                    ).fetchone()
                    if row and row[0]:
                        name = row[0]
                    else:
                        row = conn.execute(
                            "SELECT display_name FROM arch.contacts WHERE number = ?", (phone,)
                        ).fetchone()
                        if row and row[0]:
                            name = row[0]
                # delta=0 for delivered means the entry was written at send time as a
                # placeholder, not an actual delivery confirmation — suppress it.
                members.append({
                    "name": name or phone or "",
                    "jid": phone or "",
                    "delivered_ts": (msg_ts_s + dd) * 1000 if dd else None,
                    "read_ts": (msg_ts_s + rd) * 1000 if rd is not None else None,
                    "played_ts": None,
                })
            return members

    # --- old format ---
    if not is_group:
        # 1-to-1: per-entry status always 0; base_ts = delivered, base_ts + max(delta) = read.
        # Multiple entries when recipient uses multiple devices — collapse to one result.
        if not base_ts:
            return []
        deltas = []
        for entry_bytes in entries:
            try:
                _, _, delta = _parse_member_entry(entry_bytes)
                deltas.append(delta)
            except Exception:
                continue
        max_delta = max(deltas) if deltas else 0
        return [{
            "name": "",
            "jid": "",
            "delivered_ts": base_ts * 1000,
            "read_ts": (base_ts + max_delta) * 1000 if max_delta else None,
            "played_ts": None,
        }]

    members = []
    for entry_bytes in entries:
        try:
            phone, status, delta = _parse_member_entry(entry_bytes)
        except Exception:
            continue
        delivered_ts = None
        read_ts = None
        if base_ts and delta and status >= 1:
            delivered_ts = (base_ts + delta) * 1000
        if base_ts and delta and status >= 2:
            read_ts = (base_ts + delta) * 1000

        name = phone
        if phone:
            row = conn.execute(
                "SELECT display_name FROM arch.contacts WHERE number = ?", (phone,)
            ).fetchone()
            if row and row[0]:
                name = row[0]

        members.append({
            "name": name or phone or "",
            "jid": phone or "",
            "delivered_ts": delivered_ts,
            "read_ts": read_ts,
            "played_ts": None,
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

    if source_type is None:
        print("[wa_chat_viewer] Warning: No source WA DB found. Media-only mode.")
        archive_conn_tmp = sqlite3.connect(str(archive_db_path), check_same_thread=False)
        archive_conn_tmp.row_factory = sqlite3.Row
        _build_media_only(archive_conn_tmp, cache_conn, output_root)
        archive_conn_tmp.close()
        wa_conn = None
    else:
        print(f"[wa_chat_viewer] Source DB: {source_type} at {wa_db_path}")
        if rescan or _source_changed(cache_conn, wa_db_path):
            _clear_fts_index(cache_conn)
            _save_source_stamp(cache_conn, wa_db_path)
            print("[wa_chat_viewer] FTS cache cleared — chats will be indexed on first open")
        else:
            count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            print(f"[wa_chat_viewer] {count} chat(s) already indexed")

        _wa_local = threading.local()

    def get_wa():
        if source_type is None:
            return None
        conn = getattr(_wa_local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(wa_db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA cache_size = -32000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("ATTACH DATABASE ? AS arch", (str(archive_db_path),))
            if source_type == "android":
                conn.execute("""
                    CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved AS
                    SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                    FROM jid_map GROUP BY lid_row_id
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS _jid_map_resolved_lid ON _jid_map_resolved(lid_row_id)")
            if source_type == "ios":
                contacts_v2 = Path(wa_db_path).parent / "ContactsV2.sqlite"
                if contacts_v2.exists():
                    conn.execute("ATTACH DATABASE ? AS cv", (str(contacts_v2),))
                    conn.execute("""
                        CREATE TEMP TABLE IF NOT EXISTS _ios_contacts AS
                        SELECT ZLID AS jid, ZFULLNAME AS full_name
                            FROM cv.ZWAADDRESSBOOKCONTACT
                            WHERE ZLID IS NOT NULL AND ZFULLNAME IS NOT NULL AND ZFULLNAME != ''
                        UNION ALL
                        SELECT ZWHATSAPPID, ZFULLNAME
                            FROM cv.ZWAADDRESSBOOKCONTACT
                            WHERE ZWHATSAPPID IS NOT NULL AND ZFULLNAME IS NOT NULL AND ZFULLNAME != ''
                    """)
                else:
                    conn.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS _ios_contacts (jid TEXT, full_name TEXT)"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS _ios_contacts_jid ON _ios_contacts(jid)"
                )
            _wa_local.conn = conn
        return conn

    @app.teardown_appcontext
    def _close_wa_conn(exc):
        # Per-request g cleanup no longer needed; connections persist per thread.
        pass

    def get_cache():
        return cache_conn

    # ---- API: list chats ---------------------------------------------------

    @app.route("/api/chats")
    def api_chats():
        conn = get_wa()
        if conn is None:
            # media-only: derive from message_index
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
            rows = conn.execute("""
                SELECT
                    CASE WHEN c.subject IS NOT NULL THEN CAST(c._id AS TEXT)
                         ELSE COALESCE(j_chat_real.user, j_chat.user, CAST(c._id AS TEXT))
                    END                                                 AS id,
                    CASE WHEN c.subject IS NOT NULL THEN 'group'
                         ELSE 'contact' END                            AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
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
                    mm.file_path                                        AS last_msg_media_path
                FROM chat c
                LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                LEFT JOIN (
                    SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                    FROM jid_map GROUP BY lid_row_id
                ) jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                LEFT JOIN arch.contacts con ON con.number = COALESCE(j_chat_real.user, j_chat.user)
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(c._id AS TEXT)
                LEFT JOIN message m ON m._id = c.display_message_row_id
                LEFT JOIN message_media mm ON mm.message_row_id = m._id
                WHERE c.hidden = 0
                ORDER BY c.sort_timestamp DESC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    CAST(cs.Z_PK AS TEXT)                               AS id,
                    CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                         ELSE 'contact' END                            AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        con.folder,
                        grp.subject,
                        CASE WHEN SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                         INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1) = '0'
                             THEN 'WhatsApp' END,
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
                    COALESCE(m.ZISFROMME, 0)                           AS last_msg_from_me,
                    mi.ZMEDIALOCALPATH                                  AS last_msg_media_path
                FROM ZWACHATSESSION cs
                LEFT JOIN arch.contacts con
                      ON con.number = SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                             INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1)
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(cs.Z_PK AS TEXT)
                -- Find the newest message per session that survives _IOS_FILTER
                -- (has actual text or a media attachment). cs.ZLASTMESSAGE can point
                -- to system events (calls, encryption notices) which have no text/media
                -- and open as empty chats.
                JOIN (
                    SELECT msg.ZCHATSESSION, MAX(msg.Z_PK) AS last_pk
                    FROM ZWAMESSAGE msg
                    LEFT JOIN ZWAMEDIAITEM mi2 ON mi2.Z_PK = msg.ZMEDIAITEM
                    WHERE (msg.ZTEXT IS NOT NULL AND msg.ZTEXT != '')
                       OR (msg.ZMESSAGETYPE IS NOT NULL AND msg.ZMESSAGETYPE != 0
                           AND mi2.ZMEDIALOCALPATH IS NOT NULL)
                    GROUP BY msg.ZCHATSESSION
                ) last_real ON last_real.ZCHATSESSION = cs.Z_PK
                JOIN ZWAMESSAGE m ON m.Z_PK = last_real.last_pk
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                WHERE cs.ZHIDDEN = 0
                ORDER BY m.ZMESSAGEDATE DESC
            """).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            raw_type = d.pop("last_msg_type", 0)
            media_path = d.pop("last_msg_media_path", None)
            if raw_type != 0 and media_path:
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

        conn = get_wa()
        if conn is None:
            return jsonify([])

        if not before and not after:
            key = (chat_id, chat_type)
            already = get_cache().execute(
                "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?", key
            ).fetchone()
            if not already:
                with _indexing_lock:
                    if key not in _indexing_state:
                        _indexing_state[key] = "indexing"
                        threading.Thread(
                            target=_bg_index_chat,
                            args=(str(get_cache_db_path(output_root)), source_type,
                                  wa_db_path, chat_id, chat_type,
                                  _indexing_state, _indexing_lock),
                            daemon=True,
                        ).start()

        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER

        if source_type == "android":
            chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
            ts_col = _ANDROID_TS
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            ts_col = _IOS_TS

        if before:
            sql = f"{select} WHERE {chat_pred} AND {ts_col} < ? {extra} ORDER BY {ts_col} DESC LIMIT ?"
            rows = conn.execute(sql, chat_params + [int(before), limit]).fetchall()
        elif after:
            sql = f"{select} WHERE {chat_pred} AND {ts_col} > ? {extra} ORDER BY {ts_col} ASC LIMIT ?"
            rows = conn.execute(sql, chat_params + [int(after), limit]).fetchall()
        else:
            sql = f"{select} WHERE {chat_pred} {extra} ORDER BY {ts_col} DESC LIMIT ?"
            rows = conn.execute(sql, chat_params + [limit]).fetchall()

        return jsonify([dict(r) for r in rows])

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
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            ts_col = _IOS_TS

        before_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {ts_col} <= ? {extra} ORDER BY {ts_col} DESC LIMIT ?",
            chat_params + [ts, half]
        ).fetchall()
        after_rows = conn.execute(
            f"{select} WHERE {chat_pred} AND {ts_col} > ? {extra} ORDER BY {ts_col} ASC LIMIT ?",
            chat_params + [ts, half]
        ).fetchall()

        combined = list(reversed(before_rows)) + list(after_rows)
        return jsonify([dict(r) for r in combined])

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
            # media-only: return index rows directly (no full content)
            indexed_count = get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            return jsonify({"results": [dict(r) for r in idx_rows], "indexed_count": indexed_count})

        # fetch full message rows from live source DB by rowid
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

    # ---- API: message receipts ------------------------------------------------

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

        else:  # ios
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGEINFO'"
            ).fetchone()
            if not table_exists:
                return jsonify({"available": False})

            row = conn.execute(
                """SELECT mi.ZRECEIPTINFO,
                          cs.ZGROUPINFO IS NOT NULL AS is_group,
                          CAST(m.ZMESSAGEDATE + 978307200 AS INTEGER) AS msg_ts_s,
                          m.ZCHATSESSION AS chat_session_pk
                   FROM ZWAMESSAGEINFO mi
                   JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
                   JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
                   WHERE mi.ZMESSAGE = ?""",
                (message_id,)
            ).fetchone()
            if not row or not row[0]:
                return jsonify({"available": False})

            blob = bytes(row[0])
            members = _parse_ios_receipt_blob(
                blob, conn, is_group=bool(row[1]), msg_ts_s=row[2] or 0
            )

            # Filter out the user's own entry: blobs include a self-receipt for the
            # sender's own devices, but that JID never appears as a ZWAGROUPMEMBER.
            if bool(row[1]) and members:
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
            result = {"available": True, "members": members}
            if len(members) == 1:
                result["delivered_ts"] = members[0]["delivered_ts"]
                result["read_ts"] = members[0]["read_ts"]
            return jsonify(result)

    # ---- API: chat info -------------------------------------------------------

    def _group_members_android(conn, chat_id: str) -> list:
        """Return [{name, number}] for a group chat using group_participants if available.

        group_participants uses plain JID strings (gjid/jid), not row-id FKs.
        Rows with an empty jid are the current user's own placeholder — skipped.
        Falls through to the message-history fallback when group_participants is
        missing or returns no rows for this group's gjid.
        """
        try:
            rows = conn.execute("""
                SELECT
                    NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '') AS number,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        CASE WHEN NULLIF(SUBSTR(gp.jid, 1,
                                               INSTR(gp.jid || '@', '@') - 1), '') IS NOT NULL
                             THEN '+' || SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1)
                        END,
                        ''
                    )                                                             AS name
                FROM group_participants gp
                JOIN chat c ON c._id = CAST(? AS INTEGER)
                LEFT JOIN arch.contacts con
                       ON con.number = NULLIF(SUBSTR(gp.jid, 1,
                                                     INSTR(gp.jid || '@', '@') - 1), '')
                WHERE gp.gjid = (SELECT j.raw_string FROM jid j WHERE j._id = c.jid_row_id)
                  AND gp.jid != ''
                ORDER BY name
            """, (chat_id,)).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
        # Fallback: unique senders from message history
        rows = conn.execute(f"""
            SELECT DISTINCT
                COALESCE(j2.user, j.user)                                AS number,
                COALESCE(
                    NULLIF(con.display_name, ''),
                    CASE WHEN COALESCE(j2.user, j.user) IS NOT NULL
                         THEN '+' || COALESCE(j2.user, j.user) END,
                    ''
                )                                                        AS name
            FROM message m
            JOIN chat c ON c._id = m.chat_row_id
            LEFT JOIN jid j ON j._id = m.sender_jid_row_id
            LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
            LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
            LEFT JOIN arch.contacts con ON con.number = COALESCE(j2.user, j.user)
            WHERE m.chat_row_id = CAST(? AS INTEGER)
              AND m.from_me = 0
              AND COALESCE(j2.user, j.user) IS NOT NULL
            ORDER BY name
        """, (chat_id,)).fetchall()
        return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]

    def _group_members_ios(conn, chat_id: str) -> list:
        """Return [{name, number}] for an iOS group chat from ZWAGROUPMEMBER.

        ZWAGROUPMEMBER rows are referenced per-message via m.ZGROUPMEMBER, so we
        collect the distinct member PKs that actually appear in this session's messages.
        """
        rows = conn.execute("""
            SELECT DISTINCT
                NULLIF(SUBSTR(gm.ZMEMBERJID, 1,
                              INSTR(gm.ZMEMBERJID || '@', '@') - 1), '') AS number,
                COALESCE(
                    NULLIF(con.display_name, ''),
                    CASE WHEN NULLIF(SUBSTR(gm.ZMEMBERJID, 1,
                                           INSTR(gm.ZMEMBERJID || '@', '@') - 1), '') IS NOT NULL
                         THEN '+' || SUBSTR(gm.ZMEMBERJID, 1,
                                            INSTR(gm.ZMEMBERJID || '@', '@') - 1)
                    END,
                    gm.ZMEMBERJID
                )                                                         AS name
            FROM ZWAMESSAGE m
            JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
            LEFT JOIN arch.contacts con
                   ON con.number = NULLIF(SUBSTR(gm.ZMEMBERJID, 1,
                                                 INSTR(gm.ZMEMBERJID || '@', '@') - 1), '')
            WHERE m.ZCHATSESSION = CAST(? AS INTEGER)
              AND m.ZGROUPMEMBER IS NOT NULL
            ORDER BY name
        """, (chat_id,)).fetchall()
        return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]

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
        # last_ts from cache is fine; first_ts is recomputed from the live DB when
        # available because the cache was built with a looser filter that admits
        # group-creation system messages (type=7 with text_data = group name).
        last_ts = ts_row["last_ts"] if ts_row else None
        first_ts = None

        conn = get_wa()

        # Compute first_ts from the live WA DB using the stricter filter that
        # excludes system messages (type != 0 with text_data).
        # Also used as fallback when cache is empty (race condition on first open).
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
        members = []
        top_senders = []
        created_ts = None
        creator_number = None

        if conn is not None:
            if source_type == "android":
                chat_pred, chat_params = _android_chat_filter(chat_id, chat_type)
                extra = _ANDROID_FILTER
                select = _ANDROID_SELECT

                count_row = conn.execute(
                    f"SELECT COUNT(*) AS total,"
                    f" SUM(CASE WHEN from_me=1 THEN 1 ELSE 0 END) AS sent,"
                    f" SUM(CASE WHEN from_me=0 THEN 1 ELSE 0 END) AS received"
                    f" FROM ({select} WHERE {chat_pred} {extra})",
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
                    display_name = name_row["name"] if name_row else None
                else:
                    members = _group_members_android(conn, chat_id)

                    top_rows = conn.execute(
                        f"SELECT CASE WHEN from_me=1 THEN 'You' ELSE sender END AS sndr,"
                        f" COUNT(*) AS cnt FROM ({select} WHERE {chat_pred} {extra})"
                        f" WHERE from_me=1 OR sender != '' GROUP BY sndr ORDER BY cnt DESC LIMIT 5",
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
                        cre_row = conn.execute("""
                            SELECT c.created_timestamp,
                                   NULLIF(COALESCE(j_real.user, j.user), '') AS creator
                            FROM chat c
                            LEFT JOIN message m
                                   ON m.chat_row_id = c._id
                                  AND m.timestamp = c.created_timestamp
                                  AND m.message_type = 7
                            LEFT JOIN jid j ON j._id = m.sender_jid_row_id
                            LEFT JOIN (
                                SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
                                FROM jid_map GROUP BY lid_row_id
                            ) jm ON jm.lid_row_id = m.sender_jid_row_id
                            LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                            WHERE c._id = CAST(? AS INTEGER)
                            LIMIT 1
                        """, (chat_id,)).fetchone()
                        if cre_row:
                            created_ts = cre_row["created_timestamp"]
                            creator_number = cre_row["creator"]
                    except sqlite3.OperationalError:
                        pass

            else:  # ios
                chat_pred, chat_params = _ios_chat_filter(chat_id)
                extra = _IOS_FILTER
                select = _IOS_SELECT

                count_row = conn.execute(
                    f"SELECT COUNT(*) AS total,"
                    f" SUM(CASE WHEN from_me=1 THEN 1 ELSE 0 END) AS sent,"
                    f" SUM(CASE WHEN from_me=0 THEN 1 ELSE 0 END) AS received"
                    f" FROM ({select} WHERE {chat_pred} {extra})",
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
                                NULLIF(con.display_name,''), con.folder,
                                CASE WHEN NULLIF(SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                         INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '') = '0'
                                     THEN 'WhatsApp' END,
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
                        f" WHERE from_me=1 OR sender != '' GROUP BY sndr ORDER BY cnt DESC LIMIT 5",
                        chat_params,
                    ).fetchall()
                    top_senders = [{"name": r["sndr"], "count": r["cnt"]} for r in top_rows]

                    grp_row = conn.execute(
                        "SELECT COALESCE(NULLIF(grp.subject,''), grp.folder) AS name"
                        " FROM arch.groups grp WHERE grp.chat_row_id = ?",
                        (chat_id,),
                    ).fetchone()
                    display_name = grp_row["name"] if grp_row else None

                    cre_row = conn.execute("""
                        SELECT CAST((gi.ZCREATIONDATE + 978307200) * 1000 AS INTEGER) AS created_ms,
                               NULLIF(SUBSTR(COALESCE(gi.ZCREATORJID,''), 1,
                                      INSTR(COALESCE(gi.ZCREATORJID,'') || '@', '@') - 1), '') AS creator
                        FROM ZWACHATSESSION cs
                        JOIN ZWAGROUPINFO gi ON gi.Z_PK = cs.ZGROUPINFO
                        WHERE cs.Z_PK = CAST(? AS INTEGER)
                    """, (chat_id,)).fetchone()
                    if cre_row:
                        created_ts = cre_row["created_ms"]
                        creator_number = cre_row["creator"]

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
            result["members"] = members
            result["top_senders"] = top_senders
            result["created_ts"] = created_ts
            result["creator_number"] = creator_number
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

            paths = [
                r["archive_path"]
                for r in archive_conn.execute(
                    "SELECT archive_path FROM archive_copies WHERE archive_path LIKE ?",
                    (prefix + "%",),
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
        if get_cache().execute(
            "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?", key
        ).fetchone():
            return jsonify({"status": "done"})
        with _indexing_lock:
            status = _indexing_state.get(key, "idle")
        return jsonify({"status": status})

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

    # ---- static assets --------------------------------------------------------

    @app.route("/static/app.css")
    def serve_app_css():
        return send_from_directory(_CHAT_VIEWER_DIR, "app.css")

    @app.route("/static/app.js")
    def serve_app_js():
        return send_from_directory(_CHAT_VIEWER_DIR, "app.js")

    # ---- UI ----------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template_string(HTML_TEMPLATE, output_root=str(output_root))

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_output_root(output_root: Path) -> None:
    """Raise SystemExit with a clear message if output_root is not a valid archive folder."""
    hint = (
        "Run 'python wa_media_archiver.py' first to create an archive, "
        "or choose a different folder with --output_root."
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
    raw_path = args.output_root_flag or args.output_root
    output_root = Path(raw_path).expanduser().resolve()

    validate_output_root(output_root)

    print(f"[wa_chat_viewer] Opening archive: {output_root}")

    app = create_app(output_root, rescan=args.rescan)

    url = f"http://{args.host}:{args.port}"
    print(f"[wa_chat_viewer] Starting server at {url}")
    print(f"[wa_chat_viewer] Press Ctrl+C to stop")

    webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
