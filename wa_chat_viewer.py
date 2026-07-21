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
import json
import mimetypes
import os
import sqlite3
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template_string, request

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Browse archived WhatsApp chats")
    p.add_argument("output_root", help="Path to the archive output directory")
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    p.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    p.add_argument("--rescan", action="store_true", help="Force rebuild of the viewer cache DB")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_cache_db_path(output_root: Path) -> Path:
    return output_root / ".wa_chat_viewer_cache.db"


def get_archive_db_path(output_root: Path) -> Path:
    return output_root / ".wa_media_archiver.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    rowid         INTEGER PRIMARY KEY,
    chat_id       TEXT NOT NULL,
    chat_type     TEXT NOT NULL,
    timestamp_ms  INTEGER NOT NULL,
    sender        TEXT NOT NULL DEFAULT '',
    from_me       INTEGER NOT NULL DEFAULT 0,
    archive_path  TEXT,
    media_type    TEXT NOT NULL DEFAULT 'text',
    media_name    TEXT NOT NULL DEFAULT '',
    text_body     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, timestamp_ms);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    chat_id, sender, text_body, media_name, archive_path,
    content='messages', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
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


def _chat_display_name(chat_id: str, chat_type: str, cursor: sqlite3.Cursor) -> str:
    if chat_type == "contact":
        cursor.execute(
            "SELECT display_name, folder FROM contacts WHERE number = ?", (chat_id,)
        )
        row = cursor.fetchone()
        if row:
            return row["display_name"] if row["display_name"] else row["folder"]
        # Fallback: number may not have a contacts entry — the folder name
        # is "Display Name (number)" so search for it inside folder
        cursor.execute(
            "SELECT folder FROM contacts WHERE folder LIKE ?", (f"%{chat_id}%",)
        )
        row = cursor.fetchone()
        if row:
            return row["folder"]
        return chat_id
    else:
        cursor.execute("SELECT subject FROM groups WHERE chat_row_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        return chat_id


def _open_archive_db(output_root: Path) -> sqlite3.Connection:
    db_path = get_archive_db_path(output_root)
    if not db_path.exists():
        raise FileNotFoundError(f"Archive DB not found: {db_path}")
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
# Cache build
# ---------------------------------------------------------------------------

def _build_cache(archive_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                 output_root: Path, rescan: bool):
    source_type, wa_db_path = _detect_source_db(output_root)
    if source_type is None:
        print("[wa_chat_viewer] Warning: No source WA DB found (msgstore.db / ChatStorage.sqlite). "
              "Media-only mode — timestamps from file mtime, no text messages.")
        _build_media_only(archive_conn, cache_conn, output_root)
        return

    print(f"[wa_chat_viewer] Source DB: {source_type} at {wa_db_path}")
    wa_conn = sqlite3.connect(wa_db_path)
    wa_conn.row_factory = sqlite3.Row

    cache_conn.execute("DELETE FROM messages")
    cache_conn.execute("DELETE FROM messages_fts")
    cache_conn.execute("DELETE FROM sync_meta")

    if source_type == "android":
        _build_android(wa_conn, archive_conn, cache_conn)
    else:
        _build_ios(wa_conn, archive_conn, cache_conn)

    wa_conn.close()

    cache_conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_scanned_at', ?)",
        (str(int(time.time())),)
    )
    cache_conn.commit()

    new_count = cache_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"[wa_chat_viewer] Cache built: {new_count} messages")


