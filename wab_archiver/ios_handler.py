import contextlib
import logging
import os
import sqlite3

from shared.schema_validator import (
    _IOS_REQUIRED_COLUMNS,
    _IOS_REQUIRED_TABLES,
    check_wa_schema,
    get_app_version,
)

# ==============================================================================
# ios_handler.py — WhatsApp iOS schema handling
# Knows nothing about the iPhone backup format; handles only WhatsApp DB schema.
# ==============================================================================

# Seconds between 1970-01-01 (Unix epoch) and 2001-01-01 (Apple Core Data epoch)
APPLE_EPOCH_OFFSET = 978307200

_IOS_REQUIRED_TABLES = _IOS_REQUIRED_TABLES
_IOS_REQUIRED_COLUMNS = _IOS_REQUIRED_COLUMNS


ASSOCIATION_TYPE_IOS_HD_PHOTO = 10
ASSOCIATION_TYPE_IOS_HD_VIDEO = 5


def validate_ios_schema(cursor: sqlite3.Cursor, logger: logging.Logger):
    """Abort with a clear error if any required iOS table or column is missing."""
    tool_version = get_app_version()
    result = check_wa_schema(cursor, "ios", tool_version)
    if not result.is_valid:
        logger.error(result.banner)
        raise SystemExit(1)
    logger.info("iOS schema validated.")


def validate_ios_wa_root(wa_root: str, logger: logging.Logger):
    """
    Validate that wa_root is a pre-extracted iOS WhatsApp shared container.
    Checks for the Message/ subdirectory that holds iOS media.
    Used only in pre-extracted mode (--wa_root without --ios_backup).
    """
    if not os.path.isdir(wa_root):
        logger.error(f"--wa-root does not exist or is not a directory: {wa_root}")
        raise SystemExit(1)

    message_dir = os.path.join(wa_root, "Message")
    if not os.path.isdir(message_dir):
        logger.error(
            f"--wa-root has no Message/ subfolder: {wa_root}\n"
            f"  For iOS pre-extracted mode, pass the "
            f"AppDomainGroup-group.net.whatsapp.WhatsApp.shared folder."
        )
        raise SystemExit(1)


def build_ios_group_subjects_query() -> str:
    """
    Lightweight query returning the current subject for every group chat.
    Used to detect renames before processing begins.
    Intentionally unfiltered — rename detection must cover all known groups,
    not just those active since --since.
    """
    return """
        SELECT DISTINCT CAST(cs.Z_PK AS TEXT), cs.ZPARTNERNAME
        FROM ZWACHATSESSION cs
        JOIN ZWAMESSAGE m ON m.ZCHATSESSION = cs.Z_PK
        JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
        WHERE cs.ZGROUPINFO IS NOT NULL
          AND cs.ZPARTNERNAME IS NOT NULL
          AND mi.ZMEDIALOCALPATH IS NOT NULL
          AND mi.ZMEDIALOCALPATH NOT LIKE '%@status%'
    """


def find_ios_ext_db(candidate_dirs: list[str]) -> str | None:
    """Find ExtChatDatabase.sqlite in candidate directories."""
    for d in candidate_dirs:
        if not d:
            continue
        p1 = os.path.join(d, "ExtChatDatabase.sqlite")
        if os.path.isfile(p1):
            return p1
        p2 = os.path.join(d, "Whatsapp Databases", "ExtChatDatabase.sqlite")
        if os.path.isfile(p2):
            return p2
        p3 = os.path.join(d, "ExtChatDB", "ExtChatDatabase.sqlite")
        if os.path.isfile(p3):
            return p3
    return None


