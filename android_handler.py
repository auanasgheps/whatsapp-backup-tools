import logging
import os
import re
import sqlite3

# ==============================================================================
# android_handler.py — WhatsApp Android schema handling
# Knows nothing about ADB or backup format; handles only WhatsApp DB schema.
# ==============================================================================

_REQUIRED_TABLES = {'message', 'message_media', 'chat', 'jid', 'jid_map'}

_REQUIRED_COLUMNS = {
    'message':       {'_id', 'timestamp', 'sender_jid_row_id', 'from_me'},
    'message_media': {'file_path', 'mime_type', 'chat_row_id', 'message_row_id',
                      'message_url', 'media_name'},
    'chat':          {'_id', 'subject', 'jid_row_id'},
    'jid':           {'_id', 'user'},
    'jid_map':       {'lid_row_id', 'jid_row_id'},
}

_MEDIA_SUBFOLDERS = {
    'WhatsApp Images', 'WhatsApp Video', 'WhatsApp Audio',
    'WhatsApp Voice Notes', 'WhatsApp Video Notes', 'WhatsApp Animated Gifs',
    'WhatsApp Documents',
}


def validate_schema(cursor: sqlite3.Cursor, logger: logging.Logger):
    """Abort with a clear error if any required table or column is missing."""
    tables = {row[0] for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing_tables = _REQUIRED_TABLES - tables
    if missing_tables:
        logger.error(
            f"Schema validation failed — missing tables: {sorted(missing_tables)}"
        )
        raise SystemExit(1)

    for table, required_cols in _REQUIRED_COLUMNS.items():
        actual_cols = {row[1] for row in cursor.execute(
            f"PRAGMA table_info({table})"
        )}
        missing_cols = required_cols - actual_cols
        if missing_cols:
            logger.error(
                f"Schema validation failed — missing columns in '{table}': "
                f"{sorted(missing_cols)}"
            )
            raise SystemExit(1)

    logger.info("Schema validated.")


def validate_wa_root(wa_roots: list, logger: logging.Logger):
    """
    Verify that each wa_root is the correct WhatsApp folder (the one containing Media/).
    Aborts with a diagnostic hint for the most common mistakes.
    Raises SystemExit(1) only if none of the roots are valid.
    """
    any_valid = False
    for wa_root in wa_roots:
        if not os.path.isdir(wa_root):
            logger.error(f"--wa_root does not exist or is not a directory: {wa_root}")
            continue

        media_dir = os.path.join(wa_root, 'Media')
        if not os.path.isdir(media_dir):
            folder_name = os.path.basename(os.path.normpath(wa_root))
            if folder_name == 'Media':
                hint = "It looks like you passed the Media/ folder — pass its parent instead."
            elif any(os.path.isdir(os.path.join(wa_root, s)) for s in _MEDIA_SUBFOLDERS):
                hint = ("It looks like you passed a subfolder inside Media/ — "
                        "pass the WhatsApp/ folder that contains Media/ instead.")
            else:
                hint = "Expected structure: <wa_root>/Media/WhatsApp Images/ etc."
            logger.error(f"--wa_root has no Media/ subfolder: {wa_root}\n  {hint}")
            continue

        found = [s for s in _MEDIA_SUBFOLDERS if os.path.isdir(os.path.join(media_dir, s))]
        if not found:
            logger.warning(
                f"Media/ found but no WhatsApp media subfolders detected under {media_dir}. "
                f"Expected at least one of: {sorted(_MEDIA_SUBFOLDERS)}"
            )
        any_valid = True

    if not any_valid:
        logger.error("No valid --wa_root found. Aborting.")
        raise SystemExit(1)


def build_number_map(cursor: sqlite3.Cursor,
                     logger: logging.Logger) -> dict:
    """
    Build a map of old_number -> new_number from WhatsApp's own
    number change records. This ensures all media for a contact
    that changed their number ends up in one folder.

    Chains are resolved: if A->B and B->C, A maps to C.
    """
    try:
        cursor.execute("""
            SELECT jid_old.user, jid_new.user
            FROM message_system_number_change mnc
            LEFT JOIN jid AS jid_old ON jid_old._id = mnc.old_jid_row_id
            LEFT JOIN jid AS jid_new ON jid_new._id = mnc.new_jid_row_id
            WHERE jid_old.user IS NOT NULL
            AND jid_new.user IS NOT NULL
        """)
        raw_pairs = cursor.fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"Number-change table unavailable ({e}); skipping consolidation.")
        return {}

    direct = {old: new for old, new in raw_pairs}
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
        logger.debug(f"Number change chains: {len(cyclic)} cycle(s) ignored.")
    logger.info(
        f"Number consolidation map built: {len(consolidated)} "
        f"old number(s) mapped to current numbers."
    )
    return consolidated


