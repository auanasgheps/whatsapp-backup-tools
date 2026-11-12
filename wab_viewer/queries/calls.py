"""Unified call-log and in-chat call-bubble query helpers."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path

from ..db_cache import _ensure_jid_map_resolved, _find_ios_call_history_db
from .sql_builder import _resolve_android_chat_row_ids


def _get_contacts_lookup(arch_conn: sqlite3.Connection | None) -> dict[str, str]:
    """Load contacts mapping once into an in-memory dictionary to eliminate N+1 queries."""
    if arch_conn is None:
        return {}
    try:
        rows = arch_conn.execute("SELECT number, display_name, folder FROM contacts").fetchall()
        lookup: dict[str, str] = {}
        for r in rows:
            num = r[0]
            if num:
                name = r[1] or (r[2].split(" (")[0] if r[2] else "")
                lookup[str(num)] = name
        return lookup
    except (sqlite3.OperationalError, Exception):
        return {}


def _map_android_call_outcome(from_me: int, result_code: int, duration_sec: int) -> tuple[str, str]:
    """Return (call_type, outcome) for an Android call_log entry."""
    if from_me == 1:
        call_type = "outgoing"
    elif result_code in (2, 3, 5):
        call_type = "missed"
    else:
        call_type = "incoming"

    if duration_sec > 0:
        outcome = "answered"
    elif from_me == 1:
        outcome = "unanswered"
    elif result_code == 3:
        outcome = "declined"
    elif result_code in (2, 5):
        outcome = "missed"
    else:
        outcome = "unanswered"
    return call_type, outcome


def _map_ios_call_outcome(from_me: int, outcome_code: int, duration_sec: int) -> tuple[str, str]:
    """Return (call_type, outcome) for an iOS ZWACDCALLEVENT entry."""
    if from_me == 1:
        call_type = "outgoing"
    elif outcome_code in (2, 3) or (outcome_code == 4 and duration_sec == 0):
        call_type = "missed"
    else:
        call_type = "incoming"

    if duration_sec > 0:
        outcome = "answered"
    elif from_me == 1:
        outcome = "unanswered"
    elif outcome_code == 3:
        outcome = "declined"
    elif outcome_code in (2, 4):
        outcome = "missed"
    elif outcome_code == 1:
        outcome = "canceled"
    else:
        outcome = "unanswered"
    return call_type, outcome


def _resolve_android_caller_info(
    row_dict: dict, contacts_map: dict[str, str]
) -> tuple[str, str, str]:
    """Resolve (raw_num, caller_phone, caller_name) for an Android call row."""
    raw_num = row_dict.get("caller_number") or ""
    caller_phone = f"+{raw_num}" if (raw_num and raw_num.isdigit()) else raw_num
    if bool(row_dict.get("is_group_call") == 1) and row_dict.get("chat_subject"):
        return raw_num, caller_phone, str(row_dict["chat_subject"])
    caller_name = contacts_map.get(raw_num, "")
    if not caller_name and row_dict.get("con_display_name"):
        caller_name = row_dict["con_display_name"]
    if not caller_name and row_dict.get("con_folder"):
        caller_name = re.sub(r"\s*\(\+?\d+\)$", "", row_dict["con_folder"]).strip()
    if not caller_name and row_dict.get("ldn_display_name"):
        ldn_n = str(row_dict["ldn_display_name"]).strip()
        if "∙" not in ldn_n and "\u2219" not in ldn_n:
            caller_name = ldn_n
    if not caller_name:
        caller_name = row_dict.get("chat_subject") or caller_phone or "Unknown"
    return raw_num, caller_phone, caller_name


def _query_android_calls(
    conn: sqlite3.Connection,
    filter_type: str,
    arch_conn: sqlite3.Connection | None,
    fetch_limit: int | None,
) -> list[dict]:
    """Query voice and video call history from Android msgstore.db."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_log'"
    ).fetchone()
    if not has_table:
        return []

    cl_cols = {r[1] for r in conn.execute("PRAGMA table_info(call_log)").fetchall()}
    has_video = "video_call" in cl_cols
    has_duration = "duration" in cl_cols
    has_result = "call_result" in cl_cols
    has_from_me = "from_me" in cl_cols
    has_timestamp = "timestamp" in cl_cols
    has_group = "group_jid_row_id" in cl_cols
    has_jid_row = "jid_row_id" in cl_cols

    _ensure_jid_map_resolved(conn, arch_conn)

    has_chat_table = bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat'").fetchone()
    )
    chat_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(chat)").fetchall()}
        if has_chat_table
        else set()
    )
    has_account_jid = "account_jid_row_id" in chat_cols
    has_ldn = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lid_display_name'"
        ).fetchone()
    )

    video_expr = "COALESCE(cl.video_call, 0)" if has_video else "0"
    dur_expr = "COALESCE(cl.duration, 0)" if has_duration else "0"
    result_expr = "COALESCE(cl.call_result, 0)" if has_result else "0"
    from_me_expr = "COALESCE(cl.from_me, 0)" if has_from_me else "0"
    ts_expr = "COALESCE(cl.timestamp, 0)" if has_timestamp else "0"
    is_group_expr = (
        "CASE WHEN cl.group_jid_row_id IS NOT NULL AND cl.group_jid_row_id > 0 THEN 1 ELSE 0 END"
        if has_group
        else "0"
    )
    caller_join = (
        """
        LEFT JOIN jid j_caller ON j_caller._id = cl.jid_row_id
        LEFT JOIN _jid_map_resolved jm_caller ON jm_caller.lid_row_id = cl.jid_row_id
        LEFT JOIN jid j_caller_real ON j_caller_real._id = jm_caller.jid_row_id
        """
        if has_jid_row
        else ""
    )
    chat_joins_parts: list[str] = []
    chat_id_candidates: list[str] = []
    chat_subj_candidates: list[str] = []
    if has_chat_table:
        if has_group:
            chat_joins_parts.append(
                "LEFT JOIN chat c_group ON c_group.jid_row_id = cl.group_jid_row_id"
            )
        if has_jid_row:
            chat_joins_parts.append(
                "LEFT JOIN chat c_direct ON c_direct.jid_row_id = cl.jid_row_id"
            )
            chat_joins_parts.append(
                "LEFT JOIN chat c_canon ON c_canon.jid_row_id = jm_caller.jid_row_id"
            )
            chat_id_candidates.extend(["c_direct._id", "c_canon._id"])
            chat_subj_candidates.extend(["c_direct.subject", "c_canon.subject"])
            if has_account_jid:
                chat_joins_parts.append(
                    "LEFT JOIN chat c_acc ON c_acc.account_jid_row_id = cl.jid_row_id"
                )
                chat_joins_parts.append(
                    "LEFT JOIN chat c_acc_canon ON c_acc_canon.account_jid_row_id = jm_caller.jid_row_id"
                )
                chat_id_candidates.extend(["c_acc._id", "c_acc_canon._id"])
                chat_subj_candidates.extend(["c_acc.subject", "c_acc_canon.subject"])
    chat_join = "\n        ".join(chat_joins_parts)
    if has_chat_table and has_group and chat_id_candidates:
        co_id = ", ".join(chat_id_candidates)
        co_subj = ", ".join(chat_subj_candidates)
        chat_row_id_expr = (
            "CASE WHEN cl.group_jid_row_id IS NOT NULL AND cl.group_jid_row_id > 0 "
            f"THEN c_group._id ELSE COALESCE({co_id}) END"
        )
        chat_subject_expr = (
            "CASE WHEN cl.group_jid_row_id IS NOT NULL AND cl.group_jid_row_id > 0 "
            f"THEN c_group.subject ELSE COALESCE({co_subj}) END"
        )
    elif has_chat_table and chat_id_candidates:
        chat_row_id_expr = f"COALESCE({', '.join(chat_id_candidates)})"
        chat_subject_expr = f"COALESCE({', '.join(chat_subj_candidates)})"
    elif has_chat_table and has_group:
        chat_row_id_expr = "c_group._id"
        chat_subject_expr = "c_group.subject"
    else:
        chat_row_id_expr = "NULL"
        chat_subject_expr = "NULL"

    jid_cols = (
        {row[1] for row in conn.execute("PRAGMA table_info(jid)").fetchall()}
        if has_jid_row
        else set()
    )
    caller_non_lid_expr = (
        "CASE WHEN j_caller.server != 'lid' THEN j_caller.user END"
        if "server" in jid_cols
        else "CASE WHEN COALESCE(j_caller.raw_string, '') NOT LIKE '%@lid' THEN j_caller.user END"
    )
    con_caller_join = (
        f"""
        LEFT JOIN _contacts_resolved con_caller ON con_caller.clean_number = COALESCE(
            j_caller_real.user,
            {caller_non_lid_expr},
            j_caller.user
        )
        """
        if has_jid_row
        else ""
    )
    ldn_caller_join = (
        "LEFT JOIN lid_display_name ldn_caller ON ldn_caller.lid_row_id = cl.jid_row_id"
        if (has_ldn and has_jid_row)
        else ""
    )
    ldn_col = (
        "ldn_caller.display_name AS ldn_display_name,"
        if (has_ldn and has_jid_row)
        else "NULL AS ldn_display_name,"
    )

    where_parts: list[str] = []
    if filter_type == "video" and has_video:
        where_parts.append(f"{video_expr} = 1")
    elif filter_type == "missed" and (has_from_me or has_result):
        where_parts.append(f"{from_me_expr} = 0 AND {result_expr} IN (2, 3, 5)")
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    limit_sql = f"LIMIT {int(fetch_limit)}" if fetch_limit is not None else ""

    sql = f"""
        SELECT
            cl._id AS call_log_id,
            COALESCE(NULLIF(cl.call_id, ''), 'call_' || cl._id) AS id,
            {from_me_expr} AS from_me,
            {ts_expr} AS timestamp_ms,
            {dur_expr} AS duration,
            {video_expr} AS is_video,
            {result_expr} AS call_result,
            {is_group_expr} AS is_group_call,
            COALESCE(
                j_caller_real.user,
                {caller_non_lid_expr},
                j_caller.user
            ) AS caller_number,
            COALESCE(j_caller_real.raw_string, j_caller.raw_string) AS caller_raw,
            con_caller.display_name AS con_display_name,
            con_caller.folder AS con_folder,
            {ldn_col}
            {chat_row_id_expr} AS chat_row_id,
            {chat_subject_expr} AS chat_subject
        FROM call_log cl
        {caller_join}
        {chat_join}
        {con_caller_join}
        {ldn_caller_join}
        {where_sql}
        GROUP BY cl._id
        ORDER BY {ts_expr} DESC
        {limit_sql}
    """
    rows = conn.execute(sql).fetchall()
    contacts_map = _get_contacts_lookup(arch_conn)
    results = []
    seen_call_log_ids: set[int] = set()
    for r in rows:
        row_dict = dict(r)
        cl_id = int(row_dict["call_log_id"])
        if cl_id in seen_call_log_ids:
            continue
        seen_call_log_ids.add(cl_id)
        from_me = int(row_dict["from_me"])
        result_code = int(row_dict["call_result"])
        is_video = bool(row_dict["is_video"] == 1)
        duration_sec = max(0, int(row_dict["duration"] or 0))
        call_type, _ = _map_android_call_outcome(from_me, result_code, duration_sec)

        if filter_type == "missed" and call_type != "missed":
            continue
        if filter_type == "video" and not is_video:
            continue

        raw_num, caller_phone, caller_name = _resolve_android_caller_info(row_dict, contacts_map)

        is_group_call = bool(row_dict.get("is_group_call") == 1)
        if is_group_call:
            chat_id_val = (
                str(row_dict["chat_row_id"])
                if row_dict["chat_row_id"] is not None
                else (raw_num or "call")
            )
        else:
            chat_id_val = raw_num or (
                str(row_dict["chat_row_id"]) if row_dict["chat_row_id"] is not None else "call"
            )

        ts_ms = int(row_dict["timestamp_ms"])
        results.append(
            {
                "id": str(row_dict["id"]),
                "caller_name": caller_name,
                "caller_phone": caller_phone,
                "chat_id": chat_id_val,
                "call_type": call_type,
                "is_missed": bool(call_type == "missed"),
                "from_me": bool(from_me == 1),
                "is_video": is_video,
                "duration": duration_sec,
                "timestamp": int(round(ts_ms / 1000)),
                "timestamp_ms": ts_ms,
                "is_group": is_group_call,
                "is_preserved": False,
            }
        )
    return results