def check_ios_hd_association(
    cursor: sqlite3.Cursor, ext_db_path: str | None, logger: logging.Logger
) -> bool:
    """
    Check if ExtChatDatabase is available and has HD media associations (types 10 or 5).
    Attaches the database as 'ext' to the cursor connection if valid.
    """
    if not ext_db_path or not os.path.isfile(ext_db_path):
        return False

    try:
        cursor.execute("ATTACH DATABASE ? AS ext", (ext_db_path,))
        table_row = cursor.execute(
            "SELECT 1 FROM ext.sqlite_master WHERE type='table' AND name='message_parent_association'"
        ).fetchone()
        if not table_row:
            return False

        has_hd = cursor.execute(
            f"SELECT 1 FROM ext.message_parent_association WHERE type IN ({ASSOCIATION_TYPE_IOS_HD_PHOTO}, {ASSOCIATION_TYPE_IOS_HD_VIDEO}) LIMIT 1"
        ).fetchone()
        if has_hd:
            logger.info(
                "Found ExtChatDatabase with HD media associations. HD deduplication enabled."
            )
            return True
        return False
    except sqlite3.OperationalError as e:
        logger.debug(
            f"Could not inspect ExtChatDatabase ({e}); proceeding without HD deduplication."
        )
        return False


def has_ios_thumb_support(cursor: sqlite3.Cursor) -> bool:
    """Return True if ZWAMEDIAITEM table exists and has ZXMPPTHUMBPATH column."""
    tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ZWAMEDIAITEM" not in tables:
        return False
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(ZWAMEDIAITEM)")}
    return "ZXMPPTHUMBPATH" in cols