def build_group_subjects_query() -> str:
    """
    Lightweight query returning the current subject for every group chat.
    Used to detect renames before processing begins.
    Intentionally unfiltered — rename detection must cover all known groups,
    not just those active since --since.
    """
    return """
        SELECT DISTINCT CAST(message_media.chat_row_id AS TEXT), chat.subject
        FROM message_media
        JOIN chat    ON message_media.chat_row_id    = chat._id
        JOIN message ON message_media.message_row_id = message._id
        WHERE chat.subject IS NOT NULL
    """


def build_query(limit: int | None, since_ms: int | None) -> str:
    """
    Build the main media extraction query.
    If limit is provided, it is split evenly between the group chats and
    1-to-1 chats blocks (LIMIT N//2 each), so both types are always
    represented. Total rows returned is at most N.
    If since_ms is provided, only messages at or after that timestamp
    (milliseconds) are included.
    """
    block_limit_clause = f"LIMIT {limit // 2}" if limit else ""
    since_clause = f"AND message.timestamp >= {since_ms}" if since_ms else ""

    return f"""
SELECT * FROM (
    SELECT * FROM (
        -- Group chats
        SELECT
            message._id                         AS message_id,
            message.timestamp                   AS timestamp,
            message_media.file_path             AS file_path,
            message_media.mime_type             AS mime_type,
            message_media.chat_row_id           AS chat_row_id,
            chat.subject                        AS chat_subject,
            ifnull(jid2.user, jid.user)         AS sender,
            message.from_me                     AS key_from_me,
            message_media.message_url           AS message_url,
            CASE WHEN message_media.file_path LIKE 'Media/WhatsApp Documents/%'
                 THEN message_media.media_name END AS media_name
        FROM message_media
        LEFT JOIN chat    ON message_media.chat_row_id    = chat._id
        LEFT JOIN message ON message_media.message_row_id = message._id
        LEFT JOIN jid     ON jid._id = message.sender_jid_row_id
        LEFT JOIN (
            SELECT lid_row_id, MIN(jid_row_id) AS jid_row_id
            FROM jid_map
            GROUP BY lid_row_id
        ) jid_map ON jid_map.lid_row_id = message.sender_jid_row_id
        LEFT JOIN jid AS jid2 ON jid2._id = jid_map.jid_row_id
        WHERE (
            message_media.file_path LIKE 'Media/WhatsApp Images/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Video/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Audio/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Voice Notes/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Video Notes/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Animated Gifs/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Documents/%'
        )
        AND chat.subject IS NOT NULL
        {since_clause}
        {block_limit_clause}
    )

    UNION ALL

    SELECT * FROM (
        -- 1-to-1 chats
        SELECT
            message._id                         AS message_id,
            message.timestamp                   AS timestamp,
            message_media.file_path             AS file_path,
            message_media.mime_type             AS mime_type,
            message_media.chat_row_id           AS chat_row_id,
            NULL                                AS chat_subject,
            jid.user                            AS sender,
            message.from_me                     AS key_from_me,
            message_media.message_url           AS message_url,
            CASE WHEN message_media.file_path LIKE 'Media/WhatsApp Documents/%'
                 THEN message_media.media_name END AS media_name
        FROM message_media
        LEFT JOIN chat    ON message_media.chat_row_id    = chat._id
        LEFT JOIN message ON message_media.message_row_id = message._id
        LEFT JOIN jid     ON jid._id = chat.jid_row_id
        WHERE (
            message_media.file_path LIKE 'Media/WhatsApp Images/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Video/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Audio/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Voice Notes/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Video Notes/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Animated Gifs/%'
            OR message_media.file_path LIKE 'Media/WhatsApp Documents/%'
        )
        AND chat.subject IS NULL
        {since_clause}
        {block_limit_clause}
    )
)
"""


def load_contacts(file_path: str, logger: logging.Logger) -> dict[str, str]:
    """
    Load Android contacts from an ADB-exported contacts file.
    Returns {phone_number: display_name} — same format as ios_handler.load_ios_contacts.
    """
    if not file_path:
        return {}
    try:
        with open(file_path, encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError as e:
        logger.warning(f"Could not read contacts file ({e}); proceeding without names.")
        return {}
    contacts = {}
    for name, number in re.findall(
            r"display_name=(.+?), data1=([^@\n\r]+)", content):
        contacts[number.strip()] = name.strip()
    if not contacts:
        logger.warning(
            "Contacts file was read but no WhatsApp contacts were found. "
            "Folder names will show raw phone numbers.\n"
            "  If using --mode adb, check that the device contacts permission is granted."
        )
    else:
        logger.info(f"Loaded {len(contacts)} Android contacts.")
    return contacts
