"""Media gallery, thumbnail extraction, byte-range streaming, and SPA asset routes."""

from __future__ import annotations

import base64
import contextlib
import json
import mimetypes
import sqlite3
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    make_response,
    render_template_string,
    request,
    send_from_directory,
)

from ..chat_viewer.template import HTML_TEMPLATE
from ..context import get_version, get_viewer_context
from ..db_cache import GALLERY_PAGE_SIZE
from ..hydration import (
    _hydrate_link_preview_rows,
    _hydrate_live_photos,
    _hydrate_quoted_rows,
)
from ..preserved import (
    _merge_messages,
    _query_preserved_messages,
)
from ..queries.sql_builder import (
    _ANDROID_HD_DEDUP_CLAUSE,
    _IOS_HD_DEDUP_CLAUSE,
    _ios_chat_filter,
    _resolve_android_chat_filter,
)

bp = Blueprint("media", __name__)
_CHAT_VIEWER_DIR = Path(__file__).resolve().parents[1] / "chat_viewer"
_NON_GALLERY_TYPES = ("text", "service", "deleted", "poll", "location", "live_location")


@bp.route("/api/media")
def api_media():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    before = request.args.get("before")
    types_raw = request.args.get("types")
    conn = ctx.get_wa()
    if conn is None:
        return jsonify([])
    select = ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
    extra = ctx.queries.android_filter if ctx.source_type == "android" else ctx.queries.ios_filter
    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        is_media, ts_col = ctx.queries.android_is_media, ctx.queries.android_ts
        is_link = ctx.queries.android_is_link
        is_doc = ctx.queries.android_is_document_undownloaded
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        is_media, ts_col = ctx.queries.ios_is_media, ctx.queries.ios_ts
        is_link = ctx.queries.ios_is_link
        is_doc = ctx.queries.ios_is_document_undownloaded

    types_set = (
        {t.strip().lower() for t in types_raw.split(",") if t.strip()} if types_raw else set()
    )
    has_all = "all" in types_set
    has_link = has_all or "link" in types_set
    has_doc = has_all or "document" in types_set
    has_voice = has_all or "voice" in types_set
    has_audio = has_all or "audio" in types_set
    media_types = (types_set - {"link", "document", "all"}) if not has_all else set()
    if "voice" in media_types:
        media_types.remove("voice")
        media_types.add("audio")
    has_media = has_all or bool(media_types) or not types_set

    cand_clauses = []
    if has_media:
        if not has_all and media_types:
            ext_conds = []
            for mt in media_types:
                if mt == "video":
                    if ctx.source_type == "android":
                        ext_conds.append(
                            f"((m.message_type IN (3, 28, 43, 81) OR (COALESCE(m.message_type, 0) != 9 AND (LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.mp4' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.mov' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.3gp' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.mkv'))) AND NOT COALESCE(({ctx.queries.android_gif_cond}), 0))"
                        )
                    else:
                        ext_conds.append(
                            "((m.ZMESSAGETYPE IN (2, 39, 54) OR (COALESCE(m.ZMESSAGETYPE, 0) != 8 AND (LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.mp4' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.mov' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.3gp' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.mkv'))) AND m.ZMESSAGETYPE != 11)"
                        )
                elif mt == "gif":
                    is_gif = (
                        f"({ctx.queries.android_gif_cond} AND COALESCE(m.message_type, 0) != 9)"
                        if ctx.source_type == "android"
                        else "((LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.gif' OR m.ZMESSAGETYPE = 11) AND COALESCE(m.ZMESSAGETYPE, 0) != 8)"
                    )
                    ext_conds.append(is_gif)
                elif mt == "image":
                    if ctx.source_type == "android":
                        ext_conds.append(
                            "(m.message_type IN (1, 42) OR (COALESCE(m.message_type, 0) NOT IN (2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 43, 64, 66, 81, 82) AND (LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.jpg' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.jpeg' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.png' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.heic' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.heif')))"
                        )
                    else:
                        ext_conds.append(
                            "(m.ZMESSAGETYPE IN (1, 38) OR (COALESCE(m.ZMESSAGETYPE, 0) NOT IN (2, 3, 4, 5, 6, 7, 8, 10, 11, 14, 15, 16, 39, 46, 53, 54) AND (LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.jpg' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.jpeg' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.png' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.heic' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.heif')))"
                        )
                elif mt == "audio":
                    if ctx.source_type == "android":
                        ext_conds.append(
                            "(m.message_type IN (2, 82) OR (COALESCE(m.message_type, 0) != 9 AND (LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.opus' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.m4a' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.mp3' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.ogg' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.aac')))"
                        )
                    else:
                        ext_conds.append(
                            "(m.ZMESSAGETYPE IN (3, 53) OR (COALESCE(m.ZMESSAGETYPE, 0) != 8 AND (LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.opus' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.m4a' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.mp3' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.ogg' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.aac')))"
                        )
                elif mt == "sticker":
                    if ctx.source_type == "android":
                        ext_conds.append(
                            "(m.message_type = 20 OR (COALESCE(m.message_type, 0) != 9 AND (mm.file_path LIKE '%sticker%' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.webp' OR LOWER(COALESCE(ac.archive_path, mm.file_path, mm.media_name, '')) LIKE '%.was')))"
                        )
                    else:
                        ext_conds.append(
                            "(m.ZMESSAGETYPE = 15 OR (COALESCE(m.ZMESSAGETYPE, 0) != 8 AND (LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.webp' OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.was')))"
                        )
                elif mt == "vcard":
                    if ctx.source_type == "android":
                        ext_conds.append("m.message_type IN (4, 14)")
                    else:
                        ext_conds.append("m.ZMESSAGETYPE = 4")
            if ext_conds:
                prune_sql = " OR ".join(ext_conds)
                cand_clauses.append(f"({is_media} AND ({prune_sql}))")
            else:
                cand_clauses.append(f"({is_media})")
        else:
            cand_clauses.append(f"({is_media})")
    if has_link:
        cand_clauses.append(f"({is_link})")
    if has_doc:
        cand_clauses.append(
            f"({is_doc} OR (m.message_type IN (8, 9)))"
            if ctx.source_type == "android"
            else f"({is_doc} OR (m.ZMESSAGETYPE = 8))"
        )

    if not cand_clauses:
        return jsonify([])

    cand_pred = " OR ".join(cand_clauses)

    if has_all:
        non_gal_placeholders = ", ".join("?" for _ in _NON_GALLERY_TYPES)
        type_filter = f"media_type NOT IN ({non_gal_placeholders})"
        type_params = list(_NON_GALLERY_TYPES)
    elif not types_set:
        non_default = list(_NON_GALLERY_TYPES) + ["link", "document"]
        non_def_placeholders = ", ".join("?" for _ in non_default)
        type_filter = f"media_type NOT IN ({non_def_placeholders})"
        type_params = non_default
    else:
        filter_types = set(types_set)
        if "voice" in filter_types:
            filter_types.remove("voice")
            filter_types.add("audio")
        placeholders = ", ".join("?" for _ in filter_types)
        type_filter = f"media_type IN ({placeholders})"
        type_params = list(filter_types)

    if has_voice and not has_audio and not has_all:
        type_filter = f"({type_filter}) AND is_voice_note = 1"
    elif has_audio and not has_voice and not has_all:
        type_filter = f"({type_filter}) AND is_voice_note = 0"

    if ctx.source_type == "android" and ctx.caps.has_android_hd:
        hd_dedup_clause = _ANDROID_HD_DEDUP_CLAUSE
    elif ctx.source_type == "ios" and ctx.caps.has_ios_hd:
        hd_dedup_clause = _IOS_HD_DEDUP_CLAUSE
    else:
        hd_dedup_clause = ""

    id_select = (
        ctx.queries.android_media_id_select
        if ctx.source_type == "android"
        else ctx.queries.ios_media_id_select
    )
    id_col = "m._id" if ctx.source_type == "android" else "m.Z_PK"

    rows: list[dict] = []
    curr_before = int(before) if before else None
    seen_ids: set = set()
    while len(rows) < GALLERY_PAGE_SIZE:
        batch_ts_clause = f"AND {ts_col} < ?" if curr_before else ""
        batch_params = list(chat_params)
        if curr_before:
            batch_params.append(curr_before)
        needed = GALLERY_PAGE_SIZE - len(rows)
        batch_limit = max(needed, GALLERY_PAGE_SIZE)

        # Stage 1: Fast candidate ID selection (scans indexed table with limit)
        stage1_sql = f"""
            {id_select}
            WHERE {chat_pred} {extra} {hd_dedup_clause} AND ({cand_pred}) {batch_ts_clause}
            ORDER BY {ts_col} DESC
            LIMIT ?
        """
        id_rows = conn.execute(stage1_sql, batch_params + [batch_limit]).fetchall()
        if not id_rows:
            break
        cand_ids = [r[0] for r in id_rows]

        # Stage 2: Targeted hydration of selected IDs with full joins
        ph = ", ".join("?" for _ in cand_ids)
        stage2_sql = f"""
            SELECT * FROM (
                {select}
                WHERE {id_col} IN ({ph})
            )
            WHERE {type_filter}
            ORDER BY timestamp_ms DESC
        """
        raw_rows = [dict(r) for r in conn.execute(stage2_sql, cand_ids + type_params).fetchall()]
        if raw_rows:
            _hydrate_quoted_rows(raw_rows, ctx.source_type, conn)
            _hydrate_link_preview_rows(raw_rows, ctx.source_type)
            _hydrate_live_photos(raw_rows, ctx.source_type, conn)
            for r in raw_rows:
                mid = r.get("msg_id")
                if mid and mid in seen_ids:
                    continue
                if r.get("is_live_photo_video"):
                    continue
                if r.get("is_view_once") and not r.get("archive_path"):
                    continue
                if mid:
                    seen_ids.add(mid)
                rows.append(r)
                if len(rows) >= GALLERY_PAGE_SIZE:
                    break
        if len(id_rows) < batch_limit:
            break
        last_ts = id_rows[-1][1]
        if curr_before is not None and last_ts >= curr_before:
            curr_before = curr_before - 1
        else:
            curr_before = last_ts

    pres = ctx.get_preserved()
    if pres is not None:
        try:
            pres_rows = _query_preserved_messages(
                pres,
                chat_id=chat_id,
                chat_type=chat_type,
                before=int(before) if before else None,
                after=None,
                before_inclusive=False,
                after_inclusive=False,
                limit=GALLERY_PAGE_SIZE,
            )
            filtered_pres = []
            for pr in pres_rows:
                pt = pr.get("media_type")
                if pt in _NON_GALLERY_TYPES:
                    continue
                if has_all:
                    if pt in (
                        "image",
                        "video",
                        "audio",
                        "gif",
                        "sticker",
                        "document",
                        "vcard",
                        "link",
                    ):
                        filtered_pres.append(pr)
                elif not types_set:
                    if pt in ("image", "video", "audio", "gif", "sticker", "vcard"):
                        filtered_pres.append(pr)
                elif pt in types_set or (pt == "audio" and (has_voice or has_audio)):
                    if has_voice and not has_audio and not pr.get("is_voice_note"):
                        continue
                    if has_audio and not has_voice and pr.get("is_voice_note"):
                        continue
                    filtered_pres.append(pr)
            if filtered_pres:
                rows = _merge_messages(
                    rows, filtered_pres, descending=True, limit=GALLERY_PAGE_SIZE
                )
        except Exception:
            pass

    for r in rows:
        r.pop("audio_waveform", None)
        r.pop("waveform", None)
        r.pop("_raw_metadata", None)
        if not r.get("archive_path") and r.get("media_type") not in (
            "link",
            "text",
            "vcard",
        ):
            r["is_missing"] = 1
        else:
            r["is_missing"] = 0

    return jsonify(rows)


