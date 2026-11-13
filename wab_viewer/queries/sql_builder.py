"""SQL expression helpers, HD dedup predicates, and chat-filter builders."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import QuerySet, SchemaCapabilities


def _clean_user_sql(col_expr: str) -> str:
    """Return SQL expression stripping multi-device ':device' and '.agent' suffixes from a JID user string."""
    return (
        f"CASE "
        f"WHEN INSTR({col_expr}, ':') > 0 AND INSTR(SUBSTR({col_expr}, 1, INSTR({col_expr}, ':') - 1), '.') > 0 "
        f"THEN SUBSTR({col_expr}, 1, INSTR(SUBSTR({col_expr}, 1, INSTR({col_expr}, ':') - 1), '.') - 1) "
        f"WHEN INSTR({col_expr}, ':') > 0 "
        f"THEN SUBSTR({col_expr}, 1, INSTR({col_expr}, ':') - 1) "
        f"WHEN INSTR({col_expr}, '.') > 0 "
        f"THEN SUBSTR({col_expr}, 1, INSTR({col_expr}, '.') - 1) "
        f"ELSE {col_expr} END"
    )


_ANDROID_HD_DEDUP_CLAUSE = """
    AND NOT EXISTS (
        SELECT 1 FROM message_association ma
        JOIN message_media mm_hd ON mm_hd.message_row_id = ma.child_message_row_id
        WHERE ma.parent_message_row_id = m._id
          AND ma.association_type IN (12, 7)
          AND mm_hd.file_path IS NOT NULL
    )
"""


def _check_android_hd_association(wa_conn: sqlite3.Connection) -> bool:
    try:
        has_table = wa_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_association'"
        ).fetchone()
        if not has_table:
            return False
        has_hd = wa_conn.execute(
            "SELECT 1 FROM message_association WHERE association_type IN (12, 7) LIMIT 1"
        ).fetchone()
        return bool(has_hd)
    except sqlite3.OperationalError:
        return False


_IOS_HD_DEDUP_CLAUSE = """
    AND NOT EXISTS (
        SELECT 1 FROM ext.message_parent_association mpa
        JOIN ZWAMESSAGE m_hd ON m_hd.ZSTANZAID = mpa.stanza_id
        JOIN ZWAMEDIAITEM mi_hd ON mi_hd.Z_PK = m_hd.ZMEDIAITEM
        WHERE mpa.parent_stanza_id = m.ZSTANZAID
          AND mpa.type IN (10, 5)
          AND mi_hd.ZMEDIALOCALPATH IS NOT NULL
    )
"""


def _check_ios_hd_association(wa_db_path: str, ext_db_path: Path | None) -> bool:
    if ext_db_path is None or not ext_db_path.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(wa_db_path)) as conn:
            conn.execute("ATTACH DATABASE ? AS ext", (str(ext_db_path),))
            has_table = conn.execute(
                "SELECT 1 FROM ext.sqlite_master WHERE type='table' AND name='message_parent_association'"
            ).fetchone()
            if not has_table:
                return False
            has_hd = conn.execute(
                "SELECT 1 FROM ext.message_parent_association WHERE type IN (10, 5) LIMIT 1"
            ).fetchone()
            return bool(has_hd)
    except sqlite3.OperationalError:
        return False


def _android_chat_filter(chat_id: str, chat_type: str) -> tuple[str, list]:
    if chat_type == "group":
        return "m.chat_row_id = CAST(? AS INTEGER)", [chat_id]
    clean_id = chat_id.split(":")[0] if ":" in chat_id else chat_id
    pred = (
        "m.chat_row_id IN ("
        "SELECT c._id FROM chat c "
        "LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id "
        "LEFT JOIN jid j_real ON j_real._id = jm_c.jid_row_id "
        "LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id "
        "WHERE ? IN ("
        f"j_real.user, j_chat.user, {_clean_user_sql('j_real.user')}, {_clean_user_sql('j_chat.user')}, CAST(c._id AS TEXT)"
        "))"
    )
    return pred, [clean_id]


def _resolve_android_chat_row_ids(
    conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    cache: dict[tuple[str, str], list[int]],
) -> list[int]:
    """Resolve a chat ID and chat type to physical chat row IDs using a per-context cache."""
    cache_key = (chat_id, chat_type)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if chat_type == "group":
        try:
            row_ids = [int(chat_id)]
            cache[cache_key] = row_ids
            return row_ids
        except ValueError:
            return []

    clean_id = chat_id.split(":")[0] if ":" in chat_id else chat_id
    try:
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)"
        )
        rows = conn.execute(
            """
            SELECT c._id FROM chat c
            LEFT JOIN _jid_map_resolved jm_c ON jm_c.lid_row_id = c.jid_row_id
            LEFT JOIN jid j_real ON j_real._id = jm_c.jid_row_id
            LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
            WHERE ? IN (
                j_real.user, j_chat.user,
                """
            + _clean_user_sql("j_real.user")
            + ", "
            + _clean_user_sql("j_chat.user")
            + """,
                CAST(c._id AS TEXT)
            )
        """,
            (clean_id,),
        ).fetchall()
        row_ids = [r[0] for r in rows if r[0] is not None]
    except sqlite3.OperationalError:
        row_ids = []

    if row_ids:
        cache[cache_key] = row_ids
    return row_ids


def _resolve_android_chat_filter(
    conn: sqlite3.Connection,
    chat_id: str,
    chat_type: str,
    cache: dict[tuple[str, str], list[int]],
) -> tuple[str, list]:
    """Return physical column predicate and parameters for an Android chat."""
    row_ids = _resolve_android_chat_row_ids(conn, chat_id, chat_type, cache)
    if len(row_ids) == 1:
        return "m.chat_row_id = ?", [row_ids[0]]
    if row_ids:
        placeholders = ",".join("?" * len(row_ids))
        return f"m.chat_row_id IN ({placeholders})", list(row_ids)
    return _android_chat_filter(chat_id, chat_type)


def _ios_chat_filter(chat_id: str) -> tuple[str, list]:
    return "m.ZCHATSESSION = CAST(? AS INTEGER)", [chat_id]


_MEDIA_TYPE_EXPR = """
    CASE
        WHEN {col} IS NULL THEN 'text'
        WHEN LOWER({col}) LIKE '%.vcf' THEN 'vcard'
        WHEN LOWER({col}) LIKE '%sticker%' THEN 'sticker'
        WHEN LOWER({col}) LIKE '%.gif'
          OR LOWER({col}) LIKE '%/animated gifs/%'
          OR LOWER({col}) LIKE '%whatsapp animated gifs%'
          OR LOWER({col}) LIKE '%/gif-%'
          OR LOWER({col}) LIKE 'gif-%' THEN 'gif'
        WHEN LOWER({col}) LIKE '%.jpg'  OR LOWER({col}) LIKE '%.jpeg'
          OR LOWER({col}) LIKE '%.png'  OR LOWER({col}) LIKE '%.webp'
          OR LOWER({col}) LIKE '%.heic' OR LOWER({col}) LIKE '%.heif' THEN 'image'
        WHEN LOWER({col}) LIKE '%.mp4' OR LOWER({col}) LIKE '%.mov'
          OR LOWER({col}) LIKE '%.avi' OR LOWER({col}) LIKE '%.mkv'
          OR LOWER({col}) LIKE '%.3gp' THEN 'video'
        WHEN LOWER({col}) LIKE '%.mp3'  OR LOWER({col}) LIKE '%.ogg'
          OR LOWER({col}) LIKE '%.aac'  OR LOWER({col}) LIKE '%.opus'
          OR LOWER({col}) LIKE '%.m4a' THEN 'audio'
        ELSE 'document'
    END
"""


_ANDROID_JID_MAP = """
    LEFT JOIN _jid_map_resolved jm ON jm.lid_row_id = m.sender_jid_row_id
    LEFT JOIN jid j2 ON j2._id = jm.jid_row_id
"""


_SUPPORTED_ANDROID_SYSTEM_ACTIONS = (
    1,
    4,
    5,
    6,
    7,
    11,
    12,
    13,
    14,
    15,
    18,
    19,
    20,
    27,
    28,
    56,
    58,
    61,
    67,
    68,
    69,
    70,
    79,
    123,
    124,
    127,
    129,
    136,
    144,
    146,
)


_IOS_CHAT_ID = "CAST(m.ZCHATSESSION AS TEXT)"
_IOS_CHAT_TYPE = "CASE WHEN cs.ZGROUPINFO IS NOT NULL THEN 'group' ELSE 'contact' END"

_IOS_SENDER_RAW = """COALESCE(
    gm.ZMEMBERJID,
    CASE WHEN m.ZFROMJID NOT LIKE '%@g.us' THEN m.ZFROMJID END,
    ''
)"""

_IOS_SENDER_JID = f"""NULLIF(SUBSTR({_IOS_SENDER_RAW}, 1,
                    INSTR({_IOS_SENDER_RAW} || '@', '@') - 1), '')"""


_ANDROID_TS = "m.timestamp"
_IOS_TS = "CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)"


_ANDROID_IS_MEDIA = "NOT (m.message_type IS NULL OR m.message_type IN (0, 7, 15, 64, 66))"
_IOS_IS_MEDIA = "NOT (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE IN (0, 6, 7, 10, 14, 46))"


_ANDROID_IS_LINK = (
    "(m.message_type IS NULL OR m.message_type = 0)"
    " AND m.text_data IS NOT NULL"
    " AND (INSTR(LOWER(m.text_data), 'http://') > 0"
    "      OR INSTR(LOWER(m.text_data), 'https://') > 0)"
)


_ANDROID_IS_DOCUMENT_UNDOWNLOADED = (
    "NOT (m.message_type IS NULL OR m.message_type = 0)"
    " AND mm.file_path IS NULL"
    " AND mm.media_name IS NOT NULL AND mm.media_name != ''"
)
_IOS_IS_DOCUMENT_UNDOWNLOADED = (
    "m.ZMESSAGETYPE = 8"
    " AND mi.ZMEDIALOCALPATH IS NULL"
    " AND mi.ZTITLE IS NOT NULL AND mi.ZTITLE != ''"
)


_BASE_IOS_FILTER = """
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND m.ZMESSAGETYPE NOT IN (6, 10) AND (mi.ZMEDIALOCALPATH IS NOT NULL OR m.ZMESSAGETYPE IN (1, 2, 3, 5, 8, 11, 15, 16, 23, 38, 39, 54) OR mi.ZTITLE IS NOT NULL))
        OR (m.ZMESSAGETYPE = 4)
        OR (m.ZMESSAGETYPE = 14)
        OR (m.ZMESSAGETYPE IN (38, 39, 53))
        OR (m.ZMESSAGETYPE = 46)
        OR (m.ZMESSAGETYPE = 7)
        OR (m.ZMESSAGETYPE IN (6, 10))
    )
