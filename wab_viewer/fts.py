"""Full-text search (FTS5) indexing workers and chat stream builders."""

from __future__ import annotations

import sqlite3
import threading
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from .db_cache import _ensure_jid_map_resolved, _find_ios_db, get_cache_db_path
from .preserved import _detect_preserved_db
from .protobuf import _parse_ios_poll_blob
from .queries.sql_builder import (
    _ANDROID_HD_DEDUP_CLAUSE,
    _IOS_HD_DEDUP_CLAUSE,
    _check_android_hd_association,
    _check_ios_hd_association,
    _clean_user_sql,
    _resolve_android_chat_filter,
)

if TYPE_CHECKING:
    from .context import ViewerContext


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
        if msg_type == 46:
            receipt = row["poll_receipt_info"] if "poll_receipt_info" in row.keys() else None
            if receipt:
                parsed_poll = _parse_ios_poll_blob(bytes(receipt))
                if parsed_poll:
                    opts_text = " ".join(parsed_poll["options"])
                    q = parsed_poll.get("question") or text_body
                    text_body = f"{q} {opts_text}".strip()
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


def _fts_android_chat(
    wa_conn: sqlite3.Connection,
    cache_conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    on_progress,
    hd_clause: str,
):
    _ensure_jid_map_resolved(wa_conn, None)
    chat_pred, chat_params = _resolve_android_chat_filter(wa_conn, chat_id, chat_type)
    has_poll_opt = wa_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_poll_option'"
    ).fetchone()
    poll_opt_sql = (
        " || ' ' || COALESCE((SELECT GROUP_CONCAT(option_name, ' ') FROM message_poll_option WHERE message_row_id = m._id), '')"
        if has_poll_opt
        else ""
    )

    has_loc = bool(
        wa_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_location'"
        ).fetchone()
    )
    loc_join = "LEFT JOIN message_location ml ON ml.message_row_id = m._id" if has_loc else ""
    loc_text = (
        "WHEN m.message_type IN (5, 16) THEN COALESCE(NULLIF(m.text_data, ''), NULLIF(ml.place_name || ' ' || COALESCE(ml.place_address, ''), ''), ml.place_name)"
        if has_loc
        else ""
    )

    cursor = wa_conn.execute(
        f"""
        SELECT
            m._id                                                    AS rowid,
            ?                                                        AS chat_id,
            ?                                                        AS chat_type,
            COALESCE(m.timestamp, 0)                                AS timestamp_ms,
            COALESCE(
                CASE WHEN m.message_type = 66 THEN m.text_data {poll_opt_sql}
                     {loc_text}
                     ELSE m.text_data
                END,
                ''
            )                                                        AS text_body,
            m.message_type,
            mm.file_path                                            AS media_file
        FROM message m
        LEFT JOIN message_media mm ON mm.message_row_id = m._id
        {loc_join}
        WHERE {chat_pred}
        {hd_clause}
        ORDER BY m.timestamp ASC
    """,
        [chat_id, chat_type] + chat_params,
    )
    _stream_fts_rows(cursor, cache_conn, on_progress=on_progress)


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
            COALESCE(
                CASE WHEN m.ZMESSAGETYPE = 4 THEN {vcard_name_col}
                     WHEN m.ZMESSAGETYPE IN (5, 16) THEN COALESCE(NULLIF(mi.ZTITLE, '') || ' ' || NULLIF(m.ZTEXT, ''), mi.ZTITLE, m.ZTEXT)
                     ELSE m.ZTEXT
                END,
                ''
            ) AS text_body,
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


def bg_index_all(ctx: ViewerContext) -> None:
    """Index every eligible chat in the background for bulk FTS5 initialization."""
    try:
        wa_conn_bulk = sqlite3.connect(ctx.wa_db_path)
        wa_conn_bulk.row_factory = sqlite3.Row
        if ctx.source_type == "android":
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

        with ctx.indexing_lock:
            ctx.bulk_index_state["total_msgs"] = total
            ctx.bulk_index_state["indexed_msgs"] = 0

        cache_bulk = sqlite3.connect(str(get_cache_db_path(ctx.output_root)))
        cache_bulk.execute("PRAGMA journal_mode = WAL")
        cache_bulk.execute("PRAGMA synchronous = NORMAL")
        cache_bulk.row_factory = sqlite3.Row

        def _on_progress(n):
            with ctx.indexing_lock:
                ctx.bulk_index_state["indexed_msgs"] += n

        for row in chat_rows:
            already = cache_bulk.execute(
                "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?",
                (row["id"], row["type"]),
            ).fetchone()
            if not already:
                _build_fts_chat(
                    cache_bulk,
                    ctx.source_type,
                    ctx.wa_db_path,
                    row["id"],
                    row["type"],
                    on_progress=_on_progress,
                )

        cache_bulk.execute("INSERT INTO message_index_fts(message_index_fts) VALUES('optimize')")
        cache_bulk.execute("PRAGMA optimize")
        cache_bulk.execute("PRAGMA wal_checkpoint(PASSIVE)")
        cache_bulk.commit()
        cache_bulk.close()
    except Exception:
        print(f"[wab_viewer] Bulk index error:\n{traceback.format_exc()}")
    finally:
        with ctx.indexing_lock:
            ctx.bulk_index_state["running"] = False
            ctx.bulk_index_state["indexed_msgs"] = ctx.bulk_index_state["total_msgs"]
