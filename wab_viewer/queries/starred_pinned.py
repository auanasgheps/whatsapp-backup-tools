"""Starred and pinned message query helpers across Android, iOS, and preserved DBs."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from ..db_cache import _ensure_jid_map_resolved
from .calls import _get_contacts_lookup, _resolve_ios_chat_jids
from .sql_builder import (
    _clean_user_sql,
    _resolve_android_chat_filter,
    _resolve_android_chat_row_ids,
)


def _query_android_starred(
    conn: sqlite3.Connection,
    target_chat_id: str | None,
    arch_conn: sqlite3.Connection | None,
    fetch_limit: int | None,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> list[dict]:
    """Query starred messages from Android msgstore.db."""
    has_msg = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
    ).fetchone()
    if not has_msg:
        return []

    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(message)").fetchall()}
    if "starred" not in msg_cols:
        return []

    has_jm_resolved = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='_jid_map_resolved'"
        ).fetchone()
    )
    if not has_jm_resolved:
        _ensure_jid_map_resolved(conn, arch_conn)

    has_mm = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media'"
        ).fetchone()
    )
    mm_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(message_media)").fetchall()}
        if has_mm
        else set()
    )
    mm_join = "LEFT JOIN message_media mm ON mm.message_row_id = m._id" if has_mm else ""
    mm_file = "mm.file_path" if has_mm else "NULL"
    mm_mime = "mm.mime_type" if has_mm else "NULL"
    mm_duration = "COALESCE(mm.media_duration, 0)" if "media_duration" in mm_cols else "0"
    mm_name = "COALESCE(mm.media_name, '')" if "media_name" in mm_cols else "''"

    has_ml = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_location'"
        ).fetchone()
    )
    ml_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(message_location)").fetchall()}
        if has_ml
        else set()
    )
    ml_join = "LEFT JOIN message_location ml ON ml.message_row_id = m._id" if has_ml else ""
    ml_lat = "ml.latitude" if "latitude" in ml_cols else "NULL"
    ml_lng = "ml.longitude" if "longitude" in ml_cols else "NULL"
    ml_place = (
        "COALESCE(ml.place_name, ml.place_address, '')"
        if ("place_name" in ml_cols or "place_address" in ml_cols)
        else "''"
    )

    has_chat = bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'").fetchone()
    )
    chat_join = "LEFT JOIN chat c ON c._id = m.chat_row_id" if has_chat else ""

    has_jid = bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid'").fetchone()
    )
    jid_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(jid)").fetchall()} if has_jid else set()
    )
    has_jid_server = "server" in jid_cols
    has_jid_raw_string = "raw_string" in jid_cols
    has_ldn = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lid_display_name'"
        ).fetchone()
    )

    if has_chat and has_jid:
        chat_jid_joins = """
            LEFT JOIN jid j_c ON j_c._id = c.jid_row_id
            LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id
            LEFT JOIN jid j_c_real ON j_c_real._id = jm_c.jid_row_id
        """
        con_c_join = (
            f"LEFT JOIN _contacts_resolved con_c ON con_c.clean_number = "
            f"{_clean_user_sql('COALESCE(j_c_real.user, j_c.user)')}"
        )
    else:
        chat_jid_joins = ""
        con_c_join = ""

    if has_jid:
        sender_jid_joins = """
            LEFT JOIN jid j_s ON j_s._id = m.sender_jid_row_id
            LEFT JOIN _jid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id
            LEFT JOIN jid j_s_real ON j_s_real._id = jm_s.jid_row_id
        """
        con_s_join = (
            f"LEFT JOIN _contacts_resolved con_s ON con_s.clean_number = "
            f"{_clean_user_sql('COALESCE(j_s_real.user, j_s.user)')}"
        )
    else:
        sender_jid_joins = ""
        con_s_join = ""

    ldn_c_join = (
        "LEFT JOIN lid_display_name ldn_c ON ldn_c.lid_row_id = c.jid_row_id"
        if (has_chat and has_ldn)
        else ""
    )
    ldn_s_join = (
        """
            LEFT JOIN _lid_map_resolved jm_lid_s ON jm_lid_s.jid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_lid_s.lid_row_id
        """
        if (has_jid and has_ldn)
        else ""
    )

    if has_chat:
        group_c_parts = ["c.subject IS NOT NULL"]
        if has_jid and has_jid_server:
            group_c_parts.append("(j_c.server IS NOT NULL AND j_c.server = 'g.us')")
        if has_jid and has_jid_raw_string:
            group_c_parts.append("(j_c.raw_string IS NOT NULL AND j_c.raw_string LIKE '%@g.us')")
        is_group_chat = " OR ".join(group_c_parts)
    else:
        is_group_chat = "0"

    lid_c_parts = []
    if has_jid_server:
        lid_c_parts.append("(j_c.server IS NOT NULL AND j_c.server = 'lid')")
    if has_jid_raw_string:
        lid_c_parts.append("(j_c.raw_string IS NOT NULL AND j_c.raw_string LIKE '%@lid')")
    j_c_is_lid = " OR ".join(lid_c_parts) if (has_chat and has_jid and lid_c_parts) else "0"

    lid_s_parts = []
    if has_jid_server:
        lid_s_parts.append("(j_s.server IS NOT NULL AND j_s.server = 'lid')")
    if has_jid_raw_string:
        lid_s_parts.append("(j_s.raw_string IS NOT NULL AND j_s.raw_string LIKE '%@lid')")
    j_s_is_lid = " OR ".join(lid_s_parts) if (has_jid and lid_s_parts) else "0"

    where_clauses = ["m.starred = 1"]
    params: list = []
    if target_chat_id is not None:
        clean_target = target_chat_id.split(":")[0] if ":" in target_chat_id else target_chat_id
        if has_chat and has_jid:
            resolved_ids = _resolve_android_chat_row_ids(
                conn, clean_target, "contact", row_ids_cache
            )
            if resolved_ids:
                r_ph = ",".join("?" * len(resolved_ids))
                where_clauses.append(f"m.chat_row_id IN ({r_ph})")
                params.extend(resolved_ids)
            elif clean_target.isdigit():
                where_clauses.append("m.chat_row_id = ?")
                params.append(int(clean_target))
            else:
                return []
        else:
            where_clauses.append("m.chat_row_id = ?")
            params.append(int(target_chat_id) if target_chat_id.isdigit() else target_chat_id)

    where_sql = " AND ".join(where_clauses)
    limit_sql = f"LIMIT {int(fetch_limit)}" if fetch_limit is not None else ""

    if has_chat and has_jid:
        canonical_chat_id_expr = f"""
            CASE WHEN ({is_group_chat}) THEN CAST(m.chat_row_id AS TEXT)
                 ELSE COALESCE(
                     {_clean_user_sql("j_c_real.user")},
                     {_clean_user_sql("j_c.user")},
                     CAST(m.chat_row_id AS TEXT)
                 )
            END
        """
        chat_user_expr = (
            f"COALESCE({_clean_user_sql('j_c_real.user')}, {_clean_user_sql('j_c.user')})"
        )
        chat_is_unmapped_lid_expr = (
            f"CASE WHEN ({j_c_is_lid}) AND j_c_real.user IS NULL THEN 1 ELSE 0 END"
        )
        con_c_name_expr = "COALESCE(NULLIF(con_c.display_name, ''), NULLIF(con_c.folder, ''))"
    else:
        canonical_chat_id_expr = "CAST(m.chat_row_id AS TEXT)"
        chat_user_expr = "NULL"
        chat_is_unmapped_lid_expr = "0"
        con_c_name_expr = "NULL"

    if has_jid:
        sender_user_expr = (
            f"COALESCE({_clean_user_sql('j_s_real.user')}, {_clean_user_sql('j_s.user')})"
        )
        sender_is_unmapped_lid_expr = (
            f"CASE WHEN ({j_s_is_lid}) AND j_s_real.user IS NULL THEN 1 ELSE 0 END"
        )
        con_s_name_expr = "COALESCE(NULLIF(con_s.display_name, ''), NULLIF(con_s.folder, ''))"
    else:
        sender_user_expr = "NULL"
        sender_is_unmapped_lid_expr = "0"
        con_s_name_expr = "NULL"

    ldn_c_name_expr = (
        "COALESCE("
        "CASE WHEN ldn_c.display_name NOT LIKE '%∙%' AND ldn_c.display_name NOT LIKE '%\u2219%' "
        "THEN NULLIF(ldn_c.display_name, '') END, "
        "NULLIF(ldn_c.username, ''))"
        if (has_chat and has_ldn)
        else "NULL"
    )
    ldn_s_name_expr = (
        "COALESCE(NULLIF(ldn_s_direct.display_name, ''), NULLIF(ldn_s_mapped.display_name, ''))"
        if (has_jid and has_ldn)
        else "NULL"
    )

    sql = f"""
        SELECT
            m._id AS id,
            m.chat_row_id AS chat_row_id,
            {canonical_chat_id_expr} AS canonical_chat_id,
            CASE WHEN ({is_group_chat}) THEN 'group' ELSE 'contact' END AS chat_type,
            m.from_me AS from_me,
            COALESCE(m.timestamp, 0) AS timestamp_ms,
            COALESCE(m.text_data, '') AS text,
            COALESCE(m.message_type, 0) AS message_type,
            {mm_file} AS file_path,
            {mm_mime} AS mime_type,
            {mm_duration} AS media_duration,
            {mm_name} AS media_name,
            {ml_lat} AS latitude,
            {ml_lng} AS longitude,
            {ml_place} AS place_name,
            {"c.subject" if has_chat else "NULL"} AS chat_subject,
            {chat_user_expr} AS chat_user,
            {chat_is_unmapped_lid_expr} AS chat_is_unmapped_lid,
            {con_c_name_expr} AS chat_contact_name,
            {ldn_c_name_expr} AS chat_ldn_name,
            {sender_user_expr} AS sender_user,
            {sender_is_unmapped_lid_expr} AS sender_is_unmapped_lid,
            {con_s_name_expr} AS sender_contact_name,
            {ldn_s_name_expr} AS sender_ldn_name
        FROM message m
        {chat_join}
        {chat_jid_joins}
        {con_c_join}
        {ldn_c_join}
        {sender_jid_joins}
        {con_s_join}
        {ldn_s_join}
        {mm_join}
        {ml_join}
        WHERE {where_sql}
        ORDER BY m.timestamp DESC
        {limit_sql}
    """
    rows = conn.execute(sql, params).fetchall()
    contacts_map = _get_contacts_lookup(arch_conn)
    groups_map: dict[str, str] = {}
    if arch_conn is not None:
        try:
            for gr in arch_conn.execute(
                "SELECT chat_row_id, subject, folder FROM groups"
            ).fetchall():
                if gr[0]:
                    groups_map[str(gr[0])] = gr[1] or gr[2] or ""
        except sqlite3.OperationalError:
            pass

    results = []
    for r in rows:
        row_dict = dict(r)
        m_type = int(row_dict["message_type"])
        fp = (row_dict["file_path"] or "").lower()

        if m_type in (1, 42) or any(
            fp.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".heic", ".webp")
        ):
            media_type = "photo"
        elif m_type in (2, 43, 28) or any(
            fp.endswith(ext) for ext in (".mp4", ".mov", ".mkv", ".3gp")
        ):
            media_type = "video"
        elif m_type in (3, 82) or any(
            fp.endswith(ext) for ext in (".mp3", ".ogg", ".opus", ".m4a", ".aac")
        ):
            media_type = "audio"
        elif m_type in (8, 9) or any(
            fp.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".zip", ".xlsx")
        ):
            media_type = "document"
        elif (
            m_type == 7
            or "http://" in row_dict["text"].lower()
            or "https://" in row_dict["text"].lower()
        ):
            media_type = "link"
        elif m_type == 5:
            media_type = "location"
        elif m_type == 66:
            media_type = "poll"
        else:
            media_type = None

        chat_type = row_dict["chat_type"] or "contact"
        c_user = row_dict["chat_user"] or ""
        chat_name = row_dict["chat_subject"] or ""
        if not chat_name and chat_type == "group" and row_dict["chat_row_id"] is not None:
            chat_name = groups_map.get(str(row_dict["chat_row_id"]), "")
        if not chat_name and row_dict.get("chat_contact_name"):
            chat_name = row_dict["chat_contact_name"]
        if not chat_name and c_user:
            chat_name = contacts_map.get(c_user, "")
        if not chat_name and c_user == "0":
            chat_name = "WhatsApp"
        if not chat_name and row_dict.get("chat_ldn_name"):
            chat_name = row_dict["chat_ldn_name"]
        if not chat_name:
            if chat_type == "group":
                chat_name = f"Unknown Group ({row_dict['chat_row_id']})"
            elif c_user and c_user.isdigit() and not row_dict.get("chat_is_unmapped_lid"):
                chat_name = f"+{c_user}"
            elif c_user and not row_dict.get("chat_is_unmapped_lid"):
                chat_name = c_user
            elif row_dict["chat_row_id"] is not None:
                chat_name = f"Unknown Contact ({row_dict['chat_row_id']})"
            else:
                chat_name = "Unknown Contact"

        from_me = int(row_dict["from_me"])
        s_user = row_dict["sender_user"] or ""
        if from_me == 1:
            sender_name = "You"
        else:
            sender_name = row_dict.get("sender_contact_name") or ""
            if not sender_name and s_user:
                sender_name = contacts_map.get(s_user, "")
            if not sender_name and s_user == "0":
                sender_name = "WhatsApp"
            if not sender_name and row_dict.get("sender_ldn_name"):
                ldn_s_val = str(row_dict["sender_ldn_name"]).strip()
                if "∙" not in ldn_s_val and "\u2219" not in ldn_s_val:
                    sender_name = ldn_s_val
            if not sender_name:
                if s_user and s_user.isdigit() and not row_dict.get("sender_is_unmapped_lid"):
                    sender_name = f"+{s_user}"
                elif s_user and not row_dict.get("sender_is_unmapped_lid"):
                    sender_name = s_user
                elif chat_type == "contact":
                    sender_name = chat_name
                else:
                    sender_name = "Unknown"

        chat_id_val = str(
            row_dict["canonical_chat_id"]
            if row_dict["canonical_chat_id"] is not None
            else (c_user or row_dict["chat_row_id"] or "0")
        )

        file_name = row_dict.get("media_name") or ""
        if not file_name and row_dict.get("file_path"):
            file_name = Path(row_dict["file_path"]).name
        dur_sec = int(row_dict.get("media_duration") or 0)
        lat = row_dict.get("latitude")
        lng = row_dict.get("longitude")
        place = row_dict.get("place_name") or ""

        results.append(
            {
                "id": int(row_dict["id"]),
                "chat_id": chat_id_val,
                "chat_type": chat_type,
                "chat_name": chat_name,
                "sender_name": sender_name,
                "timestamp": int(round(int(row_dict["timestamp_ms"]) / 1000)),
                "text": row_dict["text"],
                "text_body": row_dict["text"],
                "media_type": media_type,
                "duration": dur_sec,
                "file_name": file_name,
                "latitude": lat,
                "longitude": lng,
                "place_name": place,
                "_raw_metadata": row_dict.get("_raw_metadata"),
            }
        )
    return results


def _query_ios_starred(
    conn: sqlite3.Connection,
    target_chat_id: str | None,
    arch_conn: sqlite3.Connection | None,
    fetch_limit: int | None,
) -> list[dict]:
    """Query starred messages from iOS ChatStorage.sqlite."""
    has_msg = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
    ).fetchone()
    if not has_msg:
        return []

    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
    if "ZSTARRED" not in msg_cols:
        return []

    has_mi = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMEDIAITEM'"
        ).fetchone()
    )
    mi_join = "LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM" if has_mi else ""
    mi_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
        if has_mi
        else set()
    )
    mi_title = "mi.ZTITLE" if has_mi else "NULL"
    mi_path = "mi.ZMEDIALOCALPATH" if has_mi else "NULL"
    mi_meta = "mi.ZMETADATA" if "ZMETADATA" in mi_cols else "NULL"
    mi_duration = "COALESCE(mi.ZMOVIEDURATION, 0)" if "ZMOVIEDURATION" in mi_cols else "0"
    mi_lat = "mi.ZLATITUDE" if "ZLATITUDE" in mi_cols else "NULL"
    mi_lng = "mi.ZLONGITUDE" if "ZLONGITUDE" in mi_cols else "NULL"

    has_cs = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWACHATSESSION'"
        ).fetchone()
    )
    cs_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if has_cs
        else set()
    )
    has_cs_group = "ZGROUPINFO" in cs_cols
    cs_join = "LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION" if has_cs else ""

    where_clauses = ["m.ZSTARRED = 1"]
    params: list = []
    if target_chat_id is not None:
        where_clauses.append("m.ZCHATSESSION = ?")
        params.append(target_chat_id)

    where_sql = " AND ".join(where_clauses)
    limit_sql = f"LIMIT {int(fetch_limit)}" if fetch_limit is not None else ""
    sql = f"""
        SELECT
            m.Z_PK AS id,
            m.ZCHATSESSION AS chat_session,
            COALESCE(m.ZISFROMME, 0) AS is_from_me,
            COALESCE(m.ZMESSAGEDATE, 0) AS msg_date,
            COALESCE(NULLIF(m.ZTEXT, ''), NULLIF({mi_title}, ''), '') AS text,
            COALESCE(m.ZMESSAGETYPE, 0) AS message_type,
            m.ZPUSHNAME AS push_name,
            m.ZFROMJID AS from_jid,
            {mi_path} AS media_path,
            {mi_meta} AS _raw_metadata,
            {mi_duration} AS media_duration,
            {mi_lat} AS latitude,
            {mi_lng} AS longitude,
            {"cs.ZPARTNERNAME" if has_cs else "NULL"} AS partner_name,
            {"cs.ZCONTACTJID" if has_cs else "NULL"} AS contact_jid,
            {"cs.ZGROUPINFO" if has_cs_group else "NULL"} AS group_info
        FROM ZWAMESSAGE m
        {cs_join}
        {mi_join}
        WHERE {where_sql}
        ORDER BY m.ZMESSAGEDATE DESC
        {limit_sql}
    """
    rows = conn.execute(sql, params).fetchall()
    contacts_map = _get_contacts_lookup(arch_conn)
    results = []
    for r in rows:
        row_dict = dict(r)
        m_type = int(row_dict["message_type"])
        fp = (row_dict["media_path"] or "").lower()

        if m_type in (1, 38) or any(fp.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".heic")):
            media_type = "photo"
        elif m_type in (2, 39, 54) or any(fp.endswith(ext) for ext in (".mp4", ".mov")):
            media_type = "video"
        elif m_type == 3 or any(fp.endswith(ext) for ext in (".mp3", ".opus", ".m4a", ".aac")):
            media_type = "audio"
        elif m_type == 8 or any(fp.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".zip")):
            media_type = "document"
        elif (
            m_type == 7
            or "http://" in row_dict["text"].lower()
            or "https://" in row_dict["text"].lower()
        ):
            media_type = "link"
        elif m_type == 5:
            media_type = "location"
        elif m_type == 46:
            media_type = "poll"
        else:
            media_type = None

        is_from_me = int(row_dict["is_from_me"])
        from_jid = row_dict["from_jid"] or ""
        from_phone = from_jid.split("@")[0] if "@" in from_jid else from_jid
        if is_from_me == 1:
            sender_name = "You"
        else:
            sender_name = row_dict["push_name"] or ""
            if not sender_name and from_phone:
                sender_name = contacts_map.get(from_phone, "")
            if not sender_name:
                sender_name = (
                    f"+{from_phone}"
                    if (from_phone and from_phone.isdigit())
                    else (from_phone or "Unknown")
                )

        contact_jid = row_dict["contact_jid"] or ""
        c_phone = contact_jid.split("@")[0] if "@" in contact_jid else contact_jid
        chat_name = row_dict["partner_name"] or ""
        if not chat_name and c_phone:
            chat_name = contacts_map.get(c_phone, "")
        if not chat_name:
            chat_name = (
                f"+{c_phone}"
                if (c_phone and c_phone.isdigit())
                else (c_phone or f"Chat {row_dict['chat_session']}")
            )

        is_group = bool(row_dict.get("group_info") is not None or contact_jid.endswith("@g.us"))
        file_name = ""
        if media_type == "document":
            file_name = row_dict["text"] or ""
        elif row_dict.get("media_path"):
            file_name = Path(row_dict["media_path"]).name
        dur_sec = int(row_dict.get("media_duration") or 0)
        lat = row_dict.get("latitude")
        lng = row_dict.get("longitude")

        results.append(
            {
                "id": int(row_dict["id"]),
                "chat_id": str(row_dict["chat_session"]),
                "chat_type": "group" if is_group else "contact",
                "chat_name": chat_name,
                "sender_name": sender_name,
                "timestamp": int(round(float(row_dict["msg_date"]) + 978307200)),
                "text": row_dict["text"],
                "text_body": row_dict["text"],
                "media_type": media_type,
                "duration": dur_sec,
                "file_name": file_name,
                "latitude": lat,
                "longitude": lng,
                "place_name": "",
                "_raw_metadata": row_dict.get("_raw_metadata"),
            }
        )
    return results


def _query_preserved_starred(
    pres_conn: sqlite3.Connection | None,
    target_chat_id: str | None,
    fetch_limit: int | None,
) -> list[dict]:
    """Query starred messages from preserved_messages.db."""
    if pres_conn is None:
        return []

    try:
        has_table = pres_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preserved_messages'"
        ).fetchone()
        if not has_table:
            return []

        cols = {r[1] for r in pres_conn.execute("PRAGMA table_info(preserved_messages)").fetchall()}
        if "is_starred" not in cols:
            return []

        where_clauses = ["is_starred = 1"]
        params: list = []
        if target_chat_id is not None:
            where_clauses.append("chat_id = ?")
            params.append(target_chat_id)

        where_sql = " AND ".join(where_clauses)
        limit_sql = f"LIMIT {int(fetch_limit)}" if fetch_limit is not None else ""
        sender_col = (
            "sender"
            if "sender" in cols
            else (
                "sender_name"
                if "sender_name" in cols
                else ("participant_name" if "participant_name" in cols else "NULL")
            )
        )
        chat_type_col = "chat_type" if "chat_type" in cols else "'contact'"
        msg_id_col = "msg_id" if "msg_id" in cols else "rowid"
        dur_col = "media_duration" if "media_duration" in cols else "0"
        name_col = "original_path" if "original_path" in cols else "NULL"
        lat_col = "latitude" if "latitude" in cols else "NULL"
        lng_col = "longitude" if "longitude" in cols else "NULL"
        place_col = "location_name" if "location_name" in cols else "''"
        sql = f"""
            SELECT
                rowid,
                {msg_id_col} AS msg_id,
                chat_id,
                {chat_type_col} AS chat_type,
                timestamp_ms,
                text_body,
                media_type,
                from_me,
                {dur_col} AS media_duration,
                {name_col} AS original_path,
                {lat_col} AS latitude,
                {lng_col} AS longitude,
                {place_col} AS place_name,
                {sender_col} AS sender_display
            FROM preserved_messages
            WHERE {where_sql}
            ORDER BY timestamp_ms DESC
            {limit_sql}
        """
        rows = pres_conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []

    results = []
    for r in rows:
        row_dict = dict(r)
        mt = row_dict["media_type"]
        if mt in ("image", "photo"):
            media_type = "photo"
        elif mt in ("video", "audio", "document", "link", "location", "poll"):
            media_type = mt
        else:
            media_type = None

        from_me = int(row_dict["from_me"])
        sender_name = "You" if from_me == 1 else (row_dict["sender_display"] or "Unknown")

        file_name = Path(row_dict["original_path"]).name if row_dict.get("original_path") else ""
        dur_sec = int(row_dict.get("media_duration") or 0)
        lat = row_dict.get("latitude")
        lng = row_dict.get("longitude")
        place = row_dict.get("place_name") or ""

        results.append(
            {
                "id": -int(row_dict["rowid"]),
                "chat_id": str(row_dict["chat_id"]),
                "chat_type": row_dict.get("chat_type") or "contact",
                "chat_name": f"Chat {row_dict['chat_id']}",
                "sender_name": sender_name,
                "timestamp": int(round(int(row_dict["timestamp_ms"]) / 1000)),
                "text": row_dict["text_body"] or "",
                "text_body": row_dict["text_body"] or "",
                "media_type": media_type,
                "duration": dur_sec,
                "file_name": file_name,
                "latitude": lat,
                "longitude": lng,
                "place_name": place,
            }
        )
    return results


def _query_android_pinned(
    conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    arch_conn: sqlite3.Connection | None,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> list[dict]:
    """Query active pinned messages for a chat from Android msgstore.db."""
    has_ao = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_add_on'"
    ).fetchone()
    if not has_ao:
        return []

    has_msg = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
    ).fetchone()
    if not has_msg:
        return []

    has_pin_table = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_add_on_pin_in_chat'"
        ).fetchone()
    )

    pin_join = (
        "JOIN message_add_on_pin_in_chat pic ON pic.message_add_on_row_id = ao._id"
        if has_pin_table
        else ""
    )
    pin_cond = "AND pic.pin_in_chat_state = 1" if has_pin_table else ""

    chat_pred, chat_params = _resolve_android_chat_filter(conn, chat_id, chat_type, row_ids_cache)

    sql = f"""
        SELECT
            m._id AS id,
            m.from_me AS from_me,
            COALESCE(m.timestamp, 0) AS timestamp_ms,
            COALESCE(m.text_data, '') AS text,
            m.message_type AS message_type,
            j_s.user AS sender_user,
            ao.expiry_timestamp AS expiry_ts
        FROM message_add_on ao
        {pin_join}
        JOIN message m ON m._id = ao.parent_message_row_id
        LEFT JOIN jid j_s ON j_s._id = m.sender_jid_row_id
        WHERE ao.message_add_on_type = 79
          {pin_cond}
          AND {chat_pred}
        ORDER BY ao._id DESC
        LIMIT 3
    """
    try:
        rows = conn.execute(sql, chat_params).fetchall()
    except sqlite3.OperationalError:
        return []

    pins = []
    seen_ids = set()
    contacts_map = _get_contacts_lookup(arch_conn)
    for r in rows:
        row_dict = dict(r)
        mid = int(row_dict["id"])
        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        from_me = int(row_dict["from_me"])
        s_user = row_dict["sender_user"] or ""
        if from_me == 1:
            sender_name = "You"
        else:
            sender_name = contacts_map.get(s_user, "")
            if not sender_name:
                sender_name = (
                    f"+{s_user}" if (s_user and s_user.isdigit()) else (s_user or "Unknown")
                )

        text = row_dict["text"]
        if not text:
            mt = int(row_dict["message_type"] or 0)
            if mt in (1, 42):
                text = "📷 Photo"
            elif mt in (2, 43, 28):
                text = "📹 Video"
            elif mt in (3, 82):
                text = "🎤 Voice message"
            elif mt in (8, 9):
                text = "📄 Document"
            elif mt == 5:
                text = "📍 Location"
            elif mt == 66:
                text = "📊 Poll"

        pins.append(
            {
                "id": mid,
                "sender_name": sender_name,
                "text": text,
                "text_body": text,
                "is_pinned": True,
                "timestamp": int(round(int(row_dict["timestamp_ms"]) / 1000)),
            }
        )
    return pins


def _query_ios_pinned(
    conn: sqlite3.Connection,
    ext_db_path: Path | None,
    chat_id: str,
    arch_conn: sqlite3.Connection | None,
) -> list[dict]:
    """Query active pinned messages for a chat from iOS databases."""
    has_msg = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
    ).fetchone()
    if not has_msg:
        return []

    zwa_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
    contacts_map = _get_contacts_lookup(arch_conn)

    session_pks: set[int] = set()
    if str(chat_id).isdigit():
        session_pks.add(int(chat_id))
    cand_jids = _resolve_ios_chat_jids(conn, chat_id)
    has_cs = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWACHATSESSION'"
        ).fetchone()
    )
    if has_cs and cand_jids:
        cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        j_ph = ",".join("?" * len(cand_jids))
        ident_cond = (
            f" OR ZCONTACTIDENTIFIER IN ({j_ph})" if "ZCONTACTIDENTIFIER" in cs_cols else ""
        )
        cs_params = cand_jids * (2 if ident_cond else 1)
        for csr in conn.execute(
            f"SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID IN ({j_ph}){ident_cond}",
            cs_params,
        ).fetchall():
            session_pks.add(int(csr[0]))

    # 1. Check ExtChatDatabase if available
    ext_pins: list[dict] = []
    if (
        ext_db_path is not None
        and ext_db_path.is_file()
        and "ZSTANZAID" in zwa_cols
        and session_pks
    ):
        with contextlib.closing(sqlite3.connect(str(ext_db_path))) as ext_conn:
            ext_conn.row_factory = sqlite3.Row
            has_tm = ext_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_messages'"
            ).fetchone()
            if has_tm:
                tm_cols = {
                    r[1] for r in ext_conn.execute("PRAGMA table_info(thread_messages)").fetchall()
                }
                key_col = (
                    "key_id"
                    if "key_id" in tm_cols
                    else ("stanza_id" if "stanza_id" in tm_cols else None)
                )
                if key_col:
                    if "chat_jid" in tm_cols and cand_jids:
                        j_ph = ",".join("?" * len(cand_jids))
                        tm_rows = ext_conn.execute(
                            f"SELECT {key_col} FROM thread_messages WHERE chat_jid IN ({j_ph}) "
                            "ORDER BY rowid DESC LIMIT 3",
                            cand_jids,
                        ).fetchall()
                    elif "chat_jid" not in tm_cols:
                        tm_rows = ext_conn.execute(
                            f"SELECT {key_col} FROM thread_messages ORDER BY rowid DESC"
                        ).fetchall()
                    else:
                        tm_rows = []
                    stanzas = [r[0] for r in tm_rows if r[0]]
                    if stanzas:
                        s_ph = ",".join("?" * len(stanzas))
                        sess_list = sorted(session_pks)
                        sess_ph = ",".join("?" * len(sess_list))
                        m_rows = conn.execute(
                            f"""
                            SELECT
                                m.Z_PK AS id,
                                COALESCE(m.ZISFROMME, 0) AS is_from_me,
                                COALESCE(m.ZMESSAGEDATE, 0) AS msg_date,
                                COALESCE(NULLIF(m.ZTEXT, ''), '') AS text,
                                COALESCE(m.ZMESSAGETYPE, 0) AS message_type,
                                m.ZPUSHNAME AS push_name,
                                m.ZFROMJID AS from_jid
                            FROM ZWAMESSAGE m
                            WHERE m.ZSTANZAID IN ({s_ph})
                              AND m.ZCHATSESSION IN ({sess_ph})
                            ORDER BY m.ZMESSAGEDATE DESC
                            LIMIT 3
                        """,
                            stanzas + sess_list,
                        ).fetchall()
                        if m_rows:
                            for mr in m_rows:
                                r_dict = dict(mr)
                                is_from_me = int(r_dict["is_from_me"])
                                from_jid = r_dict["from_jid"] or ""
                                from_phone = from_jid.split("@")[0] if "@" in from_jid else from_jid
                                sender_name = (
                                    "You"
                                    if is_from_me == 1
                                    else (
                                        r_dict["push_name"]
                                        or contacts_map.get(from_phone, "")
                                        or from_phone
                                        or "Unknown"
                                    )
                                )
                                ext_pins.append(
                                    {
                                        "id": int(r_dict["id"]),
                                        "sender_name": sender_name,
                                        "text": r_dict["text"] or "Pinned message",
                                        "text_body": r_dict["text"] or "Pinned message",
                                        "is_pinned": True,
                                        "timestamp": int(
                                            round(float(r_dict["msg_date"]) + 978307200)
                                        ),
                                    }
                                )

    if "ZPARENTMESSAGE" not in zwa_cols or "ZMESSAGETYPE" not in zwa_cols or not session_pks:
        return sorted(ext_pins, key=lambda p: p["timestamp"], reverse=True)[:3]

    # 2. Check ZMESSAGETYPE = 66 in ZWAMESSAGE
    sess_list = sorted(session_pks)
    sess_ph = ",".join("?" * len(sess_list))
    sql = f"""
        SELECT
            m.Z_PK AS id,
            COALESCE(m.ZISFROMME, 0) AS is_from_me,
            COALESCE(m.ZMESSAGEDATE, 0) AS msg_date,
            COALESCE(NULLIF(m.ZTEXT, ''), '') AS text,
            COALESCE(m.ZMESSAGETYPE, 0) AS message_type,
            m.ZPUSHNAME AS push_name,
            m.ZFROMJID AS from_jid,
            m.ZPARENTMESSAGE AS parent_msg_pk
        FROM ZWAMESSAGE m
        WHERE m.ZCHATSESSION IN ({sess_ph})
          AND m.ZMESSAGETYPE = 66
        ORDER BY m.ZMESSAGEDATE DESC
        LIMIT 3
    """
    rows = conn.execute(sql, sess_list).fetchall()

    pins = []
    for r in rows:
        r_dict = dict(r)
        parent_pk = r_dict["parent_msg_pk"]
        target = r_dict
        if parent_pk:
            p_row = conn.execute(
                """
                SELECT
                    m.Z_PK AS id,
                    COALESCE(m.ZISFROMME, 0) AS is_from_me,
                    COALESCE(m.ZMESSAGEDATE, 0) AS msg_date,
                    COALESCE(NULLIF(m.ZTEXT, ''), '') AS text,
                    COALESCE(m.ZMESSAGETYPE, 0) AS message_type,
                    m.ZPUSHNAME AS push_name,
                    m.ZFROMJID AS from_jid
                FROM ZWAMESSAGE m
                WHERE m.Z_PK = ?
            """,
                (parent_pk,),
            ).fetchone()
            if p_row:
                target = dict(p_row)

        is_from_me = int(target["is_from_me"])
        from_jid = target["from_jid"] or ""
        from_phone = from_jid.split("@")[0] if "@" in from_jid else from_jid
        sender_name = (
            "You"
            if is_from_me == 1
            else (
                target["push_name"] or contacts_map.get(from_phone, "") or from_phone or "Unknown"
            )
        )

        pins.append(
            {
                "id": int(target["id"]),
                "sender_name": sender_name,
                "text": target["text"] or "Pinned message",
                "text_body": target["text"] or "Pinned message",
                "is_pinned": True,
                "timestamp": int(round(float(target["msg_date"]) + 978307200)),
            }
        )
    merged: dict[int, dict] = {}
    for pin in pins + ext_pins:
        merged[int(pin["id"])] = pin
    return sorted(merged.values(), key=lambda p: p["timestamp"], reverse=True)[:3]


def _query_preserved_pinned(
    pres_conn: sqlite3.Connection | None,
    chat_id: str,
) -> list[dict]:
    """Query active pinned messages from preserved_messages.db."""
    if pres_conn is None:
        return []

    try:
        has_table = pres_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preserved_messages'"
        ).fetchone()
        if not has_table:
            return []

        cols = {r[1] for r in pres_conn.execute("PRAGMA table_info(preserved_messages)").fetchall()}
        if "is_pinned" not in cols:
            return []

        rows = pres_conn.execute(
            """
            SELECT
                rowid,
                msg_id,
                chat_id,
                timestamp_ms,
                text_body,
                from_me,
                participant_name,
                pin_expiry_ts
            FROM preserved_messages
            WHERE chat_id = ?
              AND is_pinned = 1
              AND (pin_expiry_ts IS NULL OR pin_expiry_ts = 0 OR pin_expiry_ts > (CAST(strftime('%s', 'now') AS INTEGER) * 1000))
            ORDER BY timestamp_ms DESC
            LIMIT 3
        """,
            (chat_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    pins = []
    for r in rows:
        row_dict = dict(r)
        from_me = int(row_dict["from_me"])
        sender_name = "You" if from_me == 1 else (row_dict["participant_name"] or "Unknown")
        pins.append(
            {
                "id": -int(row_dict["rowid"]),
                "sender_name": sender_name,
                "text": row_dict["text_body"] or "Pinned message",
                "text_body": row_dict["text_body"] or "Pinned message",
                "is_pinned": True,
                "timestamp": int(round(int(row_dict["timestamp_ms"]) / 1000)),
            }
        )
    return pins


def _count_chat_starred(
    wa_conn: sqlite3.Connection | None,
    pres_conn: sqlite3.Connection | None,
    source_type: str | None,
    target_chat_id: str,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> int:
    """Return exact count of starred messages in a chat across live + preserved DBs."""
    live_cnt = 0
    if wa_conn is not None and source_type == "android":
        has_msg = wa_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'"
        ).fetchone()
        if has_msg:
            m_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
            if "starred" in m_cols:
                clean_target = (
                    target_chat_id.split(":")[0] if ":" in target_chat_id else target_chat_id
                )
                has_chat = bool(
                    wa_conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'"
                    ).fetchone()
                )
                has_jid = bool(
                    wa_conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jid'"
                    ).fetchone()
                )
                if has_chat and has_jid:
                    resolved_ids = _resolve_android_chat_row_ids(
                        wa_conn, clean_target, "contact", row_ids_cache
                    )
                    if resolved_ids:
                        r_ph = ",".join("?" * len(resolved_ids))
                        cnt_row = wa_conn.execute(
                            f"SELECT COUNT(*) FROM message WHERE starred = 1 AND chat_row_id IN ({r_ph})",
                            resolved_ids,
                        ).fetchone()
                        live_cnt = int(cnt_row[0]) if cnt_row else 0
                    elif clean_target.isdigit():
                        cnt_row = wa_conn.execute(
                            "SELECT COUNT(*) FROM message WHERE starred = 1 AND chat_row_id = ?",
                            (int(clean_target),),
                        ).fetchone()
                        live_cnt = int(cnt_row[0]) if cnt_row else 0
                else:
                    param_val = int(clean_target) if clean_target.isdigit() else clean_target
                    cnt_row = wa_conn.execute(
                        "SELECT COUNT(*) FROM message WHERE starred = 1 AND chat_row_id = ?",
                        (param_val,),
                    ).fetchone()
                    live_cnt = int(cnt_row[0]) if cnt_row else 0
    elif wa_conn is not None and source_type == "ios":
        has_msg = wa_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAMESSAGE'"
        ).fetchone()
        if has_msg:
            m_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
            if "ZSTARRED" in m_cols:
                cnt_row = wa_conn.execute(
                    "SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZSTARRED = 1 AND ZCHATSESSION = ?",
                    (target_chat_id,),
                ).fetchone()
                live_cnt = int(cnt_row[0]) if cnt_row else 0

    pres_cnt = 0
    if pres_conn is not None:
        has_pres = pres_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preserved_messages'"
        ).fetchone()
        if has_pres:
            p_cols = {
                r[1] for r in pres_conn.execute("PRAGMA table_info(preserved_messages)").fetchall()
            }
            if "is_starred" in p_cols:
                cnt_row = pres_conn.execute(
                    "SELECT COUNT(*) FROM preserved_messages WHERE is_starred = 1 AND chat_id = ?",
                    (target_chat_id,),
                ).fetchone()
                pres_cnt = int(cnt_row[0]) if cnt_row else 0
    return live_cnt + pres_cnt
