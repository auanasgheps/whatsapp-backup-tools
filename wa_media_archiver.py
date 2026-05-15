import atexit
import argparse
import contextlib
import csv
import hashlib
import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime

# ==============================================================================
# WA Media Archiver — v0.15
# Archives WhatsApp media into a structured folder hierarchy using msgstore.db.
# Run on a backup copy of your WhatsApp data.
# Requires Python 3.10+.
# ==============================================================================

__version__ = '0.15'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a name."""
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def escape_like(s: str) -> str:
    """Escape SQLite LIKE special characters so the string is treated literally."""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def format_phone(number: str) -> str:
    """
    Format a phone number for display in folder names.
    The DB stores numbers without '+', so we prepend '00'.
    """
    return '00' + number if number else ''


def build_contact_folder_name(display_name: str, number: str) -> str:
    """
    Build folder name: 'Display Name (00391234567890)'
    Falls back to 'Unknown (00391234567890)' if contact is not resolved.
    """
    formatted = format_phone(number)
    if not display_name or display_name == number:
        label = f"Unknown ({formatted})"
    else:
        label = f"{display_name} ({formatted})"
    return sanitize_filename(label)


def file_md5(path: str) -> bytes:
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.digest()


def set_file_times(path: str, timestamp_ms: int):
    """Set file access and modification time from a WhatsApp timestamp (ms)."""
    ts = timestamp_ms / 1000.0
    os.utime(path, (ts, ts))


def get_year(timestamp_ms: int) -> str:
    """Extract year string from a WhatsApp timestamp (ms)."""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime('%Y')


def human_datetime(timestamp_ms: int) -> str:
    """Convert WhatsApp timestamp (ms) to a human-readable string."""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')


def append_sender_to_filename(filename: str, sender: str) -> str:
    """
    Append sender name to a filename before the extension.
    e.g. IMG-20260512-WA0003.jpg -> IMG-20260512-WA0003_JohnDoe.jpg
    """
    root, ext = os.path.splitext(filename)
    safe_sender = sanitize_filename(sender)
    return f"{root}_{safe_sender}{ext}"


def resolve_unique_dest(dest_path: str, src_path: str,
                        logger: logging.Logger,
                        src_hash: bytes | None = None) -> str | None:
    """
    Determine whether to copy, skip, or rename.

    Returns:
        - dest_path       → file does not exist, safe to copy
        - modified path   → collision with different content, append counter suffix
        - None            → identical file already exists, skip silently
    """
    if not os.path.exists(dest_path):
        return dest_path

    if src_hash is None:
        src_hash = file_md5(src_path)
    dst_hash = file_md5(dest_path)

    if src_hash == dst_hash:
        logger.debug(f"SKIP (identical already exists): {dest_path}")
        return None

    logger.warning(f"COLLISION (same name, different content): {dest_path}")
    base, ext = os.path.splitext(dest_path)
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not os.path.exists(candidate):
            logger.warning(f"  -> Renamed to: {candidate}")
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_path: str) -> logging.Logger:
    """Configure logging to both file (DEBUG) and console (INFO)."""
    logger = logging.getLogger('wa_media_archiver')
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

        fh = logging.FileHandler(log_path, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)

        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# Missing media CSV report
# ---------------------------------------------------------------------------

def write_missing_report(report_path: str, rows: list, logger: logging.Logger):
    """Write the collected missing media rows to a CSV file."""
    if not rows:
        logger.info("No missing media entries to report.")
        return
    fieldnames = [
        'message_id', 'timestamp_human', 'original_filename',
        'mime_type', 'sender', 'chat_name', 'direction',
        'file_path', 'message_url', 'media_name',
    ]
    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(
        f"Missing media report written to: {report_path} "
        f"({len(rows)} entries)"
    )


# ---------------------------------------------------------------------------
# Duplicate media CSV report
# ---------------------------------------------------------------------------

def write_duplicate_report(report_path: str, conn: sqlite3.Connection,
                            logger: logging.Logger):
    """Write a CSV report of media files with identical content at multiple paths."""
    rows = conn.execute("""
        WITH dup_hashes AS (
            SELECT f.md5, COUNT(ac.archive_path) AS total
            FROM files f
            JOIN archive_copies ac ON ac.original_path = f.original_path
            GROUP BY f.md5
            HAVING total >= 2
        )
        SELECT dh.md5, dh.total, ac.archive_path
        FROM dup_hashes dh
        JOIN files f ON f.md5 = dh.md5
        JOIN archive_copies ac ON ac.original_path = f.original_path
        ORDER BY dh.total DESC, dh.md5
    """).fetchall()

    if not rows:
        logger.info("No duplicate media found.")
        return

    fieldnames = ['md5_hex', 'file_count', 'archived_path']
    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for md5_bytes, file_count, archived_path in rows:
            writer.writerow({
                'md5_hex':       md5_bytes.hex(),
                'file_count':    file_count,
                'archived_path': archived_path,
            })
    logger.info(
        f"Duplicate media report written to: {report_path} "
        f"({len({r[0] for r in rows})} group(s), {len(rows)} file(s))"
    )


# ---------------------------------------------------------------------------
# Archive DB
# ---------------------------------------------------------------------------

def open_archive_db(output_root: str) -> sqlite3.Connection:
    """
    Open (or create) the archive state database.
    Returns an open connection with foreign keys enabled.
    """
    db_path = os.path.join(output_root, '.wa_media_archiver.db')
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number  TEXT PRIMARY KEY,
            folder  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_row_id  TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            subject      TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS files (
            original_path  TEXT PRIMARY KEY,
            md5            BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path  TEXT NOT NULL REFERENCES files(original_path),
            archive_path   TEXT NOT NULL,
            PRIMARY KEY (original_path, archive_path)
        );
        CREATE INDEX IF NOT EXISTS idx_files_md5 ON files(md5);
    """)
    return conn


