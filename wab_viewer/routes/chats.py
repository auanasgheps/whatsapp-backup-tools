"""Chat list, chat detail metadata, and per-chat media size endpoints."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3

from flask import Blueprint, jsonify, request

from ..context import get_viewer_context
from ..db_cache import _find_ios_call_history_db
from ..hydration import (
    _format_android_service_row,
    _format_ios_service_row,
    _hydrate_android_service_participants,
    _media_type_from_path,
    _resolve_ios_jid,
)
from ..preserved import _merge_preserved_chats
from ..protobuf import _extract_ios_group_description, _parse_ios_poll_blob
from ..queries.calls import (
    _resolve_android_call_targets,
    _resolve_ios_call_schema,
    _resolve_ios_chat_jids,
)
from ..queries.group_members import _group_members_android, _group_members_ios
from ..queries.sql_builder import (
    _SUPPORTED_ANDROID_SYSTEM_ACTIONS,
    _clean_user_sql,
    _ios_chat_filter,
    _resolve_android_chat_filter,
)
from ..system_events import ALLOWED_GROUP_SYSTEM_ACTIONS, format_participant_names

bp = Blueprint("chats", __name__)


@bp.route("/api/chats")
def api_chats():
    ctx = get_viewer_context()
    conn = ctx.get_wa()
    if conn is None:
        result = []
        pres = ctx.get_preserved()
        if pres is not None:
            result = _merge_preserved_chats(result, pres, ctx.get_archive())
        return jsonify(result)

    if ctx.source_type == "android":
        j_s_is_lid = (
            "(j_s.server IS NOT NULL AND j_s.server = 'lid')" if ctx.caps.has_jid_server else "0"
        )
        lid_c_parts = []
        if ctx.caps.has_jid_server:
            lid_c_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'lid')")
        if ctx.caps.has_jid_raw_string:
            lid_c_parts.append("(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@lid')")
        j_c_is_lid = " OR ".join(lid_c_parts) if lid_c_parts else "0"

        group_c_parts = ["c.subject IS NOT NULL"]
        if ctx.caps.has_jid_server:
            group_c_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'g.us')")
        if ctx.caps.has_jid_raw_string:
            group_c_parts.append(
                "(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@g.us')"
            )
        is_group_chat = " OR ".join(group_c_parts)
        ms_chat_col = "ms.action_type" if ctx.caps.has_message_system else "NULL"
        ms_chat_join = (
            "LEFT JOIN message_system ms ON ms.message_row_id = m._id"
            if ctx.caps.has_message_system
            else ""
        )
        ms_allowed_actions_tuple = tuple(ALLOWED_GROUP_SYSTEM_ACTIONS)
        ms_sub_join = (
            "LEFT JOIN message_system ms_sub ON ms_sub.message_row_id = m_sub._id"
            if ctx.caps.has_message_system
            else ""
        )
        ms_sub_col = "ms_sub.action_type" if ctx.caps.has_message_system else "NULL"
        ms_sub_allowed_clause = (
            f"AND ms_sub.action_type IN {ms_allowed_actions_tuple}"
            if ctx.caps.has_message_system
            else "AND 0"
        )
        mrev_sub_filter = (
            "OR EXISTS (SELECT 1 FROM message_revoked mrev_sub WHERE mrev_sub.message_row_id = m_sub._id)"
            if ctx.caps.has_message_revoked
            else ""
        )
        ldn_s_join = (
            """
            LEFT JOIN _lid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
            LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_s.lid_row_id
        """
            if ctx.caps.has_lid_dn
            else "LEFT JOIN _jid_map_resolved jm_s ON jm_s.lid_row_id = m.sender_jid_row_id"
        )
        ldn_s_arm = (
            """
            NULLIF(ldn_s_direct.display_name, ''),
            NULLIF(ldn_s_mapped.display_name, ''),
        """
            if ctx.caps.has_lid_dn
            else ""
        )
        ldn_c_join = (
            "LEFT JOIN lid_display_name ldn_c ON ldn_c.lid_row_id = c.jid_row_id"
            if ctx.caps.has_lid_dn
            else ""
        )
        con_ldn_join = (
            f"LEFT JOIN _contacts_resolved con_ldn ON con_ldn.clean_number = {_clean_user_sql('ldn_c.display_name')}"
            if ctx.caps.has_lid_dn
            else ""
        )
        con_ldn_arm = "NULLIF(con_ldn.display_name, '')," if ctx.caps.has_lid_dn else ""
        con_ldn_folder_arm = "con_ldn.folder," if ctx.caps.has_lid_dn else ""
        ldn_chat_name_arm = (
            "CASE WHEN ldn_c.display_name NOT LIKE '%∙%' AND ldn_c.display_name NOT LIKE '%\u2219%' THEN NULLIF(ldn_c.display_name, '') END,"
            if ctx.caps.has_lid_dn
            else ""
        )
        ldn_chat_user_arm = "NULLIF(ldn_c.username, '')," if ctx.caps.has_lid_dn else ""
        chat_archived_col = "COALESCE(c.archived, 0)" if ctx.caps.has_chat_archived else "0"
        rows = conn.execute(f"""
            SELECT
                c._id                                               AS chat_row_id,
                {chat_archived_col}                                 AS is_archived,
                CASE WHEN {is_group_chat} THEN CAST(c._id AS TEXT)
                     ELSE COALESCE(
                         {_clean_user_sql("j_chat_real.user")},
                         {_clean_user_sql("j_chat.user")},
                         CAST(c._id AS TEXT)
                     )
                END                                                 AS id,
                CASE WHEN {is_group_chat} THEN 'group'
                     ELSE 'contact' END                            AS type,
                CASE WHEN {is_group_chat} THEN ''
                     ELSE COALESCE(
                         {_clean_user_sql("j_chat_real.user")},
                         {_clean_user_sql("j_chat.user")},
                         ''
                     )
                END                                                 AS phone_number,
                COALESCE(
                    NULLIF(c.subject, ''),
                    NULLIF(grp.subject, ''),
                    NULLIF(con.display_name, ''),
                    {con_ldn_arm}
                    CASE WHEN COALESCE(j_chat_real.user, j_chat.user) = '0' THEN 'WhatsApp' END,
                    con.folder,
                    {con_ldn_folder_arm}
                    {ldn_chat_name_arm}
                    {ldn_chat_user_arm}
                    CASE WHEN {is_group_chat} THEN 'Unknown Group (' || c._id || ')' END,
                    CASE WHEN j_chat_real.user IS NOT NULL THEN '+' || {_clean_user_sql("j_chat_real.user")} END,
                    CASE WHEN NOT ({j_c_is_lid}) AND j_chat.user IS NOT NULL THEN '+' || {_clean_user_sql("j_chat.user")} END,
                    'Unknown Contact (' || c._id || ')'
                )                                                   AS display_name,
                COALESCE(
                    m.timestamp,
                    NULLIF(c.sort_timestamp, 0),
                    0
                ) AS newest_ts,
                COALESCE(m.text_data, '')                          AS last_msg_preview,
                COALESCE(m.message_type, 0)                        AS last_msg_type,
                COALESCE(m.from_me, 0)                             AS last_msg_from_me,
                m._id                                               AS last_msg_id,
                mm.file_path                                        AS last_msg_media_path,
                {ms_chat_col}                                       AS last_msg_action_type,
                ''                                                  AS last_msg_participant_name,
                COALESCE(
                    NULLIF(con_s.display_name, ''),
                    CASE WHEN COALESCE(j_s_real.user, CASE WHEN NOT ({j_s_is_lid}) THEN j_s.user END) = '0' THEN 'WhatsApp' END,
                    CASE WHEN j_s_real.user IS NOT NULL THEN '+' || {_clean_user_sql("j_s_real.user")} END,
                    {ldn_s_arm}
                    CASE WHEN NOT ({j_s_is_lid}) AND j_s.user IS NOT NULL THEN '+' || {_clean_user_sql("j_s.user")} END,
                    ''
                )                                                   AS last_msg_sender
            FROM chat c
            LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
            LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
            LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
            {ldn_c_join}
            LEFT JOIN _contacts_resolved con ON con.clean_number = {_clean_user_sql("COALESCE(j_chat_real.user, j_chat.user)")}
            {con_ldn_join}
            LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(c._id AS TEXT)
            LEFT JOIN message m ON m._id = (
                SELECT m_sub._id
                FROM message m_sub
                {ms_sub_join}
                WHERE m_sub.chat_row_id = c._id
                  AND m_sub.message_type NOT IN (25, 67, 74, 79, 90)
                  AND (
                      (m_sub.text_data IS NOT NULL AND m_sub.text_data != '' AND (m_sub.message_type IS NULL OR m_sub.message_type = 0 OR m_sub.message_type IN (5, 16)))
                      OR (m_sub.message_type IN (1, 2, 3, 4, 5, 9, 13, 14, 15, 16, 20, 28, 42, 43, 64, 66, 82) {mrev_sub_filter})
                      OR (m_sub.message_type = 7 AND {ms_sub_col} = 1 AND NOT ({is_group_chat}))
                      OR ({is_group_chat} AND m_sub.message_type = 7 {ms_sub_allowed_clause})
                  )
                ORDER BY m_sub.timestamp DESC, m_sub._id DESC
                LIMIT 1
            )
            LEFT JOIN message_media mm ON mm.message_row_id = m._id
            LEFT JOIN jid j_s ON j_s._id = m.sender_jid_row_id
            {ldn_s_join}
            LEFT JOIN jid j_s_real ON j_s_real._id = jm_s.jid_row_id
            LEFT JOIN _contacts_resolved con_s ON con_s.clean_number = {_clean_user_sql("COALESCE(j_s_real.user, j_s.user)")}
            {ms_chat_join}
            WHERE (
                ({chat_archived_col} = 1)
                OR
                ({is_group_chat} AND (c.hidden = 0 OR c.hidden IS NULL OR m._id IS NOT NULL))
                OR
                (NOT ({is_group_chat}) AND m._id IS NOT NULL)
            )
            ORDER BY newest_ts DESC
        """).fetchall()
        deduped_chats: dict[str, dict] = {}
        for r in rows:
            cid = r["id"]
            r_dict = dict(r)
            if cid in deduped_chats:
                if r_dict.get("is_archived"):
                    deduped_chats[cid]["is_archived"] = 1
                continue
            deduped_chats[cid] = r_dict
        rows = list(deduped_chats.values())
    else:
        group_ev_col = "m.ZGROUPEVENTTYPE" if ctx.caps.has_ios_group_event else "0"
        # Exclude internal system events (2: E2E encryption notice, 56: security code notice)
        # from preview text and sorting so only actual user interactions or group actions are shown
        ios_service_filter_last_real = (
            "OR (msg.ZMESSAGETYPE IN (6, 10) AND msg.ZGROUPEVENTTYPE IN (1, 3, 4, 5, 6, 7, 9, 10, 12, 15, 21, 26, 36, 37, 38, 44, 50, 58))"
            if ctx.caps.has_ios_group_event
            else ""
        )
        cs_ident_col = "cs.ZCONTACTIDENTIFIER" if ctx.caps.has_cs_ident else "NULL"
        ios_archived_col = (
            f"COALESCE(cs.{ctx.caps.cs_archived_col_name}, 0)" if ctx.caps.has_cs_archived else "0"
        )
        ios_raw_ts_expr = (
            "COALESCE(m.ZMESSAGEDATE, cs.ZLASTMESSAGEDATE)"
            if ctx.caps.has_cs_last_msg_date
            else "m.ZMESSAGEDATE"
        )
        mi_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
        vcard_name_ios = "mi.ZVCARDNAME" if "ZVCARDNAME" in mi_cols else "NULL"
        if "ZMOVIEDURATION" in mi_cols and "ZVCARDSTRING" in mi_cols:
            ios_dur_col = """CASE
                WHEN mi.ZMOVIEDURATION IS NOT NULL AND mi.ZMOVIEDURATION > 0 THEN mi.ZMOVIEDURATION
                WHEN mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0
                    THEN CAST(ROUND(CAST(SUBSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), INSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), ';') + 1) AS REAL)) AS INTEGER)
                ELSE 0
            END"""
        elif "ZMOVIEDURATION" in mi_cols:
            ios_dur_col = "COALESCE(mi.ZMOVIEDURATION, 0)"
        elif "ZVCARDSTRING" in mi_cols:
            ios_dur_col = """CASE
                WHEN mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0
                    THEN CAST(ROUND(CAST(SUBSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), INSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), ';') + 1) AS REAL)) AS INTEGER)
                ELSE 0
            END"""
        else:
            ios_dur_col = "0"
        ios_phone_expr = f"""
            COALESCE(
                CASE WHEN cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                     THEN SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) END,
                CASE WHEN {cs_ident_col} LIKE '%@s.whatsapp.net'
                     THEN SUBSTR({cs_ident_col}, 1, INSTR({cs_ident_col}, '@') - 1) END,
                ic.phone_number
            )
        """
        has_groupinfo = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZWAGROUPINFO'"
            ).fetchone()
        )
        gi_cols = (
            {r[1] for r in conn.execute("PRAGMA table_info(ZWAGROUPINFO)").fetchall()}
            if has_groupinfo
            else set()
        )
        gi_join = "LEFT JOIN ZWAGROUPINFO gi ON gi.Z_PK = cs.ZGROUPINFO" if has_groupinfo else ""
        extra_gi_cols = []
        if "ZDESCRIPTION" in gi_cols:
            extra_gi_cols.append("gi.ZDESCRIPTION AS group_direct_desc")
        elif "ZGROUPDESCRIPTION" in gi_cols:
            extra_gi_cols.append("gi.ZGROUPDESCRIPTION AS group_direct_desc")
        else:
            extra_gi_cols.append("NULL AS group_direct_desc")

        if "ZPICTUREID" in gi_cols:
            extra_gi_cols.append("gi.ZPICTUREID AS group_picture_id")
        else:
            extra_gi_cols.append("NULL AS group_picture_id")
        extra_gi_select = ", " + ", ".join(extra_gi_cols)

        try:
            arch_grp_cols = {
                c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()
            }
        except sqlite3.OperationalError:
            arch_grp_cols = set()
        arch_grp_desc_col = (
            "grp.description AS group_arch_desc,"
            if "description" in arch_grp_cols
            else "NULL AS group_arch_desc,"
        )

        rows = conn.execute(f"""
            SELECT
                CAST(cs.Z_PK AS TEXT)                               AS id,
                {ios_archived_col}                                  AS is_archived,
                CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group'
                     ELSE 'contact' END                            AS type,
                CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN ''
                     ELSE COALESCE(({ios_phone_expr}), '')
                END                                                 AS phone_number,
                {arch_grp_desc_col}
                COALESCE(
                    NULLIF(con.display_name, ''),
                    NULLIF(con_lid.display_name, ''),
                    CASE WHEN ({ios_phone_expr}) = '0' OR cs.ZCONTACTJID = '0@s.whatsapp.net' THEN 'WhatsApp' END,
                    CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN COALESCE(NULLIF(grp.subject, ''), NULLIF(cs.ZPARTNERNAME, ''), 'Unknown Group (' || cs.Z_PK || ')') END,
                    CASE WHEN ic.full_name IS NOT NULL AND ic.full_name != '' AND ic.full_name NOT LIKE '+%' AND ic.full_name NOT LIKE 'Unknown%' THEN ic.full_name END,
                    NULLIF(cs.ZPARTNERNAME, ''),
                    CASE WHEN con.folder IS NOT NULL AND con.folder != '' AND con.folder NOT LIKE 'Unknown%'
                         THEN RTRIM(SUBSTR(con.folder, 1, INSTR(con.folder || ' (', ' (') - 1)) END,
                    CASE WHEN ({ios_phone_expr}) IS NOT NULL AND ({ios_phone_expr}) != '' THEN '+' || ({ios_phone_expr}) END,
                    'Unknown Contact (' || cs.Z_PK || ')'
                )                                                   AS display_name,
                CASE WHEN ({ios_raw_ts_expr}) IS NOT NULL
                     THEN CAST((({ios_raw_ts_expr}) + 978307200) * 1000 AS INTEGER)
                     ELSE 0 END                                     AS newest_ts,
                COALESCE(NULLIF(m.ZTEXT, ''), NULLIF({vcard_name_ios}, ''), '') AS last_msg_preview,
                COALESCE(m.ZMESSAGETYPE, 0)                        AS last_msg_type,
                {ios_dur_col}                                      AS last_msg_duration,
                {group_ev_col}                                     AS last_msg_group_event_type,
                COALESCE(m.ZISFROMME, 0)                           AS last_msg_from_me,
                mi.ZMEDIALOCALPATH                                  AS last_msg_media_path,
                CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN m.ZFROMJID END AS last_msg_sender_jid,
                gm.ZMEMBERJID                                       AS last_msg_member_jid
                {extra_gi_select}
            FROM ZWACHATSESSION cs
            {gi_join}
            LEFT JOIN _ios_contacts ic ON ic.jid = cs.ZCONTACTJID
            LEFT JOIN arch.contacts con
                  ON con.number = ({ios_phone_expr})
            LEFT JOIN arch.contacts con_lid
                  ON con_lid.number = SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                             INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1)
            LEFT JOIN arch.groups grp ON grp.chat_row_id = CAST(cs.Z_PK AS TEXT)
            LEFT JOIN (
                SELECT msg.ZCHATSESSION, MAX(msg.Z_PK) AS last_pk
                FROM ZWAMESSAGE msg
                LEFT JOIN ZWAMEDIAITEM mi2 ON mi2.Z_PK = msg.ZMEDIAITEM
                WHERE (msg.ZTEXT IS NOT NULL AND msg.ZTEXT != '' AND (msg.ZMESSAGETYPE IS NULL OR msg.ZMESSAGETYPE = 0))
                   OR (msg.ZMESSAGETYPE IS NOT NULL AND msg.ZMESSAGETYPE NOT IN (0, 6, 10, 14, 38, 39, 53)
                       AND (mi2.ZMEDIALOCALPATH IS NOT NULL OR msg.ZMESSAGETYPE IN (1, 2, 3, 5, 7, 8, 11, 15, 23, 46, 54) OR mi2.ZTITLE IS NOT NULL))
                   OR (msg.ZMESSAGETYPE IN (4, 14, 38, 39, 53))
                   {ios_service_filter_last_real}
                GROUP BY msg.ZCHATSESSION
            ) last_real ON last_real.ZCHATSESSION = cs.Z_PK
            LEFT JOIN ZWAMESSAGE m ON m.Z_PK = last_real.last_pk
            LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
            LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
            WHERE cs.ZHIDDEN = 0
              AND (
                  ({ios_archived_col}) = 1
                  OR cs.ZGROUPINFO IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM ZWAMESSAGE msg_real
                      WHERE msg_real.ZCHATSESSION = cs.Z_PK
                        AND (msg_real.ZMESSAGETYPE NOT IN (6, 10) OR msg_real.ZMESSAGETYPE IS NULL)
                  )
              )
            ORDER BY COALESCE({ios_raw_ts_expr}, 0) DESC
        """).fetchall()

    result = []
    ios_contacts_map = {}
    if ctx.source_type == "ios":
        needed_jids = set()
        for r in rows:
            if r["last_msg_type"] in (6, 10):
                for s_jid in (r["last_msg_sender_jid"], r["last_msg_member_jid"]):
                    if s_jid:
                        needed_jids.add(s_jid)
                        if not s_jid.endswith("@s.whatsapp.net") and not s_jid.endswith("@lid"):
                            needed_jids.add(f"{s_jid}@s.whatsapp.net")
                            needed_jids.add(f"{s_jid}@lid")
                        elif s_jid.endswith("@s.whatsapp.net"):
                            needed_jids.add(s_jid.split("@")[0])
                txt = (r["last_msg_preview"] or "").strip()
                if txt.startswith("{"):
                    try:
                        data = json.loads(txt)
                        author = data.get("author")
                        if author:
                            needed_jids.add(author)
                            if not author.endswith("@s.whatsapp.net") and not author.endswith(
                                "@lid"
                            ):
                                needed_jids.add(f"{author}@s.whatsapp.net")
                                needed_jids.add(f"{author}@lid")
                            elif author.endswith("@s.whatsapp.net"):
                                needed_jids.add(author.split("@")[0])
                    except (json.JSONDecodeError, AttributeError):
                        pass
                for part in re.split(r"[;,]", txt):
                    part = part.strip()
                    if part:
                        needed_jids.add(part)
                        if not part.endswith("@s.whatsapp.net") and not part.endswith("@lid"):
                            needed_jids.add(f"{part}@s.whatsapp.net")
                            needed_jids.add(f"{part}@lid")
                        elif part.endswith("@s.whatsapp.net"):
                            needed_jids.add(part.split("@")[0])
        if needed_jids:
            needed_list = list(needed_jids)
            for i in range(0, len(needed_list), 900):
                chunk = needed_list[i : i + 900]
                ph = ",".join("?" * len(chunk))
                try:
                    c_rows = conn.execute(
                        f"SELECT jid, full_name, phone_number FROM _ios_contacts WHERE jid IN ({ph})",
                        chunk,
                    ).fetchall()
                    for cr in c_rows:
                        ios_contacts_map[cr["jid"]] = (cr["full_name"], cr["phone_number"])
                except sqlite3.OperationalError:
                    pass
                try:
                    arch_rows = conn.execute(
                        f"SELECT number, display_name FROM arch.contacts WHERE number IN ({ph})",
                        chunk,
                    ).fetchall()
                    for ar in arch_rows:
                        if ar["display_name"]:
                            ios_contacts_map[ar["number"]] = (ar["display_name"], ar["number"])
                            ios_contacts_map[f"{ar['number']}@s.whatsapp.net"] = (
                                ar["display_name"],
                                ar["number"],
                            )
                except sqlite3.OperationalError:
                    pass
                try:
                    push_rows = conn.execute(
                        f"SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID IN ({ph})",
                        chunk,
                    ).fetchall()
                    for pr in push_rows:
                        if pr["ZPUSHNAME"]:
                            if pr["ZJID"] not in ios_contacts_map:
                                ios_contacts_map[pr["ZJID"]] = (pr["ZPUSHNAME"], None)
                            bare_p = pr["ZJID"].split("@")[0]
                            if bare_p not in ios_contacts_map:
                                ios_contacts_map[bare_p] = (pr["ZPUSHNAME"], None)
                except sqlite3.OperationalError:
                    pass

        try:
            user_row = conn.execute(
                "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZFROMJID LIKE '%@g.us' AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
            ).fetchone()
            if not user_row:
                user_row = conn.execute(
                    "SELECT ZTOJID FROM ZWAMESSAGE WHERE ZISFROMME = 0 AND ZTOJID LIKE '%@s.whatsapp.net' LIMIT 1"
                ).fetchone()
            if user_row and user_row[0]:
                u_jid = user_row[0]
                u_phone = u_jid.split("@")[0]
                ios_contacts_map[u_jid] = ("You", u_phone)
                ios_contacts_map[u_phone] = ("You", u_phone)
                ios_contacts_map[f"+{u_phone}"] = ("You", u_phone)
        except sqlite3.OperationalError:
            pass

    android_p_map = {}
    android_poll_mids = set()
    android_vom_mids = set()
    android_revoked_mids = set()
    if ctx.source_type == "android":
        candidate_last_mids = [r["last_msg_id"] for r in rows if r["last_msg_id"]]
        if candidate_last_mids:
            mao_cols = {r[1] for r in conn.execute("PRAGMA table_info(message_add_on)").fetchall()}
            has_mao_type = "message_add_on_type" in mao_cols
            for i in range(0, len(candidate_last_mids), 900):
                c_chunk = candidate_last_mids[i : i + 900]
                c_ph = ",".join("?" * len(c_chunk))
                try:
                    android_poll_mids.update(
                        r[0]
                        for r in conn.execute(
                            f"SELECT message_row_id FROM message_poll WHERE message_row_id IN ({c_ph})",
                            c_chunk,
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    pass
                if has_mao_type:
                    try:
                        android_poll_mids.update(
                            r[0]
                            for r in conn.execute(
                                f"SELECT parent_message_row_id FROM message_add_on WHERE parent_message_row_id IN ({c_ph}) AND message_add_on_type = 67",
                                c_chunk,
                            ).fetchall()
                        )
                    except sqlite3.OperationalError:
                        pass
                if ctx.caps.has_message_view_once_media:
                    try:
                        android_vom_mids.update(
                            r[0]
                            for r in conn.execute(
                                f"SELECT message_row_id FROM message_view_once_media WHERE message_row_id IN ({c_ph})",
                                c_chunk,
                            ).fetchall()
                        )
                    except sqlite3.OperationalError:
                        pass
                if ctx.caps.has_message_revoked:
                    try:
                        android_revoked_mids.update(
                            r[0]
                            for r in conn.execute(
                                f"SELECT message_row_id FROM message_revoked WHERE message_row_id IN ({c_ph})",
                                c_chunk,
                            ).fetchall()
                        )
                    except sqlite3.OperationalError:
                        pass
        if ctx.caps.has_mcp:
            svc_mids = [
                r["last_msg_id"]
                for r in rows
                if r["last_msg_type"] == 7
                and r["last_msg_action_type"] in _SUPPORTED_ANDROID_SYSTEM_ACTIONS
                and r["last_msg_id"]
            ]
            if svc_mids:
                android_p_map = _hydrate_android_service_participants(
                    conn,
                    svc_mids,
                    ctx.caps.has_mcp,
                    ctx.caps.has_lid_dn,
                    ctx.caps.has_jid_server,
                    ctx.caps.has_jid_raw_string,
                )

        android_group_desc: dict[int, str | None] = {}
        group_c_ids = [
            r["chat_row_id"]
            for r in rows
            if r.get("type") == "group" and r.get("chat_row_id") is not None
        ]
        if group_c_ids:
            arch_grp_cols = {
                c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()
            }
            has_grp_desc = "description" in arch_grp_cols
            for i in range(0, len(group_c_ids), 900):
                g_chunk = group_c_ids[i : i + 900]
                g_ph = ",".join("?" * len(g_chunk))
                if ctx.caps.has_message_system:
                    try:
                        desc_rows = conn.execute(
                            f"""
                            SELECT m.chat_row_id, ms.action_type, m.text_data
                            FROM message m
                            JOIN message_system ms ON ms.message_row_id = m._id
                            WHERE ms.action_type IN (27, 28) AND m.chat_row_id IN ({g_ph})
                            ORDER BY m.timestamp ASC, m._id ASC
                            """,
                            g_chunk,
                        ).fetchall()
                        for dr in desc_rows:
                            if dr["action_type"] == 27 and dr["text_data"]:
                                android_group_desc[dr["chat_row_id"]] = (
                                    dr["text_data"].strip() or None
                                )
                            else:
                                android_group_desc[dr["chat_row_id"]] = None
                    except sqlite3.OperationalError:
                        pass
                if has_grp_desc:
                    try:
                        arch_desc_rows = conn.execute(
                            f"SELECT chat_row_id, NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id IN ({g_ph})",
                            [str(cid) for cid in g_chunk],
                        ).fetchall()
                        for ar in arch_desc_rows:
                            cid_str = str(ar["chat_row_id"])
                            cid_val = int(cid_str) if cid_str.isdigit() else cid_str
                            if ar["description"] and not android_group_desc.get(cid_val):
                                android_group_desc[cid_val] = ar["description"].strip()
                    except sqlite3.OperationalError:
                        pass

    for r in rows:
        d = dict(r)
        c_row_id = d.pop("chat_row_id", None)
        if ctx.source_type == "android":
            if d.get("type") == "group" and c_row_id is not None:
                d["description"] = android_group_desc.get(c_row_id)
            else:
                d["description"] = None
        elif ctx.source_type == "ios":
            group_direct_desc = d.pop("group_direct_desc", None)
            group_picture_id = d.pop("group_picture_id", None)
            group_arch_desc = d.pop("group_arch_desc", None)
            if d.get("type") == "group":
                if group_direct_desc:
                    d["description"] = str(group_direct_desc).strip() or None
                elif group_picture_id:
                    d["description"] = _extract_ios_group_description(group_picture_id)
                elif group_arch_desc:
                    d["description"] = str(group_arch_desc).strip() or None
                else:
                    d["description"] = None
            else:
                d["description"] = None
        if ctx.source_type == "android" and c_row_id is not None:
            c_key = (d["id"], d["type"])
            existing_c = ctx.android_chat_row_ids_cache.get(c_key)
            if existing_c is None:
                ctx.android_chat_row_ids_cache[c_key] = [c_row_id]
            elif c_row_id not in existing_c:
                ctx.android_chat_row_ids_cache[c_key] = [*existing_c, c_row_id]
        raw_type = d.pop("last_msg_type", 0)
        media_path = d.pop("last_msg_media_path", None)
        last_msg_id = d.pop("last_msg_id", None)
        last_msg_action_type = d.pop("last_msg_action_type", None)
        last_msg_participant_name = d.pop("last_msg_participant_name", None)
        last_msg_participant_names = None
        if last_msg_id:
            last_msg_participant_names = android_p_map.get(last_msg_id)
        if not last_msg_participant_names and last_msg_participant_name:
            last_msg_participant_names = [last_msg_participant_name]
        last_msg_group_event_type = d.pop("last_msg_group_event_type", None)
        last_msg_sender = d.pop("last_msg_sender", None)
        last_msg_sender_jid = d.pop("last_msg_sender_jid", None)
        last_msg_member_jid = d.pop("last_msg_member_jid", None)
        last_msg_duration = d.pop("last_msg_duration", None)

        if (
            ctx.source_type == "android"
            and (raw_type in (15, 64) or last_msg_id in android_revoked_mids)
        ) or (ctx.source_type == "ios" and raw_type == 14):
            d["last_msg_type"] = "deleted"
            if ctx.source_type == "android" and raw_type == 64:
                d["last_msg_preview"] = "This message was deleted by an admin"
            elif d.get("last_msg_from_me"):
                d["last_msg_preview"] = "You deleted this message"
            else:
                d["last_msg_preview"] = "This message was deleted"
            d["is_view_once"] = 0
        elif (
            ctx.source_type == "android"
            and (raw_type in (42, 43, 82) or last_msg_id in android_vom_mids)
        ) or (ctx.source_type == "ios" and raw_type in (38, 39, 53)):
            d["is_view_once"] = 1
            if media_path:
                path = media_path if ctx.source_type == "android" else f"Message/{media_path}"
                d["last_msg_type"] = _media_type_from_path(path)
            else:
                if (ctx.source_type == "android" and raw_type in (3, 43)) or (
                    ctx.source_type == "ios" and raw_type == 39
                ):
                    d["last_msg_type"] = "video"
                elif (ctx.source_type == "android" and raw_type in (2, 82)) or (
                    ctx.source_type == "ios" and raw_type == 53
                ):
                    d["last_msg_type"] = "audio"
                else:
                    d["last_msg_type"] = "image"
        elif ctx.source_type == "android" and raw_type == 7:
            d["is_view_once"] = 0
            if d.get("type") == "contact" and last_msg_action_type == 1:
                d["last_msg_type"] = "deleted"
                if d.get("last_msg_from_me"):
                    d["last_msg_preview"] = "You deleted this message"
                else:
                    d["last_msg_preview"] = "This message was deleted"
            elif last_msg_action_type in _SUPPORTED_ANDROID_SYSTEM_ACTIONS:
                s_row = {
                    "action_type": last_msg_action_type,
                    "text_body": d.get("last_msg_preview", ""),
                    "from_me": d.get("last_msg_from_me", 0),
                    "sender": last_msg_sender,
                    "participant_names": last_msg_participant_names,
                    "participant_name": (
                        format_participant_names(last_msg_participant_names)
                        if last_msg_participant_names
                        else (last_msg_participant_name or "")
                    ),
                }
                d["last_msg_preview"] = _format_android_service_row(s_row)
                d["last_msg_type"] = "service"
            else:
                d["last_msg_preview"] = ""
                d["last_msg_type"] = "text"
        elif ctx.source_type == "ios" and raw_type in (6, 10):
            d["is_view_once"] = 0
            if not ctx.caps.has_ios_group_event or (
                last_msg_group_event_type and last_msg_group_event_type > 0
            ):
                sender_name = (
                    _resolve_ios_jid(last_msg_sender_jid, ios_contacts_map)
                    if last_msg_sender_jid
                    else ""
                )
                s_row = {
                    "group_event_type": last_msg_group_event_type,
                    "text_body": d.get("last_msg_preview", ""),
                    "from_me": d.get("last_msg_from_me", 0),
                    "sender": sender_name,
                    "member_jid": last_msg_member_jid or "",
                    "chat_type": d.get("type", "contact"),
                }
                d["last_msg_preview"] = _format_ios_service_row(s_row, ios_contacts_map)
                d["last_msg_type"] = "service"
            else:
                d["last_msg_preview"] = ""
                d["last_msg_type"] = "text"
        elif (
            ctx.source_type == "android"
            and (raw_type == 66 or (last_msg_id and last_msg_id in android_poll_mids))
        ) or (ctx.source_type == "ios" and raw_type == 46):
            d["is_view_once"] = 0
            d["last_msg_type"] = "poll"
            if ctx.source_type == "android":
                d["last_msg_preview"] = d.get("last_msg_preview") or "Poll"
            else:
                poll_q = ""
                if last_msg_id:
                    try:
                        info_r = conn.execute(
                            "SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE ZMESSAGE = ? AND ZRECEIPTINFO IS NOT NULL",
                            (last_msg_id,),
                        ).fetchone()
                        if info_r and info_r[0]:
                            parsed = _parse_ios_poll_blob(bytes(info_r[0]))
                            if parsed and parsed["question"]:
                                poll_q = parsed["question"]
                    except sqlite3.OperationalError:
                        pass
                d["last_msg_preview"] = poll_q or "Poll"
        elif (ctx.source_type == "android" and raw_type in (4, 14)) or (
            ctx.source_type == "ios" and raw_type == 4
        ):
            d["is_view_once"] = 0
            d["last_msg_type"] = "vcard"
        elif (ctx.source_type == "android" and raw_type == 20) or (
            ctx.source_type == "ios" and raw_type == 15
        ):
            d["is_view_once"] = 0
            d["last_msg_type"] = "sticker"
        elif (ctx.source_type == "ios" and raw_type == 7) or (
            (raw_type == 0 or raw_type is None)
            and (
                "http://" in (d.get("last_msg_preview") or "").lower()
                or "https://" in (d.get("last_msg_preview") or "").lower()
            )
        ):
            d["is_view_once"] = 0
            d["last_msg_type"] = "link"
        elif raw_type != 0:
            d["is_view_once"] = 0
            if media_path:
                path = media_path if ctx.source_type == "android" else f"Message/{media_path}"
                d["last_msg_type"] = _media_type_from_path(path)
            else:
                if (ctx.source_type == "android" and raw_type in (3, 28, 81)) or (
                    ctx.source_type == "ios" and raw_type in (2, 54)
                ):
                    d["last_msg_type"] = "video"
                elif (ctx.source_type == "android" and raw_type == 2) or (
                    ctx.source_type == "ios" and raw_type in (3, 53)
                ):
                    d["last_msg_type"] = "audio"
                elif (ctx.source_type == "android" and raw_type == 9) or (
                    ctx.source_type == "ios" and raw_type == 8
                ):
                    d["last_msg_type"] = "document"
                elif (ctx.source_type == "android" and raw_type == 5) or (
                    ctx.source_type == "ios"
                    and raw_type == 5
                    and not (last_msg_duration and last_msg_duration > 0)
                ):
                    d["last_msg_type"] = "location"
                elif (ctx.source_type == "android" and raw_type == 16) or (
                    ctx.source_type == "ios"
                    and (
                        raw_type == 16
                        or (raw_type == 5 and last_msg_duration and last_msg_duration > 0)
                    )
                ):
                    d["last_msg_type"] = "live_location"
                elif (ctx.source_type == "android" and raw_type == 13) or (
                    ctx.source_type == "ios" and raw_type == 11
                ):
                    d["last_msg_type"] = "gif"
                else:
                    d["last_msg_type"] = "image"
        else:
            d["is_view_once"] = 0
            d["last_msg_type"] = "text"
        result.append(d)
    pres = ctx.get_preserved()
    if pres is not None:
        result = _merge_preserved_chats(result, pres, ctx.get_archive())
    else:
        for c in result:
            c.setdefault("is_preserved_only", False)
            c.setdefault("description", None)
    return jsonify(result)


@bp.route("/api/chat-info")
@bp.route("/api/chat_info")
def api_chat_info():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")

    cache = ctx.get_cache()
    ts_row = cache.execute(
        "SELECT MIN(timestamp_ms) AS first_ts, MAX(timestamp_ms) AS last_ts "
        "FROM message_index WHERE chat_id = ? AND chat_type = ?",
        (chat_id, chat_type),
    ).fetchone()
    last_ts = None
    first_ts = None
    sent = received = total = None

    conn = ctx.get_wa()

    if conn is not None:
        if ctx.source_type == "android":
            chat_pred, chat_params = _resolve_android_chat_filter(
                conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
            )
            extra = ctx.queries.android_filter
            stat_select = (
                ctx.queries.android_stat_aggregate_select
                if ctx.queries.android_stat_aggregate_select is not None
                else ctx.queries.android_stat_select
            )
            stat_row = conn.execute(
                f"SELECT MIN(CASE WHEN media_type != 'service' THEN timestamp_ms END) AS first_ts,"
                f" MAX(CASE WHEN media_type != 'service' THEN timestamp_ms END) AS last_ts,"
                f" COUNT(CASE WHEN media_type != 'service' THEN 1 END) AS total,"
                f" SUM(CASE WHEN media_type != 'service' AND from_me=1 THEN 1 ELSE 0 END) AS sent,"
                f" SUM(CASE WHEN media_type != 'service' AND from_me=0 THEN 1 ELSE 0 END) AS received"
                f" FROM ({stat_select} WHERE {chat_pred} {extra})",
                chat_params,
            ).fetchone()
            if stat_row:
                first_ts = stat_row["first_ts"]
                last_ts = stat_row["last_ts"]
                total = stat_row["total"] or 0
                sent = stat_row["sent"] or 0
                received = stat_row["received"] or 0
            else:
                total = sent = received = 0
        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            live = conn.execute(
                f"""
                SELECT
                    MIN(CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)) AS first_ts,
                    MAX(CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)) AS last_ts
                FROM ZWAMESSAGE m
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                WHERE {chat_pred} {ctx.queries.ios_filter_ts}
                  AND COALESCE(m.ZMESSAGETYPE, 0) NOT IN (6, 10)
                """,
                chat_params,
            ).fetchone()
            if live:
                first_ts = live["first_ts"]
                last_ts = live["last_ts"]
    elif ts_row:
        first_ts = ts_row["first_ts"]
        last_ts = ts_row["last_ts"]

    display_name = None
    number = None
    description = None
    members = []
    top_senders = []
    created_ts = None
    creator_number = None
    creator_name = None

    if conn is not None:
        if ctx.source_type == "android":
            chat_pred, chat_params = _resolve_android_chat_filter(
                conn, chat_id, chat_type, ctx.android_chat_row_ids_cache
            )
            extra = ctx.queries.android_filter

            if chat_type == "contact":
                row = conn.execute(
                    """
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
                """,
                    (chat_id,),
                ).fetchone()
                number = row["user"] if row else chat_id

                name_row = conn.execute(
                    """
                    SELECT COALESCE(NULLIF(con.display_name,''), con.folder) AS name
                    FROM arch.contacts con
                    WHERE con.number = ?
                """,
                    (chat_id,),
                ).fetchone()
                if chat_id == "0":
                    display_name = "WhatsApp"
                elif name_row and name_row["name"]:
                    display_name = name_row["name"]
                else:
                    display_name = None
            else:
                members = _group_members_android(conn, chat_id)

                top_rows = conn.execute(
                    f"SELECT CASE WHEN from_me=1 THEN 'You' ELSE sender END AS sndr,"
                    f" COUNT(*) AS cnt FROM ({ctx.queries.android_stat_select} WHERE {chat_pred} {extra})"
                    f" WHERE (from_me=1 OR sender != '') AND media_type != 'service' GROUP BY sndr ORDER BY cnt DESC LIMIT 5",
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
                    tables = {
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "message_system" in tables:
                        desc_row = conn.execute(
                            """
                            SELECT m.text_data
                            FROM message m
                            JOIN message_system ms ON ms.message_row_id = m._id
                            WHERE m.chat_row_id = CAST(? AS INTEGER)
                              AND ms.action_type = 27
                            ORDER BY m.timestamp DESC, m._id DESC
                            LIMIT 1
                        """,
                            (chat_id,),
                        ).fetchone()
                        if desc_row and desc_row["text_data"]:
                            description = desc_row["text_data"].strip() or None

                    if not description:
                        arch_grp_cols = {
                            c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()
                        }
                        if "description" in arch_grp_cols:
                            arch_desc_row = conn.execute(
                                "SELECT NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id = ?",
                                (str(chat_id),),
                            ).fetchone()
                            if arch_desc_row and arch_desc_row["description"]:
                                description = arch_desc_row["description"].strip() or None

                    jid_cols = (
                        {c[1] for c in conn.execute("PRAGMA table_info(jid)").fetchall()}
                        if "jid" in tables
                        else set()
                    )
                    j_fallback = (
                        "CASE WHEN j.server = 's.whatsapp.net' THEN j.user END"
                        if "server" in jid_cols
                        else "j.user"
                    )

                    has_lid_dn = "lid_display_name" in tables

                    jm_join = """
                        LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
                        LEFT JOIN jid j_real ON j_real._id = jm.jid_row_id
                    """
                    creator_expr = f"COALESCE(j_real.user, {j_fallback})"

                    ldn_creator_join = (
                        """
                        LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = m.sender_jid_row_id
                        LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id
                        LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
                    """
                        if has_lid_dn
                        else ""
                    )
                    ldn_creator_arm = (
                        """
                        NULLIF(ldn_direct.display_name, ''),
                        NULLIF(ldn_mapped.display_name, ''),
                    """
                        if has_lid_dn
                        else ""
                    )

                    cre_row = conn.execute(
                        f"""
                        SELECT c.created_timestamp,
                               {creator_expr} AS creator,
                               COALESCE(
                                   NULLIF(con.display_name, ''),
                                   CASE WHEN {creator_expr} IS NOT NULL THEN '+' || {creator_expr} END,
                                   {ldn_creator_arm}
                                   ''
                               ) AS creator_name
                        FROM chat c
                        LEFT JOIN message m
                               ON m.chat_row_id = c._id
                              AND m.timestamp = c.created_timestamp
                              AND m.message_type = 7
                        LEFT JOIN jid j ON j._id = m.sender_jid_row_id
                        {jm_join}
                        {ldn_creator_join}
                        LEFT JOIN arch.contacts con ON con.number = {creator_expr}
                        WHERE c._id = CAST(? AS INTEGER)
                        LIMIT 1
                    """,
                        (chat_id,),
                    ).fetchone()
                    if cre_row:
                        created_ts = cre_row["created_timestamp"]
                        creator_number = cre_row["creator"]
                        creator_name = cre_row["creator_name"]
                except sqlite3.OperationalError:
                    pass

        else:
            chat_pred, chat_params = _ios_chat_filter(chat_id)
            extra = ctx.queries.ios_filter
            select = ctx.queries.ios_select

            count_row = conn.execute(
                f"SELECT COUNT(*) AS total,"
                f" SUM(CASE WHEN from_me=1 THEN 1 ELSE 0 END) AS sent,"
                f" SUM(CASE WHEN from_me=0 THEN 1 ELSE 0 END) AS received"
                f" FROM ({select} WHERE {chat_pred} {extra}) WHERE media_type != 'service'",
                chat_params,
            ).fetchone()
            total = count_row["total"] or 0 if count_row else 0
            sent = count_row["sent"] or 0 if count_row else 0
            received = count_row["received"] or 0 if count_row else 0

            if chat_type == "contact":
                cs_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()
                }
                has_cs_ident = "ZCONTACTIDENTIFIER" in cs_cols
                cs_ident_col = "cs.ZCONTACTIDENTIFIER" if has_cs_ident else "NULL"
                phone_expr = f"""
                    COALESCE(
                        CASE WHEN cs.ZCONTACTJID LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1) END,
                        CASE WHEN {cs_ident_col} LIKE '%@s.whatsapp.net'
                             THEN SUBSTR({cs_ident_col}, 1, INSTR({cs_ident_col}, '@') - 1) END,
                        ic.phone_number
                    )
                """
                row = conn.execute(
                    f"""
                    SELECT
                        ({phone_expr}) AS user,
                        COALESCE(
                            NULLIF(con.display_name,''),
                            NULLIF(con_lid.display_name,''),
                            CASE WHEN ({phone_expr}) = '0' OR cs.ZCONTACTJID = '0@s.whatsapp.net' THEN 'WhatsApp' END,
                            CASE WHEN ic.full_name IS NOT NULL AND ic.full_name != '' AND ic.full_name NOT LIKE '+%' AND ic.full_name NOT LIKE 'Unknown%' THEN ic.full_name END,
                            NULLIF(cs.ZPARTNERNAME, ''),
                            CASE WHEN con.folder IS NOT NULL AND con.folder != '' AND con.folder NOT LIKE 'Unknown%'
                                 THEN RTRIM(SUBSTR(con.folder, 1, INSTR(con.folder || ' (', ' (') - 1)) END,
                            CASE WHEN ({phone_expr}) IS NOT NULL AND ({phone_expr}) != '' THEN '+' || ({phone_expr}) END,
                            'Unknown Contact (' || cs.Z_PK || ')'
                        ) AS name
                    FROM ZWACHATSESSION cs
                    LEFT JOIN _ios_contacts ic ON ic.jid = cs.ZCONTACTJID
                    LEFT JOIN arch.contacts con ON con.number = ({phone_expr})
                    LEFT JOIN arch.contacts con_lid ON con_lid.number = NULLIF(
                        SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                               INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '')
                    WHERE cs.Z_PK = CAST(? AS INTEGER) AND cs.ZGROUPINFO IS NULL
                """,
                    (chat_id,),
                ).fetchone()
                number = row["user"] if row else None
                display_name = row["name"] if row else None
            else:
                members = _group_members_ios(conn, chat_id)

                top_rows = conn.execute(
                    f"SELECT CASE WHEN from_me=1 THEN 'You' ELSE sender END AS sndr,"
                    f" COUNT(*) AS cnt FROM ({select} WHERE {chat_pred} {extra})"
                    f" WHERE (from_me=1 OR sender != '') AND media_type != 'service' GROUP BY sndr ORDER BY cnt DESC LIMIT 5",
                    chat_params,
                ).fetchall()
                top_senders = [{"name": r["sndr"], "count": r["cnt"]} for r in top_rows]

                grp_row = conn.execute(
                    "SELECT COALESCE(NULLIF(grp.subject,''), grp.folder) AS name"
                    " FROM arch.groups grp WHERE grp.chat_row_id = ?",
                    (chat_id,),
                ).fetchone()
                phone_expr_c = """
                    COALESCE(
                        CASE WHEN gi.ZCREATORJID LIKE '%@s.whatsapp.net'
                             THEN SUBSTR(gi.ZCREATORJID, 1, INSTR(gi.ZCREATORJID || '@', '@') - 1)
                        END,
                        ic.phone_number
                    )
                """
                raw_prefix_c = "SUBSTR(gi.ZCREATORJID, 1, INSTR(gi.ZCREATORJID || '@', '@') - 1)"
                phone_jid_c = f"({phone_expr_c} || '@s.whatsapp.net')"

                gi_cols = {c[1] for c in conn.execute("PRAGMA table_info(ZWAGROUPINFO)").fetchall()}
                extra_gi_cols = []
                if "ZPICTUREID" in gi_cols:
                    extra_gi_cols.append("gi.ZPICTUREID AS picture_id")
                if "ZDESCRIPTION" in gi_cols:
                    extra_gi_cols.append("gi.ZDESCRIPTION AS direct_desc")
                elif "ZGROUPDESCRIPTION" in gi_cols:
                    extra_gi_cols.append("gi.ZGROUPDESCRIPTION AS direct_desc")
                extra_gi_select = (", " + ", ".join(extra_gi_cols)) if extra_gi_cols else ""

                cre_row = conn.execute(
                    f"""
                    SELECT CAST((gi.ZCREATIONDATE + 978307200) * 1000 AS INTEGER) AS created_ms,
                           COALESCE({phone_expr_c}, '') AS creator_number,
                           COALESCE(
                               CASE WHEN ic.full_name NOT LIKE '+%' THEN NULLIF(ic.full_name, '') END,
                               NULLIF(con.display_name, ''),
                               NULLIF(con_lid.display_name, ''),
                               NULLIF(pp_lid.ZPUSHNAME, ''),
                               NULLIF(pp_phone.ZPUSHNAME, ''),
                               NULLIF(cs_lid.ZPARTNERNAME, ''),
                               NULLIF(cs_phone.ZPARTNERNAME, ''),
                               ''
                           ) AS creator_name
                           {extra_gi_select}
                    FROM ZWACHATSESSION cs
                    JOIN ZWAGROUPINFO gi ON gi.Z_PK = cs.ZGROUPINFO
                    LEFT JOIN _ios_contacts ic ON ic.jid = gi.ZCREATORJID
                    LEFT JOIN arch.contacts con ON con.number = {phone_expr_c}
                    LEFT JOIN arch.contacts con_lid ON con_lid.number = {raw_prefix_c}
                    LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = gi.ZCREATORJID
                    LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = {phone_jid_c}
                    LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = gi.ZCREATORJID
                    LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = {phone_jid_c}
                    WHERE cs.Z_PK = CAST(? AS INTEGER)
                """,
                    (chat_id,),
                ).fetchone()
                if cre_row:
                    created_ts = cre_row["created_ms"]
                    creator_number = cre_row["creator_number"] or None
                    creator_name = cre_row["creator_name"] or None
                    cre_keys = cre_row.keys()
                    if "direct_desc" in cre_keys and cre_row["direct_desc"]:
                        description = str(cre_row["direct_desc"]).strip() or None
                    elif "picture_id" in cre_keys and cre_row["picture_id"]:
                        description = _extract_ios_group_description(cre_row["picture_id"])

                if not description:
                    arch_grp_cols = {
                        c[1] for c in conn.execute("PRAGMA arch.table_info(groups)").fetchall()
                    }
                    if "description" in arch_grp_cols:
                        arch_desc_row = conn.execute(
                            "SELECT NULLIF(description, '') AS description FROM arch.groups WHERE chat_row_id = ?",
                            (str(chat_id),),
                        ).fetchone()
                        if arch_desc_row and arch_desc_row["description"]:
                            description = arch_desc_row["description"].strip() or None

    if total is None or total == 0:
        pres = ctx.get_preserved()
        if pres is not None:
            p_stat = pres.execute(
                """
                SELECT COUNT(CASE WHEN media_type != 'service' THEN 1 END) AS total,
                       SUM(CASE WHEN media_type != 'service' AND from_me=1 THEN 1 ELSE 0 END) AS sent,
                       SUM(CASE WHEN media_type != 'service' AND from_me=0 THEN 1 ELSE 0 END) AS received,
                       MIN(CASE WHEN media_type != 'service' THEN timestamp_ms END) AS first_ts,
                       MAX(CASE WHEN media_type != 'service' THEN timestamp_ms END) AS last_ts
                FROM preserved_messages
                WHERE chat_id = ? AND chat_type = ?
                """,
                (chat_id, chat_type),
            ).fetchone()
            if p_stat and p_stat["total"]:
                total = p_stat["total"]
                sent = p_stat["sent"] or 0
                received = p_stat["received"] or 0
                first_ts = first_ts or p_stat["first_ts"]
                last_ts = last_ts or p_stat["last_ts"]
                if not display_name:
                    arch = ctx.get_archive()
                    if chat_type == "group":
                        g_row = arch.execute(
                            "SELECT subject, folder FROM groups WHERE chat_row_id = ?",
                            (chat_id,),
                        ).fetchone()
                        if g_row:
                            display_name = g_row["subject"] or g_row["folder"]
                    else:
                        c_row = arch.execute(
                            "SELECT display_name, folder FROM contacts WHERE number = ?",
                            (chat_id,),
                        ).fetchone()
                        if c_row:
                            display_name = c_row["display_name"] or c_row["folder"]
                        elif chat_id == "0":
                            display_name = "WhatsApp"
                        else:
                            display_name = f"+{chat_id}"
                        number = chat_id

    calls_count = 0
    pres = ctx.get_preserved()
    if pres is not None:
        try:
            pc_row = pres.execute(
                """
                SELECT subject, description, created_ts, creator_jid, oldest_ts, newest_ts, msg_count, members_json
                FROM preserved_chats
                WHERE chat_id = ? AND chat_type = ?
                """,
                (chat_id, chat_type),
            ).fetchone()
            if pc_row:
                if not display_name and pc_row["subject"]:
                    display_name = pc_row["subject"]
                if not description and pc_row["description"]:
                    description = pc_row["description"]
                if created_ts is None and pc_row["created_ts"]:
                    created_ts = pc_row["created_ts"]
                if first_ts is None and pc_row["oldest_ts"]:
                    first_ts = pc_row["oldest_ts"]
                if last_ts is None and pc_row["newest_ts"]:
                    last_ts = pc_row["newest_ts"]
                if (total is None or total == 0) and pc_row["msg_count"]:
                    total = pc_row["msg_count"]
                if chat_type == "group" and not members and pc_row["members_json"]:
                    try:
                        members = json.loads(pc_row["members_json"])
                    except Exception:
                        pass
        except sqlite3.OperationalError:
            pass

        try:
            c_row = pres.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM preserved_calls
                WHERE chat_id = ? OR chat_jid = ?
                """,
                (chat_id, chat_id),
            ).fetchone()
            if c_row and c_row["cnt"]:
                calls_count = int(c_row["cnt"])
        except sqlite3.OperationalError:
            pass

    # Also count live calls for this chat
    conn_wa = ctx.get_wa()
    if conn_wa is not None and ctx.source_type == "android":
        if conn_wa.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_log'"
        ).fetchone():
            contact_jids, group_jids = _resolve_android_call_targets(
                conn_wa, ctx.get_archive(), chat_id, chat_type, ctx.android_chat_row_ids_cache
            )
            cl_cols = {row[1] for row in conn_wa.execute("PRAGMA table_info(call_log)").fetchall()}
            has_group = "group_jid_row_id" in cl_cols
            if chat_type == "group" and group_jids and has_group:
                ph = ",".join("?" for _ in group_jids)
                c_cnt_row = conn_wa.execute(
                    f"SELECT COUNT(*) FROM call_log WHERE group_jid_row_id IN ({ph})",
                    tuple(group_jids),
                ).fetchone()
            elif chat_type != "group" and contact_jids and "jid_row_id" in cl_cols:
                ph = ",".join("?" for _ in contact_jids)
                group_filter = (
                    "AND (group_jid_row_id IS NULL OR group_jid_row_id = 0)" if has_group else ""
                )
                c_cnt_row = conn_wa.execute(
                    f"SELECT COUNT(*) FROM call_log WHERE jid_row_id IN ({ph}) {group_filter}",
                    tuple(contact_jids),
                ).fetchone()
            else:
                c_cnt_row = None
            if c_cnt_row and c_cnt_row[0]:
                calls_count += int(c_cnt_row[0])
    elif ctx.source_type == "ios" and ctx.wa_db_path is not None:
        call_db_p = _find_ios_call_history_db(ctx.wa_db_path, ctx.output_root)
        if call_db_p is not None and call_db_p.is_file():
            with contextlib.closing(sqlite3.connect(str(call_db_p))) as call_conn:
                schema = _resolve_ios_call_schema(call_conn)
                if schema is not None:
                    cand_list = _resolve_ios_chat_jids(conn_wa, chat_id)
                    if cand_list:
                        ph = ",".join("?" * len(cand_list))
                        where_parts_cnt: list[str] = []
                        params_cnt: list = []
                        if chat_type == "group":
                            if schema["has_group_jid"]:
                                where_parts_cnt.append(f"cl.ZGROUPJIDSTRING IN ({ph})")
                                params_cnt.extend(cand_list)
                        else:
                            if schema["has_group_jid"]:
                                where_parts_cnt.append(
                                    "(cl.ZGROUPJIDSTRING IS NULL OR cl.ZGROUPJIDSTRING = '')"
                                )
                            match_clauses: list[str] = []
                            part_fk = schema["part_fk"]
                            part_jid_col = schema["part_jid_col"]
                            agg_fk = schema["agg_fk_col"]
                            if part_fk and part_jid_col:
                                fk_match = (
                                    f"(p.{part_fk} = cl.{agg_fk} OR p.{part_fk} = cl.Z_PK)"
                                    if agg_fk
                                    else f"p.{part_fk} = cl.Z_PK"
                                )
                                match_clauses.append(
                                    f"EXISTS (SELECT 1 FROM ZWACDCALLEVENTPARTICIPANT p "
                                    f"WHERE {fk_match} AND p.{part_jid_col} IN ({ph}))"
                                )
                                params_cnt.extend(cand_list)
                            if schema["has_creator"]:
                                match_clauses.append(f"cl.ZGROUPCALLCREATORUSERJIDSTRING IN ({ph})")
                                params_cnt.extend(cand_list)
                            if match_clauses:
                                where_parts_cnt.append("(" + " OR ".join(match_clauses) + ")")
                            else:
                                where_parts_cnt.clear()
                        if where_parts_cnt:
                            sql = (
                                "SELECT COUNT(DISTINCT cl.Z_PK) FROM ZWACDCALLEVENT cl "
                                f"WHERE {' AND '.join(where_parts_cnt)}"
                            )
                            ios_cnt = call_conn.execute(sql, params_cnt).fetchone()
                            if ios_cnt and ios_cnt[0]:
                                calls_count += int(ios_cnt[0])

    result = {
        "display_name": display_name,
        "number": number,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "sent": sent,
        "received": received,
        "total": total,
        "calls_count": calls_count,
        "calls": calls_count,
    }
    if chat_type == "group":
        result["description"] = description
        result["members"] = members
        result["top_senders"] = top_senders
        result["created_ts"] = created_ts
        result["creator_number"] = creator_number
        result["creator_name"] = creator_name
    return jsonify(result)