@bp.route("/api/media/links")
def api_media_links():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    before = request.args.get("before")
    limit = request.args.get("limit")
    conn = ctx.get_wa()
    if conn is None:
        return jsonify([])
    select = ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        is_link = ctx.queries.android_is_link
        ts_col = ctx.queries.android_ts
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        is_link = ctx.queries.ios_is_link
        ts_col = ctx.queries.ios_ts

    params = list(chat_params)
    ts_clause = ""
    if before:
        ts_clause = f"AND {ts_col} < ?"
        params.append(int(before))

    lim_clause = ""
    if limit:
        lim_clause = "LIMIT ?"
        params.append(int(limit))

    sql = (
        f"{select} WHERE {chat_pred} AND {is_link} {ts_clause} ORDER BY {ts_col} DESC {lim_clause}"
    )
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    _hydrate_link_preview_rows(rows, ctx.source_type)
    for r in rows:
        r.pop("audio_waveform", None)
        r.pop("waveform", None)
        r.pop("_raw_metadata", None)
    return jsonify(rows)


@bp.route("/api/media/documents")
def api_media_documents():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    conn = ctx.get_wa()
    if conn is None:
        return jsonify([])
    select = ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        is_doc = f"({ctx.queries.android_is_document_undownloaded} OR (m.message_type IN (8, 9)))"
        ts_col = ctx.queries.android_ts
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        is_doc = f"({ctx.queries.ios_is_document_undownloaded} OR (m.ZMESSAGETYPE = 8))"
        ts_col = ctx.queries.ios_ts
    sql = f"{select} WHERE {chat_pred} AND {is_doc} ORDER BY {ts_col} DESC"
    rows = [dict(r) for r in conn.execute(sql, chat_params).fetchall()]
    _hydrate_quoted_rows(rows, ctx.source_type, conn)
    _hydrate_link_preview_rows(rows, ctx.source_type)
    _hydrate_live_photos(rows, ctx.source_type, conn)
    for r in rows:
        r.pop("audio_waveform", None)
        r.pop("waveform", None)
        r.pop("_raw_metadata", None)
    return jsonify(rows)


