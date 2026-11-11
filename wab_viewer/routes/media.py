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
from ..queries.sql_builder import (
    _ANDROID_HD_DEDUP_CLAUSE,
    _IOS_HD_DEDUP_CLAUSE,
    _ios_chat_filter,
    _resolve_android_chat_filter,
)

bp = Blueprint("media", __name__)
_CHAT_VIEWER_DIR = Path(__file__).resolve().parents[1] / "chat_viewer"


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
        _hydrate_link_preview_rows(rows, ctx.source_type)
        _hydrate_live_photos(rows, ctx.source_type, conn)
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
                if mt == "video":
                    vid_exts = " OR ".join(
                        f"LOWER(ac.archive_path) LIKE '{ext}'" for ext in _EXT_PATTERNS["video"]
                    )
                    not_gif = (
                        f"NOT ({ctx.queries.android_gif_cond})"
                        if ctx.source_type == "android"
                        else "m.ZMESSAGETYPE != 11"
                    )
                    ext_conds.append(f"(({vid_exts}) AND {not_gif})")
                elif mt == "gif":
                    is_gif = (
                        ctx.queries.android_gif_cond
                        if ctx.source_type == "android"
                        else "(LOWER(ac.archive_path) LIKE '%.gif' OR m.ZMESSAGETYPE = 11)"
                    )
                    ext_conds.append(is_gif)
                else:
                    for ext in _EXT_PATTERNS.get(mt, []):
                        ext_conds.append(f"LOWER(ac.archive_path) LIKE '{ext}'")
                    if mt == "vcard":
                        if ctx.source_type == "android":
                            ext_conds.append("m.message_type IN (4, 14)")
                        else:
                            ext_conds.append("m.ZMESSAGETYPE = 4")
                    if mt == "sticker":
                        if ctx.source_type == "android":
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

    if ctx.source_type == "android" and ctx.caps.has_android_hd:
        hd_dedup_clause = _ANDROID_HD_DEDUP_CLAUSE
    elif ctx.source_type == "ios" and ctx.caps.has_ios_hd:
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
        dict(r) for r in conn.execute(sql, params + type_params + [GALLERY_PAGE_SIZE]).fetchall()
    ]
    _hydrate_quoted_rows(rows, ctx.source_type, conn)
    _hydrate_link_preview_rows(rows, ctx.source_type)
    _hydrate_live_photos(rows, ctx.source_type, conn)
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
    _hydrate_quoted_rows(rows, ctx.source_type, conn)
    _hydrate_link_preview_rows(rows, ctx.source_type)
    _hydrate_live_photos(rows, ctx.source_type, conn)
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
        is_doc = ctx.queries.android_is_document_undownloaded
        ts_col = ctx.queries.android_ts
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        is_doc = ctx.queries.ios_is_document_undownloaded
        ts_col = ctx.queries.ios_ts
    sql = f"{select} WHERE {chat_pred} AND {is_doc} ORDER BY {ts_col} DESC"
    rows = [dict(r) for r in conn.execute(sql, chat_params).fetchall()]
    _hydrate_quoted_rows(rows, ctx.source_type, conn)
    _hydrate_link_preview_rows(rows, ctx.source_type)
    _hydrate_live_photos(rows, ctx.source_type, conn)
    return jsonify(rows)


@bp.route("/api/media/count")
def api_media_count():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")
    conn = ctx.get_wa()
    if conn is None:
        return jsonify({})
    select = ctx.queries.android_select if ctx.source_type == "android" else ctx.queries.ios_select
    extra = ctx.queries.android_filter if ctx.source_type == "android" else ctx.queries.ios_filter
    if ctx.source_type == "android":
        chat_pred, chat_params = _resolve_android_chat_filter(
            conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
        )
        is_media = ctx.queries.android_is_media
        is_link = ctx.queries.android_is_link
        is_doc_undownloaded = ctx.queries.android_is_document_undownloaded
    else:
        chat_pred, chat_params = _ios_chat_filter(chat_id)
        is_media = ctx.queries.ios_is_media
        is_link = ctx.queries.ios_is_link
        is_doc_undownloaded = ctx.queries.ios_is_document_undownloaded
    rows = conn.execute(
        f"{select} WHERE {chat_pred} {extra} AND {is_media}", chat_params
    ).fetchall()
    link_rows = conn.execute(f"{select} WHERE {chat_pred} AND {is_link}", chat_params).fetchall()
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
        if t not in ("link", "text") and not r["archive_path"] and not is_vo:
            by_type[t]["missing"] += 1
    for r in list(doc_rows) + list(link_rows):
        t = r["media_type"]
        by_type.setdefault(t, {"count": 0, "missing": 0})
        by_type[t]["count"] += 1
    return jsonify({"total": total, "archived": archived, "by_type": by_type})


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


@bp.route("/api/thumbnail/<msg_id>")
def api_thumbnail(msg_id):
    ctx = get_viewer_context()
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
        return "Thumbnail not found", 404

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

        if not thumb_bytes and ctx.caps.has_media_hash_thumbnail and ctx.caps.has_mm_file_hash:
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

        if not thumb_bytes and ctx.caps.has_message_quoted_media:
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

        if (
            not thumb_bytes
            and ctx.caps.has_message_quoted_media
            and ctx.caps.has_mq_key_id
            and ctx.caps.has_android_key_id
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
                (msg_id_val,),
            ).fetchone()
            if row and row["thumbnail"]:
                thumb_bytes = row["thumbnail"]

        if (
            not thumb_bytes
            and ctx.caps.has_message_quoted_text
            and ctx.caps.has_mq_key_id
            and ctx.caps.has_android_key_id
        ):
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

        if not thumb_bytes and ctx.caps.has_mq_key_id and ctx.caps.has_android_key_id:
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

            if not thumb_bytes and ctx.caps.has_media_hash_thumbnail and ctx.caps.has_mm_file_hash:
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


@bp.route("/static/app.css")
def serve_app_css():
    resp = make_response(send_from_directory(_CHAT_VIEWER_DIR, "app.css"))
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@bp.route("/static/app.js")
def serve_app_js():
    resp = make_response(send_from_directory(_CHAT_VIEWER_DIR, "app.js"))
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
        version=get_version(),
        platform_name=p_name,
        schema_alert=current_app.config.get("SCHEMA_ALERT"),
    )