def _build_android(wa_conn: sqlite3.Connection, archive_conn: sqlite3.Connection,
                   cache_conn: sqlite3.Connection):
    rows = wa_conn.execute("""
        SELECT
            CASE
                WHEN c.subject IS NOT NULL THEN CAST(m.chat_row_id AS TEXT)
                ELSE COALESCE(j_chat.user, CAST(m.chat_row_id AS TEXT))
            END                                                  AS chat_id,
            CASE WHEN c.subject IS NOT NULL THEN 'group' ELSE 'contact' END AS chat_type,
            COALESCE(m.timestamp, 0)                            AS timestamp,
            COALESCE(j2.user, j.user, '')                      AS sender,
            m.from_me,
            mm.file_path,
            m.text_data,
            mm.media_name,
            m.message_type
        FROM message m
        LEFT JOIN message_media mm ON mm.message_row_id = m._id
        LEFT JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
        LEFT JOIN jid j ON j._id = m.sender_jid_row_id
        LEFT JOIN (
            SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
            FROM jid_map GROUP BY lid_row_id
        ) jm ON jm.lid_row_id = m.sender_jid_row_id
        LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
        ORDER BY m.timestamp ASC
    """).fetchall()

    archive_map = {}
    for row in archive_conn.execute(
        "SELECT original_path, archive_path FROM archive_copies"
    ).fetchall():
        archive_map[row["original_path"]] = row["archive_path"]

    _insert_message_rows(cache_conn, rows, archive_map, "android")


def _build_ios(wa_conn: sqlite3.Connection, archive_conn: sqlite3.Connection,
               cache_conn: sqlite3.Connection):
    rows = wa_conn.execute("""
        SELECT
            CAST(m.ZCHATSESSION AS TEXT)                            AS chat_id,
            CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                 ELSE 'contact' END                                  AS chat_type,
            CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)    AS timestamp_ms,
            SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1) AS sender,
            m.ZISFROMME,
            mi.ZMEDIALOCALPATH,
            m.ZTEXT,
            mi.ZTITLE,
            m.ZMESSAGETYPE
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
        LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        ORDER BY m.ZMESSAGEDATE ASC
    """).fetchall()

    archive_map = {}
    for row in archive_conn.execute(
        "SELECT original_path, archive_path FROM archive_copies"
    ).fetchall():
        archive_map[row["original_path"]] = row["archive_path"]

    _insert_message_rows(cache_conn, rows, archive_map, "ios")


