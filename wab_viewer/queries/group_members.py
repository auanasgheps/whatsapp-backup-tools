"""Group participant resolution queries for Android and iOS backups."""

from __future__ import annotations

import sqlite3


def _group_members_android(conn, chat_id: str) -> list:
    # Note on Android WhatsApp: User-chosen friendly push names (wa_name) reside
    # in /data/data/com.whatsapp/databases/wa.db (table wa_contacts). Extracting wa.db
    # requires root privileges on the device, which is not implemented in the Viewer.
    # In standard msgstore.db backups, lid_display_name only contains Meta's privacy-masked
    # phone strings (e.g. +39••••••••04), so unsaved members resolve to their real phone number
    # when mapped via jid_map, falling back to lid_display_name only if completely unmapped.
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    jid_cols = (
        {r["name"] for r in conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "jid" in tables
        else set()
    )
    has_server = "server" in jid_cols
    has_raw_string = "raw_string" in jid_cols
    has_lid_dn = "lid_display_name" in tables
    has_gpu = "group_participant_user" in tables
    j_phone_fallback = (
        "CASE WHEN j.server = 's.whatsapp.net' THEN j.user END" if has_server else "j.user"
    )

    ldn_arm = (
        """
        NULLIF(ldn_direct.display_name, ''),
        NULLIF(ldn_mapped.display_name, ''),
    """
        if has_lid_dn
        else ""
    )

    # 1. Modern WhatsApp: group_participant_user
    if has_gpu:
        try:
            phone_expr_gpu = (
                """
                COALESCE(
                    j_phone.user,
                    CASE WHEN j_user.server = 's.whatsapp.net' THEN j_user.user END
                )
            """
                if has_server
                else "COALESCE(j_phone.user, j_user.user)"
            )
            ldn_gpu_join = (
                """
                LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = gpu.user_jid_row_id
                LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = gpu.user_jid_row_id
                LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
            """
                if has_lid_dn
                else ""
            )

            rows = conn.execute(
                f"""
                SELECT
                    COALESCE({phone_expr_gpu}, '') AS number,
                    CASE WHEN j_user.raw_string = 'lid_me' THEN 'You'
                         ELSE COALESCE(
                             NULLIF(con.display_name, ''),
                             NULLIF(con_lid.display_name, ''),
                             CASE WHEN {phone_expr_gpu} IS NOT NULL
                                  THEN '+' || {phone_expr_gpu}
                             END,
                             {ldn_arm}
                             'Unknown'
                         )
                    END AS name
                FROM group_participant_user gpu
                JOIN chat c ON c._id = CAST(? AS INTEGER)
                JOIN jid j_user ON j_user._id = gpu.user_jid_row_id
                LEFT JOIN _jid_map_resolved jm_phone ON jm_phone.lid_row_id = gpu.user_jid_row_id
                LEFT JOIN jid j_phone ON j_phone._id = jm_phone.jid_row_id
                {ldn_gpu_join}
                LEFT JOIN arch.contacts con ON con.number = {phone_expr_gpu}
                LEFT JOIN arch.contacts con_lid ON con_lid.number = j_user.user
                WHERE gpu.group_jid_row_id = c.jid_row_id
                ORDER BY name
            """,
                (chat_id,),
            ).fetchall()
            if rows:
                return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
                raise

    # 2. Legacy WhatsApp: group_participants
    join_gp_jid = (
        "j_gp.raw_string = gp.jid"
        if has_raw_string
        else "j_gp.user = NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '')"
    )
    gjid_pred = (
        "gp.gjid = (SELECT j.raw_string FROM jid j WHERE j._id = c.jid_row_id)"
        if has_raw_string
        else "1=1"
    )
    try:
        phone_expr_gp = """
            COALESCE(
                j_phone.user,
                CASE WHEN gp.jid NOT LIKE '%@lid'
                     THEN NULLIF(SUBSTR(gp.jid, 1, INSTR(gp.jid || '@', '@') - 1), '')
                END
            )
        """
        ldn_gp_join = (
            """
            LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = j_gp._id
            LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = j_gp._id
            LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
        """
            if has_lid_dn
            else ""
        )

        rows = conn.execute(
            f"""
            SELECT
                COALESCE({phone_expr_gp}, '') AS number,
                COALESCE(
                    NULLIF(con.display_name, ''),
                    NULLIF(con_lid.display_name, ''),
                    CASE WHEN {phone_expr_gp} IS NOT NULL
                         THEN '+' || {phone_expr_gp}
                    END,
                    {ldn_arm}
                    'Unknown'
                ) AS name
            FROM group_participants gp
            JOIN chat c ON c._id = CAST(? AS INTEGER)
            LEFT JOIN jid j_gp ON {join_gp_jid}
            LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = j_gp._id
            LEFT JOIN jid j_phone ON j_phone._id = jm.jid_row_id
            {ldn_gp_join}
            LEFT JOIN arch.contacts con ON con.number = {phone_expr_gp}
            LEFT JOIN arch.contacts con_lid ON con_lid.number = j_gp.user
            WHERE {gjid_pred}
              AND gp.jid != ''
            ORDER BY name
        """,
            (chat_id,),
        ).fetchall()
        if rows:
            return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
            raise

    # 3. Fallback: message history
    ldn_msg_join = (
        """
        LEFT JOIN _lid_map_resolved jm_lid ON jm_lid.jid_row_id = m.sender_jid_row_id
        LEFT JOIN lid_display_name ldn_direct ON ldn_direct.lid_row_id = m.sender_jid_row_id
        LEFT JOIN lid_display_name ldn_mapped ON ldn_mapped.lid_row_id = jm_lid.lid_row_id
    """
        if has_lid_dn
        else ""
    )
    phone_expr_msg = f"COALESCE(j2.user, {j_phone_fallback})"

    rows = conn.execute(
        f"""
        SELECT DISTINCT
            COALESCE({phone_expr_msg}, '') AS number,
            COALESCE(
                NULLIF(con.display_name, ''),
                NULLIF(con_lid.display_name, ''),
                CASE WHEN {phone_expr_msg} IS NOT NULL
                     THEN '+' || {phone_expr_msg}
                END,
                {ldn_arm}
                'Unknown'
            ) AS name
        FROM message m
        JOIN chat c ON c._id = m.chat_row_id
        LEFT JOIN jid j ON j._id = m.sender_jid_row_id
        LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
        LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
        {ldn_msg_join}
        LEFT JOIN arch.contacts con ON con.number = {phone_expr_msg}
        LEFT JOIN arch.contacts con_lid ON con_lid.number = j.user
        WHERE m.chat_row_id = CAST(? AS INTEGER)
          AND m.from_me = 0
          AND m.sender_jid_row_id IS NOT NULL
        ORDER BY name
    """,
        (chat_id,),
    ).fetchall()
    return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]