@bp.route("/api/media/count")
def api_media_count():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    conn = ctx.get_wa()
    if conn is None:
        return jsonify({})

    cache_key = (chat_id, chat_type)
    if cache_key in ctx.media_counts_cache:
        return jsonify(ctx.media_counts_cache[cache_key])

    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        mvom_join = (
            "LEFT JOIN message_view_once_media mvom ON mvom.message_row_id = m._id"
            if ctx.caps.has_message_view_once_media
            else ""
        )
        mvom_or = (
            " OR mvom.message_row_id IS NOT NULL" if ctx.caps.has_message_view_once_media else ""
        )
        mrev_join = (
            "LEFT JOIN message_revoked mrev ON mrev.message_row_id = m._id"
            if ctx.caps.has_message_revoked
            else ""
        )
        mrev_pred = "mrev.message_row_id IS NOT NULL" if ctx.caps.has_message_revoked else "0"
        android_origin_check = "m.origin = 1" if ctx.caps.has_msg_origin else "0"
        android_file_path = "mm.file_path"
        android_voice_cond = (
            f"(m.message_type IN (2, 82) AND ({android_origin_check} "
            f"OR COALESCE(ac.archive_path, {android_file_path}, '') LIKE '%Voice Notes%' "
            f"OR LOWER(COALESCE(ac.archive_path, {android_file_path}, '')) LIKE '%.opus'))"
        )
        hd_dedup_clause = _ANDROID_HD_DEDUP_CLAUSE if ctx.caps.has_android_hd else ""
        has_msg_assoc = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_association'"
            ).fetchone()
        )
        motion_photo_clause = (
            "AND m._id NOT IN (SELECT child_message_row_id FROM message_association WHERE association_type = 11)"
            if has_msg_assoc
            else ""
        )
        sql_media = f"""
            SELECT
                CASE
                    WHEN m.message_type IN (15, 64) OR {mrev_pred} THEN 'deleted'
                    WHEN m.message_type IN (1, 42) THEN 'image'
                    WHEN {ctx.queries.android_gif_cond} THEN 'gif'
                    WHEN m.message_type IN (3, 28, 43, 81) THEN 'video'
                    WHEN m.message_type = 20 THEN 'sticker'
                    WHEN m.message_type = 9 OR (mm.file_path IS NULL AND mm.media_name IS NOT NULL AND mm.media_name != '') THEN 'document'
                    WHEN m.message_type IN (4, 14) THEN 'vcard'
                    WHEN m.message_type IN (2, 82) THEN
                        CASE WHEN {android_voice_cond} THEN 'voice' ELSE 'audio' END
                    ELSE 'other'
                END AS m_type,
                CASE WHEN (m.message_type IN (42, 43, 82){mvom_or}) THEN 1 ELSE 0 END AS is_vo,
                CASE WHEN ac.archive_path IS NOT NULL AND ac.archive_path != '' THEN 1 ELSE 0 END AS is_arch,
                COUNT(*) as cnt
            FROM message m
            LEFT JOIN message_media mm ON mm.message_row_id = m._id
            {mvom_join}
            {mrev_join}
            LEFT JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
            WHERE {chat_pred} {hd_dedup_clause} {motion_photo_clause}
              AND m.message_type NOT IN (0, 7)
            GROUP BY m_type, is_vo, is_arch
        """
        sql_link = f"""
            SELECT COUNT(*)
            FROM message m
            WHERE {chat_pred}
              AND (m.message_type IS NULL OR m.message_type = 0)
              AND m.text_data IS NOT NULL
              AND (INSTR(LOWER(m.text_data), 'http://') > 0 OR INSTR(LOWER(m.text_data), 'https://') > 0)
        """
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        ios_origin_check = "mi.ZMEDIAORIGIN = 1" if ctx.caps.has_ios_media_origin else "0"
        ios_voice_cond = (
            f"(m.ZMESSAGETYPE IN (3, 53) AND ({ios_origin_check} "
            f"OR LOWER(COALESCE(ac.archive_path, mi.ZMEDIALOCALPATH, '')) LIKE '%.opus'))"
        )
        hd_dedup_clause = _IOS_HD_DEDUP_CLAUSE if ctx.caps.has_ios_hd else ""
        ios_live_photo_clause = (
            "AND m.ZSTANZAID NOT IN (SELECT stanza_id FROM ext.message_parent_association WHERE type = 11)"
            if ctx.caps.has_ios_hd
            else ""
        )
        sql_media = f"""
            SELECT
                CASE
                    WHEN m.ZMESSAGETYPE = 14 THEN 'deleted'
                    WHEN m.ZMESSAGETYPE IN (1, 38) THEN 'image'
                    WHEN m.ZMESSAGETYPE IN (2, 39, 54) THEN 'video'
                    WHEN m.ZMESSAGETYPE = 11 THEN 'gif'
                    WHEN m.ZMESSAGETYPE = 15 THEN 'sticker'
                    WHEN m.ZMESSAGETYPE = 8 THEN 'document'
                    WHEN m.ZMESSAGETYPE = 4 THEN 'vcard'
                    WHEN m.ZMESSAGETYPE IN (3, 53) THEN
                        CASE WHEN {ios_voice_cond} THEN 'voice' ELSE 'audio' END
                    ELSE 'other'
                END AS m_type,
                CASE WHEN m.ZMESSAGETYPE IN (38, 39, 53) THEN 1 ELSE 0 END AS is_vo,
                CASE WHEN ac.archive_path IS NOT NULL AND ac.archive_path != '' THEN 1 ELSE 0 END AS is_arch,
                COUNT(*) as cnt
            FROM ZWAMESSAGE m
            LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
            LEFT JOIN arch.archive_copies ac ON ac.original_path = COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)
            WHERE {chat_pred} {hd_dedup_clause} {ios_live_photo_clause}
              AND NOT (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE IN (0, 5, 6, 7, 10, 14, 16, 46))
            GROUP BY m_type, is_vo, is_arch
        """
        sql_link = f"""
            SELECT COUNT(*)
            FROM ZWAMESSAGE m
            WHERE {chat_pred}
              AND (
                m.ZMESSAGETYPE = 7
                OR (
                    (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)
                    AND m.ZTEXT IS NOT NULL
                    AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0 OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                )
              )
        """

    m_rows = conn.execute(sql_media, chat_params).fetchall()
    link_cnt = conn.execute(sql_link, chat_params).fetchone()[0]

    by_type: dict = {}
    archived = 0
    for r in m_rows:
        t = r[0]
        if t in _NON_GALLERY_TYPES or t == "other":
            continue
        is_vo = r[1]
        is_arch = r[2]
        cnt = r[3]
        if is_vo and not is_arch:
            continue
        by_type.setdefault(t, {"count": 0, "missing": 0})
        by_type[t]["count"] += cnt
        if is_arch:
            archived += cnt
        elif t not in ("link", "text", "vcard"):
            by_type[t]["missing"] += cnt

    if link_cnt > 0:
        by_type.setdefault("link", {"count": 0, "missing": 0})
        by_type["link"]["count"] += link_cnt

    pres = ctx.get_preserved()
    if pres is not None:
        try:
            pres_rows = _query_preserved_messages(
                pres,
                chat_id=chat_id,
                chat_type=chat_type,
                before=None,
                after=None,
                before_inclusive=False,
                after_inclusive=False,
                limit=100000,
            )
            for pr in pres_rows:
                pt = pr.get("media_type")
                if pt in _NON_GALLERY_TYPES:
                    continue
                if pt in ("image", "video", "audio", "gif", "sticker", "document", "vcard", "link"):
                    effective_pt = "voice" if (pt == "audio" and pr.get("is_voice_note")) else pt
                    by_type.setdefault(effective_pt, {"count": 0, "missing": 0})
                    by_type[effective_pt]["count"] += 1
                    if pr.get("archive_path"):
                        archived += 1
                    elif effective_pt not in ("link", "text", "vcard") and not pr.get(
                        "is_view_once"
                    ):
                        by_type[effective_pt]["missing"] += 1
        except Exception:
            pass

    total = sum(d["count"] for d in by_type.values())
    result = {"total": total, "archived": archived, "by_type": by_type}
    ctx.media_counts_cache[cache_key] = result
    return jsonify(result)


