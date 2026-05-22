import contextlib
import logging
import os
import sqlite3

# ==============================================================================
# ios_handler.py — WhatsApp iOS schema handling
# Knows nothing about the iPhone backup format; handles only WhatsApp DB schema.
# ==============================================================================

# Seconds between 1970-01-01 (Unix epoch) and 2001-01-01 (Apple Core Data epoch)
APPLE_EPOCH_OFFSET = 978307200

_IOS_REQUIRED_TABLES = {'ZWAMESSAGE', 'ZWACHATSESSION', 'ZWAMEDIAITEM'}

_IOS_REQUIRED_COLUMNS = {
    'ZWAMESSAGE': {
        'Z_PK', 'ZMESSAGEDATE', 'ZISFROMME',
        'ZCHATSESSION', 'ZMEDIAITEM', 'ZFROMJID',
    },
    'ZWACHATSESSION': {
        'Z_PK', 'ZCONTACTJID', 'ZGROUPINFO', 'ZPARTNERNAME',
    },
    'ZWAMEDIAITEM': {
        'Z_PK', 'ZMEDIALOCALPATH', 'ZMEDIAURL', 'ZTITLE',
    },
}


def validate_ios_schema(cursor: sqlite3.Cursor, logger: logging.Logger):
    """Abort with a clear error if any required iOS table or column is missing."""
    tables = {row[0] for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing_tables = _IOS_REQUIRED_TABLES - tables
    if missing_tables:
        logger.error(
            f"iOS schema validation failed — missing tables: {sorted(missing_tables)}"
        )
        raise SystemExit(1)

    for table, required_cols in _IOS_REQUIRED_COLUMNS.items():
        actual_cols = {row[1] for row in cursor.execute(
            f"PRAGMA table_info({table})"
        )}
        missing_cols = required_cols - actual_cols
        if missing_cols:
            logger.error(
                f"iOS schema validation failed — missing columns in '{table}': "
                f"{sorted(missing_cols)}"
            )
            raise SystemExit(1)

    logger.info("iOS schema validated.")


def validate_ios_wa_root(wa_root: str, logger: logging.Logger):
    """
    Validate that wa_root is a pre-extracted iOS WhatsApp shared container.
    Checks for the Message/ subdirectory that holds iOS media.
    Used only in pre-extracted mode (--wa_root without --ios_backup).
    """
    if not os.path.isdir(wa_root):
        logger.error(f"--wa_root does not exist or is not a directory: {wa_root}")
        raise SystemExit(1)

    message_dir = os.path.join(wa_root, 'Message')
    if not os.path.isdir(message_dir):
        logger.error(
            f"--wa_root has no Message/ subfolder: {wa_root}\n"
            f"  For iOS pre-extracted mode, pass the "
            f"AppDomainGroup-group.net.whatsapp.WhatsApp.shared folder."
        )
        raise SystemExit(1)


def build_ios_group_subjects_query(since_ms: int | None) -> str:
    """
    Lightweight query returning the current subject for every group chat in scope.
    Used to detect renames before processing begins.
    since_ms is Unix epoch milliseconds; converted to Apple epoch seconds internally.
    """
    if since_ms is not None:
        ios_since = (since_ms / 1000.0) - APPLE_EPOCH_OFFSET
        since_clause = f"AND m.ZMESSAGEDATE >= {ios_since}"
    else:
        since_clause = ""

    return f"""
        SELECT DISTINCT CAST(cs.Z_PK AS TEXT), cs.ZPARTNERNAME
        FROM ZWACHATSESSION cs
        JOIN ZWAMESSAGE m ON m.ZCHATSESSION = cs.Z_PK
        JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
        WHERE cs.ZGROUPINFO IS NOT NULL
          AND cs.ZPARTNERNAME IS NOT NULL
          AND mi.ZMEDIALOCALPATH IS NOT NULL
          {since_clause}
    """


def build_ios_query(limit: int | None, since_ms: int | None) -> str:
    """
    Main iOS media extraction query. Returns the same 10 columns as the Android
    query so process_rows() is unchanged.

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
    """
    limit_clause = f"LIMIT {limit}" if limit else ""
    if since_ms is not None:
        ios_since = (since_ms / 1000.0) - APPLE_EPOCH_OFFSET
        since_clause = f"AND m.ZMESSAGEDATE >= {ios_since}"
    else:
        since_clause = ""

    return f"""
SELECT * FROM (
    -- Group chats
    SELECT
        m.Z_PK                                                         AS message_id,
        CAST((m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET}) * 1000 AS INTEGER) AS timestamp_ms,
        'Message/' || mi.ZMEDIALOCALPATH                               AS file_path,
        NULL                                                           AS mime_type,
        CAST(cs.Z_PK AS TEXT)                                          AS chat_row_id,
        cs.ZPARTNERNAME                                                AS chat_subject,
        SUBSTR(m.ZFROMJID, 1, INSTR(m.ZFROMJID, '@') - 1)            AS sender,
        m.ZISFROMME                                                    AS key_from_me,
        mi.ZMEDIAURL                                                   AS message_url,
        CASE WHEN mi.ZMEDIALOCALPATH LIKE '%Documents%'
             THEN mi.ZTITLE END                                        AS media_name
    FROM ZWAMESSAGE m
    JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    JOIN ZWAMEDIAITEM   mi ON mi.Z_PK = m.ZMEDIAITEM
    WHERE cs.ZGROUPINFO IS NOT NULL
      AND mi.ZMEDIALOCALPATH IS NOT NULL
      AND cs.ZPARTNERNAME IS NOT NULL
      {since_clause}
    {limit_clause}
)

UNION ALL

SELECT * FROM (
    -- 1-to-1 chats
    SELECT
        m.Z_PK                                                         AS message_id,
        CAST((m.ZMESSAGEDATE + {APPLE_EPOCH_OFFSET}) * 1000 AS INTEGER) AS timestamp_ms,
        'Message/' || mi.ZMEDIALOCALPATH                               AS file_path,
        NULL                                                           AS mime_type,
        CAST(cs.Z_PK AS TEXT)                                          AS chat_row_id,
        NULL                                                           AS chat_subject,
        SUBSTR(cs.ZCONTACTJID, 1, INSTR(cs.ZCONTACTJID, '@') - 1)    AS sender,
        m.ZISFROMME                                                    AS key_from_me,
        mi.ZMEDIAURL                                                   AS message_url,
        CASE WHEN mi.ZMEDIALOCALPATH LIKE '%Documents%'
             THEN mi.ZTITLE END                                        AS media_name
    FROM ZWAMESSAGE m
    JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    JOIN ZWAMEDIAITEM   mi ON mi.Z_PK = m.ZMEDIAITEM
    WHERE cs.ZGROUPINFO IS NULL
      AND mi.ZMEDIALOCALPATH IS NOT NULL
      {since_clause}
    {limit_clause}
)
"""


def build_ios_number_map(cursor: sqlite3.Cursor,
                         logger: logging.Logger) -> dict[str, str]:
    """
    Build a map of old_number -> new_number for contacts that changed their
    WhatsApp number. Uses ZCONTACTABID session grouping: when a contact changes
    their number, WhatsApp creates a new session sharing the same ZCONTACTABID.
    Ordering by ZLASTMESSAGEDATE gives the old -> new direction.

    Best-effort: only covers contacts present in the device address book.
    Contacts without an address book entry (ZCONTACTABID = NULL) are not
    consolidated — they appear as separate folders.

    Chains are resolved: A->B->C becomes A->C.
    """
    try:
        cursor.execute("""
            SELECT
                SUBSTR(cs_old.ZCONTACTJID, 1, INSTR(cs_old.ZCONTACTJID, '@') - 1),
                SUBSTR(cs_new.ZCONTACTJID, 1, INSTR(cs_new.ZCONTACTJID, '@') - 1)
            FROM ZWACHATSESSION cs_old
            JOIN ZWACHATSESSION cs_new
              ON  cs_old.ZCONTACTABID = cs_new.ZCONTACTABID
              AND cs_old.ZCONTACTJID  != cs_new.ZCONTACTJID
              AND cs_old.ZLASTMESSAGEDATE < cs_new.ZLASTMESSAGEDATE
            WHERE cs_old.ZCONTACTABID IS NOT NULL
              AND cs_old.ZGROUPINFO IS NULL
              AND cs_new.ZGROUPINFO IS NULL
        """)
        raw_pairs = cursor.fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"iOS number-change query unavailable ({e}); skipping consolidation.")
        return {}

    if not raw_pairs:
        return {}

    direct = {old: new for old, new in raw_pairs if old and new}

    def resolve(number, visited=None):
        if visited is None:
            visited = set()
        if number in visited:
            logger.warning(f"Cycle detected in iOS number change chain: {number}")
            return number
        visited.add(number)
        if number in direct:
            return resolve(direct[number], visited)
        return number

    consolidated = {old: resolve(old) for old in direct}
    logger.info(
        f"iOS number consolidation map built: {len(consolidated)} "
        f"old number(s) mapped to current numbers."
    )
    return consolidated


def load_ios_contacts(sqlite_path: str,
                      logger: logging.Logger) -> dict[str, str]:
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
            rows = conn.execute("""
                SELECT SUBSTR(ZWHATSAPPID, 1, INSTR(ZWHATSAPPID, '@') - 1),
                       ZFULLNAME
                FROM ZWAADDRESSBOOKCONTACT
                WHERE ZWHATSAPPID LIKE '%@s.whatsapp.net'
                  AND ZFULLNAME IS NOT NULL
            """).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"Could not read iOS contacts ({e}); proceeding without names.")
        return {}

    contacts = {number: name for number, name in rows if number}
    logger.info(f"Loaded {len(contacts)} iOS contacts.")
    return contacts