def _group_members_ios(conn, chat_id: str) -> list:
    phone_expr = """
        COALESCE(
            CASE WHEN gm.ZMEMBERJID LIKE '%@s.whatsapp.net'
                 THEN SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID || '@', '@') - 1)
            END,
            ic.phone_number
        )
    """
    raw_prefix = "SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID || '@', '@') - 1)"
    phone_jid = f"({phone_expr} || '@s.whatsapp.net')"

    query_template = f"""
        SELECT DISTINCT
            COALESCE(
                {phone_expr},
                ''
            ) AS number,
            COALESCE(
                CASE WHEN ic.full_name NOT LIKE '+%' THEN NULLIF(ic.full_name, '') END,
                NULLIF(con.display_name, ''),
                NULLIF(con_lid.display_name, ''),
                NULLIF(pp_lid.ZPUSHNAME, ''),
                NULLIF(pp_phone.ZPUSHNAME, ''),
                NULLIF(cs_lid.ZPARTNERNAME, ''),
                NULLIF(cs_phone.ZPARTNERNAME, ''),
                CASE WHEN {phone_expr} IS NOT NULL
                     THEN '+' || {phone_expr}
                END,
                'Unknown'
            ) AS name
        FROM {{source_table}}
        LEFT JOIN _ios_contacts ic ON ic.jid = gm.ZMEMBERJID
        LEFT JOIN arch.contacts con ON con.number = {phone_expr}
        LEFT JOIN arch.contacts con_lid ON con_lid.number = {raw_prefix}
        LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = gm.ZMEMBERJID
        LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = {phone_jid}
        LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = gm.ZMEMBERJID
        LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = {phone_jid}
        WHERE {{where_clause}}
          AND gm.ZMEMBERJID IS NOT NULL
          AND gm.ZMEMBERJID != ''
        ORDER BY name
    """
    try:
        # 1. Try active members from ZWAGROUPMEMBER
        rows = conn.execute(
            query_template.format(
                source_table="ZWAGROUPMEMBER gm",
                where_clause="gm.ZCHATSESSION = CAST(? AS INTEGER) AND gm.ZISACTIVE = 1",
            ),
            (chat_id,),
        ).fetchall()
        if not rows:
            # 2. Try all members from ZWAGROUPMEMBER
            rows = conn.execute(
                query_template.format(
                    source_table="ZWAGROUPMEMBER gm",
                    where_clause="gm.ZCHATSESSION = CAST(? AS INTEGER)",
                ),
                (chat_id,),
            ).fetchall()
        if rows:
            return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower() and "no such column" not in str(e).lower():
            raise

    # 3. Fallback to message history senders via ZWAGROUPMEMBER
    try:
        rows = conn.execute(
            query_template.format(
                source_table="ZWAMESSAGE m JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER",
                where_clause="m.ZCHATSESSION = CAST(? AS INTEGER)",
            ),
            (chat_id,),
        ).fetchall()
        if rows:
            return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise

    # 4. Fallback to message history senders via ZFROMJID
    phone_expr_m = """
        COALESCE(
            CASE WHEN m.ZFROMJID LIKE '%@s.whatsapp.net'
                 THEN SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1)
            END,
            ic.phone_number
        )
    """
    raw_prefix_m = "SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID || '@', '@') - 1)"
    phone_jid_m = f"({phone_expr_m} || '@s.whatsapp.net')"

    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                COALESCE(
                    {phone_expr_m},
                    ''
                ) AS number,
                COALESCE(
                    CASE WHEN ic.full_name NOT LIKE '+%' THEN NULLIF(ic.full_name, '') END,
                    NULLIF(con.display_name, ''),
                    NULLIF(con_lid.display_name, ''),
                    NULLIF(pp_lid.ZPUSHNAME, ''),
                    NULLIF(pp_phone.ZPUSHNAME, ''),
                    NULLIF(cs_lid.ZPARTNERNAME, ''),
                    NULLIF(cs_phone.ZPARTNERNAME, ''),
                    CASE WHEN {phone_expr_m} IS NOT NULL
                         THEN '+' || {phone_expr_m}
                    END,
                    'Unknown'
                ) AS name
            FROM ZWAMESSAGE m
            LEFT JOIN _ios_contacts ic ON ic.jid = m.ZFROMJID
            LEFT JOIN arch.contacts con ON con.number = {phone_expr_m}
            LEFT JOIN arch.contacts con_lid ON con_lid.number = {raw_prefix_m}
            LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = m.ZFROMJID
            LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = {phone_jid_m}
            LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = m.ZFROMJID
            LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = {phone_jid_m}
            WHERE m.ZCHATSESSION = CAST(? AS INTEGER)
              AND m.ZFROMJID IS NOT NULL
              AND m.ZFROMJID != ''
            ORDER BY name
        """,
            (chat_id,),
        ).fetchall()
        return [{"name": r["name"] or "", "number": r["number"] or ""} for r in rows]
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise
        return []
