"""Paginated message window, jump-to-message, receipt, reaction, and poll detail endpoints."""

from __future__ import annotations

import json
import sqlite3
import threading

from flask import Blueprint, jsonify, request

from ..context import get_viewer_context
from ..db_cache import _ensure_jid_map_resolved
from ..fts import _maybe_start_indexing
from ..hydration import (
    _backfill_recent_messages,
    _format_service_rows,
    _hydrate_link_preview_rows,
    _hydrate_live_photos,
    _hydrate_mentions,
    _hydrate_poll_rows,
    _hydrate_quoted_rows,
    _ios_reactions,
    _ios_read_without_timestamp,
    _parse_ios_receipt_blob,
    _parse_ios_receipt_device_rows,
    _resolve_and_cache_reactions,
    _strip_none_reactions,
)
from ..preserved import (
    _merge_messages,
    _query_preserved_messages,
    _resolve_quoted_msg_ids,
)
from ..protobuf import (
    _extract_ios_reaction_reactors,
    _parse_ios_poll_blob,
)
from ..queries.calls import (
    _fetch_chat_calls_for_window,
    _merge_chat_calls_with_messages,
    _preserve_target_in_slice,
)
from ..queries.sql_builder import (
    _clean_user_sql,
    _ios_chat_filter,
    _resolve_android_chat_filter,
)

bp = Blueprint("messages", __name__)

_APPLE_EPOCH_OFFSET = 978307200.0


def _ios_apple_bound(ms_val: int, inclusive: bool, is_upper: bool) -> tuple[str, float]:
    """Return (SQLite operator, Apple CoreData timestamp) for a Unix-ms boundary."""
    if is_upper:
        adj_ms = ms_val + 1 if inclusive else ms_val
        return "<", (adj_ms / 1000.0) - _APPLE_EPOCH_OFFSET
    adj_ms = ms_val if inclusive else ms_val + 1
    return ">=", (adj_ms / 1000.0) - _APPLE_EPOCH_OFFSET