def _resolve_ios_call_schema(call_conn: sqlite3.Connection) -> dict | None:
    """Inspect CoreData CallHistory.sqlite tables/columns and return schema metadata."""
    tables = {
        r[0]
        for r in call_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "ZWACDCALLEVENT" not in tables:
        return None

    cl_cols = {r[1] for r in call_conn.execute("PRAGMA table_info(ZWACDCALLEVENT)").fetchall()}
    agg_fk_col = (
        "Z1CALLEVENTS"
        if "Z1CALLEVENTS" in cl_cols
        else ("ZAGGREGATECALLEVENT" if "ZAGGREGATECALLEVENT" in cl_cols else None)
    )
    has_participants = "ZWACDCALLEVENTPARTICIPANT" in tables
    part_fk = None
    part_jid_col = None
    part_has_outcome = False
    if has_participants:
        part_cols = {
            r[1]
            for r in call_conn.execute("PRAGMA table_info(ZWACDCALLEVENTPARTICIPANT)").fetchall()
        }
        part_fk = (
            "Z1PARTICIPANTS"
            if "Z1PARTICIPANTS" in part_cols
            else ("ZCALLEVENT" if "ZCALLEVENT" in part_cols else None)
        )
        part_jid_col = (
            "ZJIDSTRING" if "ZJIDSTRING" in part_cols else ("ZJID" if "ZJID" in part_cols else None)
        )
        part_has_outcome = "ZOUTCOME" in part_cols

    return {
        "has_call_id": "ZCALLIDSTRING" in cl_cols,
        "has_group_jid": "ZGROUPJIDSTRING" in cl_cols,
        "has_duration": "ZDURATION" in cl_cols,
        "has_outcome": "ZOUTCOME" in cl_cols,
        "has_date": "ZDATE" in cl_cols,
        "has_creator": "ZGROUPCALLCREATORUSERJIDSTRING" in cl_cols,
        "agg_fk_col": agg_fk_col,
        "has_aggregate": agg_fk_col is not None and "ZWAAGGREGATECALLEVENT" in tables,
        "has_participants": has_participants,
        "part_fk": part_fk,
        "part_jid_col": part_jid_col,
        "part_has_outcome": part_has_outcome,
    }


def _fetch_ios_contacts_map(
    wa_conn: sqlite3.Connection, caller_jids: set[str]
) -> dict[str, tuple[str, str]]:
    """Batch-load resolved (full_name, phone_number) pairs from TEMP _ios_contacts."""
    if not caller_jids:
        return {}
    has_tbl = bool(
        wa_conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='_ios_contacts'"
        ).fetchone()
    )
    if not has_tbl:
        return {}
    jid_list = sorted(caller_jids)
    resolved: dict[str, tuple[str, str]] = {}
    for i in range(0, len(jid_list), 900):
        chunk = jid_list[i : i + 900]
        ph = ",".join("?" * len(chunk))
        for r in wa_conn.execute(
            f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
            chunk,
        ).fetchall():
            resolved[r[0]] = (r[1] or "", r[2] or "")
    return resolved