def _insert_message_rows(cache_conn: sqlite3.Connection, rows, archive_map, source_type):
    insert_sql = """
        INSERT INTO messages
            (chat_id, chat_type, timestamp_ms, sender, from_me, archive_path,
             media_type, media_name, text_body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    fts_insert_sql = """
        INSERT INTO messages_fts (rowid, chat_id, sender, text_body, media_name, archive_path)
        VALUES (?, ?, ?, ?, ?, ?)
    """

    batch = []
    fts_batch = []
    for row in rows:
        if source_type == "android":
            chat_id = row["chat_id"]
            chat_type = row["chat_type"]
            timestamp_ms = row["timestamp"]
            sender = row["sender"]
            from_me = row["from_me"]
            file_path = row["file_path"]
            text_body = row["text_data"] or ""
            media_name = row["media_name"] or ""
            message_type = row["message_type"]
        else:
            chat_id = row["chat_id"]
            chat_type = row["chat_type"]
            timestamp_ms = row["timestamp_ms"]
            sender = row["sender"]
            from_me = row["ZISFROMME"]
            raw_path = row["ZMEDIALOCALPATH"]
            file_path = f"Message/{raw_path}" if raw_path else None
            text_body = row["ZTEXT"] or ""
            media_name = row["ZTITLE"] or ""
            message_type = row["ZMESSAGETYPE"]

        is_media = (message_type is not None and message_type != 0)
        media_type = "text"
        archive_path = None

        if is_media:
            media_type = _media_type_from_path(file_path)
            if file_path:
                archive_path = archive_map.get(file_path)
            if not archive_path and media_name:
                for op, ap in archive_map.items():
                    if media_name in op:
                        archive_path = ap
                        break

        batch.append((
            chat_id, chat_type, timestamp_ms, sender, from_me,
            archive_path, media_type, media_name, text_body
        ))

    cache_conn.executemany(insert_sql, batch)
    cache_conn.commit()

    for rowid, (chat_id, chat_type, timestamp_ms, sender, from_me,
                archive_path, media_type, media_name, text_body) in enumerate(batch, 1):
        fts_batch.append((
            rowid, chat_id, sender, text_body, media_name,
            archive_path or ""
        ))

    if fts_batch:
        cache_conn.executemany(fts_insert_sql, fts_batch)
        cache_conn.commit()


def _build_media_only(archive_conn: sqlite3.Connection, cache_conn: sqlite3.Connection,
                      output_root: Path):
    archive_map = {}
    for row in archive_conn.execute(
        "SELECT original_path, archive_path FROM archive_copies"
    ).fetchall():
        archive_map[row["original_path"]] = row["archive_path"]

    contacts = {
        str(r["number"]): ("contact", _contact_display_name(r))
        for r in archive_conn.execute("SELECT folder, number, display_name FROM contacts").fetchall()
    }
    groups = {
        str(r["folder"]): ("group", r["subject"])
        for r in archive_conn.execute("SELECT folder, subject FROM groups").fetchall()
    }
    folder_map = {**contacts, **groups}

    rows_by_chat: dict[tuple, list] = {}
    for original_path, archive_path in archive_map.items():
        if archive_path is None:
            continue
        parts = archive_path.split("/")
        if len(parts) < 2:
            continue
        folder = parts[1]
        chat_key = folder_map.get(folder)
        if chat_key is None:
            # parts[1] is not a known chat subfolder; check if parts[0] is a contact folder name
            contact_row = archive_conn.execute(
                "SELECT number FROM contacts WHERE number = ?", (parts[0],)
            ).fetchone()
            if contact_row:
                chat_key = ("contact", str(contact_row["number"]))
            else:
                chat_key = ("contact", parts[1] if len(parts) > 1 else folder)
        chat_type, chat_id = chat_key

        full_path = output_root / archive_path
        if not full_path.exists():
            continue
        mtime_ms = int(os.path.getmtime(full_path) * 1000)
        media_type = _media_type_from_path(archive_path)
        rows_by_chat.setdefault((chat_id, chat_type), []).append((
            chat_id, chat_type, mtime_ms, "", 0,
            archive_path, media_type, os.path.basename(archive_path), ""
        ))

    insert_sql = """
        INSERT INTO messages
            (chat_id, chat_type, timestamp_ms, sender, from_me, archive_path,
             media_type, media_name, text_body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for msgs in rows_by_chat.values():
        cache_conn.executemany(insert_sql, msgs)
    cache_conn.commit()


def _contact_display_name(row) -> str:
    dn = row["display_name"]
    return dn if dn else row["folder"]


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app(output_root: Path, rescan: bool = False):
    app = Flask(__name__)
    app.config["output_root"] = str(output_root)

    archive_conn = _open_archive_db(output_root)
    cache_conn = _open_cache_db(output_root)
    _build_cache(archive_conn, cache_conn, output_root, rescan)
    archive_conn.close()

    def get_cache():
        return cache_conn

    def get_output_root():
        return output_root

    # ---- API: list chats ---------------------------------------------------

    @app.route("/api/chats")
    def api_chats():
        conn = get_cache()
        rows = conn.execute("""
            SELECT
                m.chat_id,
                m.chat_type,
                COUNT(*) AS msg_count,
                MIN(m.timestamp_ms) AS oldest_ts,
                MAX(m.timestamp_ms) AS newest_ts
            FROM messages m
            GROUP BY m.chat_id, m.chat_type
            ORDER BY m.chat_type, newest_ts DESC
        """).fetchall()

        archive_conn2 = _open_archive_db(output_root)
        result = []
        for row in rows:
            chat_id = row["chat_id"]
            chat_type = row["chat_type"]
            display_name = _chat_display_name(chat_id, chat_type, archive_conn2.cursor())
            result.append({
                "id": chat_id,
                "type": chat_type,
                "display_name": display_name,
                "msg_count": row["msg_count"],
                "oldest_ts": row["oldest_ts"],
                "newest_ts": row["newest_ts"],
            })
        archive_conn2.close()
        return jsonify(result)

    # ---- API: paginated messages -------------------------------------------

    @app.route("/api/messages")
    def api_messages():
        chat_id = request.args.get("chat_id", "")
        chat_type = request.args.get("chat_type", "")
        before = request.args.get("before")
        after = request.args.get("after")
        limit = min(int(request.args.get("limit", 50)), 200)

        conn = get_cache()
        if before:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE chat_id = ? AND chat_type = ? AND timestamp_ms < ?
                ORDER BY timestamp_ms DESC LIMIT ?
            """, (chat_id, chat_type, int(before), limit)).fetchall()
        elif after:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE chat_id = ? AND chat_type = ? AND timestamp_ms > ?
                ORDER BY timestamp_ms ASC LIMIT ?
            """, (chat_id, chat_type, int(after), limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE chat_id = ? AND chat_type = ?
                ORDER BY timestamp_ms DESC LIMIT ?
            """, (chat_id, chat_type, limit)).fetchall()

        return jsonify([dict(r) for r in rows])

    # ---- API: search -------------------------------------------------------

    @app.route("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        chat_id = request.args.get("chat_id")
        if not q:
            return jsonify([])

        conn = get_cache()
        if chat_id:
            rows = conn.execute("""
                SELECT m.* FROM messages m
                JOIN messages_fts fts ON fts.rowid = m.rowid
                WHERE messages_fts MATCH ? AND m.chat_id = ?
                ORDER BY rank LIMIT 100
            """, (q, chat_id)).fetchall()
        else:
            rows = conn.execute("""
                SELECT m.* FROM messages m
                JOIN messages_fts fts ON fts.rowid = m.rowid
                WHERE messages_fts MATCH ?
                ORDER BY rank LIMIT 100
            """, (q,)).fetchall()

        return jsonify([dict(r) for r in rows])

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
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
    }
    #chat-header h2 { font-size: 15px; font-weight: 600; }

    #message-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .sentinel { height: 1px; width: 100%; }

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

    .direction-badge {
      display: inline-block;
      font-size: 10px;
      padding: 1px 4px;
      border-radius: 3px;
      margin-right: 4px;
      vertical-align: middle;
    }
    .direction-badge.sent { background: var(--accent); color: #fff; }
    .direction-badge.recv { background: var(--text-muted); color: #fff; }
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
      </div>
      <div id="message-scroll"></div>
      <div id="empty-pane">Select a chat to browse messages</div>
    </div>
  </div>
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

    await loadMessages('down');
  }

  // ---- load messages -------------------------------------------------------

  let loading = false;

  async function loadMessages(direction) {
    if (loading) return;
    loading = true;

    const scroll = document.getElementById('message-scroll');
    const sentinel = document.createElement('div');
    sentinel.className = 'sentinel spinner';
    sentinel.id = 'sentinel-' + direction;
    sentinel.textContent = 'Loading…';
    if (direction === 'down') {
      scroll.appendChild(sentinel);
    } else {
      scroll.insertBefore(sentinel, scroll.firstChild);
    }

    const params = new URLSearchParams({
      chat_id: currentChat.id,
      chat_type: currentChat.type,
      limit: 50
    });

    if (direction === 'down' && msgList.length > 0) {
      params.set('before', msgList[msgList.length - 1].timestamp_ms);
    } else if (direction === 'up' && msgList.length > 0) {
      params.set('after', msgList[0].timestamp_ms);
    }

    try {
      const res = await fetch('/api/messages?' + params);
      const msgs = await res.json();
      sentinel.remove();
      loading = false;

      if (!msgs.length) return;

      if (direction === 'down') {
        msgs.forEach(m => msgList.push(m));
        appendMessages(msgs, 'bottom');
      } else {
        msgs.reverse().forEach(m => msgList.unshift(m));
        prependMessages(msgs);
      }

    } catch (e) {
      sentinel.textContent = 'Error loading messages';
      loading = false;
    }
  }

  // ---- render message ------------------------------------------------------

  function renderBubble(msg) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + (msg.from_me ? 'sent' : 'recv');

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';

    const senderEl = document.createElement('div');
    senderEl.className = 'msg-sender';
    senderEl.textContent = msg.sender || '';
    if (msg.sender) bubble.appendChild(senderEl);

    const badge = document.createElement('span');
    badge.className = 'direction-badge ' + (msg.from_me ? 'sent' : 'recv');
    badge.textContent = msg.from_me ? 'You' : 'Them';
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    meta.appendChild(badge);
    meta.appendChild(document.createTextNode(fmtTime(msg.timestamp_ms)));

    if (msg.media_type === 'text' || !msg.archive_path) {
      const txt = document.createElement('div');
      txt.className = 'msg-text';
      txt.innerHTML = highlight(msg.text_body, '');
      bubble.appendChild(txt);
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
      img.addEventListener('click', () => showLightbox(src));
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

  function showLightbox(src) {
    const lb = document.createElement('div');
    lb.className = 'img-lightbox';
    const img = document.createElement('img');
    img.src = src;
    lb.appendChild(img);
    lb.addEventListener('click', () => lb.remove());
    document.body.appendChild(lb);
  }

  // ---- append / prepend with pruning ---------------------------------------

  function appendMessages(msgs, where) {
    const scroll = document.getElementById('message-scroll');
    const frag = document.createDocumentFragment();
    msgs.forEach(m => frag.appendChild(renderBubble(m)));
    if (where === 'bottom') {
      scroll.appendChild(frag);
    } else {
      scroll.insertBefore(frag, scroll.firstChild);
    }
    domNodes += msgs.length;
    pruneDom(where === 'top' ? 'bottom' : 'top');
    setupObservers();
  }

  function prependMessages(msgs) {
    appendMessages(msgs, 'top');
  }

  function pruneDom(keepEnd) {
    const scroll = document.getElementById('message-scroll');
    while (domNodes > MAX_DOM && scroll.children.length > 0) {
      // Skip sentinel elements — they must always stay in the DOM
      const child = keepEnd === 'bottom' ? scroll.firstChild : scroll.lastChild;
      if (!child || child.classList.contains('sentinel')) break;
      scroll.removeChild(child);
      domNodes--;
    }
  }

  // ---- IntersectionObserver ------------------------------------------------

  let botObserver = null, topObserver = null;

  function setupObservers() {
    const scroll = document.getElementById('message-scroll');

    if (botObserver) botObserver.disconnect();
    if (topObserver) topObserver.disconnect();

    const sentinels = scroll.querySelectorAll('.sentinel');
    const topSentinel = sentinels[0];
    const botSentinel = sentinels[sentinels.length - 1];

    botObserver = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting && !loading) loadMessages('down');
    }, { root: scroll, threshold: 0 });
    if (botSentinel) botObserver.observe(botSentinel);

    topObserver = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting && !loading) loadMessages('up');
    }, { root: scroll, threshold: 0 });
    if (topSentinel) topObserver.observe(topSentinel);
  }

  // ---- search --------------------------------------------------------------

  let searchTimer = null;

  document.getElementById('search-input').addEventListener('input', function () {
    clearTimeout(searchTimer);
    const q = this.value.trim();
    if (!q) {
      document.getElementById('search-results').classList.remove('has-results');
      document.getElementById('search-results').innerHTML = '';
      return;
    }
    searchTimer = setTimeout(() => doSearch(q), 300);
  });

  async function doSearch(q) {
    const params = new URLSearchParams({ q });
    if (currentChat) params.set('chat_id', currentChat.id);
    const res = await fetch('/api/search?' + params);
    const results = await res.json();
    renderSearchResults(results, q);
  }

  function renderSearchResults(results, q) {
    const container = document.getElementById('search-results');
    if (!results.length) {
      container.classList.remove('has-results');
      container.innerHTML = '';
      return;
    }
    container.classList.add('has-results');
    container.innerHTML = '<div class="search-result-header">Search results</div>';
    results.slice(0, 50).forEach(r => {
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
        document.getElementById('search-results').classList.remove('has-results');
        document.getElementById('search-results').innerHTML = '';
        const chat = allChats.find(c => c.id === r.chat_id && c.type === r.chat_type);
        if (chat) {
          const el2 = document.querySelector(`.chat-item[data-id="${r.chat_id}"][data-type="${r.chat_type}"]`);
          if (el2) selectChat(chat, el2);
        }
      });
      container.appendChild(el);
    });
  }

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