@bp.route("/api/messages")
def api_messages():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    before = request.args.get("before")
    after = request.args.get("after")
    inclusive = request.args.get("inclusive") in ("1", "true")
    limit = min(int(request.args.get("limit", 50)), 200)

    b_val = None
    if before:
        try:
            b_val = int(float(before))
        except (ValueError, TypeError):
            b_val = None

    a_val = None
    if after:
        try:
            a_val = int(float(after))
        except (ValueError, TypeError):
            a_val = None

    if b_val is None and a_val is None and ctx.source_type == "android":
        rows = (
            ctx.get_archive()
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
                ctx.get_cache(),
                ctx.source_type,
                ctx.wa_db_path,
                ctx.output_root,
                ctx.indexing_state,
                ctx.indexing_lock,
            )
            if ctx.source_type == "android" and rows:
                rows = [dict(r) for r in rows]
                for r in rows:
                    r.setdefault("is_starred", 0)
                    r.setdefault("is_pinned", 0)
                    if r.get("chat_type") == "contact" and r.get("media_type") == "service":
                        r["media_type"] = "deleted"
                        r["text_body"] = (
                            "You deleted this message"
                            if r.get("from_me") == 1
                            else "This message was deleted"
                        )
                needs_refresh = False
                if ctx.get_wa() is not None and ctx.caps.has_message_revoked and rows:
                    cached_mids = {r["msg_id"] for r in rows}
                    min_ts = rows[-1]["timestamp_ms"]
                    max_ts = rows[0]["timestamp_ms"]
                    chat_pred, chat_params = _resolve_android_chat_filter(
                        ctx.get_wa(), chat_id, chat_type, ctx.android_chat_row_ids_cache
                    )
                    try:
                        missing_rev = (
                            ctx.get_wa()
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
                    if ctx.caps.has_android_hd and ctx.get_wa() is not None:
                        mids = [r["msg_id"] for r in rows]
                        placeholders = ",".join("?" * len(mids))
                        try:
                            hd_parents = {
                                r[0]
                                for r in ctx.get_wa()
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
                    if unresolved_media and ctx.get_wa() is not None:
                        wa = ctx.get_wa()
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
                                        ctx.get_archive().execute(
                                            "UPDATE recent_messages SET archive_path = ? WHERE msg_id = ?",
                                            (copy_map[r["msg_id"]], r["msg_id"]),
                                        )
                                ctx.get_archive().commit()
                        except sqlite3.OperationalError:
                            pass
                    missing_rx = [r["msg_id"] for r in rows if r.get("reactions") is None]
                    if missing_rx:
                        rx_cache = (
                            ctx.get_archive()
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
                        if still_missing and ctx.get_wa() is not None and ctx.caps.has_reactions:
                            wa_rows = (
                                ctx.get_wa()
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
                                ctx.get_archive().executemany(
                                    "INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions) VALUES (?, ?, ?)",
                                    cache_inserts,
                                )
                        for row in rows:
                            if row["msg_id"] in rx_map:
                                val = rx_map[row["msg_id"]]
                                row["reactions"] = val if val else None
                                ctx.get_archive().execute(
                                    "UPDATE recent_messages SET reactions = ? WHERE chat_id = ? AND msg_id = ?",
                                    (row["reactions"], chat_id, row["msg_id"]),
                                )
                        ctx.get_archive().commit()
                    _hydrate_poll_rows(rows, ctx.source_type, ctx.get_wa())
                    _hydrate_quoted_rows(rows, ctx.source_type, ctx.get_wa())
                    _hydrate_link_preview_rows(rows, ctx.source_type)
                    _hydrate_live_photos(rows, ctx.source_type, ctx.get_wa())
                    pres = ctx.get_preserved()
                    if pres is not None:
                        pres_rows = _query_preserved_messages(
                            pres, chat_id, chat_type, None, None, False, False, limit
                        )
                        if pres_rows:
                            rows = _merge_messages(rows, pres_rows, True, limit)
                    _hydrate_mentions(rows, ctx.source_type, ctx.get_wa(), pres)
                    _resolve_quoted_msg_ids(rows, ctx.source_type, ctx.get_wa(), pres)
                    live_calls, pres_calls = _fetch_chat_calls_for_window(
                        ctx.source_type,
                        ctx.get_wa(),
                        ctx.get_archive(),
                        pres,
                        ctx.wa_db_path,
                        ctx.output_root,
                        chat_id,
                        chat_type,
                        None,
                        None,
                        False,
                        False,
                        limit,
                        ctx.android_chat_row_ids_cache,
                    )
                    rows = _merge_chat_calls_with_messages(
                        rows, live_calls, pres_calls, True, limit
                    )
                    return jsonify(rows)

    conn = ctx.get_wa()
    if conn is None:
        pres = ctx.get_preserved()
        pres_rows: list[dict] = []
        if pres is not None:
            pres_rows = _query_preserved_messages(
                pres, chat_id, chat_type, b_val, a_val, inclusive, inclusive, limit
            )
            _resolve_quoted_msg_ids(pres_rows, ctx.source_type, None, pres)
        descending = a_val is None
        _, pres_calls = _fetch_chat_calls_for_window(
            ctx.source_type,
            None,
            ctx.get_archive(),
            pres,
            ctx.wa_db_path,
            ctx.output_root,
            chat_id,
            chat_type,
            b_val,
            a_val,
            inclusive,
            inclusive,
            limit,
            ctx.android_chat_row_ids_cache,
        )
        merged = _merge_chat_calls_with_messages(pres_rows, [], pres_calls, descending, limit)
        return jsonify(merged)

    if b_val is None and a_val is None:
        _maybe_start_indexing(
            chat_id,
            chat_type,
            ctx.get_cache(),
            ctx.source_type,
            ctx.wa_db_path,
            ctx.output_root,
            ctx.indexing_state,
            ctx.indexing_lock,
        )

    select = ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
    extra = ctx.queries.android_filter if ctx.source_type == "android" else ctx.queries.ios_filter

    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        ts_col = ctx.queries.android_ts
        sort_phys_col = "m._id"
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        ts_col = "m.ZMESSAGEDATE"
        sort_phys_col = "m.Z_PK"

    if b_val is not None:
        if ctx.source_type == "ios":
            b_op, b_param = _ios_apple_bound(b_val, inclusive, True)
        else:
            b_op, b_param = ("<=" if inclusive else "<"), b_val
        sql = f"{select} WHERE {chat_pred} AND {ts_col} {b_op} ? {extra} ORDER BY {ts_col} DESC, {sort_phys_col} DESC LIMIT ?"
        rows = conn.execute(sql, chat_params + [b_param, limit]).fetchall()
    elif a_val is not None:
        if ctx.source_type == "ios":
            a_op, a_param = _ios_apple_bound(a_val, inclusive, False)
        else:
            a_op, a_param = (">=" if inclusive else ">"), a_val
        sql = f"{select} WHERE {chat_pred} AND {ts_col} {a_op} ? {extra} ORDER BY {ts_col} ASC, {sort_phys_col} ASC LIMIT ?"
        rows = conn.execute(sql, chat_params + [a_param, limit]).fetchall()
    else:
        sql = f"{select} WHERE {chat_pred} {extra} ORDER BY {ts_col} DESC, {sort_phys_col} DESC LIMIT ?"
        rows = conn.execute(sql, chat_params + [limit]).fetchall()

    rows = [dict(r) for r in rows]
    _format_service_rows(
        rows,
        ctx.source_type,
        conn,
        ctx.caps.has_mcp,
        ctx.caps.has_lid_dn,
        ctx.caps.has_jid_server,
        ctx.caps.has_jid_raw_string,
    )

    if ctx.source_type == "android" and rows and ctx.caps.has_reactions:
        msg_ids = [r["msg_id"] for r in rows]
        placeholders = ",".join("?" * len(msg_ids))
        cached = (
            ctx.get_archive()
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
                ctx.get_archive().executemany(
                    "INSERT OR REPLACE INTO reactions_cache (chat_id, msg_id, reactions) "
                    "VALUES (?, ?, ?)",
                    cache_inserts,
                )
                ctx.get_archive().commit()
            cached_map.update({mid: wa_map.get(mid, "") for mid in missing})
        for row in rows:
            rx_val = cached_map.get(row["msg_id"])
            row["reactions"] = rx_val if rx_val else None
    elif ctx.source_type == "ios" and rows and ctx.caps.has_ios_reactions:
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
        row.setdefault("is_starred", 0)
        row.setdefault("is_pinned", 0)
        row.setdefault("is_video_note", 0)

    pres = ctx.get_preserved()
    _hydrate_mentions(rows, ctx.source_type, conn, pres)
    _hydrate_poll_rows(rows, ctx.source_type, conn)
    _hydrate_quoted_rows(rows, ctx.source_type, conn)
    _hydrate_link_preview_rows(rows, ctx.source_type)
    _hydrate_live_photos(rows, ctx.source_type, conn)
    if pres is not None:
        pres_rows = _query_preserved_messages(
            pres, chat_id, chat_type, b_val, a_val, inclusive, inclusive, limit
        )
        if pres_rows:
            _hydrate_mentions(pres_rows, ctx.source_type, conn, pres)
            descending = a_val is None
            rows = _merge_messages(rows, pres_rows, descending, limit)
    _resolve_quoted_msg_ids(rows, ctx.source_type, conn, pres)

    if b_val is None and a_val is None:
        threading.Thread(
            target=_backfill_recent_messages,
            args=(ctx.archive_db_path, [dict(r) for r in rows]),
            daemon=True,
        ).start()

    descending = a_val is None
    live_calls, pres_calls = _fetch_chat_calls_for_window(
        ctx.source_type,
        conn,
        ctx.get_archive(),
        pres,
        ctx.wa_db_path,
        ctx.output_root,
        chat_id,
        chat_type,
        b_val,
        a_val,
        inclusive,
        inclusive,
        limit,
        ctx.android_chat_row_ids_cache,
    )
    rows = _merge_chat_calls_with_messages(rows, live_calls, pres_calls, descending, limit)
    return jsonify(rows)


@bp.route("/api/messages/at")
def api_messages_at():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    ts_raw = request.args.get("ts")
    msg_id_raw = request.args.get("msg_id")
    limit = min(int(request.args.get("limit", 50)), 200)

    target_msg_id: int | None = None
    if msg_id_raw:
        if not msg_id_raw.startswith("call:"):
            try:
                target_msg_id = int(msg_id_raw)
            except ValueError:
                return jsonify({"error": "msg_id must be an integer"}), 400

    ts: int | None = None
    if ts_raw:
        try:
            ts = int(ts_raw)
        except ValueError:
            return jsonify({"error": "ts must be an integer"}), 400

    conn = ctx.get_wa()
    pres = ctx.get_preserved()

    if target_msg_id is not None and ts is None:
        if target_msg_id < 0 and pres is not None:
            has_pres_tbl = bool(
                pres.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preserved_messages'"
                ).fetchone()
            )
            if has_pres_tbl:
                pres_lookup_id = abs(target_msg_id)
                row = pres.execute(
                    "SELECT timestamp_ms, chat_id, chat_type FROM preserved_messages WHERE rowid = ?",
                    (pres_lookup_id,),
                ).fetchone()
                if row:
                    ts = int(row[0])
                    if not chat_id and row[1]:
                        chat_id = str(row[1])
                    if not chat_type and row[2]:
                        chat_type = str(row[2])
        elif conn is not None and ctx.source_type == "android":
            has_msg_tbl = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
                ).fetchone()
            )
            if has_msg_tbl:
                row = conn.execute(
                    """SELECT m.timestamp, m.chat_row_id,
                              CASE WHEN j.raw_string LIKE '%@g.us' OR c.subject IS NOT NULL THEN 'group' ELSE 'contact' END
                       FROM message m
                       LEFT JOIN chat c ON c._id = m.chat_row_id
                       LEFT JOIN jid j ON j._id = c.jid_row_id
                       WHERE m._id = ?""",
                    (target_msg_id,),
                ).fetchone()
                if row:
                    ts = int(row[0])
                    if not chat_id and row[1] is not None:
                        chat_id = str(row[1])
                    if not chat_type and row[2]:
                        chat_type = str(row[2])
        elif conn is not None and ctx.source_type == "ios":
            has_zwamsg = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
                ).fetchone()
            )
            if has_zwamsg:
                row = conn.execute(
                    """SELECT m.ZMESSAGEDATE, m.ZCHATSESSION,
                              CASE WHEN s.ZCONTACTJID LIKE '%@g.us' THEN 'group' ELSE 'contact' END
                       FROM ZWAMESSAGE m
                       LEFT JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
                       WHERE m.Z_PK = ?""",
                    (target_msg_id,),
                ).fetchone()
                if row and row[0] is not None:
                    ts = int(round((float(row[0]) + 978307200.0) * 1000.0))
                    if not chat_id and row[1] is not None:
                        chat_id = str(row[1])
                    if not chat_type and row[2]:
                        chat_type = str(row[2])

    if ts is None:
        return jsonify({"error": "ts or valid msg_id is required"}), 400

    half = limit // 2
    conn = ctx.get_wa()
    if conn is None:
        pres = ctx.get_preserved()
        pres_before: list[dict] = []
        pres_after: list[dict] = []
        target_key = (
            msg_id_raw if (msg_id_raw and msg_id_raw.startswith("call:")) else target_msg_id
        )
        if pres is not None:
            pres_before = _query_preserved_messages(
                pres, chat_id, chat_type, ts, None, True, False, half
            )
            pres_after = _query_preserved_messages(
                pres, chat_id, chat_type, None, ts, False, False, half
            )
            _resolve_quoted_msg_ids(pres_before, ctx.source_type, None, pres)
            _resolve_quoted_msg_ids(pres_after, ctx.source_type, None, pres)
        _, pres_calls_before = _fetch_chat_calls_for_window(
            ctx.source_type,
            None,
            ctx.get_archive(),
            pres,
            ctx.wa_db_path,
            ctx.output_root,
            chat_id,
            chat_type,
            ts,
            None,
            True,
            False,
            half,
            ctx.android_chat_row_ids_cache,
        )
        _, pres_calls_after = _fetch_chat_calls_for_window(
            ctx.source_type,
            None,
            ctx.get_archive(),
            pres,
            ctx.wa_db_path,
            ctx.output_root,
            chat_id,
            chat_type,
            None,
            ts,
            False,
            False,
            half,
            ctx.android_chat_row_ids_cache,
        )
        merged_before = _merge_chat_calls_with_messages(
            pres_before, [], pres_calls_before, True, half
        )
        merged_after = _merge_chat_calls_with_messages(
            pres_after, [], pres_calls_after, False, half
        )
        merged_before = _preserve_target_in_slice(
            merged_before, pres_before + pres_calls_before, target_key
        )
        merged_after = _preserve_target_in_slice(
            merged_after, pres_after + pres_calls_after, target_key
        )
        combined = list(reversed(merged_before)) + merged_after
        return jsonify(combined)

    select = ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
    extra = ctx.queries.android_filter if ctx.source_type == "android" else ctx.queries.ios_filter

    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        ts_col = ctx.queries.android_ts
        sort_phys_col = "m._id"
        before_op, before_param = "<=", ts
        after_op, after_param = ">", ts
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        ts_col = "m.ZMESSAGEDATE"
        sort_phys_col = "m.Z_PK"
        before_op, before_param = _ios_apple_bound(ts, True, True)
        after_op, after_param = _ios_apple_bound(ts, False, False)

    before_rows = conn.execute(
        f"{select} WHERE {chat_pred} AND {ts_col} {before_op} ? {extra} ORDER BY {ts_col} DESC, {sort_phys_col} DESC LIMIT ?",
        chat_params + [before_param, half],
    ).fetchall()
    after_rows = conn.execute(
        f"{select} WHERE {chat_pred} AND {ts_col} {after_op} ? {extra} ORDER BY {ts_col} ASC, {sort_phys_col} ASC LIMIT ?",
        chat_params + [after_param, half],
    ).fetchall()

    combined = [dict(r) for r in reversed(before_rows)] + [dict(r) for r in after_rows]
    if target_msg_id is not None and target_msg_id > 0:
        present_ids = {int(r["msg_id"]) for r in combined}
        if target_msg_id not in present_ids:
            id_col = "m._id" if ctx.source_type == "android" else "m.Z_PK"
            target_row = conn.execute(
                f"{select} WHERE {chat_pred} AND {id_col} = ? {extra} LIMIT 1",
                chat_params + [target_msg_id],
            ).fetchone()
            if target_row is not None:
                target_dict = dict(target_row)
                if int(target_dict["timestamp_ms"]) <= ts:
                    combined.insert(0, target_dict)
                else:
                    combined.append(target_dict)
    _format_service_rows(
        combined,
        ctx.source_type,
        conn,
        ctx.caps.has_mcp,
        ctx.caps.has_lid_dn,
        ctx.caps.has_jid_server,
        ctx.caps.has_jid_raw_string,
    )
    if ctx.source_type == "android" and combined and ctx.caps.has_reactions:
        _resolve_and_cache_reactions(ctx.source_type, conn, ctx.get_archive(), combined)
    elif ctx.source_type == "ios" and combined and ctx.caps.has_ios_reactions:
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
        row.setdefault("is_starred", 0)
        row.setdefault("is_pinned", 0)
        row.setdefault("is_video_note", 0)
    pres = ctx.get_preserved()
    _hydrate_mentions(combined, ctx.source_type, conn, pres)
    _hydrate_poll_rows(combined, ctx.source_type, conn)
    _hydrate_quoted_rows(combined, ctx.source_type, conn)
    _hydrate_link_preview_rows(combined, ctx.source_type)
    _hydrate_live_photos(combined, ctx.source_type, conn)
    live_before = [r for r in combined if r["timestamp_ms"] <= ts]
    live_after = [r for r in combined if r["timestamp_ms"] > ts]
    if pres is not None:
        pres_before = _query_preserved_messages(
            pres, chat_id, chat_type, ts, None, True, False, half
        )
        pres_after = _query_preserved_messages(
            pres, chat_id, chat_type, None, ts, False, False, half
        )
        if pres_before:
            _hydrate_mentions(pres_before, ctx.source_type, conn, pres)
        if pres_after:
            _hydrate_mentions(pres_after, ctx.source_type, conn, pres)
        merged_before = _merge_messages(live_before, pres_before, True, half)
        merged_after = _merge_messages(live_after, pres_after, False, half)
    else:
        merged_before = list(reversed(live_before))
        merged_after = live_after

    combined_msgs = list(reversed(merged_before)) + merged_after
    _resolve_quoted_msg_ids(combined_msgs, ctx.source_type, conn, pres)
    live_calls_before, pres_calls_before = _fetch_chat_calls_for_window(
        ctx.source_type,
        conn,
        ctx.get_archive(),
        pres,
        ctx.wa_db_path,
        ctx.output_root,
        chat_id,
        chat_type,
        ts,
        None,
        True,
        False,
        half,
        ctx.android_chat_row_ids_cache,
    )
    live_calls_after, pres_calls_after = _fetch_chat_calls_for_window(
        ctx.source_type,
        conn,
        ctx.get_archive(),
        pres,
        ctx.wa_db_path,
        ctx.output_root,
        chat_id,
        chat_type,
        None,
        ts,
        False,
        False,
        half,
        ctx.android_chat_row_ids_cache,
    )
    msgs_before = [r for r in combined_msgs if r["timestamp_ms"] <= ts]
    msgs_after = [r for r in combined_msgs if r["timestamp_ms"] > ts]
    target_key = msg_id_raw if (msg_id_raw and msg_id_raw.startswith("call:")) else target_msg_id
    final_before = _merge_chat_calls_with_messages(
        list(reversed(msgs_before)), live_calls_before, pres_calls_before, True, half
    )
    final_after = _merge_chat_calls_with_messages(
        msgs_after, live_calls_after, pres_calls_after, False, half
    )
    final_before = _preserve_target_in_slice(
        final_before,
        msgs_before + live_calls_before + pres_calls_before,
        target_key,
    )
    final_after = _preserve_target_in_slice(
        final_after,
        msgs_after + live_calls_after + pres_calls_after,
        target_key,
    )
    combined = list(reversed(final_before)) + final_after
    return jsonify(_strip_none_reactions(combined))