def build_ios_query(
    limit: int | None, since_ms: int | None, hd_dedup: bool, has_thumb: bool
) -> str:
    """
    Main iOS media extraction query. Returns the same 10 columns as the Android
    query so process_rows() is unchanged, with an optional 11th column for thumb_path.

    Columns:
        message_id   — ZWAMESSAGE.Z_PK
        timestamp_ms — (ZMESSAGEDATE + 978307200) * 1000 as Unix epoch ms
        file_path    — 'Message/' || ZMEDIALOCALPATH  (matches Manifest.db relativePath)
        mime_type    — NULL (ZWAMEDIAITEM has no ZMIMETYPE)
        chat_row_id  — ZWACHATSESSION.Z_PK cast to text
        chat_subject — ZPARTNERNAME for groups, NULL for 1-to-1
        sender       — phone number stripped from JID
        key_from_me  — ZWAMESSAGE.ZISFROMME
        message_url  — ZWAMEDIAITEM.ZMEDIAURL
        media_name   — ZWAMEDIAITEM.ZTITLE (original filename for documents)
        thumb_path   — 'Message/' || ZXMPPTHUMBPATH (optional 11th column)
    """
    block_limit_clause = f"LIMIT {limit // 2}" if limit else ""
    if since_ms is not None:
        ios_since = (since_ms / 1000.0) - APPLE_EPOCH_OFFSET
        since_clause = f"AND m.ZMESSAGEDATE >= {ios_since}"
    else:
        since_clause = ""

    hd_dedup_clause = (
        f"""
        AND NOT EXISTS (
            SELECT 1 FROM ext.message_parent_association mpa
            JOIN ZWAMESSAGE m_hd ON m_hd.ZSTANZAID = mpa.stanza_id
            JOIN ZWAMEDIAITEM mi_hd ON mi_hd.Z_PK = m_hd.ZMEDIAITEM
            WHERE mpa.parent_stanza_id = m.ZSTANZAID
              AND mpa.type IN ({ASSOCIATION_TYPE_IOS_HD_PHOTO}, {ASSOCIATION_TYPE_IOS_HD_VIDEO})
              AND mi_hd.ZMEDIALOCALPATH IS NOT NULL
        )"""
        if hd_dedup
        else ""
    )

    thumb_col = (
        ",\n            CASE WHEN mi.ZXMPPTHUMBPATH IS NOT NULL THEN 'Message/' || mi.ZXMPPTHUMBPATH ELSE NULL END AS thumb_path"
        if has_thumb
        else ""
    )
    thumb_filter = "OR mi.ZXMPPTHUMBPATH IS NOT NULL" if has_thumb else ""
    file_path_expr = (
        "CASE WHEN mi.ZMEDIALOCALPATH IS NOT NULL THEN 'Message/' || mi.ZMEDIALOCALPATH ELSE NULL END AS file_path"
        if has_thumb
        else "'Message/' || mi.ZMEDIALOCALPATH                               AS file_path"
    )

    return f"""
SELECT * FROM (
    SELECT * FROM (
        -- Group chats
        SELECT
            m.Z_PK                                                         AS message_id,
            CAST((m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET}) * 1000 AS INTEGER) AS timestamp_ms,
            {file_path_expr},
            NULL                                                           AS mime_type,
            CAST(cs.Z_PK AS TEXT)                                          AS chat_row_id,
            cs.ZPARTNERNAME                                                AS chat_subject,
            CASE
                 WHEN gm.ZMEMBERJID IS NOT NULL AND INSTR(gm.ZMEMBERJID, '@') > 0
                 THEN SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID, '@') - 1)
                 WHEN gm.ZMEMBERJID IS NOT NULL
                 THEN gm.ZMEMBERJID
                 ELSE m.ZPUSHNAME END                                      AS sender,
            m.ZISFROMME                                                    AS key_from_me,
            mi.ZMEDIAURL                                                   AS message_url,
            CASE WHEN mi.ZMEDIALOCALPATH LIKE '%Documents%'
                 THEN mi.ZTITLE END                                        AS media_name{thumb_col}
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        JOIN ZWAMEDIAITEM   mi ON mi.Z_PK = m.ZMEDIAITEM
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        WHERE cs.ZGROUPINFO IS NOT NULL
          AND (
              (mi.ZMEDIALOCALPATH IS NOT NULL AND mi.ZMEDIALOCALPATH NOT LIKE '%@status%')
              {thumb_filter}
          )
          AND cs.ZPARTNERNAME IS NOT NULL
          {since_clause}
          {hd_dedup_clause}
        {block_limit_clause}
    )

    UNION ALL

    SELECT * FROM (
        -- 1-to-1 chats
        SELECT
            m.Z_PK                                                         AS message_id,
            CAST((m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET}) * 1000 AS INTEGER) AS timestamp_ms,
            {file_path_expr},
            NULL                                                           AS mime_type,
            CAST(cs.Z_PK AS TEXT)                                          AS chat_row_id,
            NULL                                                           AS chat_subject,
            CASE WHEN INSTR(cs.ZCONTACTJID, '@') > 0
                 THEN SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1)
                 ELSE cs.ZCONTACTJID END                                    AS sender,
            m.ZISFROMME                                                    AS key_from_me,
            mi.ZMEDIAURL                                                   AS message_url,
            CASE WHEN mi.ZMEDIALOCALPATH LIKE '%Documents%'
                 THEN mi.ZTITLE END                                        AS media_name{thumb_col}
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        JOIN ZWAMEDIAITEM   mi ON mi.Z_PK = m.ZMEDIAITEM
        WHERE cs.ZGROUPINFO IS NULL
          AND (
              (mi.ZMEDIALOCALPATH IS NOT NULL AND mi.ZMEDIALOCALPATH NOT LIKE '%@status%')
              {thumb_filter}
          )
          {since_clause}
          {hd_dedup_clause}
        {block_limit_clause}
    )
)
"""


def has_ios_vcard_support(cursor: sqlite3.Cursor) -> bool:
    """Return True if ZWAMEDIAITEM table exists and has ZVCARDSTRING column."""
    tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ZWAMEDIAITEM" not in tables:
        return False
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(ZWAMEDIAITEM)")}
    return "ZVCARDSTRING" in cols