@bp.route("/media/<path:p>")
def serve_media(p):
    ctx = get_viewer_context()
    try:
        safe = (ctx.output_root / p).resolve()
        root = ctx.output_root.resolve()
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


def _fail_thumb_404(ctx, msg_id: str | int) -> tuple[str, int]:
    cache = getattr(ctx, "thumbnail_404_cache", None)
    if cache is not None:
        if len(cache) > 3000:
            cache.clear()
        cache.add(str(msg_id))
    return "Thumbnail not found", 404


@bp.route("/api/thumbnail/<msg_id>")
def api_thumbnail(msg_id):
    ctx = get_viewer_context()
    cache = getattr(ctx, "thumbnail_404_cache", None)
    if cache is not None and str(msg_id) in cache:
        return "Thumbnail not found", 404
    conn = ctx.get_wa()
    int_msg_id: int | None = None
    with contextlib.suppress(ValueError, TypeError):
        int_msg_id = int(msg_id)

    if int_msg_id is not None and int_msg_id < 0:
        pres = ctx.get_preserved()
        if pres is not None:
            pres_cols = {
                r[1] for r in pres.execute("PRAGMA table_info(preserved_messages)").fetchall()
            }
            sm_col = ", service_metadata" if "service_metadata" in pres_cols else ""
            row = pres.execute(
                f"SELECT stanza_id, archive_path{sm_col} FROM preserved_messages WHERE rowid = ?",
                (-int_msg_id,),
            ).fetchone()
            if row:
                if "archive_path" in pres_cols and row["archive_path"]:
                    cand = (ctx.output_root / row["archive_path"]).resolve()
                    if cand.exists():
                        try:
                            data = cand.read_bytes()
                            if data:
                                mime = "image/jpeg"
                                if cand.suffix.lower() == ".png":
                                    mime = "image/png"
                                elif cand.suffix.lower() == ".webp":
                                    mime = "image/webp"
                                return Response(data, mimetype=mime)
                        except OSError:
                            pass
                if "service_metadata" in pres_cols and row["service_metadata"]:
                    try:
                        sm = (
                            json.loads(row["service_metadata"])
                            if isinstance(row["service_metadata"], str)
                            else row["service_metadata"]
                        )
                        if isinstance(sm, dict) and "link_preview" in sm:
                            tb_b64 = sm["link_preview"].get("thumbnail_b64")
                            if tb_b64:
                                thumb_data = base64.b64decode(tb_b64)
                                if thumb_data:
                                    return Response(thumb_data, mimetype="image/jpeg")
                    except Exception:
                        pass
                if row["stanza_id"] and conn is not None:
                    int_msg_id = None
                    msg_id = row["stanza_id"]

    if conn is None:
        return "Not found", 404

    if int_msg_id is None:
        # Stanza / string ID lookup fallback
        if ctx.source_type == "android" and ctx.caps.has_android_key_id:
            row = conn.execute(
                "SELECT _id FROM message WHERE key_id = ? LIMIT 1", (str(msg_id),)
            ).fetchone()
            if row:
                int_msg_id = row[0]
        elif ctx.source_type == "ios":
            row = conn.execute(
                "SELECT Z_PK FROM ZWAMESSAGE WHERE ZSTANZAID = ? LIMIT 1", (str(msg_id),)
            ).fetchone()
            if row:
                int_msg_id = row[0]

    if int_msg_id is None:
        pres = ctx.get_preserved()
        if pres is not None:
            pres_cols = {
                r[1] for r in pres.execute("PRAGMA table_info(preserved_messages)").fetchall()
            }
            if "service_metadata" in pres_cols:
                pres_row = pres.execute(
                    "SELECT service_metadata FROM preserved_messages WHERE stanza_id = ? LIMIT 1",
                    (str(msg_id),),
                ).fetchone()
                if pres_row and pres_row["service_metadata"]:
                    try:
                        sm = (
                            json.loads(pres_row["service_metadata"])
                            if isinstance(pres_row["service_metadata"], str)
                            else pres_row["service_metadata"]
                        )
                        if isinstance(sm, dict) and "link_preview" in sm:
                            tb_b64 = sm["link_preview"].get("thumbnail_b64")
                            if tb_b64:
                                thumb_data = base64.b64decode(tb_b64)
                                if thumb_data:
                                    return Response(thumb_data, mimetype="image/jpeg")
                    except Exception:
                        pass
        return _fail_thumb_404(ctx, msg_id)

    msg_id_val = int_msg_id

    thumb_bytes = None
    if ctx.source_type == "android":
        if ctx.caps.has_message_thumbnail:
            row = conn.execute(
                "SELECT thumbnail FROM message_thumbnail WHERE message_row_id = ?",
                (msg_id_val,),
            ).fetchone()
            if row and row["thumbnail"]:
                thumb_bytes = row["thumbnail"]

        if not thumb_bytes:
            has_mm = False
            try:
                has_mm = bool(
                    conn.execute(
                        "SELECT 1 FROM message_media WHERE message_row_id = ? LIMIT 1",
                        (msg_id_val,),
                    ).fetchone()
                )
            except sqlite3.OperationalError:
                pass

            if has_mm and ctx.caps.has_media_hash_thumbnail and ctx.caps.has_mm_file_hash:
                row = conn.execute(
                    """
                    SELECT mht.thumbnail
                    FROM media_hash_thumbnail mht
                    JOIN message_media mm ON mm.file_hash = mht.media_hash
                    WHERE mm.message_row_id = ?
                    """,
                    (msg_id_val,),
                ).fetchone()
                if row and row["thumbnail"]:
                    thumb_bytes = row["thumbnail"]

            has_mq = False
            if not thumb_bytes:
                try:
                    has_mq = bool(
                        conn.execute(
                            "SELECT 1 FROM message_quoted WHERE message_row_id = ? LIMIT 1",
                            (msg_id_val,),
                        ).fetchone()
                    )
                except sqlite3.OperationalError:
                    pass

            if not thumb_bytes and has_mq:
                if ctx.caps.has_message_quoted_media:
                    row = conn.execute(
                        "SELECT thumbnail FROM message_quoted_media WHERE message_row_id = ?",
                        (msg_id_val,),
                    ).fetchone()
                    if row and row["thumbnail"]:
                        thumb_bytes = row["thumbnail"]

                if not thumb_bytes and ctx.caps.has_message_quoted_text:
                    row = conn.execute(
                        "SELECT thumbnail FROM message_quoted_text WHERE message_row_id = ?",
                        (msg_id_val,),
                    ).fetchone()
                    if row and row["thumbnail"]:
                        thumb_bytes = row["thumbnail"]

                if not thumb_bytes and ctx.caps.has_message_quoted_location:
                    row = conn.execute(
                        "SELECT thumbnail FROM message_quoted_location WHERE message_row_id = ?",
                        (msg_id_val,),
                    ).fetchone()
                    if row and row["thumbnail"]:
                        thumb_bytes = row["thumbnail"]

            if not thumb_bytes and ctx.caps.has_android_key_id:
                if ctx.caps.has_message_quoted_media and ctx.caps.has_mq_key_id:
                    row = conn.execute(
                        """
                        SELECT mqm.thumbnail
                        FROM message m
                        JOIN message_quoted mq ON mq.key_id = m.key_id
                        JOIN message_quoted_media mqm ON mqm.message_row_id = mq.message_row_id
                        WHERE m._id = ? AND mqm.thumbnail IS NOT NULL
                        LIMIT 1
                        """,
                        (msg_id_val,),
                    ).fetchone()
                    if row and row["thumbnail"]:
                        thumb_bytes = row["thumbnail"]

                if not thumb_bytes and ctx.caps.has_message_quoted_text and ctx.caps.has_mq_key_id:
                    row = conn.execute(
                        """
                        SELECT mqt.thumbnail
                        FROM message m
                        JOIN message_quoted mq ON mq.key_id = m.key_id
                        JOIN message_quoted_text mqt ON mqt.message_row_id = mq.message_row_id
                        WHERE m._id = ? AND mqt.thumbnail IS NOT NULL
                        LIMIT 1
                        """,
                        (msg_id_val,),
                    ).fetchone()
                    if row and row["thumbnail"]:
                        thumb_bytes = row["thumbnail"]

                if not thumb_bytes and has_mq and ctx.caps.has_mq_key_id:
                    if ctx.caps.has_message_thumbnail:
                        row = conn.execute(
                            """
                            SELECT mt.thumbnail
                            FROM message_quoted mq
                            JOIN message m_orig ON m_orig.key_id = mq.key_id
                            JOIN message_thumbnail mt ON mt.message_row_id = m_orig._id
                            WHERE mq.message_row_id = ? AND mt.thumbnail IS NOT NULL
                            LIMIT 1
                            """,
                            (msg_id_val,),
                        ).fetchone()
                        if row and row["thumbnail"]:
                            thumb_bytes = row["thumbnail"]

                    if not thumb_bytes and ctx.caps.has_message_quoted_media:
                        row = conn.execute(
                            """
                            SELECT mqm.thumbnail
                            FROM message_quoted mq
                            JOIN message m_orig ON m_orig.key_id = mq.key_id
                            JOIN message_quoted_media mqm ON mqm.message_row_id = m_orig._id
                            WHERE mq.message_row_id = ? AND mqm.thumbnail IS NOT NULL
                            LIMIT 1
                            """,
                            (msg_id_val,),
                        ).fetchone()
                        if row and row["thumbnail"]:
                            thumb_bytes = row["thumbnail"]

                    if not thumb_bytes and ctx.caps.has_message_quoted_text:
                        row = conn.execute(
                            """
                            SELECT mqt.thumbnail
                            FROM message_quoted mq
                            JOIN message m_orig ON m_orig.key_id = mq.key_id
                            JOIN message_quoted_text mqt ON mqt.message_row_id = m_orig._id
                            WHERE mq.message_row_id = ? AND mqt.thumbnail IS NOT NULL
                            LIMIT 1
                            """,
                            (msg_id_val,),
                        ).fetchone()
                        if row and row["thumbnail"]:
                            thumb_bytes = row["thumbnail"]

                    if (
                        not thumb_bytes
                        and ctx.caps.has_media_hash_thumbnail
                        and ctx.caps.has_mm_file_hash
                    ):
                        row = conn.execute(
                            """
                            SELECT mht.thumbnail
                            FROM message_quoted mq
                            JOIN message m_orig ON m_orig.key_id = mq.key_id
                            JOIN message_media mm ON mm.message_row_id = m_orig._id
                            JOIN media_hash_thumbnail mht ON mht.media_hash = mm.file_hash
                            WHERE mq.message_row_id = ? AND mht.thumbnail IS NOT NULL
                            LIMIT 1
                            """,
                            (msg_id_val,),
                        ).fetchone()
                        if row and row["thumbnail"]:
                            thumb_bytes = row["thumbnail"]
    elif ctx.source_type == "ios":
        thumb_cols = []
        if "ZXMPPTHUMBPATH" in ctx.caps.mi_cols:
            thumb_cols.append("mi.ZXMPPTHUMBPATH")
        if "ZTHUMBNAILLOCALPATH" in ctx.caps.mi_cols:
            thumb_cols.append("mi.ZTHUMBNAILLOCALPATH")
        if ctx.caps.has_ios_mdi and ctx.caps.has_ios_mdi_thumb:
            thumb_cols.append("mdi.ZTHUMBNAILPATH")
        if thumb_cols:
            mdi_join_clause = (
                "LEFT JOIN ZWAMESSAGEDATAITEM mdi ON mdi.ZMESSAGE = m.Z_PK AND mdi.ZINDEX = 0"
                if (ctx.caps.has_ios_mdi and ctx.caps.has_ios_mdi_thumb)
                else ""
            )
            row = conn.execute(
                f"""
                SELECT {", ".join(thumb_cols)}
                FROM ZWAMESSAGE m
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                {mdi_join_clause}
                WHERE m.Z_PK = ?
                """,
                (msg_id_val,),
            ).fetchone()
            if row:
                thumb_path = (
                    row["ZXMPPTHUMBPATH"]
                    if "ZXMPPTHUMBPATH" in ctx.caps.mi_cols and row["ZXMPPTHUMBPATH"]
                    else (
                        row["ZTHUMBNAILLOCALPATH"]
                        if "ZTHUMBNAILLOCALPATH" in ctx.caps.mi_cols and row["ZTHUMBNAILLOCALPATH"]
                        else (
                            row["ZTHUMBNAILPATH"]
                            if (
                                ctx.caps.has_ios_mdi
                                and ctx.caps.has_ios_mdi_thumb
                                and "ZTHUMBNAILPATH" in row.keys()
                                and row["ZTHUMBNAILPATH"]
                            )
                            else None
                        )
                    )
                )
                if thumb_path:
                    cand_paths = [
                        (
                            ctx.output_root
                            / "Whatsapp Databases"
                            / ".thumbnails"
                            / Path(thumb_path).name
                        ).resolve(),
                        (ctx.output_root / thumb_path).resolve(),
                        (ctx.output_root / "Message" / thumb_path).resolve(),
                    ]
                    for app_domain in (
                        "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared",
                        "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
                    ):
                        cand_paths.append((ctx.output_root / app_domain / thumb_path).resolve())

                    for cand in cand_paths:
                        if cand.exists():
                            try:
                                thumb_bytes = cand.read_bytes()
                                if thumb_bytes:
                                    break
                            except OSError:
                                pass

                    if not thumb_bytes:
                        try:
                            ac_row = conn.execute(
                                "SELECT archive_path FROM arch.archive_copies WHERE original_path IN (?, ?, ?, ?)",
                                (
                                    thumb_path,
                                    "Message/" + thumb_path,
                                    "Media/" + thumb_path,
                                    Path(thumb_path).name,
                                ),
                            ).fetchone()
                            if ac_row and ac_row["archive_path"]:
                                cand_ac = (ctx.output_root / ac_row["archive_path"]).resolve()
                                if cand_ac.exists():
                                    try:
                                        thumb_bytes = cand_ac.read_bytes()
                                    except OSError:
                                        pass
                        except sqlite3.OperationalError:
                            pass

        if not thumb_bytes and "ZMETADATA" in ctx.caps.mi_cols:
            meta_row = conn.execute(
                """
                SELECT mi.ZMETADATA
                FROM ZWAMESSAGE m
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                WHERE m.Z_PK = ?
                """,
                (msg_id_val,),
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
                    # 1. Check if another message replied to this message via ZPARENTMESSAGE
                    reply_row = conn.execute(
                        """
                        SELECT reply_mi.ZMETADATA
                        FROM ZWAMESSAGE reply_m
                        JOIN ZWAMEDIAITEM reply_mi ON reply_mi.Z_PK = reply_m.ZMEDIAITEM
                        WHERE reply_m.ZPARENTMESSAGE = ?
                          AND reply_mi.ZMETADATA IS NOT NULL
                          AND INSTR(reply_mi.ZMETADATA, X'ffd8ff') > 0
                        LIMIT 1
                        """,
                        (msg_id_val,),
                    ).fetchone()
                    if reply_row and reply_row[0]:
                        rblob = reply_row[0]
                        if isinstance(rblob, bytes):
                            j_idx = rblob.find(b"\xff\xd8\xff")
                            if j_idx != -1:
                                e_idx = rblob.find(b"\xff\xd9", j_idx)
                                if e_idx != -1:
                                    thumb_bytes = rblob[j_idx : e_idx + 2]

                    # 2. Modern iOS: replies store parent stanza in reply_mi.ZMETADATA (scoped to same chat session)
                    if not thumb_bytes:
                        reply_row = conn.execute(
                            """
                            SELECT reply_mi.ZMETADATA
                            FROM ZWAMESSAGE m
                            JOIN ZWAMESSAGE reply_m ON reply_m.ZCHATSESSION = m.ZCHATSESSION
                            JOIN ZWAMEDIAITEM reply_mi ON reply_mi.Z_PK = reply_m.ZMEDIAITEM
                            WHERE m.Z_PK = ?
                              AND reply_mi.ZMETADATA IS NOT NULL
                              AND INSTR(reply_mi.ZMETADATA, m.ZSTANZAID) > 0
                              AND INSTR(reply_mi.ZMETADATA, X'ffd8ff') > 0
                            LIMIT 1
                            """,
                            (msg_id_val,),
                        ).fetchone()
                        if reply_row and reply_row[0]:
                            rblob = reply_row[0]
                            if isinstance(rblob, bytes):
                                j_idx = rblob.find(b"\xff\xd8\xff")
                                if j_idx != -1:
                                    e_idx = rblob.find(b"\xff\xd9", j_idx)
                                    if e_idx != -1:
                                        thumb_bytes = rblob[j_idx : e_idx + 2]

                    # 2. Check if this message was a reply quoting a parent with thumbnail
                    if not thumb_bytes:
                        parent_row = conn.execute(
                            """
                            SELECT parent_mi.ZMETADATA
                            FROM ZWAMESSAGE m
                            JOIN ZWAMESSAGE parent_m ON parent_m.Z_PK = m.ZPARENTMESSAGE
                            JOIN ZWAMEDIAITEM parent_mi ON parent_mi.Z_PK = parent_m.ZMEDIAITEM
                            WHERE m.Z_PK = ?
                              AND parent_mi.ZMETADATA IS NOT NULL
                              AND INSTR(parent_mi.ZMETADATA, X'ffd8ff') > 0
                            LIMIT 1
                            """,
                            (msg_id_val,),
                        ).fetchone()
                        if parent_row and parent_row[0]:
                            rblob = parent_row[0]
                            if isinstance(rblob, bytes):
                                j_idx = rblob.find(b"\xff\xd8\xff")
                                if j_idx != -1:
                                    e_idx = rblob.find(b"\xff\xd9", j_idx)
                                    if e_idx != -1:
                                        thumb_bytes = rblob[j_idx : e_idx + 2]
                except sqlite3.OperationalError:
                    pass

    if not thumb_bytes or len(thumb_bytes) < 16:
        return _fail_thumb_404(ctx, msg_id)

    mime = "image/jpeg"
    if thumb_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif thumb_bytes.startswith(b"RIFF") and b"WEBP" in thumb_bytes[:16]:
        mime = "image/webp"

    resp = Response(thumb_bytes, 200, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    resp.headers["Content-Length"] = len(thumb_bytes)
    return resp


@bp.route("/static/app.css")
def serve_app_css():
    resp = make_response(send_from_directory(_CHAT_VIEWER_DIR, "app.css"))
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@bp.route("/static/app.js")
def serve_app_js():
    from ..chat_viewer import builder

    js_code = builder.get_bundle_content(current_app.debug)
    resp = Response(js_code, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@bp.route("/static/favicon.svg")
@bp.route("/favicon.ico")
def serve_favicon():
    return send_from_directory(_CHAT_VIEWER_DIR, "favicon.svg", mimetype="image/svg+xml")


@bp.route("/")
def index():
    ctx = get_viewer_context()
    p_name = (
        "iOS" if ctx.source_type == "ios" else ("Android" if ctx.source_type == "android" else "")
    )
    return render_template_string(
        HTML_TEMPLATE,
        output_root=str(ctx.output_root),
        base_path=current_app.config.get("BASE_PATH", ""),
        version=get_version(),
        platform_name=p_name,
        schema_alert=current_app.config.get("SCHEMA_ALERT"),
    )