"""

_DEFAULT_ANDROID_FILTER = """
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND m.message_type != 7 AND (mm.file_path IS NOT NULL OR mm.message_row_id IS NOT NULL OR m.message_type IN (1, 2, 3, 5, 9, 13, 16, 20, 28)))
        OR (m.message_type IN (4, 14))
        OR (m.message_type IN (15, 64))
        OR (m.message_type IN (42, 43, 82))
        OR (m.message_type = 7)
        OR (m.message_type = 66)
    )
"""

quoted_stanza_id_ios = "NULL"
ios_starred_expr = "COALESCE(m.ZSTARRED, 0)"
ios_pinned_expr = "0"

_DEFAULT_IOS_SELECT = f"""
    SELECT
        m.Z_PK                                                       AS msg_id,
        m.Z_PK                                                       AS sort_id,
        {_IOS_CHAT_ID}                                               AS chat_id,
        {_IOS_CHAT_TYPE}                                             AS chat_type,
        CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)        AS timestamp_ms,
        COALESCE(
            CASE WHEN ({_IOS_SENDER_JID}) = '0' THEN 'WhatsApp' END,
            CASE WHEN ic_s.full_name NOT LIKE '+%' THEN NULLIF(ic_s.full_name, '') END,
            NULLIF(con_s.display_name, ''),
            NULLIF(con_lid_s.display_name, ''),
            NULLIF(pp_lid.ZPUSHNAME, ''),
            NULLIF(pp_phone.ZPUSHNAME, ''),
            NULLIF(cs_lid.ZPARTNERNAME, ''),
            NULLIF(cs_phone.ZPARTNERNAME, ''),
            CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN NULLIF(m.ZPUSHNAME, '') END,
            CASE WHEN COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            ) IS NOT NULL
            THEN '+' || COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            )
            END,
            ''
        )                                                            AS sender,
        m.ZISFROMME                                                  AS from_me,
        ac.archive_path,
        CASE
            WHEN m.ZMESSAGETYPE = 14 THEN 'deleted'
            WHEN m.ZMESSAGETYPE IN (6, 10) THEN 'service'
            WHEN m.ZMESSAGETYPE = 4 THEN 'vcard'
            WHEN m.ZMESSAGETYPE = 16 OR (m.ZMESSAGETYPE = 5 AND ((mi.ZMOVIEDURATION IS NOT NULL AND mi.ZMOVIEDURATION > 0) OR (mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0))) THEN 'live_location'
            WHEN m.ZMESSAGETYPE = 5 THEN 'location'
            WHEN m.ZMESSAGETYPE = 15 THEN 'sticker'
            WHEN m.ZMESSAGETYPE IN (1, 38) THEN 'image'
            WHEN m.ZMESSAGETYPE IN (2, 39, 54) THEN 'video'
            WHEN m.ZMESSAGETYPE IN (3, 53) THEN 'audio'
            WHEN m.ZMESSAGETYPE = 8 THEN 'document'
            WHEN m.ZMESSAGETYPE = 11 THEN 'gif'
            WHEN m.ZMESSAGETYPE = 46 THEN 'poll'
            WHEN m.ZMESSAGETYPE = 7 THEN 'link'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN m.ZTEXT IS NOT NULL
                      AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0
                           OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="('Message/' || COALESCE(mi.ZMEDIALOCALPATH,''))")}
        END                                                           AS media_type,
        COALESCE(
            CASE WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(m.ZTEXT, '') END,
            CASE WHEN m.ZMESSAGETYPE = 11 THEN '' END,
            mi.ZMEDIALOCALPATH,
            ''
        )                                                             AS media_name,
        COALESCE(
            CASE
                WHEN m.ZISFROMME = 1 AND m.ZMESSAGETYPE = 14 THEN 'You deleted this message'
                WHEN m.ZMESSAGETYPE = 14 THEN 'This message was deleted'
                WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(mi.ZTITLE, '')
                WHEN m.ZMESSAGETYPE IN (1, 2, 3, 11, 38, 39, 54) THEN COALESCE(NULLIF(m.ZTEXT, ''), NULLIF(mi.ZTITLE, ''))
                ELSE m.ZTEXT
            END,
            ''
        )                                                             AS text_body,
        COALESCE(
            qm.ZTEXT,
            CASE
                WHEN qm.ZMESSAGETYPE = 46 THEN 'Poll'
                WHEN qm.ZMESSAGETYPE IN (1, 38) THEN '📷 Photo'
                WHEN qm.ZMESSAGETYPE IN (2, 39, 54) THEN '🎥 Video'
                WHEN qm.ZMESSAGETYPE IN (3, 53) THEN '🎵 Audio'
                WHEN qm.ZMESSAGETYPE = 11 THEN '🎞️ GIF'
                WHEN qm.ZMESSAGETYPE = 5 THEN '📍 Location'
                WHEN qm.ZMESSAGETYPE = 16 THEN '📍 Live location'
                WHEN qm.ZMESSAGETYPE = 15 THEN 'Sticker'
            END,
            ''
        )                                                             AS quoted_text,
        CASE WHEN qm.ZISFROMME = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 NULLIF(qm.ZPUSHNAME, ''),
                 CASE WHEN SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                   INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1) != ''
                      THEN '+' || SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                         INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
                 END,
                 '')
        END                                                          AS quoted_sender,
        COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0) AS quoted_ts,
        qm.Z_PK                                                      AS quoted_msg_id,
        {quoted_stanza_id_ios}                                       AS quoted_stanza_id,
        NULL                                                          AS reactions,
        0                                                            AS is_edited,
        NULL                                                          AS edited_ts,
        CASE WHEN m.ZMESSAGETYPE IN (38, 39, 53) THEN 1 ELSE 0 END   AS is_view_once,
        CASE WHEN m.ZMESSAGETYPE = 54 THEN 1 ELSE 0 END               AS is_video_note,
        {ios_starred_expr}                                           AS is_starred,
        {ios_pinned_expr}                                            AS is_pinned,
        m.ZMESSAGETYPE                                               AS raw_msg_type,
        0                                                            AS group_event_type,
        gm.ZMEMBERJID                                                AS member_jid,
        0                                                            AS media_width,
        0                                                            AS media_height,
        0                                                            AS media_size,
        0                                                            AS media_duration,
        0.0                                                          AS aspect_ratio,
        0                                                            AS has_thumbnail,
        CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZTITLE, '') END  AS link_title,
        NULL                                                         AS link_desc,
        CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZMEDIAURL, '') END AS link_url,
        0                                                            AS link_preview_type,
        mi.ZLATITUDE                                                 AS latitude,
        mi.ZLONGITUDE                                                AS longitude,
        COALESCE(
            CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDNAME IS NOT NULL THEN
                CASE WHEN INSTR(mi.ZVCARDNAME, CHAR(10)) > 0
                     THEN SUBSTR(mi.ZVCARDNAME, 1, INSTR(mi.ZVCARDNAME, CHAR(10)) - 1)
                     ELSE mi.ZVCARDNAME END
            END,
            CASE WHEN m.ZMESSAGETYPE IN (5, 16) THEN NULLIF(mi.ZTITLE, '') END
        )                                                            AS location_name,
        COALESCE(
            CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDNAME IS NOT NULL AND INSTR(mi.ZVCARDNAME, CHAR(10)) > 0
                 THEN NULLIF(SUBSTR(mi.ZVCARDNAME, INSTR(mi.ZVCARDNAME, CHAR(10)) + 1), '')
            END,
            CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZTITLE IS NOT NULL AND m.ZTEXT != mi.ZTITLE THEN NULLIF(m.ZTEXT, '') END
        )                                                            AS location_address,
        CASE WHEN m.ZMESSAGETYPE IN (5, 16) THEN NULLIF(mi.ZMEDIAURL, '') END AS location_url,
        CASE
            WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZMOVIEDURATION IS NOT NULL AND mi.ZMOVIEDURATION > 0
                THEN mi.ZMOVIEDURATION
            WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0
                THEN CAST(ROUND(CAST(SUBSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), INSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), ';') + 1) AS REAL)) AS INTEGER)
        END                                                          AS live_location_share_duration,
        NULL                                                         AS _raw_metadata
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
    LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = ({_IOS_SENDER_RAW})
    LEFT JOIN _ios_contacts ic_s ON ic_s.jid = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE
    LEFT JOIN arch.contacts con_s
          ON con_s.number = COALESCE(
              CASE WHEN ({_IOS_SENDER_RAW}) LIKE '%@lid' THEN ic_s.phone_number END,
              ({_IOS_SENDER_JID})
          )
    LEFT JOIN arch.contacts con_lid_s
          ON con_lid_s.number = SUBSTR(({_IOS_SENDER_RAW}), 1,
                                       INSTR(({_IOS_SENDER_RAW}) || '@', '@') - 1)
    LEFT JOIN arch.contacts con_sq
          ON con_sq.number = SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                    INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
    LEFT JOIN arch.archive_copies ac
          ON ac.original_path = COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)