def load_contacts_from_db(conn: sqlite3.Connection) -> dict:
    """Load contact folder index: number -> folder."""
    return {row[0]: row[1]
            for row in conn.execute("SELECT number, folder FROM contacts")}


def save_contacts_to_db(conn: sqlite3.Connection, index: dict):
    """Persist contact folder index to DB."""
    conn.executemany(
        "INSERT OR REPLACE INTO contacts (number, folder) VALUES (?, ?)",
        index.items()
    )


def load_groups_from_db(conn: sqlite3.Connection) -> dict:
    """Load group folder index: str(chat_row_id) -> {folder, subject}."""
    return {
        row[0]: {'folder': row[1], 'subject': row[2]}
        for row in conn.execute(
            "SELECT chat_row_id, folder, subject FROM groups"
        )
    }


def save_groups_to_db(conn: sqlite3.Connection, index: dict):
    """Persist group folder index to DB."""
    conn.executemany(
        "INSERT OR REPLACE INTO groups (chat_row_id, folder, subject) VALUES (?, ?, ?)",
        ((key, val['folder'], val['subject']) for key, val in index.items())
    )


def record_file_archived(cursor: sqlite3.Cursor,
                         original_path: str, md5: bytes, archive_path: str):
    """Record that original_path was archived to archive_path with given md5."""
    cursor.execute(
        "INSERT INTO files (original_path, md5) VALUES (?, ?) "
        "ON CONFLICT(original_path) DO UPDATE SET md5 = excluded.md5",
        (original_path, md5)
    )
    cursor.execute(
        "INSERT OR IGNORE INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
        (original_path, archive_path)
    )


def check_db_health(conn: sqlite3.Connection, logger: logging.Logger):
    """Run quick integrity and foreign key checks; abort on failure."""
    results = conn.execute("PRAGMA quick_check").fetchall()
    if results != [('ok',)]:
        for row in results:
            logger.error(f"Database integrity issue: {row[0]}")
        raise SystemExit(1)

    issues = conn.execute("PRAGMA foreign_key_check").fetchall()
    if issues:
        for table, rowid, parent, fkid in issues:
            logger.error(
                f"Foreign key violation in '{table}': rowid={rowid}, "
                f"references '{parent}' (fk #{fkid})"
            )
        raise SystemExit(1)

    logger.debug("Database health check passed.")


def _unique_group_name(desired: str, existing: set) -> str:
    """Return desired if unused, else append ' (2)', ' (3)', ... until unique."""
    if desired not in existing:
        return desired
    counter = 2
    while True:
        candidate = f"{desired} ({counter})"
        if candidate not in existing:
            return candidate
        counter += 1