def build_ios_vcard_query(limit: int | None, since_ms: int | None) -> str:
    """
    Build query to extract vCard contacts from iOS ChatStorage.sqlite.
    Returns:
        message_id   — ZWAMESSAGE.Z_PK
        timestamp_ms — Unix epoch ms
        chat_row_id  — CAST(cs.Z_PK AS TEXT)
        chat_subject — cs.ZPARTNERNAME for groups, NULL for 1-to-1
        sender       — phone number stripped from JID
        key_from_me  — m.ZISFROMME
        contact_name — mi.ZVCARDNAME
        vcard_text   — mi.ZVCARDSTRING
    """
    block_limit_clause = f"LIMIT {limit // 2}" if limit else ""
    if since_ms is not None:
        ios_since = (since_ms / 1000.0) - APPLE_EPOCH_OFFSET
        since_clause = f"AND m.ZMESSAGEDATE >= {ios_since}"
    else:
        since_clause = ""

    return f"""
SELECT * FROM (
    SELECT * FROM (
        -- Group chats
        SELECT
            m.Z_PK                                                         AS message_id,
            CAST((m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET}) * 1000 AS INTEGER) AS timestamp_ms,
            CAST(cs.Z_PK AS TEXT)                                          AS chat_row_id,
            cs.ZPARTNERNAME                                                AS chat_subject,
            CASE
                 WHEN gm.ZMEMBERJID IS NOT NULL AND INSTR(gm.ZMEMBERJID, '@') > 0
                 THEN SUBSTR(gm.ZMEMBERJID, 1, INSTR(gm.ZMEMBERJID, '@') - 1)
                 WHEN gm.ZMEMBERJID IS NOT NULL
                 THEN gm.ZMEMBERJID
                 ELSE m.ZPUSHNAME END                                      AS sender,
            m.ZISFROMME                                                    AS key_from_me,
            mi.ZVCARDNAME                                                  AS contact_name,
            mi.ZVCARDSTRING                                                AS vcard_text
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        JOIN ZWAMEDIAITEM   mi ON mi.Z_PK = m.ZMEDIAITEM
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        WHERE m.ZMESSAGETYPE = 4
          AND cs.ZGROUPINFO IS NOT NULL
          AND mi.ZVCARDSTRING IS NOT NULL AND mi.ZVCARDSTRING != ''
          {since_clause}
        {block_limit_clause}
    )

    UNION ALL

    SELECT * FROM (
        -- 1-to-1 chats
        SELECT
            m.Z_PK                                                         AS message_id,
            CAST((m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET}) * 1000 AS INTEGER) AS timestamp_ms,
            CAST(cs.Z_PK AS TEXT)                                          AS chat_row_id,
            NULL                                                           AS chat_subject,
            CASE
                 WHEN cs.ZCONTACTJID IS NOT NULL AND INSTR(cs.ZCONTACTJID, '@') > 0
                 THEN SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1)
                 WHEN cs.ZCONTACTJID IS NOT NULL
                 THEN cs.ZCONTACTJID
                 ELSE m.ZPUSHNAME END                                      AS sender,
            m.ZISFROMME                                                    AS key_from_me,
            mi.ZVCARDNAME                                                  AS contact_name,
            mi.ZVCARDSTRING                                                AS vcard_text
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
        JOIN ZWAMEDIAITEM   mi ON mi.Z_PK = m.ZMEDIAITEM
        WHERE m.ZMESSAGETYPE = 4
          AND cs.ZGROUPINFO IS NULL
          AND mi.ZVCARDSTRING IS NOT NULL AND mi.ZVCARDSTRING != ''
          {since_clause}
        {block_limit_clause}
    )
)
ORDER BY timestamp_ms ASC
"""