"""

_DEFAULT_IOS_IS_LINK = (
    "(m.ZMESSAGETYPE = 7 OR ((m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"
    " AND m.ZTEXT IS NOT NULL"
    " AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0"
    "      OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)))"
)


_ANDROID_SELECT = None
_ANDROID_STAT_SELECT = None
_ANDROID_FILTER = _DEFAULT_ANDROID_FILTER
_ANDROID_FILTER_TS = _ANDROID_FILTER
_BASE_IOS_FILTER_TS = _BASE_IOS_FILTER
_IOS_FILTER = _BASE_IOS_FILTER
_IOS_FILTER_TS = _BASE_IOS_FILTER_TS
_IOS_IS_LINK = _DEFAULT_IOS_IS_LINK
_IOS_SELECT = _DEFAULT_IOS_SELECT


def detect_schema_capabilities(
    wa_db_path: str | None,
    source_type: str | None,
    output_root: Path,
) -> SchemaCapabilities:
    """Inspect source database tables and columns to build immutable SchemaCapabilities."""
    from ..context import SchemaCapabilities
    from ..db_cache import _find_ios_db

    has_android_hd = False
    has_ios_hd = False
    has_ios_ext_pinned = False
    ext_pin_key_col = "key_id"
    has_ext_thread_id_deleted = False
    if source_type == "android" and wa_db_path is not None:
        try:
            tmp_chk = sqlite3.connect(wa_db_path)
            has_android_hd = _check_android_hd_association(tmp_chk)
            tmp_chk.close()
        except sqlite3.OperationalError:
            has_android_hd = False
    if source_type == "ios" and wa_db_path is not None:
        ext_db_init = _find_ios_db(wa_db_path, "ExtChatDatabase.sqlite", output_root)
        if not ext_db_init:
            ext_db_init = _find_ios_db(wa_db_path, "ExtChatDB/ExtChatDatabase.sqlite", output_root)
        has_ios_hd = _check_ios_hd_association(wa_db_path, ext_db_init)
        if ext_db_init and ext_db_init.is_file():
            try:
                with sqlite3.connect(
                    f"file:{ext_db_init.resolve().as_posix()}?mode=ro", uri=True
                ) as _ec:
                    has_tm = _ec.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_messages'"
                    ).fetchone()
                    if has_tm:
                        has_ios_ext_pinned = True
                        tm_cols = {
                            r[1]
                            for r in _ec.execute("PRAGMA table_info(thread_messages)").fetchall()
                        }
                        ext_pin_key_col = (
                            "key_id"
                            if "key_id" in tm_cols
                            else ("stanza_id" if "stanza_id" in tm_cols else "key_id")
                        )
                        has_ti = _ec.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_id'"
                        ).fetchone()
                        if has_ti:
                            ti_cols = {
                                r[1] for r in _ec.execute("PRAGMA table_info(thread_id)").fetchall()
                            }
                            has_ext_thread_id_deleted = (
                                "deleted" in ti_cols and "thread_key_id" in tm_cols
                            )
            except Exception:
                has_ios_ext_pinned = False
                has_ext_thread_id_deleted = False

    has_reactions = False
    has_ios_reactions = False
    has_lid_dn = False
    has_message_system = False
    has_mcp = False
    has_ios_group_event = False
    has_jid_server = False
    has_jid_raw_string = False
    has_android_edit_info = False
    has_android_add_on = False
    has_mei_ts = False
    has_mao_ts = False
    has_android_key_id = False
    has_ios_stanza_id = False
    has_message_thumbnail = False
    has_media_hash_thumbnail = False
    has_message_quoted_media = False
    has_message_quoted_text = False
    has_message_quoted_location = False
    has_mm_width = False
    has_mm_height = False
    has_mm_size = False
    has_mm_duration = False
    has_mm_file_hash = False
    has_mm_gif_attribution = False
    has_ios_aspect_ratio = False
    has_ios_filesize = False
    has_ios_duration = False
    has_mq_key_id = False
    has_mq_msg_type = False
    has_mq_pm_chat = False
    has_message_view_once_media = False
    has_message_revoked = False
    has_message_location = False
    has_ml_lat = False
    has_ml_lng = False
    has_ml_name = False
    has_ml_address = False
    has_ml_url = False
    has_ml_duration = False
    has_ios_lat = False
    has_ios_lng = False
    _mi_cols = set()
    _mdi_cols = set()
    has_ios_mdi = False
    has_ios_mdi_thumb = False
    has_ios_mdi_matched = False
    has_ios_mdi_title = False
    has_ios_mdi_summary = False
    has_pin_in_chat_table = False
    has_chat_archived = False
    has_cs_archived = False
    has_cs_ident = False
    has_cs_last_msg_date = False
    if wa_db_path is not None:
        with contextlib.closing(sqlite3.connect(str(wa_db_path))) as _tmp_wa:
            _wa_tables = {
                r[0]
                for r in _tmp_wa.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            has_message_view_once_media = "message_view_once_media" in _wa_tables
            has_message_revoked = "message_revoked" in _wa_tables
            has_reactions = (
                "message_add_on" in _wa_tables and "message_add_on_reaction" in _wa_tables
            )
            has_message_poll = "message_poll" in _wa_tables
            has_pin_in_chat_table = "message_add_on_pin_in_chat" in _wa_tables
            _mao_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_add_on)").fetchall()}
                if "message_add_on" in _wa_tables
                else set()
            )
            has_message_add_on = (
                "message_add_on" in _wa_tables and "message_add_on_type" in _mao_cols
            )
            has_ios_reactions = "ZWAMESSAGEINFO" in _wa_tables
            has_lid_dn = "lid_display_name" in _wa_tables
            has_message_system = "message_system" in _wa_tables
            has_mcp = "message_system_chat_participant" in _wa_tables
            has_message_thumbnail = "message_thumbnail" in _wa_tables
            has_media_hash_thumbnail = "media_hash_thumbnail" in _wa_tables
            has_message_quoted_media = "message_quoted_media" in _wa_tables
            has_message_quoted_text = "message_quoted_text" in _wa_tables
            has_message_quoted_location = "message_quoted_location" in _wa_tables
            _mq_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_quoted)").fetchall()}
                if "message_quoted" in _wa_tables
                else set()
            )
            has_mq_key_id = "key_id" in _mq_cols
            has_mq_msg_type = "message_type" in _mq_cols
            has_mq_pm_chat = "parent_message_chat_row_id" in _mq_cols
            _mm_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_media)").fetchall()}
                if "message_media" in _wa_tables
                else set()
            )
            has_mm_width = "width" in _mm_cols
            has_mm_height = "height" in _mm_cols
            has_mm_size = "file_size" in _mm_cols
            has_mm_duration = "media_duration" in _mm_cols
            has_mm_file_hash = "file_hash" in _mm_cols
            has_mm_gif_attribution = "gif_attribution" in _mm_cols
            _msg_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message)").fetchall()}
                if "message" in _wa_tables
                else set()
            )
            has_android_key_id = "key_id" in _msg_cols
            _mei_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_edit_info)").fetchall()}
                if "message_edit_info" in _wa_tables
                else set()
            )
            has_android_edit_info = (
                "message_edit_info" in _wa_tables and "message_row_id" in _mei_cols
            )
            has_mei_ts = "edited_timestamp" in _mei_cols

            _mao_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_add_on)").fetchall()}
                if "message_add_on" in _wa_tables
                else set()
            )
            has_android_add_on = (
                "message_add_on" in _wa_tables
                and "message_add_on_type" in _mao_cols
                and "parent_message_row_id" in _mao_cols
            )
            has_mao_ts = "timestamp" in _mao_cols
            has_message_media_map = (
                "message_media_map" in _wa_tables
                and "extended_media_data" in _wa_tables
                and bool(_tmp_wa.execute("SELECT 1 FROM message_media_map LIMIT 1").fetchone())
            )
            _jid_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(jid)").fetchall()}
                if "jid" in _wa_tables
                else set()
            )
            has_jid_server = "server" in _jid_cols
            has_jid_raw_string = "raw_string" in _jid_cols
            _mi_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
                if "ZWAMEDIAITEM" in _wa_tables
                else set()
            )
            has_ios_vcard_name = "ZVCARDNAME" in _mi_cols
            has_ios_vcard_string = "ZVCARDSTRING" in _mi_cols
            has_ios_aspect_ratio = "ZASPECTRATIO" in _mi_cols
            has_ios_filesize = "ZFILESIZE" in _mi_cols
            has_ios_duration = "ZMOVIEDURATION" in _mi_cols
            has_ios_title = "ZTITLE" in _mi_cols
            has_ios_mediaurl = "ZMEDIAURL" in _mi_cols
            has_ios_metadata = "ZMETADATA" in _mi_cols
            has_message_text = "message_text" in _wa_tables
            _mt_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_text)").fetchall()}
                if has_message_text
                else set()
            )
            has_mt_title = "page_title" in _mt_cols
            has_mt_desc = "description" in _mt_cols
            has_mt_url = "url" in _mt_cols
            has_mt_preview_type = "preview_type" in _mt_cols
            has_message_location = "message_location" in _wa_tables
            _ml_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(message_location)").fetchall()}
                if has_message_location
                else set()
            )
            has_ml_lat = "latitude" in _ml_cols
            has_ml_lng = "longitude" in _ml_cols
            has_ml_name = "place_name" in _ml_cols
            has_ml_address = "place_address" in _ml_cols
            has_ml_url = "url" in _ml_cols
            has_ml_duration = "live_location_share_duration" in _ml_cols
            has_ios_lat = "ZLATITUDE" in _mi_cols
            has_ios_lng = "ZLONGITUDE" in _mi_cols
            _mdi_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWAMESSAGEDATAITEM)").fetchall()}
                if "ZWAMESSAGEDATAITEM" in _wa_tables
                else set()
            )
            has_ios_mdi = bool(_mdi_cols)
            has_ios_mdi_thumb = "ZTHUMBNAILPATH" in _mdi_cols
            has_ios_mdi_matched = "ZMATCHEDTEXT" in _mdi_cols
            has_ios_mdi_title = "ZTITLE" in _mdi_cols
            has_ios_mdi_summary = "ZSUMMARY" in _mdi_cols
            _zwa_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()}
                if "ZWAMESSAGE" in _wa_tables
                else set()
            )
            has_ios_group_event = "ZGROUPEVENTTYPE" in _zwa_cols
            has_ios_stanza_id = "ZSTANZAID" in _zwa_cols
            _chat_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(chat)").fetchall()}
                if "chat" in _wa_tables
                else set()
            )
            has_chat_archived = "archived" in _chat_cols
            _cs_cols = (
                {r[1] for r in _tmp_wa.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
                if "ZWACHATSESSION" in _wa_tables
                else set()
            )
            has_cs_archived = "ZARCHIVED" in _cs_cols or "ZISARCHIVED" in _cs_cols
            cs_archived_col_name = (
                "ZARCHIVED"
                if "ZARCHIVED" in _cs_cols
                else ("ZISARCHIVED" if "ZISARCHIVED" in _cs_cols else "ZARCHIVED")
            )
            has_cs_ident = "ZCONTACTIDENTIFIER" in _cs_cols
            has_cs_last_msg_date = "ZLASTMESSAGEDATE" in _cs_cols

    has_ios_sort = "ZSORT" in _zwa_cols if wa_db_path is not None else False
    has_ios_starred = "ZSTARRED" in _zwa_cols if wa_db_path is not None else False
    has_msg_starred = "starred" in _msg_cols if wa_db_path is not None else False
    has_wa_zwamessage_parent = (
        ("ZWAMESSAGE" in _wa_tables and "ZPARENTMESSAGE" in _zwa_cols)
        if wa_db_path is not None
        else False
    )
    return SchemaCapabilities(
        has_android_hd=has_android_hd,
        has_ios_hd=has_ios_hd,
        has_ios_ext_pinned=has_ios_ext_pinned,
        ext_pin_key_col=ext_pin_key_col,
        has_ext_thread_id_deleted=has_ext_thread_id_deleted,
        has_reactions=has_reactions,
        has_ios_reactions=has_ios_reactions,
        has_message_poll=has_message_poll if wa_db_path is not None else False,
        has_message_add_on=has_message_add_on,
        has_pin_in_chat_table=has_pin_in_chat_table,
        has_lid_dn=has_lid_dn,
        has_message_system=has_message_system,
        has_mcp=has_mcp,
        has_ios_group_event=has_ios_group_event,
        has_jid_server=has_jid_server,
        has_jid_raw_string=has_jid_raw_string,
        has_android_edit_info=has_android_edit_info,
        has_android_add_on=has_android_add_on,
        has_mei_ts=has_mei_ts,
        has_mao_ts=has_mao_ts,
        has_android_key_id=has_android_key_id,
        has_ios_stanza_id=has_ios_stanza_id,
        has_ios_sort=has_ios_sort,
        has_ios_starred=has_ios_starred,
        has_msg_starred=has_msg_starred,
        has_wa_zwamessage_parent=has_wa_zwamessage_parent,
        has_message_thumbnail=has_message_thumbnail,
        has_media_hash_thumbnail=has_media_hash_thumbnail,
        has_message_quoted_media=has_message_quoted_media,
        has_message_quoted_text=has_message_quoted_text,
        has_message_quoted_location=has_message_quoted_location,
        has_mm_width=has_mm_width,
        has_mm_height=has_mm_height,
        has_mm_size=has_mm_size,
        has_mm_duration=has_mm_duration,
        has_mm_file_hash=has_mm_file_hash,
        has_mm_gif_attribution=has_mm_gif_attribution,
        has_message_media_map=has_message_media_map if wa_db_path is not None else False,
        has_ios_vcard_name=has_ios_vcard_name if wa_db_path is not None else False,
        has_ios_vcard_string=has_ios_vcard_string if wa_db_path is not None else False,
        has_ios_aspect_ratio=has_ios_aspect_ratio,
        has_ios_filesize=has_ios_filesize,
        has_ios_duration=has_ios_duration,
        has_ios_title=has_ios_title if wa_db_path is not None else False,
        has_ios_mediaurl=has_ios_mediaurl if wa_db_path is not None else False,
        has_ios_metadata=has_ios_metadata if wa_db_path is not None else False,
        has_ios_lat=has_ios_lat,
        has_ios_lng=has_ios_lng,
        has_mq_key_id=has_mq_key_id,
        has_mq_msg_type=has_mq_msg_type,
        has_mq_pm_chat=has_mq_pm_chat,
        has_message_view_once_media=has_message_view_once_media,
        has_message_revoked=has_message_revoked,
        has_message_location=has_message_location,
        has_ml_lat=has_ml_lat,
        has_ml_lng=has_ml_lng,
        has_ml_name=has_ml_name,
        has_ml_address=has_ml_address,
        has_ml_url=has_ml_url,
        has_ml_duration=has_ml_duration,
        has_message_text=has_message_text if wa_db_path is not None else False,
        has_mt_title=has_mt_title if wa_db_path is not None else False,
        has_mt_desc=has_mt_desc if wa_db_path is not None else False,
        has_mt_url=has_mt_url if wa_db_path is not None else False,
        has_mt_preview_type=has_mt_preview_type if wa_db_path is not None else False,
        has_chat_archived=has_chat_archived,
        has_cs_archived=has_cs_archived,
        cs_archived_col_name=cs_archived_col_name,
        has_cs_ident=has_cs_ident,
        has_cs_last_msg_date=has_cs_last_msg_date,
        mi_cols=frozenset(_mi_cols),
        mdi_cols=frozenset(_mdi_cols),
        has_ios_mdi=has_ios_mdi,
        has_ios_mdi_thumb=has_ios_mdi_thumb,
        has_ios_mdi_matched=has_ios_mdi_matched,
        has_ios_mdi_title=has_ios_mdi_title,
        has_ios_mdi_summary=has_ios_mdi_summary,
    )


def build_viewer_queries(caps: SchemaCapabilities, source_type: str | None) -> QuerySet:
    """Construct per-app SQL query templates from detected capabilities without mutating globals."""
    from ..context import QuerySet

    ios_select = _DEFAULT_IOS_SELECT
    ios_filter = _BASE_IOS_FILTER
    ios_filter_ts = _BASE_IOS_FILTER
    ios_is_link = _DEFAULT_IOS_IS_LINK
    if source_type == "ios":
        vcard_name_ios = "mi.ZVCARDNAME" if caps.has_ios_vcard_name else "NULL"
        group_ev_col = "m.ZGROUPEVENTTYPE" if caps.has_ios_group_event else "0"
        sort_col_ios = "COALESCE(m.ZSORT, m.Z_PK)" if caps.has_ios_sort else "m.Z_PK"
        stanza_col_ios = (
            "COALESCE(NULLIF(m.ZSTANZAID, ''), 'msg_' || m.Z_PK)"
            if caps.has_ios_stanza_id
            else "'msg_' || m.Z_PK"
        )
        quoted_stanza_id_ios = "qm.ZSTANZAID" if caps.has_ios_stanza_id else "NULL"
        ios_filesize_col = "COALESCE(mi.ZFILESIZE, 0)" if caps.has_ios_filesize else "0"
        ios_duration_col = "COALESCE(mi.ZMOVIEDURATION, 0)" if caps.has_ios_duration else "0"
        ios_aspect_ratio_col = (
            "COALESCE(mi.ZASPECTRATIO, 0.0)" if caps.has_ios_aspect_ratio else "0.0"
        )
        if caps.has_ios_title and caps.has_ios_mdi_title:
            ios_link_title = "COALESCE(CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZTITLE, '') END, NULLIF(mdi.ZTITLE, ''))"
        elif caps.has_ios_title:
            ios_link_title = "CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZTITLE, '') END"
        elif caps.has_ios_mdi_title:
            ios_link_title = "NULLIF(mdi.ZTITLE, '')"
        else:
            ios_link_title = "NULL"

        if caps.has_ios_mediaurl and caps.has_ios_mdi_matched:
            ios_link_url = "COALESCE(CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZMEDIAURL, '') END, NULLIF(mdi.ZMATCHEDTEXT, ''))"
        elif caps.has_ios_mediaurl:
            ios_link_url = "CASE WHEN m.ZMESSAGETYPE = 7 THEN NULLIF(mi.ZMEDIAURL, '') END"
        elif caps.has_ios_mdi_matched:
            ios_link_url = "NULLIF(mdi.ZMATCHEDTEXT, '')"
        else:
            ios_link_url = "NULL"

        ios_link_desc = "NULLIF(mdi.ZSUMMARY, '')" if caps.has_ios_mdi_summary else "NULL"
        ios_raw_metadata = "mi.ZMETADATA" if caps.has_ios_metadata else "NULL"
        ios_latitude_col = "mi.ZLATITUDE" if caps.has_ios_lat else "NULL"
        ios_longitude_col = "mi.ZLONGITUDE" if caps.has_ios_lng else "NULL"
        if caps.has_ios_vcard_name and caps.has_ios_title:
            ios_location_name_col = """COALESCE(
                CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDNAME IS NOT NULL THEN
                    CASE WHEN INSTR(mi.ZVCARDNAME, CHAR(10)) > 0
                         THEN SUBSTR(mi.ZVCARDNAME, 1, INSTR(mi.ZVCARDNAME, CHAR(10)) - 1)
                         ELSE mi.ZVCARDNAME END
                END,
                CASE WHEN m.ZMESSAGETYPE IN (5, 16) THEN NULLIF(mi.ZTITLE, '') END
            )"""
            ios_location_address_col = """COALESCE(
                CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDNAME IS NOT NULL AND INSTR(mi.ZVCARDNAME, CHAR(10)) > 0
                     THEN NULLIF(SUBSTR(mi.ZVCARDNAME, INSTR(mi.ZVCARDNAME, CHAR(10)) + 1), '')
                END,
                CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZTITLE IS NOT NULL AND m.ZTEXT != mi.ZTITLE THEN NULLIF(m.ZTEXT, '') END
            )"""
        elif caps.has_ios_vcard_name:
            ios_location_name_col = """CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDNAME IS NOT NULL THEN
                CASE WHEN INSTR(mi.ZVCARDNAME, CHAR(10)) > 0
                     THEN SUBSTR(mi.ZVCARDNAME, 1, INSTR(mi.ZVCARDNAME, CHAR(10)) - 1)
                     ELSE mi.ZVCARDNAME END
            END"""
            ios_location_address_col = """CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDNAME IS NOT NULL AND INSTR(mi.ZVCARDNAME, CHAR(10)) > 0
                THEN NULLIF(SUBSTR(mi.ZVCARDNAME, INSTR(mi.ZVCARDNAME, CHAR(10)) + 1), '')
            END"""
        elif caps.has_ios_title:
            ios_location_name_col = (
                "CASE WHEN m.ZMESSAGETYPE IN (5, 16) THEN NULLIF(mi.ZTITLE, '') END"
            )
            ios_location_address_col = "CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZTITLE IS NOT NULL AND m.ZTEXT != mi.ZTITLE THEN NULLIF(m.ZTEXT, '') END"
        else:
            ios_location_name_col = "NULL"
            ios_location_address_col = "NULL"

        ios_location_url_col = (
            "CASE WHEN m.ZMESSAGETYPE IN (5, 16) THEN NULLIF(mi.ZMEDIAURL, '') END"
            if caps.has_ios_mediaurl
            else "NULL"
        )
        ios_vcs_dur_expr = (
            "CAST(ROUND(CAST(SUBSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), INSTR(SUBSTR(mi.ZVCARDSTRING, INSTR(mi.ZVCARDSTRING, ';') + 1), ';') + 1) AS REAL)) AS INTEGER)"
            if caps.has_ios_vcard_string
            else "NULL"
        )
        if caps.has_ios_duration and caps.has_ios_vcard_string:
            ios_live_location_dur_col = f"""CASE
                WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZMOVIEDURATION IS NOT NULL AND mi.ZMOVIEDURATION > 0
                    THEN mi.ZMOVIEDURATION
                WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0
                    THEN {ios_vcs_dur_expr}
            END"""
            ios_live_type_cond = "WHEN m.ZMESSAGETYPE = 16 OR (m.ZMESSAGETYPE = 5 AND ((mi.ZMOVIEDURATION IS NOT NULL AND mi.ZMOVIEDURATION > 0) OR (mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0))) THEN 'live_location'"
        elif caps.has_ios_duration:
            ios_live_location_dur_col = (
                "CASE WHEN m.ZMESSAGETYPE IN (5, 16) THEN mi.ZMOVIEDURATION END"
            )
            ios_live_type_cond = "WHEN m.ZMESSAGETYPE = 16 OR (m.ZMESSAGETYPE = 5 AND mi.ZMOVIEDURATION IS NOT NULL AND mi.ZMOVIEDURATION > 0) THEN 'live_location'"
        elif caps.has_ios_vcard_string:
            ios_live_location_dur_col = f"CASE WHEN m.ZMESSAGETYPE IN (5, 16) AND mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0 THEN {ios_vcs_dur_expr} END"
            ios_live_type_cond = "WHEN m.ZMESSAGETYPE = 16 OR (m.ZMESSAGETYPE = 5 AND mi.ZVCARDSTRING IS NOT NULL AND INSTR(mi.ZVCARDSTRING, ';') > 0) THEN 'live_location'"
        else:
            ios_live_location_dur_col = "NULL"
            ios_live_type_cond = "WHEN m.ZMESSAGETYPE = 16 THEN 'live_location'"
        ios_thumb_arms = []
        if "ZXMPPTHUMBPATH" in caps.mi_cols:
            ios_thumb_arms.append("mi.ZXMPPTHUMBPATH IS NOT NULL")
        if "ZTHUMBNAILLOCALPATH" in caps.mi_cols:
            ios_thumb_arms.append("mi.ZTHUMBNAILLOCALPATH IS NOT NULL")
        if caps.has_ios_mdi and caps.has_ios_mdi_thumb:
            ios_thumb_arms.append("mdi.ZTHUMBNAILPATH IS NOT NULL")
        if "ZMETADATA" in caps.mi_cols:
            ios_thumb_arms.append(
                "(m.ZMESSAGETYPE != 0 AND mi.ZMETADATA IS NOT NULL AND INSTR(mi.ZMETADATA, X'ffd8ff') > 0)"
            )
        ios_has_thumb_expr = (
            "CASE WHEN " + " OR ".join(ios_thumb_arms) + " THEN 1 ELSE 0 END"
            if ios_thumb_arms
            else "0"
        )
        ios_service_filter = (
            "OR (m.ZMESSAGETYPE IN (6, 10) AND m.ZGROUPEVENTTYPE IN (1, 2, 3, 4, 5, 6, 7, 9, 10, 12, 15, 21, 26, 36, 37, 38, 44, 50, 56, 58))"
            if caps.has_ios_group_event
            else "OR (m.ZMESSAGETYPE IN (6, 10))"
        )
        ios_service_case = (
            "WHEN m.ZMESSAGETYPE = 6 OR (m.ZMESSAGETYPE = 10 AND m.ZGROUPEVENTTYPE IS NOT NULL AND m.ZGROUPEVENTTYPE > 0) THEN 'service'"
            if caps.has_ios_group_event
            else "WHEN m.ZMESSAGETYPE IN (6, 10) THEN 'service'"
        )
        base_ios_filter = f"""
    AND (
        (m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND (m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0))
        OR (m.ZMESSAGETYPE IS NOT NULL AND m.ZMESSAGETYPE != 0 AND m.ZMESSAGETYPE NOT IN (6, 10) AND (mi.ZMEDIALOCALPATH IS NOT NULL OR m.ZMESSAGETYPE IN (1, 2, 3, 5, 8, 11, 15, 16, 23, 38, 39, 54) OR mi.ZTITLE IS NOT NULL))
        OR (m.ZMESSAGETYPE = 4)
        OR (m.ZMESSAGETYPE = 14)
        OR (m.ZMESSAGETYPE IN (38, 39, 53))
        OR (m.ZMESSAGETYPE = 46)
        OR (m.ZMESSAGETYPE = 7)
        {ios_service_filter}
    )
    """
        base_ios_filter_ts = base_ios_filter
        if caps.has_ios_mdi and caps.has_ios_mdi_matched:
            ios_is_link = (
                "(m.ZMESSAGETYPE = 7 OR (mdi.ZMATCHEDTEXT IS NOT NULL AND mdi.ZMATCHEDTEXT != '')"
                " OR ((m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"
                " AND m.ZTEXT IS NOT NULL"
                " AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0"
                "      OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)))"
            )
            ios_link_type_cond = "(m.ZTEXT IS NOT NULL AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0 OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)) OR (mdi.ZMATCHEDTEXT IS NOT NULL AND mdi.ZMATCHEDTEXT != '')"
        else:
            ios_is_link = (
                "(m.ZMESSAGETYPE = 7 OR ((m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0)"
                " AND m.ZTEXT IS NOT NULL"
                " AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0"
                "      OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)))"
            )
            ios_link_type_cond = "m.ZTEXT IS NOT NULL AND (INSTR(LOWER(m.ZTEXT), 'http://') > 0 OR INSTR(LOWER(m.ZTEXT), 'https://') > 0)"

        if caps.has_ios_hd:
            ios_filter = base_ios_filter + _IOS_HD_DEDUP_CLAUSE
            ios_filter_ts = base_ios_filter_ts + _IOS_HD_DEDUP_CLAUSE
        else:
            ios_filter = base_ios_filter
            ios_filter_ts = base_ios_filter_ts

        ios_starred_expr = "COALESCE(m.ZSTARRED, 0)" if caps.has_ios_starred else "0"
        ios_type66_cond = (
            "EXISTS ("
            "SELECT 1 FROM ZWAMESSAGE pin_m "
            "WHERE pin_m.ZCHATSESSION = m.ZCHATSESSION "
            "AND pin_m.ZMESSAGETYPE = 66 "
            "AND pin_m.ZPARENTMESSAGE = m.Z_PK "
            "AND pin_m.ZMEDIAITEM IS NULL"
            ")"
            if caps.has_wa_zwamessage_parent
            else "0=1"
        )
        if caps.has_ios_ext_pinned:
            if caps.has_ext_thread_id_deleted:
                ext_pin_arm = (
                    f" OR (m.ZSTANZAID IS NOT NULL AND EXISTS ("
                    f"SELECT 1 FROM ext.thread_messages tm "
                    f"LEFT JOIN ext.thread_id ti ON ti.key_id = tm.thread_key_id "
                    f"WHERE tm.{caps.ext_pin_key_col} = m.ZSTANZAID AND COALESCE(ti.deleted, 0) = 0))"
                )
            else:
                ext_pin_arm = (
                    f" OR (m.ZSTANZAID IS NOT NULL AND EXISTS ("
                    f"SELECT 1 FROM ext.thread_messages tm "
                    f"WHERE tm.{caps.ext_pin_key_col} = m.ZSTANZAID))"
                )
        else:
            ext_pin_arm = ""
        ios_pinned_expr = f"CASE WHEN ({ios_type66_cond}{ext_pin_arm}) THEN 1 ELSE 0 END"

        mdi_join = (
            "    LEFT JOIN ZWAMESSAGEDATAITEM mdi ON mdi.ZMESSAGE = m.Z_PK AND mdi.ZINDEX = 0\n"
            if caps.has_ios_mdi
            else ""
        )

        ios_select = f"""
    SELECT
        m.Z_PK                                                       AS msg_id,
        {stanza_col_ios}                                             AS stanza_id,
        {sort_col_ios}                                               AS sort_id,
        {_IOS_CHAT_ID}                                               AS chat_id,
        {_IOS_CHAT_TYPE}                                             AS chat_type,
        CAST((m.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER)        AS timestamp_ms,
        COALESCE(
            CASE WHEN ({_IOS_SENDER_JID}) = '0' THEN 'WhatsApp' END,
            CASE WHEN ic_s.full_name NOT LIKE '+%' THEN NULLIF(ic_s.full_name, '') END,
            NULLIF(con_s.display_name, ''),
            NULLIF(con_lid_s.display_name, ''),
            NULLIF(pp_lid.ZPUSHNAME, ''),
            NULLIF(pp_phone.ZPUSHNAME, ''),
            NULLIF(cs_lid.ZPARTNERNAME, ''),
            NULLIF(cs_phone.ZPARTNERNAME, ''),
            CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN NULLIF(m.ZPUSHNAME, '') END,
            CASE WHEN COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            ) IS NOT NULL
            THEN '+' || COALESCE(
                CASE WHEN ({_IOS_SENDER_RAW}) NOT LIKE '%@lid' THEN ({_IOS_SENDER_JID}) END,
                ic_s.phone_number
            )
            END,
            ''
        )                                                            AS sender,
        m.ZISFROMME                                                  AS from_me,
        ac.archive_path,
        CASE
            WHEN m.ZMESSAGETYPE = 14 THEN 'deleted'
            {ios_service_case}
            WHEN m.ZMESSAGETYPE = 4 THEN 'vcard'
            {ios_live_type_cond}
            WHEN m.ZMESSAGETYPE = 5 THEN 'location'
            WHEN m.ZMESSAGETYPE = 15 THEN 'sticker'
            WHEN m.ZMESSAGETYPE IN (1, 38) THEN 'image'
            WHEN m.ZMESSAGETYPE IN (2, 39, 54) THEN 'video'
            WHEN m.ZMESSAGETYPE IN (3, 53) THEN 'audio'
            WHEN m.ZMESSAGETYPE = 8 THEN 'document'
            WHEN m.ZMESSAGETYPE = 11 THEN 'gif'
            WHEN m.ZMESSAGETYPE = 46 THEN 'poll'
            WHEN m.ZMESSAGETYPE = 7 THEN 'link'
            WHEN m.ZMESSAGETYPE IS NULL OR m.ZMESSAGETYPE = 0 THEN
                CASE WHEN {ios_link_type_cond}
                     THEN 'link' ELSE 'text' END
            ELSE {_MEDIA_TYPE_EXPR.format(col="COALESCE('Message/' || mi.ZMEDIALOCALPATH, mi.ZTITLE)")}
        END                                                           AS media_type,
        COALESCE(
            CASE WHEN m.ZMESSAGETYPE = 4 THEN {vcard_name_ios} END,
            CASE WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(m.ZTEXT, '') END,
            CASE WHEN m.ZMESSAGETYPE = 11 THEN '' END,
            mi.ZMEDIALOCALPATH,
            ''
        )                                                             AS media_name,
        COALESCE(
            CASE
                WHEN m.ZISFROMME = 1 AND m.ZMESSAGETYPE = 14 THEN 'You deleted this message'
                WHEN m.ZMESSAGETYPE = 14 THEN 'This message was deleted'
                WHEN m.ZMESSAGETYPE = 4 THEN {vcard_name_ios}
                WHEN m.ZMESSAGETYPE = 8 THEN NULLIF(mi.ZTITLE, '')
                WHEN m.ZMESSAGETYPE IN (1, 2, 3, 11, 38, 39, 54) THEN COALESCE(NULLIF(m.ZTEXT, ''), NULLIF(mi.ZTITLE, ''))
                ELSE m.ZTEXT
            END,
            ''
        )                                                             AS text_body,
        COALESCE(
            qm.ZTEXT,
            CASE
                WHEN qm.ZMESSAGETYPE = 46 THEN 'Poll'
                WHEN qm.ZMESSAGETYPE IN (1, 38) THEN '📷 Photo'
                WHEN qm.ZMESSAGETYPE IN (2, 39, 54) THEN '🎥 Video'
                WHEN qm.ZMESSAGETYPE IN (3, 53) THEN '🎵 Audio'
                WHEN qm.ZMESSAGETYPE = 11 THEN '🎞️ GIF'
                WHEN qm.ZMESSAGETYPE = 5 THEN '📍 Location'
                WHEN qm.ZMESSAGETYPE = 16 THEN '📍 Live location'
                WHEN qm.ZMESSAGETYPE = 15 THEN 'Sticker'
            END,
            ''
        )                                                             AS quoted_text,
        CASE WHEN qm.ZISFROMME = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 NULLIF(qm.ZPUSHNAME, ''),
                 CASE WHEN SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                   INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1) != ''
                      THEN '+' || SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                         INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
                 END,
                 '')
        END                                                          AS quoted_sender,
        COALESCE(CAST((qm.ZMESSAGEDATE + 978307200) * 1000 AS INTEGER), 0) AS quoted_ts,
        qm.Z_PK                                                      AS quoted_msg_id,
        {quoted_stanza_id_ios}                                       AS quoted_stanza_id,
        NULL                                                          AS reactions,
        0                                                            AS is_edited,
        NULL                                                          AS edited_ts,
        CASE WHEN m.ZMESSAGETYPE IN (38, 39, 53) THEN 1 ELSE 0 END   AS is_view_once,
        CASE WHEN m.ZMESSAGETYPE = 54 THEN 1 ELSE 0 END               AS is_video_note,
        {ios_starred_expr}                                           AS is_starred,
        {ios_pinned_expr}                                            AS is_pinned,
        m.ZMESSAGETYPE                                               AS raw_msg_type,
        {group_ev_col}                                               AS group_event_type,
        gm.ZMEMBERJID                                                AS member_jid,
        0                                                            AS media_width,
        0                                                            AS media_height,
        {ios_filesize_col}                                           AS media_size,
        {ios_duration_col}                                           AS media_duration,
        {ios_aspect_ratio_col}                                       AS aspect_ratio,
        {ios_has_thumb_expr}                                         AS has_thumbnail,
        {ios_link_title}                                             AS link_title,
        {ios_link_desc}                                              AS link_desc,
        {ios_link_url}                                               AS link_url,
        0                                                            AS link_preview_type,
        {ios_latitude_col}                                           AS latitude,
        {ios_longitude_col}                                          AS longitude,
        {ios_location_name_col}                                      AS location_name,
        {ios_location_address_col}                                   AS location_address,
        {ios_location_url_col}                                       AS location_url,
        {ios_live_location_dur_col}                                  AS live_location_share_duration,
        {ios_raw_metadata}                                           AS _raw_metadata
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
    {mdi_join}    LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWACHATSESSION cs_lid ON cs_lid.ZCONTACTJID = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_lid ON pp_lid.ZJID = ({_IOS_SENDER_RAW})
    LEFT JOIN _ios_contacts ic_s ON ic_s.jid = ({_IOS_SENDER_RAW})
    LEFT JOIN ZWAPROFILEPUSHNAME pp_phone ON pp_phone.ZJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWACHATSESSION cs_phone ON cs_phone.ZCONTACTJID = (ic_s.phone_number || '@s.whatsapp.net')
    LEFT JOIN ZWAMESSAGE qm ON qm.Z_PK = m.ZPARENTMESSAGE
    LEFT JOIN arch.contacts con_s
          ON con_s.number = COALESCE(
              CASE WHEN ({_IOS_SENDER_RAW}) LIKE '%@lid' THEN ic_s.phone_number END,
              ({_IOS_SENDER_JID})
          )
    LEFT JOIN arch.contacts con_lid_s
          ON con_lid_s.number = SUBSTR(({_IOS_SENDER_RAW}), 1,
                                       INSTR(({_IOS_SENDER_RAW}) || '@', '@') - 1)
    LEFT JOIN arch.contacts con_sq
          ON con_sq.number = SUBSTR(COALESCE(qm.ZFROMJID,''), 1,
                                    INSTR(COALESCE(qm.ZFROMJID,'') || '@', '@') - 1)
    LEFT JOIN arch.archive_copies ac
          ON ac.original_path = COALESCE('Message/' || mi.ZMEDIALOCALPATH, 'vcard:' || m.Z_PK)
    """

    ms_join = (
        "LEFT JOIN message_system ms ON ms.message_row_id = m._id"
        if caps.has_message_system
        else ""
    )
    ms_col = "ms.action_type" if caps.has_message_system else "NULL"
    mrev_join = (
        "    LEFT JOIN message_revoked mrev ON mrev.message_row_id = m._id\n"
        if caps.has_message_revoked
        else ""
    )
    mrev_pred = "mrev.message_row_id IS NOT NULL" if caps.has_message_revoked else "0"

    ms_action_clause = (
        f"ms.action_type IN {tuple(_SUPPORTED_ANDROID_SYSTEM_ACTIONS)}"
        if caps.has_message_system
        else "(m.text_data IS NOT NULL AND m.text_data != '')"
    )
    android_hd_clause = _ANDROID_HD_DEDUP_CLAUSE if caps.has_android_hd else ""

    poll_subquery_parts = []
    if caps.has_message_poll:
        poll_subquery_parts.append(
            "EXISTS (SELECT 1 FROM message_poll mp WHERE mp.message_row_id = m._id)"
        )
    if caps.has_message_add_on:
        poll_subquery_parts.append(
            "EXISTS (SELECT 1 FROM message_add_on mao WHERE mao.parent_message_row_id = m._id AND mao.message_add_on_type = 67)"
        )
    extra_poll_filter = (
        f"\n        OR ({' OR '.join(poll_subquery_parts)})" if poll_subquery_parts else ""
    )
    extra_poll_select = f" OR ({' OR '.join(poll_subquery_parts)})" if poll_subquery_parts else ""
    mvom_filter_or = (
        " OR (mvom.message_row_id IS NOT NULL)" if caps.has_message_view_once_media else ""
    )
    extra_mrev_filter = (
        "\n        OR (EXISTS (SELECT 1 FROM message_revoked mrev_f WHERE mrev_f.message_row_id = m._id))"
        if caps.has_message_revoked
        else ""
    )

    android_filter = f"""
    AND (
        (m.text_data IS NOT NULL AND m.text_data != '' AND (m.message_type IS NULL OR m.message_type = 0 OR m.message_type IN (5, 16)))
        OR (m.message_type IS NOT NULL AND m.message_type != 0 AND m.message_type != 7 AND (mm.file_path IS NOT NULL OR mm.message_row_id IS NOT NULL OR m.message_type IN (1, 2, 3, 5, 9, 13, 16, 20, 28)))
        OR (m.message_type IN (4, 14))
        OR (m.message_type IN (15, 64){extra_mrev_filter})
        OR (m.message_type IN (42, 43, 82){mvom_filter_or})
        OR (m.message_type = 7 AND {ms_action_clause})
        OR (m.message_type = 66){extra_poll_filter}
    )
    {android_hd_clause}
    """
    android_filter_ts = android_filter

    ldn_sender_joins = (
        """
    LEFT JOIN _lid_map_resolved jm_lid_s ON jm_lid_s.jid_row_id = m.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_s_direct ON ldn_s_direct.lid_row_id = m.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_s_mapped ON ldn_s_mapped.lid_row_id = jm_lid_s.lid_row_id
    """
        if caps.has_lid_dn
        else ""
    )

    ldn_quoted_joins = (
        """
    LEFT JOIN _lid_map_resolved jm_lid_sq ON jm_lid_sq.jid_row_id = mq.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_sq_direct ON ldn_sq_direct.lid_row_id = mq.sender_jid_row_id
    LEFT JOIN lid_display_name ldn_sq_mapped ON ldn_sq_mapped.lid_row_id = jm_lid_sq.lid_row_id
    """
        if caps.has_lid_dn
        else ""
    )
    ldn_select_joins = ldn_sender_joins + ldn_quoted_joins

    ldn_sender_arm = (
        """
            NULLIF(ldn_s_direct.display_name, ''),
            NULLIF(ldn_s_mapped.display_name, ''),
    """
        if caps.has_lid_dn
        else ""
    )

    ldn_quoted_sender_arm = (
        """
                 NULLIF(ldn_sq_direct.display_name, ''),
                 NULLIF(ldn_sq_mapped.display_name, ''),
    """
        if caps.has_lid_dn
        else ""
    )

    j_is_lid = "j.server = 'lid'" if caps.has_jid_server else "0"
    jq_is_lid = "jq.server = 'lid'" if caps.has_jid_server else "0"

    if caps.has_android_edit_info:
        edit_join = "LEFT JOIN message_edit_info mei ON mei.message_row_id = m._id"
        edit_is_edited_col = "CASE WHEN mei.message_row_id IS NOT NULL THEN 1 ELSE 0 END"
        edit_ts_col = "mei.edited_timestamp" if caps.has_mei_ts else "NULL"
    elif caps.has_android_add_on:
        edit_join = (
            "LEFT JOIN message_add_on mao_edit "
            "ON mao_edit.parent_message_row_id = m._id AND mao_edit.message_add_on_type = 74"
        )
        edit_is_edited_col = "CASE WHEN mao_edit._id IS NOT NULL THEN 1 ELSE 0 END"
        edit_ts_col = "mao_edit.timestamp" if caps.has_mao_ts else "NULL"
    else:
        edit_join = ""
        edit_is_edited_col = "0"
        edit_ts_col = "NULL"

    stanza_col_android = (
        "COALESCE(NULLIF(m.key_id, ''), 'msg_' || m._id)"
        if caps.has_android_key_id
        else "'msg_' || m._id"
    )

    if caps.has_message_media_map:
        emd_join = (
            "    LEFT JOIN (\n"
            "        SELECT mmm.message_row_id, emd.file_path, emd.mime_type, emd.width, emd.height, emd.file_size, emd.external_url\n"
            "        FROM message_media_map mmm\n"
            "        JOIN extended_media_data emd ON emd.row_id = mmm.media_row_id\n"
            "    ) emd_map ON emd_map.message_row_id = m._id\n"
        )
        android_file_path = "COALESCE(mm.file_path, emd_map.file_path)"
        mm_width_col = (
            "COALESCE(mm.width, emd_map.width, 0)"
            if caps.has_mm_width
            else "COALESCE(emd_map.width, 0)"
        )
        mm_height_col = (
            "COALESCE(mm.height, emd_map.height, 0)"
            if caps.has_mm_height
            else "COALESCE(emd_map.height, 0)"
        )
        mm_size_col = (
            "COALESCE(mm.file_size, emd_map.file_size, 0)"
            if caps.has_mm_size
            else "COALESCE(emd_map.file_size, 0)"
        )
    else:
        emd_join = ""
        android_file_path = "mm.file_path"
        mm_width_col = "COALESCE(mm.width, 0)" if caps.has_mm_width else "0"
        mm_height_col = "COALESCE(mm.height, 0)" if caps.has_mm_height else "0"
        mm_size_col = "COALESCE(mm.file_size, 0)" if caps.has_mm_size else "0"

    mm_duration_col = "COALESCE(mm.media_duration, 0)" if caps.has_mm_duration else "0"
    mm_aspect_col = (
        f"CASE WHEN {mm_height_col} > 0 AND {mm_width_col} > 0 "
        f"THEN ROUND(CAST({mm_width_col} AS REAL) / CAST({mm_height_col} AS REAL), 3) "
        "ELSE 0.0 END"
    )

    android_thumb_joins = ""
    android_has_thumb_arms = []
    if caps.has_message_thumbnail:
        android_thumb_joins += "    LEFT JOIN message_thumbnail mt ON mt.message_row_id = m._id\n"
        android_has_thumb_arms.append("mt.message_row_id IS NOT NULL")
    if caps.has_media_hash_thumbnail and caps.has_mm_file_hash:
        android_thumb_joins += (
            "    LEFT JOIN media_hash_thumbnail mht ON mht.media_hash = mm.file_hash\n"
        )
        android_has_thumb_arms.append("mht.media_hash IS NOT NULL")
    if caps.has_message_quoted_media:
        android_thumb_joins += (
            "    LEFT JOIN message_quoted_media mqm ON mqm.message_row_id = m._id\n"
        )
        android_has_thumb_arms.append(
            "(mqm.message_row_id IS NOT NULL AND mqm.thumbnail IS NOT NULL)"
        )
    if caps.has_message_quoted_text:
        android_thumb_joins += (
            "    LEFT JOIN message_quoted_text mqt ON mqt.message_row_id = m._id\n"
        )
        android_has_thumb_arms.append(
            "(mqt.message_row_id IS NOT NULL AND mqt.thumbnail IS NOT NULL)"
        )
    if caps.has_message_quoted_media and caps.has_mq_key_id and caps.has_android_key_id:
        android_has_thumb_arms.append(
            "(m.message_type IN (1, 3, 9, 20, 26, 42, 43, 82) AND EXISTS ("
            "    SELECT 1 FROM message_quoted mq_orig "
            "    JOIN message_quoted_media mqm_orig ON mqm_orig.message_row_id = mq_orig.message_row_id "
            "    WHERE mq_orig.key_id = m.key_id AND mqm_orig.thumbnail IS NOT NULL"
            "))"
        )
    if caps.has_message_quoted_text and caps.has_mq_key_id and caps.has_android_key_id:
        android_has_thumb_arms.append(
            "(m.message_type = 0 AND EXISTS ("
            "    SELECT 1 FROM message_quoted mq_orig "
            "    JOIN message_quoted_text mqt_orig ON mqt_orig.message_row_id = mq_orig.message_row_id "
            "    WHERE mq_orig.key_id = m.key_id AND mqt_orig.thumbnail IS NOT NULL"
            "))"
        )

    android_has_thumb_expr = (
        "CASE WHEN " + " OR ".join(android_has_thumb_arms) + " THEN 1 ELSE 0 END"
        if android_has_thumb_arms
        else "0"
    )

    quoted_msg_id_col = "NULL"
    quoted_stanza_id_col = "NULL"
    quoted_ts_col = "COALESCE(mq.timestamp, 0)"
    m_orig_join = ""
    if caps.has_android_key_id and caps.has_mq_key_id:
        pm_clause = (
            "AND (m_orig.chat_row_id = mq.parent_message_chat_row_id OR mq.parent_message_chat_row_id IS NULL) "
            if caps.has_mq_pm_chat
            else ""
        )
        m_orig_join = f"    LEFT JOIN message m_orig ON m_orig.key_id = mq.key_id {pm_clause}\n"
        if caps.has_message_thumbnail:
            m_orig_join += (
                "    LEFT JOIN message_thumbnail mt_orig ON mt_orig.message_row_id = m_orig._id\n"
            )
        if caps.has_media_hash_thumbnail and caps.has_mm_file_hash:
            m_orig_join += (
                "    LEFT JOIN message_media mm_orig ON mm_orig.message_row_id = m_orig._id\n"
                "    LEFT JOIN media_hash_thumbnail mht_orig ON mht_orig.media_hash = mm_orig.file_hash\n"
            )
        if caps.has_message_quoted_media:
            m_orig_join += "    LEFT JOIN message_quoted_media mqm_orig ON mqm_orig.message_row_id = m_orig._id\n"
        if caps.has_message_quoted_text:
            m_orig_join += "    LEFT JOIN message_quoted_text mqt_orig ON mqt_orig.message_row_id = m_orig._id\n"
        quoted_msg_id_col = "m_orig._id"
        quoted_stanza_id_col = "mq.key_id"
        quoted_ts_col = "COALESCE(NULLIF(mq.timestamp, 0), m_orig.timestamp, 0)"

    quoted_thumb_conds = []
    if caps.has_message_quoted_media:
        quoted_thumb_conds.append("(mqm.message_row_id IS NOT NULL AND mqm.thumbnail IS NOT NULL)")
    if caps.has_message_quoted_text:
        quoted_thumb_conds.append("(mqt.message_row_id IS NOT NULL AND mqt.thumbnail IS NOT NULL)")
    if caps.has_android_key_id and caps.has_mq_key_id:
        if caps.has_message_thumbnail:
            quoted_thumb_conds.append(
                "(mt_orig.message_row_id IS NOT NULL AND mt_orig.thumbnail IS NOT NULL)"
            )
        if caps.has_media_hash_thumbnail and caps.has_mm_file_hash:
            quoted_thumb_conds.append(
                "(mht_orig.media_hash IS NOT NULL AND mht_orig.thumbnail IS NOT NULL)"
            )
        if caps.has_message_quoted_media:
            quoted_thumb_conds.append(
                "(mqm_orig.message_row_id IS NOT NULL AND mqm_orig.thumbnail IS NOT NULL)"
            )
        if caps.has_message_quoted_text:
            quoted_thumb_conds.append(
                "(mqt_orig.message_row_id IS NOT NULL AND mqt_orig.thumbnail IS NOT NULL)"
            )
        quoted_thumb_conds.append("(m_orig.message_type IN (1, 3, 9, 13, 20, 26, 28, 42, 43, 81))")
    if caps.has_mq_msg_type:
        quoted_thumb_conds.append("(mq.message_type IN (1, 3, 9, 13, 20, 26, 28, 42, 43, 81))")

    android_quoted_has_thumb_expr = (
        f"CASE WHEN {' OR '.join(quoted_thumb_conds)} THEN 1 ELSE 0 END"
        if quoted_thumb_conds
        else "0"
    )

    orig_text_arm = (
        "NULLIF(m_orig.text_data, ''),\n            "
        if (caps.has_android_key_id and caps.has_mq_key_id)
        else ""
    )
    orig_gif_arm = (
        " OR (m_orig.message_type = 13)" if (caps.has_android_key_id and caps.has_mq_key_id) else ""
    )
    if caps.has_mq_msg_type:
        quoted_text_expr = f"""COALESCE(
            NULLIF(mq.text_data, ''),
            {orig_text_arm}CASE
                WHEN mq.message_type = 13{orig_gif_arm} THEN '🎞️ GIF'
                WHEN mq.message_type IN (1, 42) THEN '📷 Photo'
                WHEN mq.message_type IN (2, 82) THEN '🎵 Audio'
                WHEN mq.message_type IN (3, 43) THEN '🎥 Video'
                WHEN mq.message_type = 5 THEN '📍 Location'
                WHEN mq.message_type = 16 THEN '📍 Live location'
                WHEN mq.message_type = 20 THEN 'Sticker'
                WHEN mq.message_type = 66 THEN 'Poll'
            END,
            ''
        )"""
    else:
        quoted_text_expr = f"COALESCE(NULLIF(mq.text_data, ''), {orig_text_arm}'')"

    group_cond_parts = ["c.subject IS NOT NULL"]
    if caps.has_jid_server:
        group_cond_parts.append("(j_chat.server IS NOT NULL AND j_chat.server = 'g.us')")
    if caps.has_jid_raw_string:
        group_cond_parts.append(
            "(j_chat.raw_string IS NOT NULL AND j_chat.raw_string LIKE '%@g.us')"
        )
    is_group_chat_select = " OR ".join(group_cond_parts)

    android_chat_id = f"""
        CASE
            WHEN ({is_group_chat_select}) THEN CAST(m.chat_row_id AS TEXT)
            ELSE COALESCE(
                {_clean_user_sql("j_chat_real.user")},
                {_clean_user_sql("j_chat.user")},
                CAST(m.chat_row_id AS TEXT)
            )
        END
    """
    android_chat_type = f"CASE WHEN ({is_group_chat_select}) THEN 'group' ELSE 'contact' END"

    android_text_join = (
        "    LEFT JOIN message_text mtxt ON mtxt.message_row_id = m._id\n"
        if caps.has_message_text
        else ""
    )
    android_loc_join = (
        "    LEFT JOIN message_location ml ON ml.message_row_id = m._id\n"
        if caps.has_message_location
        else ""
    )
    android_latitude_col = "ml.latitude" if caps.has_ml_lat else "NULL"
    android_longitude_col = "ml.longitude" if caps.has_ml_lng else "NULL"
    android_location_name_col = "ml.place_name" if caps.has_ml_name else "NULL"
    android_location_address_col = "ml.place_address" if caps.has_ml_address else "NULL"
    android_location_url_col = "ml.url" if caps.has_ml_url else "NULL"
    android_live_location_dur_col = (
        "ml.live_location_share_duration" if caps.has_ml_duration else "NULL"
    )
    android_live_type_cond = (
        "WHEN m.message_type = 16 OR (m.message_type = 5 AND ml.live_location_share_duration IS NOT NULL AND ml.live_location_share_duration > 0) THEN 'live_location'"
        if caps.has_ml_duration
        else "WHEN m.message_type = 16 THEN 'live_location'"
    )
    mt_title_col = "NULLIF(mtxt.page_title, '')" if caps.has_mt_title else "NULL"
    mt_desc_col = "NULLIF(mtxt.description, '')" if caps.has_mt_desc else "NULL"
    mt_url_col = "NULLIF(mtxt.url, '')" if caps.has_mt_url else "NULL"
    mt_preview_type_col = "COALESCE(mtxt.preview_type, 0)" if caps.has_mt_preview_type else "0"
    mvom_join = (
        "    LEFT JOIN message_view_once_media mvom ON mvom.message_row_id = m._id\n"
        if caps.has_message_view_once_media
        else ""
    )
    mvom_or = " OR mvom.message_row_id IS NOT NULL" if caps.has_message_view_once_media else ""

    android_gif_attr_cond = (
        "(mm.gif_attribution IS NOT NULL AND mm.gif_attribution > 0)"
        if caps.has_mm_gif_attribution
        else "0=1"
    )
    android_gif_cond = f"""(
        m.message_type = 13
        OR {android_gif_attr_cond}
        OR mm.file_path LIKE '%WhatsApp Animated Gifs/%'
        OR mm.file_path LIKE '%/Animated Gifs/%'
        OR mm.file_path LIKE '%/GIF-%'
        OR mm.file_path LIKE 'GIF-%'
        OR ac.archive_path LIKE '%WhatsApp Animated Gifs/%'
        OR ac.archive_path LIKE '%/Animated Gifs/%'
        OR ac.archive_path LIKE '%/GIF-%'
        OR ac.archive_path LIKE '%.gif'
    )"""

    has_starred_col = caps.has_msg_starred
    android_starred_expr = "COALESCE(m.starred, 0)" if has_starred_col else "0"
    pin_table_join = (
        "LEFT JOIN message_add_on_pin_in_chat mao_pic ON mao_pic.message_add_on_row_id = mao_pin._id\n        "
        if caps.has_pin_in_chat_table
        else ""
    )
    pin_table_cond = (
        "AND (mao_pic.pin_in_chat_state IS NULL OR mao_pic.pin_in_chat_state = 1) "
        if caps.has_pin_in_chat_table
        else ""
    )
    android_pinned_expr = (
        "CASE WHEN EXISTS ("
        "SELECT 1 FROM message_add_on mao_pin "
        f"{pin_table_join}"
        "WHERE mao_pin.parent_message_row_id = m._id "
        "AND mao_pin.message_add_on_type = 79 "
        f"{pin_table_cond}"
        ") THEN 1 ELSE 0 END"
        if caps.has_android_add_on
        else "0"
    )

    android_select = f"""
    SELECT
        m._id                                                        AS msg_id,
        {stanza_col_android}                                         AS stanza_id,
        {android_chat_id}                                           AS chat_id,
        {android_chat_type}                                         AS chat_type,
        COALESCE(m.timestamp, 0)                                     AS timestamp_ms,
        COALESCE(
            NULLIF(con_s.display_name, ''),
            NULLIF(con_s.folder, ''),
            CASE WHEN COALESCE(j2.user, CASE WHEN NOT ({j_is_lid}) THEN j.user END) = '0' THEN 'WhatsApp' END,
            CASE WHEN j2.user IS NOT NULL THEN '+' || {_clean_user_sql("j2.user")} END,
            {ldn_sender_arm}
            CASE WHEN NOT ({j_is_lid}) AND j.user IS NOT NULL THEN '+' || {_clean_user_sql("j.user")} END,
            ''
        )                                                            AS sender,
        m.from_me,
        ac.archive_path,
        CASE
            WHEN m.message_type IN (15, 64) OR {mrev_pred} THEN 'deleted'
            WHEN m.message_type = 7 AND ({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select}) THEN 'deleted'
            WHEN m.message_type = 7 THEN 'service'
            WHEN m.message_type IN (4, 14) THEN 'vcard'
            WHEN m.message_type = 20 THEN 'sticker'
            WHEN {android_gif_cond} THEN 'gif'
            WHEN m.message_type = 42 THEN 'image'
            WHEN m.message_type IN (28, 43, 81) THEN 'video'
            WHEN m.message_type = 82 THEN 'audio'
            WHEN m.message_type = 66{extra_poll_select} THEN 'poll'
            {android_live_type_cond}
            WHEN m.message_type = 5 THEN 'location'
            WHEN m.message_type IS NULL OR m.message_type = 0 THEN
                CASE WHEN m.text_data IS NOT NULL
                      AND (INSTR(LOWER(m.text_data), 'http://') > 0
                           OR INSTR(LOWER(m.text_data), 'https://') > 0)
                     THEN 'link' ELSE 'text' END
            WHEN {android_file_path} IS NULL AND m.message_type = 1 THEN 'image'
            WHEN {android_file_path} IS NULL AND m.message_type = 2 THEN 'audio'
            WHEN {android_file_path} IS NULL AND m.message_type IN (3, 28, 81) THEN 'video'
            WHEN {android_file_path} IS NULL AND m.message_type = 9 THEN 'document'
            ELSE {_MEDIA_TYPE_EXPR.format(col=f"COALESCE({android_file_path}, mm.media_name)")}
        END                                                          AS media_type,
        COALESCE(
            CASE WHEN {android_gif_cond} THEN '' END,
            mm.media_name,
            CASE WHEN m.message_type IN (4, 14) THEN m.text_data END,
            ''
        )                                                            AS media_name,
        COALESCE(
            m.text_data,
            CASE
                WHEN m.message_type = 64 THEN 'This message was deleted by an admin'
                WHEN (m.from_me = 1 AND (m.message_type = 15 OR {mrev_pred})) OR (m.from_me = 1 AND m.message_type = 7 AND ({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select})) THEN 'You deleted this message'
                WHEN m.message_type = 15 OR {mrev_pred} OR (m.message_type = 7 AND ({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select})) THEN 'This message was deleted'
                WHEN m.message_type IN (4, 14) THEN m.text_data
                WHEN m.message_type = 66{extra_poll_select} THEN 'Poll'
                WHEN m.message_type = 5 THEN COALESCE({android_location_name_col}, 'Location')
                WHEN m.message_type = 16 THEN 'Live Location'
            END,
            ''
        )                                                            AS text_body,
        {quoted_text_expr}                                           AS quoted_text,
        CASE WHEN mq.from_me = 1 THEN 'You'
             ELSE COALESCE(
                 NULLIF(con_sq.display_name, ''),
                 NULLIF(con_sq.folder, ''),
                 CASE WHEN jq2.user IS NOT NULL THEN '+' || {_clean_user_sql("jq2.user")} END,
                 {ldn_quoted_sender_arm}
                 CASE WHEN NOT ({jq_is_lid}) AND jq.user IS NOT NULL THEN '+' || {_clean_user_sql("jq.user")} END,
                 '') END                                             AS quoted_sender,
        {quoted_ts_col}                                              AS quoted_ts,
        {quoted_msg_id_col}                                          AS quoted_msg_id,
        {quoted_stanza_id_col}                                       AS quoted_stanza_id,
        {android_quoted_has_thumb_expr}                              AS quoted_has_thumb,
        {edit_is_edited_col}                                         AS is_edited,
        {edit_ts_col}                                                AS edited_ts,
        CASE WHEN m.message_type IN (42, 43, 82){mvom_or} THEN 1 ELSE 0 END   AS is_view_once,
        CASE WHEN m.message_type IN (28, 81) THEN 1 ELSE 0 END        AS is_video_note,
        {android_starred_expr}                                       AS is_starred,
        {android_pinned_expr}                                        AS is_pinned,
        m.message_type                                               AS raw_msg_type,
        {ms_col}                                                     AS action_type,
        ''                                                           AS participant_name,
        {mm_width_col}                                               AS media_width,
        {mm_height_col}                                              AS media_height,
        {mm_size_col}                                                AS media_size,
        {mm_duration_col}                                            AS media_duration,
        {mm_aspect_col}                                              AS aspect_ratio,
        {android_has_thumb_expr}                                     AS has_thumbnail,
        {mt_title_col}                                               AS link_title,
        {mt_desc_col}                                                AS link_desc,
        {mt_url_col}                                                 AS link_url,
        {mt_preview_type_col}                                        AS link_preview_type,
        {android_latitude_col}                                       AS latitude,
        {android_longitude_col}                                      AS longitude,
        {android_location_name_col}                                  AS location_name,
        {android_location_address_col}                               AS location_address,
        {android_location_url_col}                                   AS location_url,
        {android_live_location_dur_col}                              AS live_location_share_duration
    FROM message m
    LEFT JOIN message_media mm ON mm.message_row_id = m._id
    {emd_join}{android_thumb_joins}{android_text_join}{android_loc_join}{mvom_join}    LEFT JOIN chat c ON c._id = m.chat_row_id
    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
    LEFT JOIN _jid_map_resolved jm_chat ON jm_chat.lid_row_id = c.jid_row_id
    LEFT JOIN jid j_chat_real ON j_chat_real._id = jm_chat.jid_row_id
    LEFT JOIN jid j ON j._id = m.sender_jid_row_id
    {_ANDROID_JID_MAP}
    LEFT JOIN message_quoted mq ON mq.message_row_id = m._id
    {m_orig_join}    LEFT JOIN jid jq ON jq._id = mq.sender_jid_row_id
    LEFT JOIN _jid_map_resolved jmq ON jmq.lid_row_id = mq.sender_jid_row_id
    LEFT JOIN jid jq2 ON jq2._id = jmq.jid_row_id
    {ldn_select_joins}
    {ms_join}
    {mrev_join}
    {edit_join}
    LEFT JOIN _contacts_resolved con_s  ON con_s.clean_number  = {_clean_user_sql("COALESCE(j2.user, j.user)")}
    LEFT JOIN _contacts_resolved con_sq ON con_sq.clean_number = {_clean_user_sql("COALESCE(jq2.user, jq.user)")}
    LEFT JOIN arch.archive_copies ac ON ac.original_path IN (
        COALESCE({android_file_path}, 'vcard:' || m._id),
        REPLACE(COALESCE({android_file_path}, ''), '\\', '/'),
        REPLACE(COALESCE({android_file_path}, ''), '/', '\\'),
        CASE WHEN {android_file_path} LIKE 'WhatsApp/%' OR {android_file_path} LIKE 'WhatsApp\\%'
             THEN SUBSTR(REPLACE({android_file_path}, '\\', '/'), 10) END,
        CASE WHEN {android_file_path} LIKE 'WhatsApp/%' OR {android_file_path} LIKE 'WhatsApp\\%'
             THEN REPLACE(SUBSTR(REPLACE({android_file_path}, '\\', '/'), 10), '/', '\\') END,
        CASE WHEN {android_file_path} NOT LIKE 'WhatsApp/%' AND {android_file_path} NOT LIKE 'WhatsApp\\%' AND {android_file_path} IS NOT NULL
             THEN 'WhatsApp/' || REPLACE({android_file_path}, '\\', '/') END,
        CASE WHEN {android_file_path} NOT LIKE 'WhatsApp/%' AND {android_file_path} NOT LIKE 'WhatsApp\\%' AND {android_file_path} IS NOT NULL
             THEN 'WhatsApp\\' || REPLACE({android_file_path}, '/', '\\') END
    )
    """

    android_stat_select = f"""
    SELECT
        m._id                                                        AS msg_id,
        COALESCE(m.timestamp, 0)                                     AS timestamp_ms,
        COALESCE(
            NULLIF(con_s.display_name, ''),
            NULLIF(con_s.folder, ''),
            CASE WHEN COALESCE(j2.user, CASE WHEN NOT ({j_is_lid}) THEN j.user END) = '0' THEN 'WhatsApp' END,
            CASE WHEN j2.user IS NOT NULL THEN '+' || {_clean_user_sql("j2.user")} END,
            {ldn_sender_arm}
            CASE WHEN NOT ({j_is_lid}) AND j.user IS NOT NULL THEN '+' || {_clean_user_sql("j.user")} END,
            ''
        )                                                            AS sender,
        m.from_me,
        CASE
            WHEN m.message_type = 7 AND NOT (({ms_col} = 1 OR {ms_col} IS NULL) AND NOT ({is_group_chat_select})) THEN 'service'
            ELSE 'other'
        END                                                          AS media_type
    FROM message m
    LEFT JOIN message_media mm ON mm.message_row_id = m._id
    {mvom_join}
    LEFT JOIN chat c ON c._id = m.chat_row_id
    LEFT JOIN jid j_chat ON j_chat._id = c.jid_row_id
    LEFT JOIN jid j ON j._id = m.sender_jid_row_id
    {_ANDROID_JID_MAP}
    {ldn_sender_joins}
    {ms_join}
    {mrev_join}
    LEFT JOIN _contacts_resolved con_s  ON con_s.clean_number  = {_clean_user_sql("COALESCE(j2.user, j.user)")}
    """

    return QuerySet(
        android_select=android_select,
        android_stat_select=android_stat_select,
        android_filter=android_filter,
        android_filter_ts=android_filter_ts,
        android_ts=_ANDROID_TS,
        android_is_media=_ANDROID_IS_MEDIA,
        android_is_link=_ANDROID_IS_LINK,
        android_is_document_undownloaded=_ANDROID_IS_DOCUMENT_UNDOWNLOADED,
        android_gif_cond=android_gif_cond,
        ios_select=ios_select,
        ios_filter=ios_filter,
        ios_filter_ts=ios_filter_ts,
        ios_ts=_IOS_TS,
        ios_is_media=_IOS_IS_MEDIA,
        ios_is_link=ios_is_link,
        ios_is_document_undownloaded=_IOS_IS_DOCUMENT_UNDOWNLOADED,
    )