def _query_ios_calls(
    call_db_path: Path,
    wa_conn: sqlite3.Connection,
    filter_type: str,
    arch_conn: sqlite3.Connection | None,
    fetch_limit: int | None,
) -> list[dict]:
    """Query voice and video call history from iOS CallHistory.sqlite."""
    if not call_db_path.is_file():
        return []

    with contextlib.closing(sqlite3.connect(str(call_db_path))) as call_conn:
        call_conn.row_factory = sqlite3.Row
        schema = _resolve_ios_call_schema(call_conn)
        if schema is None:
            return []

        has_call_id = schema["has_call_id"]
        has_group_jid = schema["has_group_jid"]
        has_duration = schema["has_duration"]
        has_outcome = schema["has_outcome"]
        has_date = schema["has_date"]
        has_creator = schema["has_creator"]
        agg_fk_col = schema["agg_fk_col"]
        has_aggregate = schema["has_aggregate"]
        part_fk = schema["part_fk"]
        part_jid_col = schema["part_jid_col"]

        call_id_expr = (
            "COALESCE(NULLIF(cl.ZCALLIDSTRING, ''), 'call_' || cl.Z_PK)"
            if has_call_id
            else "'call_' || cl.Z_PK"
        )
        group_jid_expr = "cl.ZGROUPJIDSTRING" if has_group_jid else "NULL"
        creator_expr = "cl.ZGROUPCALLCREATORUSERJIDSTRING" if has_creator else "NULL"
        dur_expr = "COALESCE(cl.ZDURATION, 0)" if has_duration else "0"
        outcome_expr = "COALESCE(cl.ZOUTCOME, 0)" if has_outcome else "0"
        ts_expr = "CAST((cl.ZDATE + 978307200) AS INTEGER)" if has_date else "0"
        agg_join = (
            f"LEFT JOIN ZWAAGGREGATECALLEVENT agg ON agg.Z_PK = cl.{agg_fk_col}"
            if has_aggregate
            else ""
        )
        agg_pk_expr = f"cl.{agg_fk_col}" if has_aggregate else "NULL"
        video_expr = "COALESCE(agg.ZVIDEO, 0)" if has_aggregate else "0"
        from_me_expr = "CASE WHEN agg.ZINCOMING = 0 THEN 1 ELSE 0 END" if has_aggregate else "0"

        where_parts: list[str] = []
        apply_sql_limit = fetch_limit is not None
        if filter_type == "video":
            if has_aggregate:
                where_parts.append("agg.ZVIDEO = 1")
            else:
                apply_sql_limit = False
        elif filter_type == "missed":
            missed_conds: list[str] = []
            if has_aggregate:
                missed_conds.append("agg.ZINCOMING = 1")
            if has_outcome:
                missed_outcome_expr = (
                    "(cl.ZOUTCOME IN (2, 3) OR (cl.ZOUTCOME = 4 AND COALESCE(cl.ZDURATION, 0) = 0))"
                    if has_duration
                    else "cl.ZOUTCOME IN (2, 3)"
                )
                missed_conds.append(missed_outcome_expr)
            else:
                apply_sql_limit = False
            if missed_conds:
                where_parts.append(" AND ".join(missed_conds))

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        limit_sql = (
            f"LIMIT {int(fetch_limit)}" if (apply_sql_limit and fetch_limit is not None) else ""
        )

        raw_zdate_expr = "cl.ZDATE AS raw_zdate," if has_date else "NULL AS raw_zdate,"
        sql = f"""
            SELECT
                cl.Z_PK AS call_pk,
                {agg_pk_expr} AS agg_pk,
                {call_id_expr} AS id,
                {group_jid_expr} AS group_jid,
                {creator_expr} AS creator_jid,
                {ts_expr} AS timestamp,
                {raw_zdate_expr}
                {dur_expr} AS duration,
                {video_expr} AS is_video,
                {outcome_expr} AS outcome,
                {from_me_expr} AS from_me
            FROM ZWACDCALLEVENT cl
            {agg_join}
            {where_sql}
            ORDER BY {ts_expr} DESC
            {limit_sql}
        """
        call_rows = [dict(r) for r in call_conn.execute(sql).fetchall()]
        if not call_rows:
            return []

        participants_by_key: dict[int, list[dict]] = {}
        if part_fk and part_jid_col:
            out_col = "COALESCE(p.ZOUTCOME, 0)" if schema["part_has_outcome"] else "0"
            all_keys = set()
            for r in call_rows:
                if r.get("call_pk") is not None:
                    all_keys.add(r["call_pk"])
                if r.get("agg_pk") is not None:
                    all_keys.add(r["agg_pk"])
            key_list = list(all_keys)
            for i in range(0, len(key_list), 900):
                chunk = key_list[i : i + 900]
                ph = ",".join("?" * len(chunk))
                p_sql = f"""
                    SELECT p.{part_fk}, p.{part_jid_col}, {out_col}
                    FROM ZWACDCALLEVENTPARTICIPANT p
                    WHERE p.{part_fk} IN ({ph})
                """
                for pr in call_conn.execute(p_sql, chunk).fetchall():
                    kid = pr[0]
                    p_jid = pr[1] or ""
                    participants_by_key.setdefault(kid, []).append(
                        {
                            "jid": p_jid,
                            "outcome": int(pr[2] or 0),
                        }
                    )

    # Map chat sessions from ChatStorage.sqlite to resolve chat_id and contact names
    chat_session_map: dict[str, dict] = {}
    has_cs = bool(
        wa_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWACHATSESSION'"
        ).fetchone()
    )
    if has_cs:
        cs_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        ident_col = "ZCONTACTIDENTIFIER" if "ZCONTACTIDENTIFIER" in cs_cols else "NULL"
        cs_rows = wa_conn.execute(
            f"SELECT Z_PK, ZCONTACTJID, {ident_col}, ZPARTNERNAME FROM ZWACHATSESSION"
        ).fetchall()
        for csr in cs_rows:
            c_pk = str(csr[0])
            p_name = csr[3] or ""
            jid = csr[1]
            ident = csr[2]
            if jid:
                chat_session_map[jid] = {
                    "chat_id": c_pk,
                    "partner_name": p_name,
                    "companion_jid": ident,
                }
            if ident and ident != jid:
                chat_session_map[ident] = {
                    "chat_id": c_pk,
                    "partner_name": p_name,
                    "companion_jid": jid,
                }

    needed_jids: set[str] = set()
    for r in call_rows:
        c_pk = r["call_pk"]
        a_pk = r.get("agg_pk")
        row_parts = (
            participants_by_key[a_pk]
            if (a_pk is not None and a_pk in participants_by_key)
            else participants_by_key.get(c_pk, [])
        )
        cj = row_parts[0]["jid"] if row_parts else (r.get("creator_jid") or "")
        if cj:
            needed_jids.add(cj)
    ios_contacts_map = _fetch_ios_contacts_map(wa_conn, needed_jids)

    contacts_map = _get_contacts_lookup(arch_conn)
    results = []
    for r in call_rows:
        call_pk = r["call_pk"]
        agg_pk = r.get("agg_pk")
        from_me = int(r["from_me"])
        outcome = int(r["outcome"])
        duration_sec = max(0, int(r["duration"] or 0))
        is_video = bool(r["is_video"] == 1)

        call_type, _ = _map_ios_call_outcome(from_me, outcome, duration_sec)

        if filter_type == "missed" and call_type != "missed":
            continue
        if filter_type == "video" and not is_video:
            continue

        # Look up participants by aggregate PK first (CoreData canonical), fallback to call_pk
        parts = []
        if agg_pk is not None and agg_pk in participants_by_key:
            parts = participants_by_key[agg_pk]
        elif call_pk in participants_by_key:
            parts = participants_by_key[call_pk]

        group_jid = r["group_jid"]
        is_group = bool(group_jid and group_jid != "")
        caller_jid = parts[0]["jid"] if parts else (r.get("creator_jid") or "")
        resolved_name, resolved_phone = ios_contacts_map.get(caller_jid, ("", ""))

        if is_group:
            cs_info = chat_session_map.get(group_jid, {})
            chat_id_val = cs_info.get("chat_id") or (
                group_jid.split("@")[0] if "@" in group_jid else group_jid
            )
            if resolved_phone:
                caller_phone = f"+{resolved_phone.lstrip('+')}"
            elif caller_jid.endswith("@s.whatsapp.net"):
                caller_phone = f"+{caller_jid.split('@')[0]}"
            else:
                caller_phone = f"+{caller_jid.split('@')[0]}" if "@" in caller_jid else caller_jid
            caller_name = cs_info.get("partner_name") or caller_phone or "Unknown Group"
        else:
            cs_info = chat_session_map.get(caller_jid, {})
            if resolved_phone:
                caller_phone = f"+{resolved_phone.lstrip('+')}"
            elif caller_jid.endswith("@s.whatsapp.net"):
                caller_phone = f"+{caller_jid.split('@')[0]}"
            elif (cs_info.get("companion_jid") or "").endswith("@s.whatsapp.net"):
                caller_phone = f"+{cs_info['companion_jid'].split('@')[0]}"
            else:
                caller_phone = f"+{caller_jid.split('@')[0]}" if "@" in caller_jid else caller_jid

            chat_id_val = cs_info.get("chat_id")
            if not chat_id_val:
                clean_p = caller_phone.lstrip("+")
                chat_id_val = (
                    clean_p
                    if clean_p
                    else (caller_jid.split("@")[0] if "@" in caller_jid else "call")
                )

            caller_name = None
            if (
                resolved_name
                and not resolved_name.startswith("+")
                and not resolved_name.startswith("Unknown")
            ):
                caller_name = resolved_name
            if not caller_name:
                caller_name = cs_info.get("partner_name")
            if not caller_name and caller_phone:
                clean_num = caller_phone.lstrip("+")
                caller_name = contacts_map.get(clean_num, "")
            if not caller_name:
                caller_name = caller_phone or "Unknown"

        raw_zdate = r.get("raw_zdate")
        ts_ms = (
            int(round((float(raw_zdate) + 978307200.0) * 1000.0))
            if raw_zdate is not None
            else int(r["timestamp"]) * 1000
        )
        results.append(
            {
                "id": str(r["id"]),
                "caller_name": caller_name,
                "caller_phone": caller_phone,
                "chat_id": chat_id_val,
                "call_type": call_type,
                "is_missed": bool(call_type == "missed"),
                "from_me": bool(call_type == "outgoing"),
                "is_video": is_video,
                "duration": duration_sec,
                "timestamp": int(r["timestamp"]),
                "timestamp_ms": ts_ms,
                "is_group": is_group,
                "is_preserved": False,
            }
        )
        if fetch_limit is not None and len(results) >= fetch_limit:
            break

    return results