@bp.route("/api/message_receipts/<int:message_id>")
def api_message_receipts(message_id):
    ctx = get_viewer_context()
    conn = ctx.get_wa()
    if conn is None:
        return jsonify({"available": False})

    if ctx.source_type == "android":
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


@bp.route("/api/reaction_details/<message_id>")
def api_reaction_details(message_id):
    ctx = get_viewer_context()
    try:
        mid = int(message_id)
    except (ValueError, TypeError):
        return jsonify({"available": False}), 400

    if mid < 0:
        pres = ctx.get_preserved()
        if pres is None:
            return jsonify({"available": False})
        pr_row = pres.execute(
            "SELECT reactions, from_me, sender FROM preserved_messages WHERE rowid = ?",
            (-mid,),
        ).fetchone()
        if not pr_row or not pr_row["reactions"]:
            return jsonify({"available": False})
        rx_list = [e.strip() for e in pr_row["reactions"].split(",") if e.strip()]
        reactors = [
            {
                "name": "You" if pr_row["from_me"] else (pr_row["sender"] or "Unknown"),
                "emoji": e,
                "from_me": int(pr_row["from_me"] or 0),
            }
            for e in rx_list
        ]
        return jsonify({"available": True, "total": len(reactors), "reactors": reactors})

    conn = ctx.get_wa()
    if conn is None:
        return jsonify({"available": False})

    message_id = mid

    if ctx.source_type == "android":
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "message_add_on" not in tables:
            return jsonify({"available": False})
        _ensure_jid_map_resolved(conn, ctx.get_archive())
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