def sync_group_names(group_subjects: dict, output_root: str,
                     group_index: dict, logger: logging.Logger,
                     conn: sqlite3.Connection | None = None) -> dict:
    """
    Detect group renames since the last run and rename folders on disk.
    group_subjects: {str(chat_row_id): current chat_subject} built from query rows.
    Mirrors sync_folder_names behaviour for contacts.
    """
    groups_root = os.path.join(output_root, 'Groups')
    updated = dict(group_index)

    for key, current_subject in group_subjects.items():
        if key not in updated:
            continue  # new group — assigned later in resolve_group_folder

        entry = updated[key]
        old_folder = entry['folder']
        old_subject = entry.get('subject', '')

        if current_subject == old_subject:
            continue

        desired = sanitize_filename(current_subject) if current_subject \
            else f"Unknown Group ({key})"
        existing = {v['folder'] for k2, v in updated.items() if k2 != key}
        new_folder = _unique_group_name(desired, existing)

        old_path = os.path.join(groups_root, old_folder)
        new_path = os.path.join(groups_root, new_folder)

        if os.path.exists(old_path):
            if os.path.exists(new_path):
                logger.warning(
                    f"RENAME skipped — target already exists: "
                    f"{old_folder} -> {new_folder}"
                )
                continue
            else:
                os.rename(old_path, new_path)
                logger.info(f"RENAMED group folder: {old_folder} -> {new_folder}")
                if conn is not None:
                    old_prefix = f"Groups/{old_folder}/"
                    new_prefix = f"Groups/{new_folder}/"
                    # Known race: os.rename is immediate; this UPDATE is committed
                    # later in main(). A crash between the two leaves archive_copies
                    # with stale paths (restore mode will report those files as
                    # unrestorable on the next run). Accepted — no mitigation.
                    conn.execute(
                        "UPDATE archive_copies "
                        "SET archive_path = ? || SUBSTR(archive_path, ?) "
                        "WHERE archive_path LIKE ? ESCAPE '\\'",
                        (new_prefix, len(old_prefix) + 1,
                         f"{escape_like(old_prefix)}%")
                    )
        else:
            logger.debug(
                f"Group folder name changed but no folder on disk yet: "
                f"{old_folder} -> {new_folder}"
            )

        updated[key] = {'folder': new_folder, 'subject': current_subject}

    return updated


def resolve_group_folder(chat_row_id: int, chat_subject: str | None,
                         group_index: dict) -> str:
    """
    Return the stable folder name for a group.
    If the group is new, assign a unique name — disambiguating with a counter
    suffix if another group already uses the same subject.
    Mutates group_index in-place.
    """
    key = str(chat_row_id)
    if key in group_index:
        return group_index[key]['folder']

    desired = sanitize_filename(chat_subject) if chat_subject \
        else f"Unknown Group ({chat_row_id})"
    existing = {v['folder'] for v in group_index.values()}
    folder = _unique_group_name(desired, existing)
    group_index[key] = {'folder': folder, 'subject': chat_subject or ''}
    return folder



def sync_folder_names(contacts: dict, number_map: dict, output_root: str,
                      folder_index: dict, logger: logging.Logger,
                      conn: sqlite3.Connection | None = None) -> dict:
    """
    Compare current contact names against the persisted folder index.
    If a contact name has changed since the last run, rename the folder
    on disk so existing files are not re-copied and the archive stays
    consolidated.

    Returns an updated copy of the index.
    """
    contacts_root = os.path.join(output_root, 'Contacts')
    updated_index = dict(folder_index)

    for number, display_name in contacts.items():
        # Resolve to canonical number in case of number change
        canonical = number_map.get(number, number)
        new_folder = build_contact_folder_name(display_name, canonical)
        old_folder = folder_index.get(canonical)

        if old_folder is None:
            # First time we have seen this contact — just register it
            updated_index[canonical] = new_folder
            continue

        if old_folder == new_folder:
            # Name unchanged — nothing to do
            continue

        # Name has changed — rename folder on disk if it exists
        old_path = os.path.join(contacts_root, old_folder)
        new_path = os.path.join(contacts_root, new_folder)

        if os.path.exists(old_path):
            if os.path.exists(new_path):
                # Edge case: new folder name already exists (another contact?)
                logger.warning(
                    f"RENAME skipped — target already exists: "
                    f"{old_folder} -> {new_folder}"
                )
                continue
            else:
                os.rename(old_path, new_path)
                logger.info(
                    f"RENAMED contact folder: {old_folder} -> {new_folder}"
                )
                if conn is not None:
                    old_prefix = f"Contacts/{old_folder}/"
                    new_prefix = f"Contacts/{new_folder}/"
                    # Known race: os.rename is immediate; this UPDATE is committed
                    # later in main(). A crash between the two leaves archive_copies
                    # with stale paths (restore mode will report those files as
                    # unrestorable on the next run). Accepted — no mitigation.
                    conn.execute(
                        "UPDATE archive_copies "
                        "SET archive_path = ? || SUBSTR(archive_path, ?) "
                        "WHERE archive_path LIKE ? ESCAPE '\\'",
                        (new_prefix, len(old_prefix) + 1,
                         f"{escape_like(old_prefix)}%")
                    )
        else:
            logger.debug(
                f"Folder name changed but no folder on disk yet: "
                f"{old_folder} -> {new_folder}"
            )

        updated_index[canonical] = new_folder

    return updated_index


# ---------------------------------------------------------------------------
# Number consolidation map (handles contact number changes)
# ---------------------------------------------------------------------------

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

    # Build a direct map first
    direct = {old: new for old, new in raw_pairs}

    # Resolve chains: A->B->C becomes A->C
    def resolve(number, visited=None):
        if visited is None:
            visited = set()
        if number in visited:
            # Cycle guard — should never happen in real data
            logger.warning(f"Cycle detected in number change chain: {number}")
            return number
        visited.add(number)
        if number in direct:
            return resolve(direct[number], visited)
        return number

    consolidated = {old: resolve(old) for old in direct}
    logger.info(
        f"Number consolidation map built: {len(consolidated)} "
        f"old number(s) mapped to current numbers."
    )
    return consolidated


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_REQUIRED_TABLES = {'message', 'message_media', 'chat', 'jid', 'jid_map'}