def _query_preserved_calls(
    pres_conn: sqlite3.Connection | None,
    filter_type: str,
    fetch_limit: int | None,
) -> list[dict]:
    """Query preserved calls from preserved_messages.db."""
    if pres_conn is None:
        return []

    try:
        has_table = pres_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preserved_calls'"
        ).fetchone()
        if not has_table:
            return []

        cols = {r[1] for r in pres_conn.execute("PRAGMA table_info(preserved_calls)").fetchall()}
        dur_expr = (
            "duration_seconds"
            if "duration_seconds" in cols
            else ("duration_sec" if "duration_sec" in cols else "0")
        )
        video_expr = (
            "is_video" if "is_video" in cols else ("video_call" if "video_call" in cols else "0")
        )
        chat_jid_expr = "chat_jid" if "chat_jid" in cols else "NULL"

        where_parts: list[str] = []
        if filter_type == "video":
            where_parts.append(f"{video_expr} = 1")
        elif filter_type == "missed":
            where_parts.append("from_me = 0 AND call_result IN (2, 3, 5)")
        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        limit_sql = f"LIMIT {int(fetch_limit)}" if fetch_limit is not None else ""

        rows = pres_conn.execute(f"""
            SELECT
                call_id,
                chat_id,
                {chat_jid_expr} AS chat_jid,
                caller_jid,
                caller_name,
                from_me,
                timestamp_ms,
                {dur_expr} AS duration_seconds,
                {video_expr} AS is_video,
                call_result
            FROM preserved_calls
            {where_sql}
            ORDER BY timestamp_ms DESC
            {limit_sql}
        """).fetchall()
    except sqlite3.OperationalError:
        return []

    results = []
    for r in rows:
        row_dict = dict(r)
        from_me = int(row_dict["from_me"])
        result_code = int(row_dict["call_result"])
        is_video = bool(row_dict["is_video"] == 1)

        if from_me == 1:
            call_type = "outgoing"
        elif result_code in (2, 3, 5):
            call_type = "missed"
        else:
            call_type = "incoming"

        if filter_type == "missed" and call_type != "missed":
            continue
        if filter_type == "video" and not is_video:
            continue

        c_jid = row_dict["caller_jid"] or ""
        caller_phone = f"+{c_jid.split('@')[0]}" if "@" in c_jid else c_jid
        caller_name = row_dict["caller_name"] or caller_phone or "Unknown"
        chat_id_val = str(row_dict["chat_id"]) if row_dict["chat_id"] else "call"

        ts_ms = int(row_dict["timestamp_ms"])
        is_group_call = bool(
            row_dict.get("is_group_call") == 1 or ("@g.us" in (row_dict.get("chat_jid") or ""))
        )
        results.append(
            {
                "id": str(row_dict["call_id"]),
                "caller_name": caller_name,
                "caller_phone": caller_phone,
                "chat_id": chat_id_val,
                "call_type": call_type,
                "is_missed": bool(call_type == "missed"),
                "from_me": bool(call_type == "outgoing"),
                "is_video": is_video,
                "duration": int(row_dict["duration_seconds"] or 0),
                "timestamp": int(round(ts_ms / 1000)),
                "timestamp_ms": ts_ms,
                "is_group": is_group_call,
                "is_preserved": True,
            }
        )
    return results


def _build_chat_call_bubble(
    call_id: str,
    source: str,
    timestamp_ms: int,
    from_me: bool,
    is_video: bool,
    call_type: str,
    outcome: str,
    duration_sec: int,
    participants: int,
    is_group: bool,
    sender_name: str | None,
    is_preserved: bool,
    preservation_reason: str | None,
    preserved_at: str | int | None,
    source_platform: str | None,
) -> dict:
    """Build a message-compatible call bubble payload for chat timelines."""
    return {
        "msg_id": f"call:{call_id}",
        "sort_id": 0,
        "stanza_id": None,
        "timestamp_ms": int(timestamp_ms),
        "sender": sender_name,
        "from_me": 1 if from_me else 0,
        "archive_path": None,
        "media_type": "call",
        "media_name": None,
        "text_body": None,
        "quoted_text": None,
        "quoted_sender": None,
        "quoted_ts": None,
        "quoted_msg_id": None,
        "quoted_has_thumb": 0,
        "reactions": None,
        "is_edited": 0,
        "edited_ts": None,
        "is_view_once": 0,
        "link_title": None,
        "link_desc": None,
        "link_url": None,
        "link_preview_type": 0,
        "has_thumbnail": 0,
        "is_starred": 0,
        "is_pinned": 0,
        "is_preserved": 1 if is_preserved else 0,
        "preservation_reason": preservation_reason,
        "preserved_at": preserved_at,
        "source_platform": source_platform,
        "call": {
            "id": str(call_id),
            "source": source,
            "video": bool(is_video),
            "from_me": bool(from_me),
            "call_type": call_type,
            "outcome": outcome,
            "duration_sec": max(0, int(duration_sec)),
            "participants": max(0, int(participants)),
            "is_group": bool(is_group),
            "sender_name": sender_name,
        },
    }