@bp.route("/api/poll_details/<message_id>")
def api_poll_details(message_id):
    ctx = get_viewer_context()
    try:
        mid = int(message_id)
    except (ValueError, TypeError):
        return jsonify({"available": False}), 400

    conn = ctx.get_wa()
    if mid < 0:
        pres = ctx.get_preserved()
        if pres is None:
            return jsonify({"available": False})
        pr_row = pres.execute(
            "SELECT * FROM preserved_messages WHERE rowid = ?", (-mid,)
        ).fetchone()
        if not pr_row:
            return jsonify({"available": False})
        pr = dict(pr_row)
        if conn is not None and pr.get("stanza_id"):
            try:
                live_m = conn.execute(
                    "SELECT _id FROM message WHERE key_id = ?", (pr["stanza_id"],)
                ).fetchone()
                if live_m:
                    mid = live_m[0]
            except sqlite3.OperationalError:
                pass
        if mid < 0:
            p_data = {}
            if pr.get("poll_data"):
                try:
                    p_data = (
                        json.loads(pr["poll_data"])
                        if isinstance(pr["poll_data"], str)
                        else pr["poll_data"]
                    )
                except Exception:
                    pass
            options = []
            for opt in p_data.get("options") or []:
                vc = opt.get("vote_count", opt.get("votes", 0))
                options.append(
                    {
                        "id": opt.get("id"),
                        "name": opt.get("name", ""),
                        "vote_count": vc,
                        "voters": [],
                    }
                )
            multi = p_data.get("multiple_answers")
            if multi is None:
                multi = p_data.get("selectable_options_count") == 0
            total_v = p_data.get("total_votes", sum(o["vote_count"] for o in options))
            return jsonify(
                {
                    "available": True,
                    "question": p_data.get("question") or pr.get("text_body") or "Poll",
                    "multiple_answers": bool(multi),
                    "selectable_count": 0 if multi else 1,
                    "total_voters": p_data.get("total_voters", total_v),
                    "options": options,
                }
            )

    message_id = mid
    if conn is None:
        return jsonify({"available": False})

    if ctx.source_type == "android":
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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
            mao_cols = {r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()}
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