_REQUIRED_COLUMNS = {
    'message':       {'_id', 'timestamp', 'sender_jid_row_id', 'from_me'},
    'message_media': {'file_path', 'mime_type', 'chat_row_id', 'message_row_id',
                      'message_url', 'media_name'},
    'chat':          {'_id', 'subject', 'jid_row_id'},
    'jid':           {'_id', 'user'},
    'jid_map':       {'lid_row_id', 'jid_row_id'},
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


_MEDIA_SUBFOLDERS = {
    'WhatsApp Images', 'WhatsApp Video', 'WhatsApp Audio',
    'WhatsApp Voice Notes', 'WhatsApp Video Notes', 'WhatsApp Animated Gifs',
    'WhatsApp Documents',
}


def validate_wa_root(wa_root: str, logger: logging.Logger):
    """
    Verify that wa_root is the correct WhatsApp folder (the one containing Media/).
    Aborts with a diagnostic hint for the most common mistakes.
    """
    if not os.path.isdir(wa_root):
        logger.error(f"--wa_root does not exist or is not a directory: {wa_root}")
        raise SystemExit(1)

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
        raise SystemExit(1)

    found = [s for s in _MEDIA_SUBFOLDERS if os.path.isdir(os.path.join(media_dir, s))]
    if not found:
        logger.warning(
            f"Media/ found but no WhatsApp media subfolders detected under {media_dir}. "
            f"The archive will likely be empty. "
            f"Expected at least one of: {sorted(_MEDIA_SUBFOLDERS)}"
        )


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

def build_group_subjects_query(since_ms: int | None) -> str:
    """
    Lightweight query returning the current subject for every group chat in scope.
    Used to detect renames before processing begins.
    No LIMIT — covers all groups regardless of --limit on the main query.
    """
    since_clause = f"AND message.timestamp >= {since_ms}" if since_ms else ""
    return f"""
        SELECT DISTINCT CAST(message_media.chat_row_id AS TEXT), chat.subject
        FROM message_media
        JOIN chat    ON message_media.chat_row_id    = chat._id
        JOIN message ON message_media.message_row_id = message._id
        WHERE chat.subject IS NOT NULL
        {since_clause}
    """


def build_query(limit: int | None, since_ms: int | None) -> str:
    """
    Build the main media extraction query.
    If limit is provided, each UNION ALL block is independently
    capped to that number of rows, giving a balanced sample
    from both group chats and 1-to-1 chats.
    If since_ms is provided, only messages at or after that timestamp
    (milliseconds) are included.
    """
    limit_clause = f"LIMIT {limit}" if limit else ""
    since_clause = f"AND message.timestamp >= {since_ms}" if since_ms else ""

    return f"""
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
        message_media.media_name            AS media_name
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
    {limit_clause}
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
        message_media.media_name            AS media_name
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
    {limit_clause}
)
"""

# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_rows(rows, total: int, contacts, number_map, folder_index, group_index,
                 wa_root, output_root, logger, dry_run, conn=None):
    stats = {'copied': 0, 'skipped': 0, 'missing': 0, 'warnings': 0}
    updated_index = dict(folder_index)
    updated_group_index = dict(group_index)
    missing_rows = []
    cursor = conn.cursor() if conn is not None else None

    for i, (msg_id, timestamp, file_path, mime_type, chat_row_id,
            chat_subject, sender, key_from_me, message_url, media_name) in enumerate(rows, 1):

        if i % 1000 == 0:
            logger.info(f"Progress: {i}/{total} rows processed...")

        # --- Null/empty file_path guard ---
        if not file_path:
            logger.debug(f"SKIP (no file_path): message_id={msg_id}")
            _canonical = number_map.get(sender, sender) if sender else None
            missing_rows.append({
                'message_id':        msg_id,
                'timestamp_human':   human_datetime(timestamp) if timestamp else '',
                'original_filename': '',
                'mime_type':         mime_type or '',
                'sender':            contacts.get(_canonical, _canonical) if _canonical else 'Me',
                'chat_name':         chat_subject or (
                    contacts.get(_canonical, _canonical) if _canonical else 'Unknown'),
                'direction':         'Sent' if key_from_me == 1 else 'Received',
                'file_path':         '',
                'message_url':       message_url or '',
                'media_name':        media_name or '',
            })
            stats['missing'] += 1
            continue

        # --- Resolve source path ---
        src = os.path.join(wa_root, *file_path.split('/'))
        is_document = file_path.startswith('Media/WhatsApp Documents/')
        filename = (media_name if media_name else os.path.basename(file_path)) \
            if is_document else os.path.basename(file_path)
        is_group = chat_subject is not None

        if not os.path.isfile(src):
            if is_group:
                chat_display = chat_subject
                canonical = number_map.get(sender, sender)
                sender_display = 'Me' if (key_from_me == 1 or not sender) \
                    else contacts.get(canonical, format_phone(canonical))
            else:
                # Resolve number change before contact lookup
                canonical = number_map.get(sender, sender) if sender else None
                chat_display = contacts.get(canonical, canonical) \
                    if canonical else 'Unknown'
                sender_display = 'Me' if key_from_me == 1 \
                    else contacts.get(canonical, canonical) \
                    if canonical else 'Unknown'

            logger.warning(
                f"MISSING source file (message_id={msg_id}): {src}"
            )
            missing_rows.append({
                'message_id':        msg_id,
                'timestamp_human':   human_datetime(timestamp) if timestamp else '',
                'original_filename': filename,
                'mime_type':         mime_type or '',
                'sender':            sender_display,
                'chat_name':         chat_display or '',
                'direction':         'Sent' if key_from_me == 1 else 'Received',
                'file_path':         file_path,
                'message_url':       message_url or '',
                'media_name':        media_name or '',
            })
            stats['missing'] += 1
            continue

        # Guard: skip rows with no timestamp
        if not timestamp:
            logger.warning(
                f"SKIP (no timestamp): message_id={msg_id}, file={file_path}"
            )
            stats['warnings'] += 1
            continue

        year = get_year(timestamp)

        # --- GROUP CHAT ---
        if is_group:
            group_name = resolve_group_folder(chat_row_id, chat_subject,
                                              updated_group_index)

            canonical = number_map.get(sender, sender)
            sender_display = 'Me' if (key_from_me == 1 or not sender) \
                else contacts.get(canonical, format_phone(canonical))

            dest_filename = append_sender_to_filename(filename, sender_display)
            dest_dir = os.path.join(output_root, 'Groups', group_name, year)

        # --- 1-to-1 CHAT ---
        else:
            # Resolve number change to canonical number
            contact_number = number_map.get(sender, sender) \
                if sender else 'unknown'
            contact_display = contacts.get(contact_number, None)
            folder_name = build_contact_folder_name(
                contact_display or contact_number, contact_number
            )

            # Update the folder index with the current resolved name
            updated_index[contact_number] = folder_name

            direction = 'Sent' if key_from_me == 1 else 'Received'
            dest_filename = filename
            dest_dir = os.path.join(
                output_root, 'Contacts', folder_name, year, direction
            )

        dest_path = os.path.join(dest_dir, dest_filename)

        # --- Dry run ---
        if dry_run:
            resolved = resolve_unique_dest(dest_path, src, logger)
            if resolved is None:
                stats['skipped'] += 1
            else:
                logger.info(f"[DRY RUN] Would copy: {src} -> {resolved}")
                stats['copied'] += 1
            continue

        # --- Hash source file ---
        src_hash = file_md5(src)

        # --- Conflict / duplicate resolution ---
        resolved = resolve_unique_dest(dest_path, src, logger, src_hash=src_hash)
        if resolved is None:
            stats['skipped'] += 1
            if cursor is not None:
                rel = os.path.relpath(dest_path, output_root).replace(os.sep, '/')
                record_file_archived(cursor, file_path, src_hash, rel)
            continue

        # --- Copy and set timestamps ---
        os.makedirs(dest_dir, exist_ok=True)
        try:
            shutil.copy2(src, resolved)
            set_file_times(resolved, timestamp)
            logger.debug(f"COPIED: {src} -> {resolved}")
            stats['copied'] += 1
            if cursor is not None:
                rel = os.path.relpath(resolved, output_root).replace(os.sep, '/')
                record_file_archived(cursor, file_path, src_hash, rel)
        except Exception as e:
            logger.error(f"ERROR copying {src} -> {resolved}: {e}")
            stats['warnings'] += 1

    return stats, updated_index, updated_group_index, missing_rows