def _resolve_ios_chat_jids(wa_conn: sqlite3.Connection | None, chat_id: str) -> list[str]:
    """Resolve canonical JID and companion LID candidates for an iOS chat_id."""
    if not chat_id:
        return []
    target_jid = None
    ident_jid = None
    if wa_conn is not None and str(chat_id).isdigit():
        with contextlib.suppress(sqlite3.OperationalError):
            cs_cols = {
                r[1] for r in wa_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()
            }
            ident_col = ", ZCONTACTIDENTIFIER" if "ZCONTACTIDENTIFIER" in cs_cols else ""
            csr = wa_conn.execute(
                f"SELECT ZCONTACTJID{ident_col} FROM ZWACHATSESSION WHERE Z_PK = ?",
                (int(chat_id),),
            ).fetchone()
            if csr:
                target_jid = csr[0]
                if len(csr) > 1 and csr[1]:
                    ident_jid = csr[1]

    candidates: set[str] = set()
    if target_jid:
        candidates.add(target_jid)
    if ident_jid:
        candidates.add(ident_jid)
    if not candidates and "@" in str(chat_id):
        candidates.add(str(chat_id))
    elif not candidates and str(chat_id).lstrip("+").isdigit():
        candidates.add(f"{str(chat_id).lstrip('+')}@s.whatsapp.net")

    if wa_conn is not None and candidates:
        for c_jid in list(candidates):
            with contextlib.suppress(sqlite3.OperationalError):
                clean_p = c_jid.split("@")[0]
                c_rows = wa_conn.execute(
                    "SELECT jid, phone_number FROM _ios_contacts WHERE jid = ? OR phone_number = ?",
                    (c_jid, clean_p),
                ).fetchall()
                for cr in c_rows:
                    if cr[0]:
                        candidates.add(cr[0])
                    if cr[1]:
                        candidates.add(f"{cr[1]}@s.whatsapp.net")
    return sorted(candidates)


def _resolve_android_call_targets(
    conn: sqlite3.Connection,
    arch_conn: sqlite3.Connection | None,
    chat_id: str,
    chat_type: str,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> tuple[set[int], set[int]]:
    """Return (contact_jid_row_ids, group_jid_row_ids) for an Android chat."""
    _ensure_jid_map_resolved(conn, arch_conn)
    row_ids = _resolve_android_chat_row_ids(conn, chat_id, chat_type, row_ids_cache)

    chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat)").fetchall()}
    has_account_jid = "account_jid_row_id" in chat_cols

    if chat_type == "group":
        if not row_ids:
            return set(), set()
        ph = ",".join("?" * len(row_ids))
        g_rows = conn.execute(
            f"SELECT jid_row_id FROM chat WHERE _id IN ({ph}) AND jid_row_id IS NOT NULL",
            row_ids,
        ).fetchall()
        return set(), {int(r[0]) for r in g_rows if r[0] is not None}

    base_jid_ids: set[int] = set()
    if row_ids:
        ph = ",".join("?" * len(row_ids))
        acct_col = ", account_jid_row_id" if has_account_jid else ""
        c_rows = conn.execute(
            f"SELECT jid_row_id{acct_col} FROM chat WHERE _id IN ({ph})",
            row_ids,
        ).fetchall()
        for cr in c_rows:
            if cr[0] is not None:
                base_jid_ids.add(int(cr[0]))
            if has_account_jid and len(cr) > 1 and cr[1] is not None:
                base_jid_ids.add(int(cr[1]))

    clean_id = chat_id.split(":")[0] if ":" in chat_id else chat_id
    if clean_id and not row_ids:
        with contextlib.suppress(sqlite3.OperationalError):
            j_rows = conn.execute(
                "SELECT _id FROM jid WHERE user = ?",
                (clean_id,),
            ).fetchall()
            for jr in j_rows:
                if jr[0] is not None:
                    base_jid_ids.add(int(jr[0]))

    if not base_jid_ids:
        return set(), set()

    expanded = set(base_jid_ids)
    jid_list = list(base_jid_ids)
    ph = ",".join("?" * len(jid_list))
    with contextlib.suppress(sqlite3.OperationalError):
        jm_rows = conn.execute(
            f"""
            SELECT lid_row_id, jid_row_id
            FROM _jid_map_resolved
            WHERE lid_row_id IN ({ph}) OR jid_row_id IN ({ph})
            """,
            jid_list + jid_list,
        ).fetchall()
        for jm in jm_rows:
            if jm[0] is not None:
                expanded.add(int(jm[0]))
            if jm[1] is not None:
                expanded.add(int(jm[1]))
    return expanded, set()


