#!/usr/bin/env python3
"""
wa_chat_viewer.py — Browse archived WhatsApp chats via a local Flask web UI.

Usage:
    python wa_chat_viewer.py <output_root> [--port PORT] [--host HOST] [--rescan]

Dependencies:
    pip install flask
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sqlite3
import sys
import time
import webbrowser
from pathlib import Path

try:
    from flask import Flask, Response, jsonify, render_template_string, request
except ImportError:
    sys.exit("Flask is not installed. Run: pip install flask")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Browse archived WhatsApp chats")
    p.add_argument("output_root", help="Path to the archive output directory")
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    p.add_argument("--rescan", action="store_true", help="Force rebuild of the FTS index")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_cache_db_path(output_root: Path) -> Path:
    return output_root / ".wa_chat_viewer_cache.db"


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
"""

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
    conn.executescript(CACHE_SCHEMA)
    conn.row_factory = sqlite3.Row
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
# FTS index build
# ---------------------------------------------------------------------------

def _build_fts_index(cache_conn: sqlite3.Connection, source_type: str, wa_db_path: str):
    wa_conn = sqlite3.connect(wa_db_path)
    wa_conn.row_factory = sqlite3.Row

    cache_conn.execute("DELETE FROM message_index")
    cache_conn.execute("DELETE FROM message_index_fts")
    cache_conn.commit()

    if source_type == "android":
        _fts_android(wa_conn, cache_conn)
    else:
        _fts_ios(wa_conn, cache_conn)

    wa_conn.close()
    cache_conn.commit()
    cache_conn.execute("VACUUM")
    cache_conn.commit()

    count = cache_conn.execute("SELECT COUNT(*) FROM message_index").fetchone()[0]
    print(f"[wa_chat_viewer] FTS index built: {count} messages")


def _fts_android(wa_conn: sqlite3.Connection, cache_conn: sqlite3.Connection):
    cursor = wa_conn.execute("""
        SELECT
            m._id                                                    AS rowid,
            CASE
                WHEN c.subject IS NOT NULL THEN CAST(m.chat_row_id AS TEXT)
                ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
            END                                                      AS chat_id,
            CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
            COALESCE(m.timestamp, 0)                                AS timestamp_ms,
            COALESCE(m.text_data, '')                               AS text_body,
            m.message_type
        FROM message m
        LEFT JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
        ORDER BY m.timestamp ASC
    """)
    _stream_fts_rows(cursor, cache_conn)


def _fts_ios(wa_conn: sqlite3.Connection, cache_conn: sqlite3.Connection):
    cursor = wa_conn.execute("""
        SELECT
            m.Z_PK                                                      AS rowid,
            CAST(m.ZCHATSESSION AS TEXT)                               AS chat_id,
            CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                 ELSE 'contact' END                                     AS chat_type,
            CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)       AS timestamp_ms,
            COALESCE(m.ZTEXT, '')                                       AS text_body,
            m.ZMESSAGETYPE                                              AS message_type
        FROM ZWAMESSAGE m
        LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        ORDER BY m.ZMESSAGEDATE ASC
    """)
    _stream_fts_rows(cursor, cache_conn)


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
        is_media = row["message_type"] is not None and row["message_type"] != 0
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
            m.message_type
        FROM message m
        LEFT JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
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
            m.ZMESSAGETYPE                                              AS message_type
        FROM ZWAMESSAGE m
        LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
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

# Shared JID subquery for Android sender resolution via jid_map
_ANDROID_JID_MAP = """
    LEFT JOIN (
        SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
        FROM jid_map GROUP BY lid_row_id
    ) jm ON jm.lid_row_id = m.sender_jid_row_id
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
        {_ANDROID_CHAT_ID}                                           AS chat_id,
        {_ANDROID_CHAT_TYPE}                                         AS chat_type,
        COALESCE(m.timestamp, 0)                                     AS timestamp_ms,
        COALESCE(j2.user, j.user, '')                               AS sender,
        m.from_me,
        ac.archive_path,
        CASE
            WHEN m.message_type IS NULL OR m.message_type = 0 THEN 'text'
            ELSE {_MEDIA_TYPE_EXPR.format(col='mm.file_path')}
        END                                                          AS media_type,
        COALESCE(mm.media_name, '')                                 AS media_name,
        COALESCE(m.text_data, '')                                   AS text_body,
        COALESCE(mq.text_data, '')                                  AS quoted_text,
        CASE WHEN mq.from_me = 1 THEN 'You'
             ELSE COALESCE(jq.user, '') END                         AS quoted_sender,
        COALESCE(mq.timestamp, 0)                                   AS quoted_ts
    FROM message m
    LEFT JOIN message_media mm ON mm.message_row_id = m._id
    LEFT JOIN chat c ON c._id = m.chat_row_id
    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
    LEFT JOIN jid j ON j._id = m.sender_jid_row_id
    {_ANDROID_JID_MAP}
    LEFT JOIN message_quoted mq ON mq.message_row_id = m._id
    LEFT JOIN jid jq ON jq._id = mq.sender_jid_row_id
    LEFT JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
"""

_ANDROID_FILTER = """
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '')
        OR (m.message_type IS NOT NULL AND m.message_type != 0)
    )
"""

_IOS_CHAT_ID = "CAST(m.ZCHATSESSION AS TEXT)"
_IOS_CHAT_TYPE = "CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END"

_IOS_SELECT = f"""
    SELECT
        {_IOS_CHAT_ID}                                               AS chat_id,
        {_IOS_CHAT_TYPE}                                             AS chat_type,
        CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)         AS timestamp_ms,
        SUBSTR(COALESCE(m.ZFROMJID,''), 1,
               INSTR(COALESCE(m.ZFROMJID,'') || '@', '@') - 1)      AS sender,
        m.ZISFROMME                                                  AS from_me,
        ac.archive_path,
        CASE
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN 'text'
            ELSE {_MEDIA_TYPE_EXPR.format(col="('Message/' || COALESCE(mi.ZMEDIALOCALPATH,''))")}
        END                                                          AS media_type,
        COALESCE(mi.ZTITLE, '')                                     AS media_name,
        COALESCE(m.ZTEXT, '')                                       AS text_body,
        COALESCE(qm.ZTEXT, '')                                      AS quoted_text,
        CASE WHEN qm.ZISFROMME = 1 THEN 'You'
             ELSE SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                  INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
        END                                                         AS quoted_sender,
        COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0) AS quoted_ts
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
    LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE
    LEFT JOIN arch.archive_copies ac
          ON ac.original_path = 'Message/' || COALESCE(mi.ZMEDIALOCALPATH, '')
"""