def build_ios_number_map(cursor: sqlite3.Cursor, logger: logging.Logger) -> dict[str, str]:
    """
    Build a map of old_number -> new_number for contacts that changed their
    WhatsApp number. Uses ZCONTACTABID session grouping: when a contact changes
    their number, WhatsApp creates a new session sharing the same ZCONTACTABID.
    Ordering by ZLASTMESSAGEDATE gives the old -> new direction.

    Only ZCONTACTABID values shared by exactly 2 sessions are considered — the
    clean "one person changed their number once" case. Groups of 3+ sessions
    sharing the same ZCONTACTABID are skipped to avoid misrouting media from
    unrelated contacts whose address book entries were merged.

    Best-effort: only covers contacts present in the device address book.
    Contacts without an address book entry (ZCONTACTABID = NULL) are not
    consolidated — they appear as separate folders.

    Chains are resolved: A->B->C becomes A->C.
    """
    try:
        cursor.execute("""
            SELECT
                CASE WHEN INSTR(cs_old.ZCONTACTJID, '@') > 0
                     THEN SUBSTR(cs_old.ZCONTACTJID, 1, INSTR(cs_old.ZCONTACTJID, '@') - 1)
                     ELSE cs_old.ZCONTACTJID END,
                CASE WHEN INSTR(cs_new.ZCONTACTJID, '@') > 0
                     THEN SUBSTR(cs_new.ZCONTACTJID, 1, INSTR(cs_new.ZCONTACTJID, '@') - 1)
                     ELSE cs_new.ZCONTACTJID END
            FROM ZWACHATSESSION cs_old
            JOIN ZWACHATSESSION cs_new
              ON  cs_old.ZCONTACTABID = cs_new.ZCONTACTABID
              AND cs_old.ZCONTACTJID  != cs_new.ZCONTACTJID
              AND cs_old.ZLASTMESSAGEDATE < cs_new.ZLASTMESSAGEDATE
            WHERE cs_old.ZCONTACTABID IS NOT NULL
              AND cs_old.ZGROUPINFO IS NULL
              AND cs_new.ZGROUPINFO IS NULL
              AND cs_old.ZCONTACTABID IN (
                  SELECT ZCONTACTABID
                  FROM ZWACHATSESSION
                  WHERE ZCONTACTABID IS NOT NULL
                    AND ZGROUPINFO IS NULL
                  GROUP BY ZCONTACTABID
                  HAVING COUNT(*) = 2
              )
        """)
        raw_pairs = cursor.fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"iOS number-change query unavailable ({e}); skipping consolidation.")
        raw_pairs = []

    direct = {old: new for old, new in raw_pairs if old and new}
    try:
        cs_cols = {r[1] for r in cursor.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZCONTACTIDENTIFIER" in cs_cols:
            lid_pairs = cursor.execute("""
                SELECT
                    CASE WHEN INSTR(ZCONTACTJID, '@') > 0
                         THEN SUBSTR(ZCONTACTJID, 1, INSTR(ZCONTACTJID, '@') - 1)
                         ELSE ZCONTACTJID END,
                    CASE WHEN INSTR(ZCONTACTIDENTIFIER, '@') > 0
                         THEN SUBSTR(ZCONTACTIDENTIFIER, 1, INSTR(ZCONTACTIDENTIFIER, '@') - 1)
                         ELSE ZCONTACTIDENTIFIER END
                FROM ZWACHATSESSION
                WHERE ZCONTACTJID LIKE '%@lid'
                  AND ZCONTACTIDENTIFIER LIKE '%@s.whatsapp.net'
            """).fetchall()
            for lid, phone in lid_pairs:
                if lid and phone:
                    direct[lid] = phone

            lid_pairs_inv = cursor.execute("""
                SELECT
                    CASE WHEN INSTR(ZCONTACTIDENTIFIER, '@') > 0
                         THEN SUBSTR(ZCONTACTIDENTIFIER, 1, INSTR(ZCONTACTIDENTIFIER, '@') - 1)
                         ELSE ZCONTACTIDENTIFIER END,
                    CASE WHEN INSTR(ZCONTACTJID, '@') > 0
                         THEN SUBSTR(ZCONTACTJID, 1, INSTR(ZCONTACTJID, '@') - 1)
                         ELSE ZCONTACTJID END
                FROM ZWACHATSESSION
                WHERE ZCONTACTJID LIKE '%@s.whatsapp.net'
                  AND ZCONTACTIDENTIFIER LIKE '%@lid'
            """).fetchall()
            for lid, phone in lid_pairs_inv:
                if lid and phone:
                    direct[lid] = phone
    except sqlite3.OperationalError as e:
        logger.debug(f"ZCONTACTIDENTIFIER LID mapping query unavailable ({e}); skipping.")

    if not direct:
        return {}

    cyclic: set[str] = set()

    def resolve(number, visited=None):
        if visited is None:
            visited = set()
        if number in visited:
            cyclic.add(number)
            return number
        visited.add(number)
        if number in direct:
            return resolve(direct[number], visited)
        return number

    consolidated = {old: resolve(old) for old in direct}
    if cyclic:
        logger.debug(f"iOS number change chains: {len(cyclic)} cycle(s) ignored.")
    logger.info(
        f"iOS number consolidation map built: {len(consolidated)} "
        f"old number(s) mapped to current numbers."
    )
    return consolidated


def build_ios_pushname_map(cursor: sqlite3.Cursor, logger: logging.Logger) -> dict[str, str]:
    """
    Build a {jid_prefix: pushname} map from ZWAPROFILEPUSHNAME.
    Used as a fallback for contacts not in ContactsV2.sqlite — e.g. group
    members who are not saved in the device address book.
    """
    try:
        rows = cursor.execute(
            "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME "
            "WHERE ZJID IS NOT NULL AND ZPUSHNAME IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.debug(f"ZWAPROFILEPUSHNAME unavailable ({e}); skipping push names.")
        return {}

    result = {}
    for jid, name in rows:
        prefix = jid.split("@")[0] if "@" in jid else jid
        if prefix:
            result[prefix] = name
    logger.debug(f"iOS push name map: {len(result)} entries.")
    return result


def load_ios_contacts(sqlite_path: str, logger: logging.Logger) -> dict[str, str]:
    """
    Load WhatsApp contacts from ContactsV2.sqlite.
    Returns {phone_number: display_name} — same format as Android contacts dict.
    Phone numbers are stripped from JIDs (e.g. '393357214425@s.whatsapp.net' -> '393357214425').
    """
    if not sqlite_path:
        return {}

    if not os.path.isfile(sqlite_path):
        logger.warning(f"iOS contacts file not found: {sqlite_path}")
        return {}

    try:
        with contextlib.closing(sqlite3.connect(sqlite_path)) as conn:
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(ZWAADDRESSBOOKCONTACT)").fetchall()
            }
            rows = conn.execute("""
                SELECT SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1),
                       ZFULLNAME
                FROM ZWAADDRESSBOOKCONTACT
                WHERE ZWHATSAPPID LIKE '%@s.whatsapp.net'
                  AND ZFULLNAME IS NOT NULL
            """).fetchall()
            contacts = {number: name for number, name in rows if number}

            if "ZLID" in cols:
                lid_rows = conn.execute("""
                    SELECT SUBSTR(ZLID, 1, INSTR(ZLID, '@') - 1),
                           ZFULLNAME
                    FROM ZWAADDRESSBOOKCONTACT
                    WHERE ZLID LIKE '%@lid'
                      AND ZFULLNAME IS NOT NULL
                """).fetchall()
                for lid, name in lid_rows:
                    if lid and name:
                        contacts.setdefault(lid, name)
    except sqlite3.OperationalError as e:
        logger.warning(f"Could not read iOS contacts ({e}); proceeding without names.")
        return {}
    unique_contacts = len({name for name in contacts.values() if name})
    logger.info(f"Loaded {len(contacts)} phone number(s) across {unique_contacts} iOS contact(s).")
    contacts.setdefault("0", "WhatsApp")
    return contacts


def load_ios_lid_map(sqlite_path: str, logger: logging.Logger) -> dict[str, str]:
    """
    Load LID -> phone_number mapping from ContactsV2.sqlite (ZWAADDRESSBOOKCONTACT).
    Returns {lid_prefix: phone_number}.
    """
    if not sqlite_path or not os.path.isfile(sqlite_path):
        return {}

    try:
        with contextlib.closing(sqlite3.connect(sqlite_path)) as conn:
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(ZWAADDRESSBOOKCONTACT)").fetchall()
            }
            if "ZLID" not in cols or "ZWHATSAPPID" not in cols:
                return {}
            rows = conn.execute("""
                SELECT SUBSTR(ZLID, 1, INSTR(ZLID, '@') - 1),
                       SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1)
                FROM ZWAADDRESSBOOKCONTACT
                WHERE ZLID LIKE '%@lid'
                  AND ZWHATSAPPID LIKE '%@s.whatsapp.net'
            """).fetchall()
            return {lid: phone for lid, phone in rows if lid and phone}
    except sqlite3.OperationalError as e:
        logger.debug(f"Could not read iOS ContactsV2 LID mapping ({e}).")
        return {}