def _resolve_preserved_call_jids(
    wa_conn: sqlite3.Connection | None,
    source_type: str,
    chat_id: str,
    chat_type: str,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> list[str]:
    """Resolve possible chat_jid strings used by preserved_calls for a chat."""
    candidates: set[str] = {str(chat_id)}
    if "@" not in str(chat_id):
        suffix = "@g.us" if chat_type == "group" else "@s.whatsapp.net"
        candidates.add(f"{chat_id}{suffix}")

    if wa_conn is None:
        return sorted(candidates)

    if source_type == "ios":
        candidates.update(_resolve_ios_chat_jids(wa_conn, chat_id))
    elif source_type == "android":
        row_ids = _resolve_android_chat_row_ids(wa_conn, chat_id, chat_type, row_ids_cache)
        if row_ids:
            ph = ",".join("?" * len(row_ids))
            with contextlib.suppress(sqlite3.OperationalError):
                j_rows = wa_conn.execute(
                    f"""
                    SELECT j.raw_string, j_real.raw_string
                    FROM chat c
                    LEFT JOIN jid j ON j._id = c.jid_row_id
                    LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = c.jid_row_id
                    LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                    WHERE c._id IN ({ph})
                    """,
                    row_ids,
                ).fetchall()
                for jr in j_rows:
                    if jr[0]:
                        candidates.add(str(jr[0]))
                    if jr[1]:
                        candidates.add(str(jr[1]))
    return sorted(candidates)


def _query_android_chat_calls(
    conn: sqlite3.Connection,
    arch_conn: sqlite3.Connection | None,
    chat_id: str,
    chat_type: str,
    before_ms: int | None,
    after_ms: int | None,
    before_inclusive: bool,
    after_inclusive: bool,
    limit: int,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> list[dict]:
    """Query calls belonging to a single Android chat within the requested window."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_log'"
    ).fetchone()
    if not has_table:
        return []

    cl_cols = {r[1] for r in conn.execute("PRAGMA table_info(call_log)").fetchall()}
    if "timestamp" not in cl_cols or "jid_row_id" not in cl_cols:
        return []

    has_video = "video_call" in cl_cols
    has_duration = "duration" in cl_cols
    has_result = "call_result" in cl_cols
    has_from_me = "from_me" in cl_cols
    has_group = "group_jid_row_id" in cl_cols
    has_call_id = "call_id" in cl_cols

    if chat_type == "group" and not has_group:
        return []

    contact_jid_ids, group_jid_ids = _resolve_android_call_targets(
        conn, arch_conn, chat_id, chat_type, row_ids_cache
    )
    if chat_type == "group":
        if not group_jid_ids:
            return []
        target_ids = sorted(group_jid_ids)
        scope_ph = ",".join("?" * len(target_ids))
        scope_pred = f"cl.group_jid_row_id IN ({scope_ph})"
        scope_params: list = list(target_ids)
    else:
        if not contact_jid_ids:
            return []
        target_ids = sorted(contact_jid_ids)
        scope_ph = ",".join("?" * len(target_ids))
        if has_group:
            scope_pred = (
                f"cl.jid_row_id IN ({scope_ph}) "
                "AND (cl.group_jid_row_id IS NULL OR cl.group_jid_row_id = 0)"
            )
        else:
            scope_pred = f"cl.jid_row_id IN ({scope_ph})"
        scope_params = list(target_ids)

    where_parts = [scope_pred]
    params: list = list(scope_params)
    if before_ms is not None:
        op = "<=" if before_inclusive else "<"
        where_parts.append(f"cl.timestamp {op} ?")
        params.append(before_ms)
    elif after_ms is not None:
        op = ">=" if after_inclusive else ">"
        where_parts.append(f"cl.timestamp {op} ?")
        params.append(after_ms)

    order_dir = "ASC" if (after_ms is not None and before_ms is None) else "DESC"
    params.append(limit)

    has_ldn = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lid_display_name'"
        ).fetchone()
    )
    jid_cols = {r[1] for r in conn.execute("PRAGMA table_info(jid)").fetchall()}
    non_lid_cond = (
        "j_caller.server != 'lid'"
        if "server" in jid_cols
        else "COALESCE(j_caller.raw_string, '') NOT LIKE '%@lid'"
    )
    id_expr = (
        "COALESCE(NULLIF(cl.call_id, ''), 'call_' || cl._id)"
        if has_call_id
        else "'call_' || cl._id"
    )
    video_expr = "COALESCE(cl.video_call, 0)" if has_video else "0"
    dur_expr = "COALESCE(cl.duration, 0)" if has_duration else "0"
    result_expr = "COALESCE(cl.call_result, 0)" if has_result else "0"
    from_me_expr = "COALESCE(cl.from_me, 0)" if has_from_me else "0"
    is_group_expr = (
        "CASE WHEN cl.group_jid_row_id IS NOT NULL AND cl.group_jid_row_id > 0 THEN 1 ELSE 0 END"
        if has_group
        else "0"
    )
    ldn_join = (
        "LEFT JOIN lid_display_name ldn_caller ON ldn_caller.lid_row_id = cl.jid_row_id"
        if has_ldn
        else ""
    )
    ldn_col = (
        "ldn_caller.display_name AS ldn_display_name," if has_ldn else "NULL AS ldn_display_name,"
    )

    sql = f"""
        SELECT
            cl._id AS call_log_id,
            {id_expr} AS id,
            {from_me_expr} AS from_me,
            cl.timestamp AS timestamp_ms,
            {dur_expr} AS duration,
            {video_expr} AS is_video,
            {result_expr} AS call_result,
            {is_group_expr} AS is_group_call,
            COALESCE(
                j_caller_real.user,
                CASE WHEN {non_lid_cond} THEN j_caller.user END,
                j_caller.user
            ) AS caller_number,
            con_caller.display_name AS con_display_name,
            con_caller.folder AS con_folder,
            {ldn_col}
            NULL AS chat_subject
        FROM call_log cl
        LEFT JOIN jid j_caller ON j_caller._id = cl.jid_row_id
        LEFT JOIN _jid_map_resolved jm_caller ON jm_caller.lid_row_id = cl.jid_row_id
        LEFT JOIN jid j_caller_real ON j_caller_real._id = jm_caller.jid_row_id
        LEFT JOIN _contacts_resolved con_caller ON con_caller.clean_number = COALESCE(
            j_caller_real.user,
            CASE WHEN {non_lid_cond} THEN j_caller.user END,
            j_caller.user
        )
        {ldn_join}
        WHERE {" AND ".join(where_parts)}
        ORDER BY cl.timestamp {order_dir}, cl._id {order_dir}
        LIMIT ?
    """
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return []

    participant_counts: dict[int, int] = {}
    if chat_type == "group":
        has_parts = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_log_participant_v2'"
            ).fetchone()
        )
        if has_parts:
            p_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(call_log_participant_v2)").fetchall()
            }
            if "call_log_row_id" in p_cols:
                row_pks = [r["call_log_id"] for r in rows]
                ph = ",".join("?" * len(row_pks))
                with contextlib.suppress(sqlite3.OperationalError):
                    for pr in conn.execute(
                        f"SELECT call_log_row_id, COUNT(*) FROM call_log_participant_v2 WHERE call_log_row_id IN ({ph}) GROUP BY call_log_row_id",
                        row_pks,
                    ).fetchall():
                        participant_counts[int(pr[0])] = int(pr[1])

    contacts_map = _get_contacts_lookup(arch_conn) if chat_type == "group" else {}
    bubbles: list[dict] = []
    for row_dict in rows:
        from_me_int = int(row_dict["from_me"] or 0)
        from_me = from_me_int == 1
        duration_sec = max(0, int(row_dict["duration"] or 0))
        is_video = bool(row_dict["is_video"] == 1)
        result_code = int(row_dict["call_result"] or 0)
        call_type, outcome = _map_android_call_outcome(from_me_int, result_code, duration_sec)
        is_group = bool(row_dict.get("is_group_call") == 1) or chat_type == "group"

        sender_name = None
        if is_group and not from_me:
            _, _, sender_name = _resolve_android_caller_info(row_dict, contacts_map)

        parts_count = participant_counts.get(int(row_dict["call_log_id"]), 1 if not is_group else 2)
        bubbles.append(
            _build_chat_call_bubble(
                str(row_dict["id"]),
                "android",
                int(row_dict["timestamp_ms"]),
                from_me,
                is_video,
                call_type,
                outcome,
                duration_sec,
                parts_count,
                is_group,
                sender_name,
                False,
                None,
                None,
                "android",
            )
        )
    return bubbles


def _query_ios_chat_calls(
    call_db_path: Path | None,
    wa_conn: sqlite3.Connection | None,
    arch_conn: sqlite3.Connection | None,
    chat_id: str,
    chat_type: str,
    before_ms: int | None,
    after_ms: int | None,
    before_inclusive: bool,
    after_inclusive: bool,
    limit: int,
) -> list[dict]:
    """Query calls belonging to a single iOS chat within the requested window."""
    if call_db_path is None or not call_db_path.is_file() or wa_conn is None:
        return []

    candidate_jids = _resolve_ios_chat_jids(wa_conn, chat_id)
    if not candidate_jids:
        return []

    with contextlib.closing(sqlite3.connect(str(call_db_path))) as call_conn:
        call_conn.row_factory = sqlite3.Row
        schema = _resolve_ios_call_schema(call_conn)
        if schema is None or not schema["has_date"]:
            return []

        has_call_id = schema["has_call_id"]
        has_group_jid = schema["has_group_jid"]
        has_duration = schema["has_duration"]
        has_outcome = schema["has_outcome"]
        has_creator = schema["has_creator"]
        agg_fk_col = schema["agg_fk_col"]
        has_aggregate = schema["has_aggregate"]
        part_fk = schema["part_fk"]
        part_jid_col = schema["part_jid_col"]

        if chat_type == "group" and not has_group_jid:
            return []

        ph = ",".join("?" * len(candidate_jids))
        where_parts: list[str] = []
        params: list = []

        if chat_type == "group":
            where_parts.append(f"cl.ZGROUPJIDSTRING IN ({ph})")
            params.extend(candidate_jids)
        else:
            if has_group_jid:
                where_parts.append("(cl.ZGROUPJIDSTRING IS NULL OR cl.ZGROUPJIDSTRING = '')")
            match_clauses: list[str] = []
            if part_fk and part_jid_col:
                fk_match = (
                    f"(p.{part_fk} = cl.{agg_fk_col} OR p.{part_fk} = cl.Z_PK)"
                    if agg_fk_col
                    else f"p.{part_fk} = cl.Z_PK"
                )
                match_clauses.append(
                    f"EXISTS (SELECT 1 FROM ZWACDCALLEVENTPARTICIPANT p WHERE {fk_match} AND p.{part_jid_col} IN ({ph}))"
                )
                params.extend(candidate_jids)
            if has_creator:
                match_clauses.append(f"cl.ZGROUPCALLCREATORUSERJIDSTRING IN ({ph})")
                params.extend(candidate_jids)
            if not match_clauses:
                return []
            where_parts.append("(" + " OR ".join(match_clauses) + ")")

        if before_ms is not None:
            before_apple = (float(before_ms) / 1000.0) - 978307200.0 + 0.002
            where_parts.append("cl.ZDATE <= ?")
            params.append(before_apple)
        elif after_ms is not None:
            after_apple = (float(after_ms) / 1000.0) - 978307200.0 - 0.002
            where_parts.append("cl.ZDATE >= ?")
            params.append(after_apple)

        order_dir = "ASC" if (after_ms is not None and before_ms is None) else "DESC"
        params.append(limit + 8)

        call_id_expr = (
            "COALESCE(NULLIF(cl.ZCALLIDSTRING, ''), 'call_' || cl.Z_PK)"
            if has_call_id
            else "'call_' || cl.Z_PK"
        )
        group_jid_expr = "cl.ZGROUPJIDSTRING" if has_group_jid else "NULL"
        creator_expr = "cl.ZGROUPCALLCREATORUSERJIDSTRING" if has_creator else "NULL"
        dur_expr = "COALESCE(cl.ZDURATION, 0)" if has_duration else "0"
        outcome_expr = "COALESCE(cl.ZOUTCOME, 0)" if has_outcome else "0"
        agg_join = (
            f"LEFT JOIN ZWAAGGREGATECALLEVENT agg ON agg.Z_PK = cl.{agg_fk_col}"
            if has_aggregate
            else ""
        )
        agg_pk_expr = f"cl.{agg_fk_col}" if has_aggregate else "NULL"
        video_expr = "COALESCE(agg.ZVIDEO, 0)" if has_aggregate else "0"
        from_me_expr = "CASE WHEN agg.ZINCOMING = 0 THEN 1 ELSE 0 END" if has_aggregate else "0"

        sql = f"""
            SELECT
                cl.Z_PK AS call_pk,
                {agg_pk_expr} AS agg_pk,
                {call_id_expr} AS id,
                {group_jid_expr} AS group_jid,
                {creator_expr} AS creator_jid,
                cl.ZDATE AS raw_zdate,
                {dur_expr} AS duration,
                {video_expr} AS is_video,
                {outcome_expr} AS outcome,
                {from_me_expr} AS from_me
            FROM ZWACDCALLEVENT cl
            {agg_join}
            WHERE {" AND ".join(where_parts)}
            ORDER BY cl.ZDATE {order_dir}, cl.Z_PK {order_dir}
            LIMIT ?
        """
        call_rows = [dict(r) for r in call_conn.execute(sql, params).fetchall()]
        if not call_rows:
            return []

        part_counts: dict[int, int] = {}
        first_part_jid: dict[int, str] = {}
        if part_fk and part_jid_col:
            all_keys = {
                k for r in call_rows for k in (r.get("call_pk"), r.get("agg_pk")) if k is not None
            }
            if all_keys:
                key_list = list(all_keys)
                for i in range(0, len(key_list), 900):
                    chunk = key_list[i : i + 900]
                    k_ph = ",".join("?" * len(chunk))
                    for pr in call_conn.execute(
                        f"SELECT p.{part_fk}, p.{part_jid_col} FROM ZWACDCALLEVENTPARTICIPANT p WHERE p.{part_fk} IN ({k_ph})",
                        chunk,
                    ).fetchall():
                        kid = pr[0]
                        part_counts[kid] = part_counts.get(kid, 0) + 1
                        if kid not in first_part_jid and pr[1]:
                            first_part_jid[kid] = pr[1]

    contacts_map = _get_contacts_lookup(arch_conn) if chat_type == "group" else {}
    needed_jids: set[str] = set()
    if chat_type == "group":
        for r in call_rows:
            cj = (
                r.get("creator_jid")
                or first_part_jid.get(r.get("agg_pk"))
                or first_part_jid.get(r["call_pk"])
                or ""
            )
            if cj:
                needed_jids.add(cj)
    ios_contacts_map = _fetch_ios_contacts_map(wa_conn, needed_jids)

    bubbles: list[dict] = []
    for r in call_rows:
        if r.get("raw_zdate") is None:
            continue
        ts_ms = int(round((float(r["raw_zdate"]) + 978307200.0) * 1000.0))
        if before_ms is not None:
            if before_inclusive and ts_ms > before_ms:
                continue
            if not before_inclusive and ts_ms >= before_ms:
                continue
        elif after_ms is not None:
            if after_inclusive and ts_ms < after_ms:
                continue
            if not after_inclusive and ts_ms <= after_ms:
                continue

        from_me_int = int(r["from_me"] or 0)
        from_me = from_me_int == 1
        duration_sec = max(0, int(r["duration"] or 0))
        is_video = bool(r["is_video"] == 1)
        outcome_code = int(r["outcome"] or 0)
        call_type, outcome = _map_ios_call_outcome(from_me_int, outcome_code, duration_sec)
        is_group = bool(r.get("group_jid")) or chat_type == "group"

        agg_pk = r.get("agg_pk")
        call_pk = r["call_pk"]
        parts_count = part_counts.get(agg_pk, part_counts.get(call_pk, 1 if not is_group else 2))

        sender_name = None
        if is_group and not from_me:
            caller_jid = (
                r.get("creator_jid")
                or first_part_jid.get(agg_pk)
                or first_part_jid.get(call_pk)
                or ""
            )
            if caller_jid:
                ic_info = ios_contacts_map.get(caller_jid)
                if ic_info and ic_info[0]:
                    sender_name = ic_info[0]
                if not sender_name and "@" in caller_jid:
                    clean_num = caller_jid.split("@")[0]
                    sender_name = contacts_map.get(clean_num) or f"+{clean_num}"

        bubbles.append(
            _build_chat_call_bubble(
                str(r["id"]),
                "ios",
                ts_ms,
                from_me,
                is_video,
                call_type,
                outcome,
                duration_sec,
                parts_count,
                is_group,
                sender_name,
                False,
                None,
                None,
                "ios",
            )
        )
        if len(bubbles) >= limit:
            break
    return bubbles


def _query_preserved_chat_calls(
    pres_conn: sqlite3.Connection | None,
    wa_conn: sqlite3.Connection | None,
    source_type: str,
    chat_id: str,
    chat_type: str,
    before_ms: int | None,
    after_ms: int | None,
    before_inclusive: bool,
    after_inclusive: bool,
    limit: int,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> list[dict]:
    """Query preserved calls belonging to a single chat within the requested window."""
    if pres_conn is None:
        return []

    try:
        has_table = pres_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preserved_calls'"
        ).fetchone()
        if not has_table:
            return []

        cols = {r[1] for r in pres_conn.execute("PRAGMA table_info(preserved_calls)").fetchall()}
        dur_expr = (
            "duration_seconds"
            if "duration_seconds" in cols
            else ("duration_sec" if "duration_sec" in cols else "0")
        )
        video_expr = (
            "is_video" if "is_video" in cols else ("video_call" if "video_call" in cols else "0")
        )
        has_chat_jid = "chat_jid" in cols
        has_is_group = "is_group_call" in cols
        has_parts = "participants" in cols
        has_pres_at = "preserved_at" in cols
        has_platform = "source_platform" in cols

        cand_jids = _resolve_preserved_call_jids(
            wa_conn, source_type, chat_id, chat_type, row_ids_cache
        )
        where_parts: list[str] = []
        params: list = []

        if has_chat_jid and cand_jids:
            ph = ",".join("?" * len(cand_jids))
            where_parts.append(f"(chat_id = ? OR chat_jid IN ({ph}))")
            params.append(str(chat_id))
            params.extend(cand_jids)
        else:
            where_parts.append("chat_id = ?")
            params.append(str(chat_id))

        if has_is_group:
            where_parts.append("COALESCE(is_group_call, 0) = ?")
            params.append(1 if chat_type == "group" else 0)

        if before_ms is not None:
            op = "<=" if before_inclusive else "<"
            where_parts.append(f"timestamp_ms {op} ?")
            params.append(before_ms)
        elif after_ms is not None:
            op = ">=" if after_inclusive else ">"
            where_parts.append(f"timestamp_ms {op} ?")
            params.append(after_ms)

        order_dir = "ASC" if (after_ms is not None and before_ms is None) else "DESC"
        params.append(limit)

        group_col = (
            "COALESCE(is_group_call, 0) AS is_group_call,"
            if has_is_group
            else "0 AS is_group_call,"
        )
        parts_col = "participants," if has_parts else "NULL AS participants,"
        pres_at_col = "preserved_at," if has_pres_at else "NULL AS preserved_at,"
        platform_col = "source_platform," if has_platform else "NULL AS source_platform,"

        rows = [
            dict(r)
            for r in pres_conn.execute(
                f"""
                SELECT
                    call_id,
                    caller_jid,
                    caller_name,
                    from_me,
                    timestamp_ms,
                    {dur_expr} AS duration_seconds,
                    {video_expr} AS is_video,
                    call_result,
                    {group_col}
                    {parts_col}
                    {pres_at_col}
                    {platform_col}
                    chat_id
                FROM preserved_calls
                WHERE {" AND ".join(where_parts)}
                ORDER BY timestamp_ms {order_dir}
                LIMIT ?
                """,
                params,
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return []

    bubbles: list[dict] = []
    for row_dict in rows:
        from_me_int = int(row_dict.get("from_me") or 0)
        from_me = from_me_int == 1
        duration_sec = max(0, int(row_dict.get("duration_seconds") or 0))
        is_video = bool(row_dict.get("is_video") == 1)
        result_code = int(row_dict.get("call_result") or 0)
        platform = str(row_dict.get("source_platform") or source_type or "").lower()
        if platform == "ios":
            call_type, outcome = _map_ios_call_outcome(from_me_int, result_code, duration_sec)
        else:
            call_type, outcome = _map_android_call_outcome(from_me_int, result_code, duration_sec)

        is_group = bool(row_dict.get("is_group_call") == 1) or chat_type == "group"
        parts_count = 1 if not is_group else 2
        raw_parts = row_dict.get("participants")
        if raw_parts:
            try:
                parsed_parts = json.loads(raw_parts) if isinstance(raw_parts, str) else raw_parts
                if isinstance(parsed_parts, list) and parsed_parts:
                    parts_count = len(parsed_parts)
            except (TypeError, ValueError):
                pass

        sender_name = row_dict.get("caller_name") if (is_group and not from_me) else None
        bubbles.append(
            _build_chat_call_bubble(
                str(row_dict["call_id"]),
                "preserved",
                int(row_dict["timestamp_ms"]),
                from_me,
                is_video,
                call_type,
                outcome,
                duration_sec,
                parts_count,
                is_group,
                sender_name,
                True,
                "deleted_on_device",
                row_dict.get("preserved_at"),
                row_dict.get("source_platform"),
            )
        )
    return bubbles


def _merge_chat_calls_with_messages(
    msg_rows: list[dict],
    live_calls: list[dict],
    pres_calls: list[dict],
    descending: bool,
    limit: int,
) -> list[dict]:
    """Deduplicate preserved vs live calls and merge sorted items up to limit."""
    deduped_calls = list(live_calls)
    live_ids = {c["call"]["id"] for c in live_calls}
    for pc in pres_calls:
        if pc["call"]["id"] in live_ids:
            continue
        pc_ts = pc["timestamp_ms"]
        pc_fm = pc["from_me"]
        pc_vid = pc["call"]["video"]
        is_dup = any(
            lc["from_me"] == pc_fm
            and lc["call"]["video"] == pc_vid
            and abs(lc["timestamp_ms"] - pc_ts) <= 2000
            for lc in live_calls
        )
        if not is_dup:
            deduped_calls.append(pc)

    if not deduped_calls:
        return msg_rows[:limit]

    combined = list(msg_rows) + deduped_calls
    combined.sort(
        key=lambda r: (
            int(r.get("timestamp_ms") or 0),
            int(r["sort_id"])
            if isinstance(r.get("sort_id"), int)
            else (int(r["msg_id"]) if isinstance(r.get("msg_id"), int) else 0),
            str(r.get("msg_id") or ""),
        ),
        reverse=descending,
    )
    return combined[:limit]


def _preserve_target_in_slice(
    sliced: list[dict],
    candidates: list[dict],
    target_key: int | str | None,
) -> list[dict]:
    """Ensure a jumped-to target item is not displaced past limit by co-located calls."""
    if target_key is None or not sliced:
        return sliced
    t_str = str(target_key)
    if any(str(r.get("msg_id")) == t_str for r in sliced):
        return sliced
    for item in candidates:
        if str(item.get("msg_id")) == t_str:
            return sliced[:-1] + [item]
    return sliced


def _fetch_chat_calls_for_window(
    source_type: str,
    wa_conn: sqlite3.Connection | None,
    arch_conn: sqlite3.Connection | None,
    pres_conn: sqlite3.Connection | None,
    wa_db_path: str | None,
    output_root: Path | None,
    chat_id: str,
    chat_type: str,
    before_ms: int | None,
    after_ms: int | None,
    before_inclusive: bool,
    after_inclusive: bool,
    limit: int,
    row_ids_cache: dict[tuple[str, str], list[int]],
) -> tuple[list[dict], list[dict]]:
    """Fetch (live_calls, preserved_calls) for a chat pagination window."""
    live_calls: list[dict] = []
    if wa_conn is not None:
        if source_type == "android":
            live_calls = _query_android_chat_calls(
                wa_conn,
                arch_conn,
                chat_id,
                chat_type,
                before_ms,
                after_ms,
                before_inclusive,
                after_inclusive,
                limit,
                row_ids_cache,
            )
        elif source_type == "ios" and wa_db_path is not None:
            call_db_p = _find_ios_call_history_db(wa_db_path, output_root)
            live_calls = _query_ios_chat_calls(
                call_db_p,
                wa_conn,
                arch_conn,
                chat_id,
                chat_type,
                before_ms,
                after_ms,
                before_inclusive,
                after_inclusive,
                limit,
            )

    pres_calls = _query_preserved_chat_calls(
        pres_conn,
        wa_conn,
        source_type,
        chat_id,
        chat_type,
        before_ms,
        after_ms,
        before_inclusive,
        after_inclusive,
        limit,
        row_ids_cache,
    )
    return live_calls, pres_calls