_IOS_FILTER = """
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '')
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0)
    )
"""


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app(output_root: Path, rescan: bool = False):
    app = Flask(__name__)

    archive_db_path = get_archive_db_path(output_root)
    source_type, wa_db_path = _detect_source_db(output_root)
    cache_conn = _open_cache_db(output_root)

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

        wa_conn = sqlite3.connect(wa_db_path, check_same_thread=False)
        wa_conn.row_factory = sqlite3.Row
        wa_conn.execute("ATTACH DATABASE ? AS arch", (str(archive_db_path),))

    def get_wa():
        return wa_conn

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
                       COUNT(*) AS msg_count,
                       MAX(NULLIF(timestamp_ms, 0)) AS newest_ts,
                       MIN(NULLIF(timestamp_ms, 0)) AS oldest_ts
                FROM message_index
                GROUP BY chat_id, chat_type
                ORDER BY type, newest_ts DESC
            """).fetchall()
            return jsonify([{
                "id": r["id"], "type": r["type"],
                "display_name": r["id"],
                "msg_count": r["msg_count"],
                "oldest_ts": r["oldest_ts"], "newest_ts": r["newest_ts"],
            } for r in rows])

        if source_type == "android":
            rows = conn.execute(f"""
                SELECT
                    {_ANDROID_CHAT_ID}                                  AS id,
                    {_ANDROID_CHAT_TYPE}                                AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        con.folder,
                        grp.subject,
                        COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
                    )                                                   AS display_name,
                    COUNT(*)                                            AS msg_count,
                    MAX(NULLIF(COALESCE(m.timestamp, 0), 0))           AS newest_ts,
                    MIN(NULLIF(COALESCE(m.timestamp, 0), 0))           AS oldest_ts
                FROM message m
                LEFT JOIN chat c ON c._id = m.chat_row_id
                LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                LEFT JOIN arch.contacts con ON con.number = j_chat.user
                LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(m.chat_row_id AS TEXT)
                WHERE (
                    (m.text_data IS NOT NULL AND m.text_data != '')
                    OR (m.message_type IS NOT NULL AND m.message_type != 0)
                )
                GROUP BY {_ANDROID_CHAT_ID}, {_ANDROID_CHAT_TYPE}
                ORDER BY {_ANDROID_CHAT_TYPE}, newest_ts DESC
            """).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT
                    {_IOS_CHAT_ID}                                      AS id,
                    {_IOS_CHAT_TYPE}                                    AS type,
                    COALESCE(
                        NULLIF(con.display_name, ''),
                        con.folder,
                        cs.ZGROUPINFO,
                        CAST(m.ZCHATSESSION AS TEXT)
                    )                                                   AS display_name,
                    COUNT(*)                                            AS msg_count,
                    MAX(NULLIF(CAST((m.ZMESSAGEDATE+978307200)*1000 AS INTEGER), 0)) AS newest_ts,
                    MIN(NULLIF(CAST((m.ZMESSAGEDATE+978307200)*1000 AS INTEGER), 0)) AS oldest_ts
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
                LEFT JOIN arch.contacts con ON con.number =
                    SUBSTR(COALESCE(m.ZFROMJID,''), 1,
                           INSTR(COALESCE(m.ZFROMJID,'') || '@', '@') - 1)
                WHERE (
                    (m.ZTEXT IS NOT NULL AND m.ZTEXT != '')
                    OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0)
                )
                GROUP BY {_IOS_CHAT_ID}, {_IOS_CHAT_TYPE}
                ORDER BY {_IOS_CHAT_TYPE}, newest_ts DESC
            """).fetchall()

        return jsonify([dict(r) for r in rows])

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
            _ensure_chat_indexed(get_cache(), source_type, wa_db_path, chat_id, chat_type)

        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER

        if before:
            sql = f"{select} WHERE chat_id = ? AND chat_type = ? AND timestamp_ms < ? {extra} ORDER BY timestamp_ms DESC LIMIT ?"
            rows = conn.execute(sql, (chat_id, chat_type, int(before), limit)).fetchall()
        elif after:
            sql = f"{select} WHERE chat_id = ? AND chat_type = ? AND timestamp_ms > ? {extra} ORDER BY timestamp_ms ASC LIMIT ?"
            rows = conn.execute(sql, (chat_id, chat_type, int(after), limit)).fetchall()
        else:
            sql = f"{select} WHERE chat_id = ? AND chat_type = ? {extra} ORDER BY timestamp_ms DESC LIMIT ?"
            rows = conn.execute(sql, (chat_id, chat_type, limit)).fetchall()

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

        before_rows = conn.execute(
            f"{select} WHERE chat_id = ? AND chat_type = ? AND timestamp_ms <= ? {extra} ORDER BY timestamp_ms DESC LIMIT ?",
            (chat_id, chat_type, ts, half)
        ).fetchall()
        after_rows = conn.execute(
            f"{select} WHERE chat_id = ? AND chat_type = ? AND timestamp_ms > ? {extra} ORDER BY timestamp_ms ASC LIMIT ?",
            (chat_id, chat_type, ts, half)
        ).fetchall()

        combined = list(reversed(before_rows)) + list(after_rows)
        return jsonify([dict(r) for r in combined])

    # ---- API: media gallery ------------------------------------------------

    @app.route("/api/media")
    def api_media():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        conn = get_wa()
        if conn is None:
            return jsonify([])
        select = _ANDROID_SELECT if source_type == "android" else _IOS_SELECT
        extra = _ANDROID_FILTER if source_type == "android" else _IOS_FILTER
        sql = (
            f"{select} WHERE chat_id = ? AND chat_type = ? {extra}"
            " AND media_type != 'text' AND archive_path IS NOT NULL"
            " ORDER BY timestamp_ms DESC"
        )
        rows = conn.execute(sql, (chat_id, chat_type)).fetchall()
        return jsonify([dict(r) for r in rows])

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

    # ---- API: media file serving -------------------------------------------

    @app.route("/media/<path:p>")
    def serve_media(p):
        safe = get_output_root() / p
        try:
            safe = safe.resolve()
            root = get_output_root().resolve()
        except OSError:
            return "Invalid path", 400
        if not str(safe).startswith(str(root)):
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
            data = safe.read_bytes()[start:end + 1]
            resp = Response(data, 206)
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Content-Length"] = length
            resp.headers["Content-Type"] = content_type or "application/octet-stream"
            return resp
        else:
            data = safe.read_bytes()
            resp = Response(data, 200)
            resp.headers["Content-Length"] = file_size
            resp.headers["Content-Type"] = content_type or "application/octet-stream"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

    # ---- UI ----------------------------------------------------------------

    def get_output_root():
        return output_root

    @app.route("/")
    def index():
        return render_template_string(HTML_TEMPLATE, output_root=str(output_root))

    return app


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WA Chat Viewer</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg: #111b21;
      --surface: #1f2c33;
      --surface2: #2a3942;
      --bubble-in: #d9fdd3;
      --bubble-out: #dcf8c6;
      --bubble-in-text: #111b21;
      --bubble-out-text: #111b21;
      --text: #e9edef;
      --text-muted: #8696a0;
      --accent: #00a884;
      --border: #222d34;
      --sidebar-w: 300px;
    }

    html, body { height: 100%; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

    #app { display: flex; flex-direction: column; height: 100vh; }

    #header {
      background: var(--surface);
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }

    #header h1 { font-size: 16px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #header .subtitle { font-size: 11px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    #body { display: flex; flex: 1; overflow: hidden; }

    #sidebar {
      width: var(--sidebar-w);
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      overflow: hidden;
    }

    #search-box { padding: 10px; border-bottom: 1px solid var(--border); }
    #search-input {
      width: 100%;
      background: var(--surface2);
      border: none;
      border-radius: 8px;
      padding: 8px 12px;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }
    #search-input:focus { box-shadow: 0 0 0 2px var(--accent); }

    #chat-list { flex: 1; overflow-y: auto; }

    .section-label {
      padding: 8px 16px 4px;
      font-size: 11px;
      font-weight: 600;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .chat-item {
      padding: 12px 16px;
      cursor: pointer;
      border-bottom: 1px solid var(--border);
      transition: background 0.15s;
    }
    .chat-item:hover { background: var(--surface2); }
    .chat-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }
    .chat-item .chat-name { font-size: 14px; font-weight: 500; margin-bottom: 2px; }
    .chat-item .chat-meta { font-size: 12px; color: var(--text-muted); }
    .chat-item.group .chat-name::before { content: '👥 '; }

    #search-results { display: none; }
    #search-results.has-results { display: block; }
    #search-results-header { padding: 8px 16px 4px; font-size: 11px; color: var(--text-muted); }

    #chat-pane {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    #chat-header {
      background: var(--surface);
      padding: 8px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
      flex-wrap: wrap;
    }
    #chat-header h2 { font-size: 15px; font-weight: 600; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    #chat-toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }

    #toolbar-toggle {
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 18px;
      padding: 4px 6px;
      border-radius: 4px;
      line-height: 1;
    }
    #toolbar-toggle:hover { background: var(--surface2); color: var(--text); }

    #toolbar-expanded {
      display: none;
      align-items: center;
      gap: 8px;
    }
    #toolbar-expanded.open { display: flex; }

    #chat-search-input {
      background: var(--surface2);
      border: none;
      border-radius: 6px;
      padding: 5px 10px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      width: 180px;
    }
    #chat-search-input:focus { box-shadow: 0 0 0 2px var(--accent); }

    #chat-search-nav { display: flex; gap: 2px; align-items: center; }
    #chat-search-count { font-size: 12px; color: var(--text-muted); min-width: 50px; text-align: center; }
    .nav-btn {
      background: var(--surface2);
      border: none;
      border-radius: 4px;
      color: var(--text);
      cursor: pointer;
      padding: 4px 8px;
      font-size: 14px;
      line-height: 1;
    }
    .nav-btn:hover { background: var(--accent); color: #fff; }
    .nav-btn:disabled { opacity: 0.3; cursor: default; }

    #date-picker-input {
      background: var(--surface2);
      border: none;
      border-radius: 6px;
      padding: 5px 8px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      color-scheme: dark;
    }
    #date-picker-input:focus { box-shadow: 0 0 0 2px var(--accent); }

    #message-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .msg-row {
      display: flex;
      flex-direction: column;
      margin-bottom: 2px;
    }
    .msg-row.sent { align-items: flex-end; }
    .msg-row.recv { align-items: flex-start; }

    .msg-bubble {
      max-width: 70%;
      padding: 6px 10px 8px;
      border-radius: 7px;
      position: relative;
    }
    .msg-row.sent .msg-bubble {
      background: var(--bubble-out);
      color: var(--bubble-out-text);
      border-bottom-right-radius: 2px;
    }
    .msg-row.recv .msg-bubble {
      background: var(--bubble-in);
      color: var(--bubble-in-text);
      border-bottom-left-radius: 2px;
    }

    .msg-sender { font-size: 11px; font-weight: 600; color: var(--accent); margin-bottom: 2px; }
    .msg-text { font-size: 14px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
    .msg-unavailable { color: var(--text-muted); font-style: italic; }
    .msg-caption { font-size: 14px; line-height: 1.4; margin-top: 4px; }
    .msg-meta { font-size: 10px; color: var(--text-muted); text-align: right; margin-top: 2px; }

    .msg-row.sent .msg-meta { color: rgba(17,27,33,0.5); }
    .msg-row.recv .msg-meta { color: rgba(17,27,33,0.5); }

    .msg-media { border-radius: 4px; overflow: hidden; max-width: 100%; }
    .msg-media img, .msg-media video { display: block; max-width: 320px; max-height: 300px; width: 100%; height: auto; cursor: pointer; }
    .msg-media audio { display: block; max-width: 300px; width: 100%; margin: 4px 0; }
    .msg-media.sticker img { max-width: 180px; max-height: 180px; }

    .doc-link { display: flex; align-items: center; gap: 8px; padding: 4px; font-size: 13px; text-decoration: none; }
    .doc-link .doc-icon { font-size: 20px; }

    #empty-pane {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--text-muted);
      font-size: 15px;
    }

    .spinner {
      display: flex; justify-content: center; padding: 16px;
      color: var(--text-muted); font-size: 13px;
    }

    .img-lightbox {
      position: fixed; inset: 0; background: rgba(0,0,0,0.9);
      display: flex; align-items: center; justify-content: center;
      z-index: 1000; cursor: zoom-out;
    }
    .img-lightbox img { max-width: 90vw; max-height: 90vh; object-fit: contain; }
    .img-lightbox video { max-width: 90vw; max-height: 90vh; }

    #media-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      font-size: 16px;
      padding: 4px 6px;
      border-radius: 4px;
      line-height: 1;
    }
    #media-btn:hover { background: var(--surface2); color: var(--text); }

    #media-gallery {
      position: fixed; inset: 0; background: var(--bg); z-index: 900;
      display: none; flex-direction: column;
    }
    #media-gallery.open { display: flex; }
    #media-gallery-header {
      display: flex; align-items: center; padding: 10px 16px;
      background: var(--surface); border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    #media-gallery-title { flex: 1; font-weight: 600; font-size: 15px; }
    #media-gallery-close {
      background: none; border: none; color: var(--text-secondary);
      font-size: 18px; cursor: pointer; padding: 4px 6px; border-radius: 4px;
    }
    #media-gallery-close:hover { background: var(--surface2); }
    #media-gallery-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 4px; overflow-y: auto; padding: 8px;
    }
    .gallery-item {
      position: relative; aspect-ratio: 1/1; cursor: pointer;
      background: var(--surface); overflow: hidden; border-radius: 4px;
    }
    .gallery-item img, .gallery-item video {
      width: 100%; height: 100%; object-fit: cover; display: block;
    }
    .gallery-item .gallery-doc {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; height: 100%; font-size: 12px;
      color: var(--text-secondary); padding: 4px; text-align: center;
      word-break: break-all;
    }
    .gallery-goto {
      position: absolute; bottom: 4px; right: 4px;
      background: rgba(0,0,0,0.6); color: #fff; border: none;
      border-radius: 4px; font-size: 11px; padding: 2px 5px; cursor: pointer;
      opacity: 0; transition: opacity 0.15s;
    }
    .gallery-item:hover .gallery-goto { opacity: 1; }

    .search-result-item {
      padding: 10px 16px;
      cursor: pointer;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .search-result-item:hover { background: var(--surface2); }
    .search-result-item .sr-chat { font-weight: 600; font-size: 12px; margin-bottom: 2px; }
    .search-result-item .sr-text { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .search-result-item .sr-time { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
    mark { background: #ffe082; color: #111; border-radius: 2px; padding: 0 1px; }

    .sr-section-label {
      padding: 6px 16px 4px;
      font-size: 10px;
      font-weight: 700;
      color: var(--accent);
      text-transform: uppercase;
      letter-spacing: 0.6px;
      border-top: 1px solid var(--border);
    }
    .sr-section-label:first-child { border-top: none; }

    .search-index-notice {
      padding: 8px 16px;
      font-size: 11px;
      color: var(--text-muted);
      border-top: 1px solid var(--border);
      font-style: italic;
    }

    .direction-badge {
      display: inline-block;
      font-size: 10px;
      padding: 1px 4px;
      border-radius: 3px;
      margin-right: 4px;
      vertical-align: middle;
    }
    .direction-badge.sent { background: var(--accent); color: #fff; }

    .date-separator {
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 8px 0;
    }
    .date-separator span {
      background: var(--surface2);
      color: var(--text-muted);
      font-size: 11px;
      padding: 3px 10px;
      border-radius: 8px;
    }

    .load-spinner {
      display: flex;
      justify-content: center;
      padding: 10px;
      color: var(--text-muted);
      font-size: 20px;
    }

    .msg-quote {
      background: rgba(0,0,0,0.08);
      border-left: 3px solid var(--accent);
      border-radius: 4px;
      padding: 4px 8px;
      margin-bottom: 4px;
      max-width: 100%;
      overflow: hidden;
    }
    .msg-row.sent .msg-quote { background: rgba(0,0,0,0.1); }
    .msg-quote-sender { font-size: 11px; font-weight: 600; color: var(--accent); margin-bottom: 1px; }
    .msg-quote-text { font-size: 12px; color: rgba(17,27,33,0.75); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .msg-quote[style*="cursor: pointer"]:hover { background: rgba(0,0,0,0.15); }

    .msg-bubble.search-highlight { outline: 2px solid var(--accent); }
  </style>
</head>
<body>
<div id="app">
  <div id="header">
    <h1>WA Chat Viewer</h1>
    <span class="subtitle">{{ output_root }}</span>
  </div>

  <div id="body">
    <div id="sidebar">
      <div id="search-box">
        <input id="search-input" type="search" placeholder="Search messages…" autocomplete="off">
      </div>
      <div id="search-results"></div>
      <div id="chat-list"></div>
    </div>

    <div id="chat-pane">
      <div id="chat-header" style="display:none;">
        <h2 id="chat-title"></h2>
        <div id="chat-toolbar">
          <button id="media-btn" title="Media">&#128247;</button>
          <button id="toolbar-toggle" title="Search &amp; date">&#128269;</button>
          <div id="toolbar-expanded">
            <input id="chat-search-input" type="search" placeholder="Find in chat…" autocomplete="off">
            <div id="chat-search-nav" style="display:none;">
              <button class="nav-btn" id="search-prev" title="Previous">&#8679;</button>
              <span id="chat-search-count"></span>
              <button class="nav-btn" id="search-next" title="Next">&#8681;</button>
            </div>
            <input id="date-picker-input" type="date" title="Jump to date">
          </div>
        </div>
      </div>
      <div id="message-scroll"></div>
      <div id="empty-pane">Select a chat to browse messages</div>
    </div>
  </div>
</div>

<div id="media-gallery">
  <div id="media-gallery-header">
    <span id="media-gallery-title">Media</span>
    <button id="media-gallery-close" title="Close">&#10005;</button>
  </div>
  <div id="media-gallery-grid"></div>
</div>

<script>
(function () {
  'use strict';

  const outputRoot = {{ output_root | tojson }};
  let allChats = [];
  let currentChat = null;
  let msgList = [];
  let domNodes = 0;
  const MAX_DOM = 100;

  // ---- util ----------------------------------------------------------------

  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function fmtDateSep(ts) {
    const d = new Date(ts);
    return d.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' });
  }

  function dayKey(ts) {
    const d = new Date(ts);
    return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function highlight(text, q) {
    if (!q || !text) return esc(text || '');
    const words = q.trim().split(/\s+/);
    let result = esc(text);
    for (const w of words) {
      const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
      result = result.replace(re, m => '<mark>' + m + '</mark>');
    }
    return result;
  }

  function makeDateSeparator(ts) {
    const el = document.createElement('div');
    el.className = 'date-separator';
    el.dataset.dayKey = dayKey(ts);
    el.innerHTML = `<span>${fmtDateSep(ts)}</span>`;
    return el;
  }

  // ---- load chats ----------------------------------------------------------

  async function loadChats() {
    const res = await fetch('/api/chats');
    allChats = await res.json();
    renderChatList();
  }

  function renderChatList() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const contacts = allChats.filter(c => c.type === 'contact');
    const groups = allChats.filter(c => c.type === 'group');

    function addSection(label, items) {
      if (!items.length) return;
      const sec = document.createElement('div');
      sec.className = 'section-label';
      sec.textContent = label;
      list.appendChild(sec);
      items.forEach(chat => {
        const el = document.createElement('div');
        el.className = 'chat-item' + (chat.type === 'group' ? ' group' : '');
        el.dataset.id = chat.id;
        el.dataset.type = chat.type;
        el.innerHTML = `<div class="chat-name">${esc(chat.display_name)}</div><div class="chat-meta">${chat.msg_count} messages · ${fmtTime(chat.newest_ts)}</div>`;
        el.addEventListener('click', () => selectChat(chat, el));
        list.appendChild(el);
      });
    }

    addSection('Contacts', contacts);
    addSection('Groups', groups);
  }

  // ---- select chat ---------------------------------------------------------

  async function selectChat(chat, el) {
    document.querySelectorAll('.chat-item').forEach(e => e.classList.remove('active'));
    el.classList.add('active');
    currentChat = chat;
    msgList = [];

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    domNodes = 0;

    document.getElementById('chat-header').style.display = '';
    document.getElementById('chat-title').textContent = chat.display_name;
    document.getElementById('empty-pane').style.display = 'none';
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';

    // reset in-chat search when switching chats
    document.getElementById('chat-search-input').value = '';
    clearChatSearch();

    await loadMessages('older');
    scroll.scrollTop = scroll.scrollHeight;
  }

  // ---- load messages -------------------------------------------------------

  let loading = false;

  function showSpinner(position) {
    const el = document.createElement('div');
    el.className = 'load-spinner';
    el.id = 'load-spinner-' + position;
    el.textContent = '⟳';
    const scroll = document.getElementById('message-scroll');
    if (position === 'top') scroll.insertBefore(el, scroll.firstChild);
    else scroll.appendChild(el);
    return el;
  }

  function removeSpinner(position) {
    const el = document.getElementById('load-spinner-' + position);
    if (el) el.remove();
  }

  async function loadMessages(direction) {
    if (loading) return;
    loading = true;

    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      limit: 50
    });

    if (direction === 'older' && msgList.length > 0) {
      params.set('before', msgList[0].timestamp_ms);
    } else if (direction === 'newer' && msgList.length > 0) {
      params.set('after', msgList[msgList.length - 1].timestamp_ms);
    }

    const spinner = showSpinner(direction === 'older' ? 'top' : 'bottom');

    try {
      const res = await fetch('/api/messages?' + params);
      const msgs = await res.json();
      removeSpinner(direction === 'older' ? 'top' : 'bottom');

      if (!msgs.length) { loading = false; return; }

      const scroll = document.getElementById('message-scroll');

      if (direction === 'older') {
        // API returns DESC; reverse to get chronological order for prepending
        const ordered = msgs.slice().reverse();
        // The message currently at the top of our list is the boundary for date seps
        const firstExistingTs = msgList.length > 0 ? msgList[0].timestamp_ms : null;

        // Prepend into msgList
        for (let i = ordered.length - 1; i >= 0; i--) {
          msgList.unshift(ordered[i]);
        }

        const prevHeight = scroll.scrollHeight;
        const prevTop = scroll.scrollTop;

        // Insert into DOM oldest-first (each goes before the current firstChild)
        // so final order is oldest-at-top. For date sep: compare each msg with
        // the one that comes after it in the DOM (i.e. ordered[i+1] or firstExistingTs).
        for (let i = ordered.length - 1; i >= 0; i--) {
          const m = ordered[i];
          const nextTs = i < ordered.length - 1 ? ordered[i + 1].timestamp_ms : firstExistingTs;
          const nodes = renderBubbleWithSep(m, nextTs, 'before');
          nodes.forEach(node => scroll.insertBefore(node, scroll.firstChild));
        }

        domNodes += ordered.length;
        scroll.scrollTop = prevTop + (scroll.scrollHeight - prevHeight);
        pruneDom('top');
      } else {
        const prevLastTs = msgList.length > 0 ? msgList[msgList.length - 1].timestamp_ms : null;
        msgs.forEach(m => msgList.push(m));
        msgs.forEach((m, i) => {
          const prevTs = i === 0 ? prevLastTs : msgs[i - 1].timestamp_ms;
          renderBubbleWithSep(m, prevTs, 'after').forEach(node => scroll.appendChild(node));
        });
        domNodes += msgs.length;
        pruneDom('bottom');
      }

    } catch (e) {
      removeSpinner(direction === 'older' ? 'top' : 'bottom');
    }
    loading = false;
  }

  function renderBubbleWithSep(msg, prevTs, direction) {
    const nodes = [];
    const needsSep = prevTs === null || dayKey(msg.timestamp_ms) !== dayKey(prevTs);
    if (needsSep && direction === 'after') nodes.push(makeDateSeparator(msg.timestamp_ms));
    nodes.push(renderBubble(msg));
    // 'before': separator goes after the bubble so insertBefore puts it above the next row;
    // label uses prevTs (= the chronologically newer neighbour, start of that day)
    if (needsSep && direction === 'before' && prevTs !== null) nodes.push(makeDateSeparator(prevTs));
    return nodes;
  }

  // ---- append / prepend with pruning ---------------------------------------

  function pruneDom(keepEnd) {
    const scroll = document.getElementById('message-scroll');
    while (domNodes > MAX_DOM && scroll.children.length > 0) {
      const child = keepEnd === 'top' ? scroll.lastChild : scroll.firstChild;
      if (!child) break;
      scroll.removeChild(child);
      if (child.classList && child.classList.contains('msg-row')) domNodes--;
    }
    // Re-sync msgList boundaries from surviving DOM rows
    const rows = scroll.querySelectorAll('.msg-row[data-ts]');
    if (rows.length === 0) { msgList = []; return; }
    const minTs = parseInt(rows[0].dataset.ts);
    const maxTs = parseInt(rows[rows.length - 1].dataset.ts);
    msgList = msgList.filter(m => m.timestamp_ms >= minTs && m.timestamp_ms <= maxTs);
  }

  // ---- scroll trigger -------------------------------------------------------

  function setupScrollTrigger() {
    const scroll = document.getElementById('message-scroll');
    scroll.addEventListener('scroll', function () {
      if (loading) return;
      if (scroll.scrollTop < 100) {
        loadMessages('older');
      } else if (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100) {
        loadMessages('newer');
      }
    });
  }
  setupScrollTrigger();

  function renderBubble(msg) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');
    row.dataset.ts = msg.timestamp_ms;

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    const senderEl = document.createElement('div');
    senderEl.className = 'msg-sender';
    senderEl.textContent = msg.sender || '';
    if (msg.sender) bubble.appendChild(senderEl);

    if (msg.quoted_text) {
      const quote = document.createElement('div');
      quote.className = 'msg-quote';
      const qSender = document.createElement('div');
      qSender.className = 'msg-quote-sender';
      const qs = msg.quoted_sender;
      qSender.textContent = qs || (currentChat && currentChat.type === 'contact' ? currentChat.display_name : '');
      const qText = document.createElement('div');
      qText.className = 'msg-quote-text';
      qText.textContent = msg.quoted_text;
      quote.appendChild(qSender);
      quote.appendChild(qText);
      if (msg.quoted_ts) {
        quote.style.cursor = 'pointer';
        quote.title = 'Jump to original message';
        quote.addEventListener('click', () => jumpToTimestamp(msg.quoted_ts));
      }
      bubble.appendChild(quote);
    }

    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    if (msg.from_me) {
      const badge = document.createElement('span');
      badge.className = 'direction-badge sent';
      badge.textContent = 'You';
      meta.appendChild(badge);
    }
    meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));

    if (msg.media_type === 'text' || !msg.archive_path) {
      if (!msg.archive_path && msg.media_type !== 'text') {
        const txt = document.createElement('div');
        txt.className = 'msg-text msg-unavailable';
        const icon = msg.media_type === 'image' ? '🖼️' : msg.media_type === 'video' ? '🎥' :
                     msg.media_type === 'audio' ? '🎵' : msg.media_type === 'sticker' ? '🩹' :
                     msg.media_type === 'gif' ? '🎞️' : '📄';
        txt.textContent = icon + ' ' + (msg.text_body || 'Media not available');
        bubble.appendChild(txt);
      } else {
        const txt = document.createElement('div');
        txt.className = 'msg-text';
        txt.innerHTML = highlight(msg.text_body, '');
        bubble.appendChild(txt);
      }
    } else {
      const mediaEl = renderMedia(msg);
      bubble.appendChild(mediaEl);
      if (msg.text_body) {
        const cap = document.createElement('div');
        cap.className = 'msg-caption';
        cap.innerHTML = highlight(msg.text_body, '');
        bubble.appendChild(cap);
      }
    }

    bubble.appendChild(meta);
    row.appendChild(bubble);
    return row;
  }

  function renderMedia(msg) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-media';
    const path = msg.archive_path;
    const src = '/media/' + path;
    const mt = msg.media_type;

    if (mt === 'image' || mt === 'gif' || mt === 'sticker') {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = msg.media_name || 'image';
      img.addEventListener('click', () => showLightbox(src, mt));
      wrap.appendChild(img);
    } else if (mt === 'video') {
      const vid = document.createElement('video');
      vid.controls = true;
      vid.preload = 'none';
      vid.src = src;
      wrap.appendChild(vid);
    } else if (mt === 'audio') {
      const aud = document.createElement('audio');
      aud.controls = true;
      aud.src = src;
      wrap.appendChild(aud);
    } else {
      const a = document.createElement('a');
      a.href = src;
      a.className = 'doc-link';
      a.download = msg.media_name || 'file';
      a.innerHTML = `<span class="doc-icon">📄</span><span>${esc(msg.media_name || 'Download')}</span>`;
      wrap.appendChild(a);
    }
    return wrap;
  }

  function showLightbox(src, mt) {
    const lb = document.createElement('div');
    lb.className = 'img-lightbox';
    let media;
    if (mt === 'video') {
      media = document.createElement('video');
      media.controls = true;
      media.autoplay = true;
      media.src = src;
    } else {
      media = document.createElement('img');
      media.src = src;
    }
    lb.appendChild(media);
    lb.addEventListener('click', e => { if (e.target === lb) lb.remove(); });
    document.body.appendChild(lb);
  }

  // ---- media gallery -------------------------------------------------------

  function closeMediaGallery() {
    document.getElementById('media-gallery').classList.remove('open');
  }

  function renderGalleryItem(msg) {
    const cell = document.createElement('div');
    cell.className = 'gallery-item';
    const src = '/media/' + msg.archive_path;
    const mt = msg.media_type;

    if (mt === 'image' || mt === 'gif' || mt === 'sticker') {
      const img = document.createElement('img');
      img.src = src;
      img.loading = 'lazy';
      img.alt = msg.media_name || '';
      img.addEventListener('click', () => showLightbox(src, mt));
      cell.appendChild(img);
    } else if (mt === 'video') {
      const vid = document.createElement('video');
      vid.src = src;
      vid.preload = 'none';
      vid.addEventListener('click', () => showLightbox(src, mt));
      cell.appendChild(vid);
    } else if (mt === 'audio') {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">🎵</span><span>' + esc(msg.media_name || 'audio') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    } else {
      const d = document.createElement('div');
      d.className = 'gallery-doc';
      d.innerHTML = '<span style="font-size:28px">📄</span><span>' + esc(msg.media_name || 'file') + '</span>';
      d.addEventListener('click', () => window.open(src, '_blank'));
      cell.appendChild(d);
    }

    const btn = document.createElement('button');
    btn.className = 'gallery-goto';
    btn.title = 'Go to message';
    btn.textContent = '→ in chat';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      closeMediaGallery();
      jumpToTimestamp(msg.timestamp_ms);
    });
    cell.appendChild(btn);
    return cell;
  }

  async function openMediaGallery() {
    if (!currentChat) return;
    const grid = document.getElementById('media-gallery-grid');
    grid.innerHTML = '<div style="color:var(--text-secondary);padding:16px">Loading…</div>';
    document.getElementById('media-gallery').classList.add('open');

    const r = await fetch(
      '/api/media?chat_id=' + encodeURIComponent(currentChat.id) +
      '&chat_type=' + encodeURIComponent(currentChat.type)
    );
    const items = await r.json();
    grid.innerHTML = '';
    if (!items.length) {
      grid.innerHTML = '<div style="color:var(--text-secondary);padding:16px">No archived media in this chat.</div>';
      return;
    }
    items.forEach(msg => grid.appendChild(renderGalleryItem(msg)));
  }

  document.getElementById('media-btn').addEventListener('click', openMediaGallery);
  document.getElementById('media-gallery-close').addEventListener('click', closeMediaGallery);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeMediaGallery();
  });

  // ---- sidebar search -------------------------------------------------------

  let searchTimer = null;

  document.getElementById('search-input').addEventListener('input', function () {
    clearTimeout(searchTimer);
    const q = this.value.trim();
    if (!q) {
      clearSearchResults();
      return;
    }
    searchTimer = setTimeout(() => doSearch(q), 300);
  });

  async function doSearch(q) {
    // Contact name matches — client-side, instant
    const ql = q.toLowerCase();
    const contactMatches = allChats.filter(c =>
      c.display_name.toLowerCase().includes(ql) || c.id.toLowerCase().includes(ql)
    );

    // Full-text search across indexed chats
    const res = await fetch('/api/search?' + new URLSearchParams({ q }));
    const data = await res.json();

    renderSearchResults(q, contactMatches, data.results, data.indexed_count);
  }

  function clearSearchResults() {
    document.getElementById('search-results').classList.remove('has-results');
    document.getElementById('search-results').innerHTML = '';
  }

  function makeSectionLabel(text) {
    const el = document.createElement('div');
    el.className = 'sr-section-label';
    el.textContent = text;
    return el;
  }

  function makeContactResultItem(chat, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    el.innerHTML = `<div class="sr-chat">${highlight(chat.display_name, q)}</div><div class="sr-text">${esc(chat.id)}</div>`;
    el.addEventListener('click', () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const el2 = document.querySelector(`.chat-item[data-id="${chat.id}"][data-type="${chat.type}"]`);
      if (el2) selectChat(chat, el2);
    });
    return el;
  }

  function makeTextResultItem(r, q) {
    const el = document.createElement('div');
    el.className = 'search-result-item';
    const chatName = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type)?.display_name || r.chat_id;
    const snippet = r.text_body || (r.media_name ? '[📎 ' + r.media_name + ']' : '');
    el.innerHTML = `
      <div class="sr-chat">${esc(chatName)}</div>
      <div class="sr-text">${highlight(snippet, q)}</div>
      <div class="sr-time">${fmtTime(r.timestamp_ms)}</div>
    `;
    el.addEventListener('click', () => {
      document.getElementById('search-input').value = '';
      clearSearchResults();
      const chat = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type);
      if (chat) {
        const el2 = document.querySelector(`.chat-item[data-id="${r.chat_id}"][data-type="${r.chat_type}"]`);
        if (el2) selectChat(chat, el2);
      }
    });
    return el;
  }

  function renderSearchResults(q, contactMatches, textResults, indexedCount) {
    const container = document.getElementById('search-results');
    if (!contactMatches.length && !textResults.length) {
      clearSearchResults();
      return;
    }
    container.classList.add('has-results');
    container.innerHTML = '';

    if (contactMatches.length) {
      container.appendChild(makeSectionLabel('Contacts'));
      contactMatches.forEach(chat => container.appendChild(makeContactResultItem(chat, q)));
    }

    if (textResults.length) {
      container.appendChild(makeSectionLabel('Messages'));
      textResults.slice(0, 50).forEach(r => container.appendChild(makeTextResultItem(r, q)));
    }

    if (indexedCount != null && indexedCount < allChats.length) {
      const notice = document.createElement('div');
      notice.className = 'search-index-notice';
      notice.textContent = `Searched ${indexedCount} of ${allChats.length} chats. Open more chats to index them.`;
      container.appendChild(notice);
    }
  }

  // ---- in-chat search -------------------------------------------------------

  let chatSearchResults = [];
  let chatSearchIdx = -1;
  let chatSearchTimer = null;

  document.getElementById('chat-search-input').addEventListener('input', function () {
    clearTimeout(chatSearchTimer);
    const q = this.value.trim();
    if (!q) {
      clearChatSearch();
      return;
    }
    chatSearchTimer = setTimeout(() => doChatSearch(q), 300);
  });

  document.getElementById('chat-search-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.shiftKey) navigateChatSearch(-1);
      else navigateChatSearch(1);
    } else if (e.key === 'Escape') {
      clearChatSearch();
      this.value = '';
    }
  });

  document.getElementById('search-prev').addEventListener('click', () => navigateChatSearch(-1));
  document.getElementById('search-next').addEventListener('click', () => navigateChatSearch(1));

  async function doChatSearch(q) {
    if (!currentChat) return;
    const params = new URLSearchParams({ q, chat_id: currentChat.id, chat_type: currentChat.type });
    const res = await fetch('/api/search?' + params);
    const data = await res.json();
    chatSearchResults = data.results;
    chatSearchIdx = chatSearchResults.length > 0 ? 0 : -1;
    updateChatSearchNav();
    if (chatSearchIdx >= 0) jumpToChatSearchResult(chatSearchIdx);
  }

  function clearChatSearch() {
    chatSearchResults = [];
    chatSearchIdx = -1;
    document.getElementById('chat-search-nav').style.display = 'none';
    document.getElementById('chat-search-count').textContent = '';
    document.querySelectorAll('.search-highlight').forEach(el => el.classList.remove('search-highlight'));
  }

  function navigateChatSearch(delta) {
    if (!chatSearchResults.length) return;
    chatSearchIdx = (chatSearchIdx + delta + chatSearchResults.length) % chatSearchResults.length;
    updateChatSearchNav();
    jumpToChatSearchResult(chatSearchIdx);
  }

  function updateChatSearchNav() {
    const nav = document.getElementById('chat-search-nav');
    const count = document.getElementById('chat-search-count');
    nav.style.display = 'flex';
    if (!chatSearchResults.length) {
      count.textContent = 'No results';
      document.getElementById('search-prev').disabled = true;
      document.getElementById('search-next').disabled = true;
      return;
    }
    count.textContent = `${chatSearchIdx + 1}/${chatSearchResults.length}`;
    document.getElementById('search-prev').disabled = false;
    document.getElementById('search-next').disabled = false;
  }

  async function jumpToChatSearchResult(idx) {
    const result = chatSearchResults[idx];
    if (!result) return;
    document.querySelectorAll('.search-highlight').forEach(el => el.classList.remove('search-highlight'));
    const existing = document.querySelector(`.msg-row[data-ts="${result.timestamp_ms}"]`);
    if (existing) {
      highlightMessageRow(existing);
      return;
    }
    await jumpToTimestamp(result.timestamp_ms);
    const loaded = document.querySelector(`.msg-row[data-ts="${result.timestamp_ms}"]`);
    if (loaded) highlightMessageRow(loaded);
  }

  function highlightMessageRow(row) {
    const bubble = row.querySelector('.msg-bubble');
    if (bubble) bubble.classList.add('search-highlight');
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---- date picker ----------------------------------------------------------

  document.getElementById('date-picker-input').addEventListener('change', function () {
    const val = this.value;
    if (!val || !currentChat) return;
    const ts = new Date(val).getTime();
    jumpToTimestamp(ts);
  });

  async function jumpToTimestamp(ts) {
    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      ts,
      limit: 50
    });
    const res = await fetch('/api/messages/at?' + params);
    const msgs = await res.json();
    if (!msgs.length) return;

    const scroll = document.getElementById('message-scroll');
    scroll.innerHTML = '';
    msgList = [];
    domNodes = 0;

    msgs.forEach((m, i) => {
      msgList.push(m);
      const prevTs = i === 0 ? null : msgs[i - 1].timestamp_ms;
      renderBubbleWithSep(m, prevTs, 'after').forEach(node => scroll.appendChild(node));
      domNodes++;
    });

    const target = msgs.reduce((best, m) =>
      Math.abs(m.timestamp_ms - ts) < Math.abs(best.timestamp_ms - ts) ? m : best
    );
    const targetEl = document.querySelector(`.msg-row[data-ts="${target.timestamp_ms}"]`);
    if (targetEl) targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---- toolbar toggle -------------------------------------------------------

  document.getElementById('toolbar-toggle').addEventListener('click', function () {
    const exp = document.getElementById('toolbar-expanded');
    const open = exp.classList.toggle('open');
    if (open) document.getElementById('chat-search-input').focus();
    else {
      clearChatSearch();
      document.getElementById('chat-search-input').value = '';
    }
  });

  // ---- init ----------------------------------------------------------------

  loadChats();
})();
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    output_root = Path(args.output_root).expanduser().resolve()

    if not output_root.exists():
        print(f"Error: output_root does not exist: {output_root}", file=sys.stderr)
        sys.exit(1)

    archive_db = get_archive_db_path(output_root)
    if not archive_db.exists():
        print(f"Error: archive DB not found: {archive_db}", file=sys.stderr)
        sys.exit(1)

    print(f"[wa_chat_viewer] Opening archive: {output_root}")

    app = create_app(output_root, rescan=args.rescan)

    url = f"http://{args.host}:{args.port}"
    print(f"[wa_chat_viewer] Starting server at {url}")
    print(f"[wa_chat_viewer] Press Ctrl+C to stop")

    webbrowser.open(url)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
