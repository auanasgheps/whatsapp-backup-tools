"""Global and per-chat collections endpoints: calls, starred, pinned, and search."""

from __future__ import annotations

import sqlite3

from flask import Blueprint, jsonify, request

from ..context import get_viewer_context
from ..db_cache import _find_ios_call_history_db, _find_ios_db
from ..hydration import _hydrate_link_preview_rows, _hydrate_live_photos
from ..queries.calls import (
    _query_android_calls,
    _query_ios_calls,
    _query_preserved_calls,
)
from ..queries.starred_pinned import (
    _count_chat_starred,
    _query_android_pinned,
    _query_android_starred,
    _query_ios_pinned,
    _query_ios_starred,
    _query_preserved_pinned,
    _query_preserved_starred,
)

bp = Blueprint("collections", __name__)


@bp.route("/api/calls")
def api_calls():
    ctx = get_viewer_context()
    raw_filter = request.args.get("filter", "all")
    filter_type = raw_filter.strip().lower() if raw_filter else "all"
    if filter_type not in ("all", "missed", "video"):
        filter_type = "all"
    raw_offset = request.args.get("offset")
    raw_limit = request.args.get("limit")
    offset = int(raw_offset) if raw_offset and raw_offset.isdigit() else 0
    limit = min(int(raw_limit), 200) if raw_limit and raw_limit.isdigit() else 50
    offset = max(0, offset)
    limit = max(1, limit)
    fetch_limit = offset + limit + 1

    live_calls: list[dict] = []
    conn = ctx.get_wa()
    if conn is not None and ctx.source_type == "android":
        live_calls = _query_android_calls(conn, filter_type, ctx.get_archive(), fetch_limit)
    elif ctx.source_type == "ios" and ctx.wa_db_path is not None:
        call_db_p = _find_ios_call_history_db(ctx.wa_db_path, ctx.output_root)
        if call_db_p is not None and conn is not None:
            live_calls = _query_ios_calls(
                call_db_p, conn, filter_type, ctx.get_archive(), fetch_limit
            )

    pres_calls = _query_preserved_calls(ctx.get_preserved(), filter_type, fetch_limit)

    merged: list[dict] = []
    seen_ids: set[str] = set()
    for c in live_calls:
        cid = c["id"]
        if cid not in seen_ids:
            seen_ids.add(cid)
            merged.append(c)

    for c in pres_calls:
        cid = c["id"]
        if cid not in seen_ids:
            seen_ids.add(cid)
            merged.append(c)

    merged.sort(key=lambda x: x.get("timestamp_ms") or (x["timestamp"] * 1000), reverse=True)
    has_more = len(merged) > offset + limit
    paged = merged[offset : offset + limit]
    return jsonify(
        {
            "calls": paged,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
    )


@bp.route("/api/starred")
def api_starred():
    ctx = get_viewer_context()
    raw_offset = request.args.get("offset")
    raw_limit = request.args.get("limit")
    offset = int(raw_offset) if raw_offset and raw_offset.isdigit() else 0
    limit = min(int(raw_limit), 200) if raw_limit and raw_limit.isdigit() else 50
    offset = max(0, offset)
    limit = max(1, limit)
    fetch_limit = offset + limit + 1

    live_starred: list[dict] = []
    conn = ctx.get_wa()
    if conn is not None and ctx.source_type == "android":
        live_starred = _query_android_starred(
            conn, None, ctx.get_archive(), fetch_limit, ctx.android_chat_row_ids_cache
        )
    elif conn is not None and ctx.source_type == "ios":
        live_starred = _query_ios_starred(conn, None, ctx.get_archive(), fetch_limit)

    pres_starred = _query_preserved_starred(ctx.get_preserved(), None, fetch_limit)

    merged: list[dict] = []
    seen_ids: set[int] = set()
    for m in live_starred:
        mid = m["id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            merged.append(m)

    for m in pres_starred:
        mid = m["id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            merged.append(m)

    merged.sort(key=lambda x: x["timestamp"], reverse=True)
    has_more = len(merged) > offset + limit
    paged = merged[offset : offset + limit]
    return jsonify(
        {
            "messages": paged,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
    )


@bp.route("/api/chat/<chat_id>/starred")
def api_chat_starred(chat_id: str):
    ctx = get_viewer_context()
    chat_id_clean = str(chat_id).strip()
    raw_offset = request.args.get("offset")
    raw_limit = request.args.get("limit")
    offset = int(raw_offset) if raw_offset and raw_offset.isdigit() else 0
    limit = min(int(raw_limit), 200) if raw_limit and raw_limit.isdigit() else 50
    offset = max(0, offset)
    limit = max(1, limit)
    fetch_limit = offset + limit

    live_starred: list[dict] = []
    conn = ctx.get_wa()
    pres = ctx.get_preserved()
    if conn is not None and ctx.source_type == "android":
        live_starred = _query_android_starred(
            conn, chat_id_clean, ctx.get_archive(), fetch_limit, ctx.android_chat_row_ids_cache
        )
    elif conn is not None and ctx.source_type == "ios":
        live_starred = _query_ios_starred(conn, chat_id_clean, ctx.get_archive(), fetch_limit)

    pres_starred = _query_preserved_starred(pres, chat_id_clean, fetch_limit)

    merged: list[dict] = []
    seen_ids: set[int] = set()
    for m in live_starred:
        mid = m["id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            merged.append(m)

    for m in pres_starred:
        mid = m["id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            merged.append(m)

    merged.sort(key=lambda x: x["timestamp"], reverse=True)
    total_count = _count_chat_starred(
        conn, pres, ctx.source_type, chat_id_clean, ctx.android_chat_row_ids_cache
    )
    if total_count < len(merged):
        total_count = len(merged)
    has_more = total_count > offset + limit
    paged = merged[offset : offset + limit]
    return jsonify(
        {
            "count": total_count,
            "messages": paged,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }
    )


@bp.route("/api/chat/<chat_id>/pinned")
def api_chat_pinned(chat_id: str):
    ctx = get_viewer_context()
    chat_id_clean = str(chat_id).strip()
    raw_type = request.args.get("chat_type", "contact")
    chat_type = raw_type.strip().lower() if raw_type else "contact"
    live_pins: list[dict] = []
    conn = ctx.get_wa()
    if conn is not None and ctx.source_type == "android":
        live_pins = _query_android_pinned(
            conn, chat_id_clean, chat_type, ctx.get_archive(), ctx.android_chat_row_ids_cache
        )
    elif conn is not None and ctx.source_type == "ios":
        ext_db_p = (
            _find_ios_db(ctx.wa_db_path, "ExtChatDatabase.sqlite", ctx.output_root)
            if ctx.wa_db_path
            else None
        )
        live_pins = _query_ios_pinned(conn, ext_db_p, chat_id_clean, ctx.get_archive())

    pres_pins = _query_preserved_pinned(ctx.get_preserved(), chat_id_clean)

    merged: list[dict] = []
    seen_ids: set[int] = set()
    for p in live_pins:
        mid = p["id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            merged.append(p)

    for p in pres_pins:
        mid = p["id"]
        if mid not in seen_ids:
            seen_ids.add(mid)
            merged.append(p)

    first_pin = merged[0] if merged else None
    return jsonify({"pinned_message": first_pin, "pinned_messages": merged})


@bp.route("/api/search")
def api_search():
    ctx = get_viewer_context()
    q = request.args.get("q", "").strip()
    chat_id = request.args.get("chat_id")
    chat_type = request.args.get("chat_type")
    if not q:
        return jsonify({"results": [], "indexed_count": 0})

    fts_conn = ctx.get_cache()
    wa = ctx.get_wa()

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
        indexed_count = ctx.get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
        return jsonify({"results": [], "indexed_count": indexed_count})

    rowids = [r["rowid"] for r in idx_rows]
    live_rowids = [r for r in rowids if r > 0]
    pres_rowids = [-r for r in rowids if r < 0]

    results = []
    if live_rowids and wa is not None:
        placeholders = ",".join("?" * len(live_rowids))
        id_col = "m._id" if ctx.source_type == "android" else "m.Z_PK"
        select = (
            ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
        )
        extra = (
            ctx.queries.android_filter if ctx.source_type == "android" else ctx.queries.ios_filter
        )
        rows = [
            dict(r)
            for r in wa.execute(
                f"{select} WHERE {id_col} IN ({placeholders}) {extra} ORDER BY timestamp_ms ASC",
                live_rowids,
            ).fetchall()
        ]
        _hydrate_link_preview_rows(rows, ctx.source_type)
        _hydrate_live_photos(rows, ctx.source_type, wa)
        results.extend(rows)
    elif live_rowids and wa is None:
        results.extend([dict(r) for r in idx_rows if r["rowid"] > 0])

    if pres_rowids:
        pres = ctx.get_preserved()
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
    indexed_count = ctx.get_cache().execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
    return jsonify({"results": results, "indexed_count": indexed_count})