# ---------------------------------------------------------------------------
# Restore mode
# ---------------------------------------------------------------------------

def run_restore_mode(args, logger):
    """
    Reconstruct the original WhatsApp Media/ folder structure from the archive.
    Queries .wa_media_archiver.db to determine original file paths and their archive copies.
    The reconstructed tree is written to <output>/Media/.
    """
    conn = open_archive_db(args.output)
    try:
        check_db_health(conn, logger)
        file_count = conn.execute(
            "SELECT COUNT(DISTINCT original_path) FROM archive_copies"
        ).fetchone()[0]
        if file_count == 0:
            logger.error(
                "No restore data found in the archive database.\n"
                "  Either this archive was built before v0.12, or no files have "
                "been archived yet. Re-run the archiver (forward mode) to build "
                "the restore data, then run restore mode again."
            )
            raise SystemExit(1)

        logger.info(f"Restore data loaded: {file_count} unique original file(s).")

        stats = {'restored': 0, 'skipped': 0, 'unrestorable': 0, 'warnings': 0}
        report_rows = []
        restore_report_path = os.path.join(args.output, 'restore_report.csv')

        # Build original_path -> [archive_paths] in one query
        restore_map: dict[str, list[str]] = {}
        for original_path, archive_path in conn.execute(
            "SELECT original_path, archive_path "
            "FROM archive_copies ORDER BY original_path, rowid"
        ):
            restore_map.setdefault(original_path, []).append(archive_path)

        for original_path, archive_paths in restore_map.items():

            # --- Find first valid archive copy ---
            src = None
            src_rel = None
            for rel_path in archive_paths:
                candidate = os.path.join(args.output, *rel_path.split('/'))
                if os.path.isfile(candidate):
                    src = candidate
                    src_rel = rel_path
                    break

            if src is None:
                logger.warning(f"UNRESTORABLE (no archive copy found): {original_path}")
                stats['unrestorable'] += 1
                report_rows.append({
                    'original_path':       original_path,
                    'source_archive_path': '',
                    'status':              'unrestorable',
                })
                continue

            dest = os.path.join(args.output, *original_path.split('/'))

            # --- Safe re-run / dry run: check for existing destination ---
            if os.path.exists(dest):
                if file_md5(src) == file_md5(dest):
                    logger.debug(f"SKIP (identical already restored): {original_path}")
                    stats['skipped'] += 1
                    continue
                logger.warning(
                    f"{'[DRY RUN] ' if args.dry_run else ''}"
                    f"COLLISION at restore destination (different content): {dest}"
                )
                stats['warnings'] += 1
                if not args.dry_run:
                    report_rows.append({
                        'original_path':       original_path,
                        'source_archive_path': src_rel,
                        'status':              'collision_skipped',
                    })
                continue

            # --- Dry run (no existing dest) ---
            if args.dry_run:
                logger.info(f"[DRY RUN] Would restore: {original_path}")
                stats['restored'] += 1
                continue

            # --- Copy ---
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.copy2(src, dest)
                logger.debug(f"RESTORED: {original_path}")
                stats['restored'] += 1
            except Exception as e:
                logger.error(f"ERROR restoring {original_path}: {e}")
                stats['warnings'] += 1
                report_rows.append({
                    'original_path':       original_path,
                    'source_archive_path': src_rel,
                    'status':              f'error: {e}',
                })

        # --- Write restore report ---
        if not args.dry_run:
            if report_rows:
                fieldnames = ['original_path', 'source_archive_path', 'status']
                with open(restore_report_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(report_rows)
                logger.info(
                    f"Restore report written to: {restore_report_path} "
                    f"({len(report_rows)} issue(s))"
                )
            else:
                logger.info("No restore issues to report.")

        # --- Final summary ---
        logger.info("=== Restore complete ===")
        if args.dry_run:
            logger.info("  (DRY RUN — no files were written)")
        logger.info(f"  {'Would restore' if args.dry_run else 'Restored':<20}: {stats['restored']}")
        logger.info(f"  {'Skipped (identical)':<20}: {stats['skipped']}")
        logger.info(f"  {'Unrestorable':<20}: {stats['unrestorable']}")
        logger.info(f"  {'Errors / Warnings':<20}: {stats['warnings']}")
        if not args.dry_run and report_rows:
            logger.info(f"  {'Restore report':<20}: {restore_report_path}")

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='wa_media_archiver.py',
        description='Archive WhatsApp media into a structured folder hierarchy.',
        epilog='Requires an unencrypted msgstore.db or a crypt15 backup + e2e key.'
    )
    parser.add_argument('--version', action='version',
                        version=f'WhatsApp Media Archiver v{__version__}')
    parser.add_argument('-msg', '--msgstore',
                        default='msgstore.db',
                        help='Path to msgstore.db[.crypt15] '
                             '(default: current folder)')
    parser.add_argument('-e2e', '--e2e_key',
                        help='E2E decryption key for encrypted '
                             'msgstore.db.crypt15')
    parser.add_argument('-c', '--contacts',
                        help='Path to contacts export file')
    parser.add_argument('-wa', '--wa_root',
                        required=False,
                        default=None,
                        help='Root path of WhatsApp folder on disk '
                             '(required unless --mode restore)')
    parser.add_argument('-o', '--output',
                        required=True,
                        help='Output root folder for the archive')
    parser.add_argument('-l', '--log',
                        default=None,
                        help='Log file path '
                             '(default: <output>/wa_media_archiver.log)')
    parser.add_argument('-mode', '--mode',
                        choices=['adb', 'restore'],
                        help='adb = pull msgstore and contacts via ADB; '
                             'restore = reconstruct original Media/ tree from archive')
    parser.add_argument('--dry-run',
                        action='store_true',
                        help='Simulate the run without copying any files')

    parser.add_argument('--limit',
                        type=int,
                        default=None,
                        help='Limit the number of rows extracted per query block '
                             '(e.g. --limit 250 for testing). '
                             'Omit for a full run.')
    parser.add_argument('--since',
                        default=None,
                        metavar='DATE',
                        help='Only include messages on or after this date '
                             '(format: YYYY-MM-DD). Combines with --limit.')
    args = parser.parse_args()

    if args.mode != 'restore' and not args.wa_root:
        parser.error("--wa_root / -wa is required unless --mode restore")

    # --- Output dir and logging ---
    os.makedirs(args.output, exist_ok=True)
    log_path = args.log or os.path.join(args.output, 'wa_media_archiver.log')
    logger = setup_logging(log_path)

    if args.mode == 'restore':
        logger.info(f"=== WhatsApp Archiver v{__version__} started (restore mode) ===")
        if args.dry_run:
            logger.info("*** DRY RUN MODE — no files will be copied ***")
        run_restore_mode(args, logger)
        return

    validate_wa_root(args.wa_root, logger)

    if args.dry_run:
        logger.info("*** DRY RUN MODE — no files will be copied ***")

    logger.info(f"=== WhatsApp Archiver v{__version__} started ===")

    # --- Contacts ---
    contacts = {}

    if args.mode == 'adb':
        if not shutil.which('adb'):
            logger.error(
                "ADB not found. Install Android SDK Platform Tools and ensure "
                "'adb' is on your PATH, then retry."
            )
            raise SystemExit(1)

        logger.info("Pulling msgstore backup via ADB...")
        try:
            subprocess.run([
                'adb', 'pull',
                '/storage/emulated/0/Android/media/com.whatsapp/WhatsApp'
                '/Databases/msgstore.db.crypt15',
                'msgstore.db.crypt15'
            ], check=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            logger.error(
                f"ADB pull failed.\n"
                f"  {e.stderr.decode(errors='replace').strip()}\n"
                f"  Make sure the device is connected, USB debugging is enabled, "
                f"and the connection is authorised on the phone."
            )
            raise SystemExit(1)
        args.msgstore = 'msgstore.db.crypt15'

        logger.info("Pulling contacts via ADB...")
        try:
            result = subprocess.run(
                ['adb', 'shell', 'content', 'query',
                 '--uri', 'content://com.android.contacts/data',
                 '--projection', 'display_name:data1'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                f"ADB contacts query failed.\n"
                f"  {e.stderr.decode(errors='replace').strip()}"
            )
            raise SystemExit(1)
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix='wa_contacts_', mode='wb')
        tmp.write(result.stdout)
        tmp.close()
        args.contacts = tmp.name
        atexit.register(os.unlink, tmp.name)

    if args.contacts:
        logger.info(f"Reading contacts from: {args.contacts}")
        with open(args.contacts, 'r', encoding='utf-8') as f:
            content = f.read()
        for name, number in re.findall(
                r"display_name=(.+?), data1=([^@\n\r]+)", content):
            contacts[number.strip()] = name.strip()
        logger.info(f"Loaded {len(contacts)} contacts.")

    # --- Decrypt if needed ---
    if args.msgstore.endswith('.crypt15'):
        if not args.e2e_key:
            logger.error(
                "Encrypted msgstore detected but no --e2e_key provided. "
                "Exiting."
            )
            raise SystemExit(1)
        logger.info("Decrypting msgstore...")
        try:
            from wa_crypt_tools.lib.db.dbfactory import DatabaseFactory
            from wa_crypt_tools.lib.key.keyfactory import KeyFactory
        except ImportError:
            logger.info("wa-crypt-tools not found — installing...")
            try:
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', 'wa-crypt-tools'],
                    check=True
                )
            except subprocess.CalledProcessError:
                logger.error(
                    "Failed to install wa-crypt-tools automatically.\n"
                    "  Run manually: pip3 install wa-crypt-tools"
                )
                raise SystemExit(1)
            from wa_crypt_tools.lib.db.dbfactory import DatabaseFactory
            from wa_crypt_tools.lib.key.keyfactory import KeyFactory

        with open(args.msgstore, 'rb') as msg:
            raw = msg.read()
        db = DatabaseFactory.from_file(io.BytesIO(raw))
        key = KeyFactory.new(args.e2e_key)
        decrypted = db.decrypt(key, raw)
        try:
            output_file = zlib.decompressobj().decompress(decrypted)
        except zlib.error:
            output_file = decrypted  # already plain SQLite (newer wa_crypt_tools)
        args.msgstore = os.path.join(args.output, 'msgstore.db')
        if os.path.exists(args.msgstore):
            logger.warning(f"Overwriting existing {args.msgstore} with decrypted database.")
        with open(args.msgstore, 'wb') as out:
            out.write(output_file)
        logger.info("Decryption complete.")

    # --- DB connection ---
    logger.info(f"Connecting to: {args.msgstore}")
    if not os.path.isfile(args.msgstore):
        logger.error(f"Database file not found: {args.msgstore}")
        raise SystemExit(1)
    with contextlib.closing(sqlite3.connect(args.msgstore)) as msgstore_conn:
        cursor = msgstore_conn.cursor()

        # --- Validate WhatsApp DB schema ---
        validate_schema(cursor, logger)

        # --- Build number consolidation map ---
        number_map = build_number_map(cursor, logger)

        # --- Build queries ---
        logger.info("Executing query...")
        since_ms = None
        if args.since:
            try:
                since_ms = int(datetime.strptime(args.since, '%Y-%m-%d').timestamp() * 1000)
            except ValueError:
                logger.error(f"Invalid --since date '{args.since}'. Expected format: YYYY-MM-DD")
                raise SystemExit(1)
            logger.info(f"SINCE filter active: on or after {args.since}.")
        if args.limit:
            logger.info(f"LIMIT active: {args.limit} rows per query block "
                f"({args.limit * 2} max total rows).")
        query = build_query(args.limit, since_ms)

        # Prelim: group subjects for rename sync (no LIMIT — covers all groups in scope)
        group_subjects = dict(
            cursor.execute(build_group_subjects_query(since_ms)).fetchall()
        )

        # Count without loading all rows into memory
        total_rows = cursor.execute(
            f"SELECT COUNT(*) FROM ({query})"
        ).fetchone()[0]
        logger.info(f"Query returned {total_rows} rows.")

        # --- Open archive DB inside msgstore scope so cursor stays open for streaming ---
        stats = {'copied': 0, 'skipped': 0, 'missing': 0, 'warnings': 0}
        archive_conn = open_archive_db(args.output)
        try:
            check_db_health(archive_conn, logger)
            folder_index = load_contacts_from_db(archive_conn)
            logger.info(f"Contact index loaded: {len(folder_index)} known contact(s).")

            group_index = load_groups_from_db(archive_conn)
            logger.info(f"Group index loaded: {len(group_index)} known group(s).")

            # --- Sync folder names for renamed contacts ---
            if not args.dry_run:
                folder_index = sync_folder_names(
                    contacts, number_map, args.output, folder_index, logger,
                    conn=archive_conn
                )

            # --- Sync folder names for renamed groups ---
            if not args.dry_run:
                group_index = sync_group_names(
                    group_subjects, args.output, group_index, logger,
                    conn=archive_conn
                )

            report_path = os.path.join(args.output, 'missing_media_report.csv')

            # --- Stream main query ---
            cursor.execute(query)
            stats, updated_index, updated_group_index, missing_rows = process_rows(
                cursor, total_rows, contacts, number_map, folder_index, group_index,
                args.wa_root, args.output, logger, args.dry_run,
                conn=archive_conn if not args.dry_run else None,
            )

            # --- Persist small indices, commit, and analyse ---
            if not args.dry_run:
                archive_conn.commit()
                save_contacts_to_db(archive_conn, updated_index)
                save_groups_to_db(archive_conn, updated_group_index)
                archive_conn.execute("ANALYZE")
                archive_conn.commit()
                logger.info(f"Archive DB saved: {len(updated_index)} contact(s), "
                            f"{len(updated_group_index)} group(s).")

            # --- Write missing media CSV ---
            if not args.dry_run:
                write_missing_report(report_path, missing_rows, logger)
            else:
                logger.info(
                    f"[DRY RUN] Would write missing media report with "
                    f"{len(missing_rows)} entries to: {report_path}"
                )

            # --- Write duplicate media CSV ---
            dup_report_path = os.path.join(args.output, 'duplicate_media_report.csv')
            if not args.dry_run:
                write_duplicate_report(dup_report_path, archive_conn, logger)

        finally:
            archive_conn.close()

    # --- Final summary ---
    db_path = os.path.join(args.output, '.wa_media_archiver.db')
    logger.info("=== Run complete ===")
    if args.dry_run:
        logger.info("  (DRY RUN — no files were written)")
    logger.info(f"  {'Would copy' if args.dry_run else 'Copied':<20}: {stats['copied']}")
    logger.info(f"  Skipped             : {stats['skipped']}")
    logger.info(f"  Missing source files: {stats['missing']}")
    logger.info(f"  Errors / Warnings   : {stats['warnings']}")
    logger.info(f"  Log file            : {log_path}")
    if not args.dry_run:
        logger.info(f"  {'Missing media report':<20}: {report_path}")
        logger.info(f"  {'Duplicate media report':<20}: {dup_report_path}")
        logger.info(f"  {'Archive DB':<20}: {db_path}")


if __name__ == '__main__':
    main()