@bp.route("/api/chat-info/media-size")
def api_chat_info_media_size():
    ctx = get_viewer_context()
    chat_id = request.args.get("chat_id", "")
    chat_type = request.args.get("chat_type", "")

    conn = ctx.get_wa()
    prefix = None

    archive_conn = sqlite3.connect(str(ctx.archive_db_path), timeout=5.0)
    archive_conn.row_factory = sqlite3.Row
    try:
        if chat_type == "contact":
            folder_row = archive_conn.execute(
                "SELECT folder FROM contacts WHERE number = ? OR number = ?",
                (chat_id, chat_id.lstrip("+")),
            ).fetchone()
            if folder_row and folder_row["folder"]:
                prefix = f"Contacts/{folder_row['folder']}/"
            elif conn is not None:
                if ctx.source_type == "ios":
                    row = conn.execute(
                        """
                        SELECT COALESCE(con1.folder, con2.folder) AS folder
                        FROM ZWACHATSESSION cs
                        LEFT JOIN _ios_contacts ic ON ic.jid = cs.ZCONTACTJID
                        LEFT JOIN arch.contacts con1 ON con1.number = NULLIF(
                            SUBSTR(COALESCE(cs.ZCONTACTJID,''), 1,
                                   INSTR(COALESCE(cs.ZCONTACTJID,'') || '@', '@') - 1), '')
                        LEFT JOIN arch.contacts con2 ON con2.number = ic.phone_number
                        WHERE cs.Z_PK = CAST(? AS INTEGER)
                        """,
                        (chat_id,),
                    ).fetchone()
                    if row and row["folder"]:
                        prefix = f"Contacts/{row['folder']}/"
                    else:
                        sample = conn.execute(
                            """
                            SELECT ac.archive_path
                            FROM ZWAMESSAGE m
                            JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                            JOIN arch.archive_copies ac ON ac.original_path = 'Message/' || mi.ZMEDIALOCALPATH
                            WHERE m.ZCHATSESSION = CAST(? AS INTEGER) AND ac.archive_path IS NOT NULL
                            LIMIT 1
                            """,
                            (chat_id,),
                        ).fetchone()
                        if sample and sample["archive_path"]:
                            parts = sample["archive_path"].replace("\\", "/").split("/")
                            if len(parts) >= 2:
                                prefix = f"{parts[0]}/{parts[1]}/"
                else:
                    row = conn.execute(
                        f"""
                        SELECT con.folder
                        FROM chat c
                        LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                        LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                        LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                        LEFT JOIN arch.contacts con ON (
                            con.number = {_clean_user_sql("j_chat_real.user")}
                            OR con.number = {_clean_user_sql("j_chat.user")}
                            OR con.number = ?
                            OR con.number = ?
                        )
                        WHERE c.subject IS NULL
                          AND (
                              c._id = CAST(? AS INTEGER)
                              OR COALESCE(j_chat_real.user, j_chat.user) = ?
                              OR j_chat.user = ?
                              OR j_chat_real.user = ?
                              OR {_clean_user_sql("COALESCE(j_chat_real.user, j_chat.user)")} = ?
                          )
                        LIMIT 1
                        """,
                        (
                            chat_id,
                            chat_id.lstrip("+"),
                            chat_id,
                            chat_id,
                            chat_id,
                            chat_id,
                            chat_id.split(":")[0],
                        ),
                    ).fetchone()
                    if row and row["folder"]:
                        prefix = f"Contacts/{row['folder']}/"
                    else:
                        clean_cid = chat_id.split(":")[0]
                        sample = conn.execute(
                            f"""
                            SELECT ac.archive_path
                            FROM message m
                            JOIN message_media mm ON mm.message_row_id = m._id
                            JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
                            WHERE m.chat_row_id IN (
                                SELECT c._id FROM chat c
                                LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
                                LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
                                LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
                                WHERE c.subject IS NULL
                                  AND (
                                      c._id = CAST(? AS INTEGER)
                                      OR ? IN (
                                          j_chat_real.user, j_chat.user,
                                          {_clean_user_sql("j_chat_real.user")},
                                          {_clean_user_sql("j_chat.user")}
                                      )
                                      OR ? IN (
                                          j_chat_real.user, j_chat.user,
                                          {_clean_user_sql("j_chat_real.user")},
                                          {_clean_user_sql("j_chat.user")}
                                      )
                                  )
                            )
                            AND ac.archive_path IS NOT NULL
                            LIMIT 1
                            """,
                            (chat_id, chat_id, clean_cid),
                        ).fetchone()
                        if sample and sample["archive_path"]:
                            parts = sample["archive_path"].replace("\\", "/").split("/")
                            if len(parts) >= 2:
                                prefix = f"{parts[0]}/{parts[1]}/"
        else:
            folder_row = archive_conn.execute(
                "SELECT folder FROM groups WHERE chat_row_id = ?", (chat_id,)
            ).fetchone()
            prefix = (
                f"Groups/{folder_row['folder']}/" if folder_row and folder_row["folder"] else None
            )
            if prefix is None and conn is not None:
                if ctx.source_type == "ios":
                    sample = conn.execute(
                        """
                        SELECT ac.archive_path
                        FROM ZWAMESSAGE m
                        JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                        JOIN arch.archive_copies ac ON ac.original_path = 'Message/' || mi.ZMEDIALOCALPATH
                        WHERE m.ZCHATSESSION = CAST(? AS INTEGER) AND ac.archive_path IS NOT NULL
                        LIMIT 1
                        """,
                        (chat_id,),
                    ).fetchone()
                else:
                    sample = conn.execute(
                        """
                        SELECT ac.archive_path
                        FROM message m
                        JOIN message_media mm ON mm.message_row_id = m._id
                        JOIN arch.archive_copies ac ON ac.original_path = mm.file_path
                        WHERE m.chat_row_id = CAST(? AS INTEGER) AND ac.archive_path IS NOT NULL
                        LIMIT 1
                        """,
                        (chat_id,),
                    ).fetchone()
                if sample and sample["archive_path"]:
                    parts = sample["archive_path"].replace("\\", "/").split("/")
                    if len(parts) >= 2:
                        prefix = f"{parts[0]}/{parts[1]}/"

        if prefix is None:
            return jsonify({"bytes": 0})

        norm_prefix_fwd = prefix.replace("\\", "/")
        upper_fwd = norm_prefix_fwd[:-1] + chr(ord(norm_prefix_fwd[-1]) + 1)
        norm_prefix_back = norm_prefix_fwd.replace("/", "\\")
        upper_back = norm_prefix_back[:-1] + chr(ord(norm_prefix_back[-1]) + 1)
        range_pred = "(ac.archive_path >= ? AND ac.archive_path < ?) OR (ac.archive_path >= ? AND ac.archive_path < ?)"
        range_params = (norm_prefix_fwd, upper_fwd, norm_prefix_back, upper_back)

        has_files_table = (
            archive_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            is not None
        )

        has_size_col = False
        if has_files_table:
            files_cols = {r[1] for r in archive_conn.execute("PRAGMA table_info(files)").fetchall()}
            has_size_col = "size" in files_cols

        paths_missing = []
        total_bytes = 0

        if has_size_col:
            stat_row = archive_conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_count,
                    COUNT(f.size) AS known_count,
                    COALESCE(SUM(f.size), 0) AS known_bytes
                FROM archive_copies ac
                LEFT JOIN files f ON f.original_path = ac.original_path
                WHERE {range_pred}
                """,
                range_params,
            ).fetchone()
            if stat_row:
                total_bytes = stat_row["known_bytes"]
                if stat_row["known_count"] < stat_row["total_count"]:
                    paths_missing = [
                        r["archive_path"]
                        for r in archive_conn.execute(
                            f"""
                            SELECT ac.archive_path
                            FROM archive_copies ac
                            LEFT JOIN files f ON f.original_path = ac.original_path
                            WHERE ({range_pred}) AND f.size IS NULL
                            """,
                            range_params,
                        ).fetchall()
                    ]
        else:
            paths_missing = [
                r["archive_path"]
                for r in archive_conn.execute(
                    f"SELECT archive_path FROM archive_copies ac WHERE {range_pred}",
                    range_params,
                ).fetchall()
            ]
    finally:
        archive_conn.close()

    for p in paths_missing:
        full = ctx.output_root / p
        try:
            total_bytes += full.stat().st_size
        except OSError:
            pass
    return jsonify({"bytes": total_bytes})
