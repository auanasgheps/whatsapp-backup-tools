import atexit
import argparse
import contextlib
import csv
import hashlib
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

import adb_extractor
import android_handler
import backup_reader
import ios_handler

# ==============================================================================
# WA Media Archiver — v0.20
# Archives WhatsApp media into a structured folder hierarchy using msgstore.db
# (Android) or ChatStorage.sqlite (iOS). Run on a backup copy of your data.
# Requires Python 3.10+.
# ==============================================================================

__version__ = '0.22'

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
# Platform detection
# ---------------------------------------------------------------------------

def detect_db_platform(path: str) -> str:
    """
    Inspect a WhatsApp database and return 'ios' or 'android'.
    iOS databases contain ZWAMESSAGE; Android databases do not.
    """
    with contextlib.closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    return 'ios' if 'ZWAMESSAGE' in tables else 'android'


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
                     conn: sqlite3.Connection | None = None,
                     dry_run: bool = False) -> dict:
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
                if dry_run:
                    logger.info(f"[DRY RUN] Would rename group folder: {old_folder} -> {new_folder}")
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
                      conn: sqlite3.Connection | None = None,
                      dry_run: bool = False) -> dict:
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
                if dry_run:
                    logger.info(f"[DRY RUN] Would rename contact folder: {old_folder} -> {new_folder}")
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
# Core processing
# ---------------------------------------------------------------------------

def process_rows(rows, total: int, contacts, number_map, folder_index, group_index,
                 media_resolver, output_root, logger, dry_run, conn=None):
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
        src = media_resolver(file_path)
        filename = media_name if media_name else os.path.basename(file_path)
        is_group = chat_subject is not None

        if src is None or not os.path.isfile(src):
            if is_group:
                chat_display = chat_subject
                canonical = number_map.get(sender, sender)
                sender_display = 'Me' if (key_from_me == 1 or not sender) \
                    else contacts.get(canonical, format_phone(canonical))
            else:
                canonical = number_map.get(sender, sender) if sender else None
                chat_display = contacts.get(canonical, canonical) \
                    if canonical else 'Unknown'
                sender_display = 'Me' if key_from_me == 1 \
                    else contacts.get(canonical, canonical) \
                    if canonical else 'Unknown'

            logger.warning(
                f"MISSING source file (message_id={msg_id}): {file_path}"
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
            contact_number = number_map.get(sender, sender) \
                if sender else 'unknown'
            contact_display = contacts.get(contact_number, None)
            folder_name = build_contact_folder_name(
                contact_display or contact_number, contact_number
            )

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

        # --- Conflict / duplicate resolution (lazy src hash: only when dest exists) ---
        if os.path.exists(dest_path):
            src_hash = file_md5(src)
            resolved = resolve_unique_dest(dest_path, src, logger, src_hash=src_hash)
            if resolved is None:
                stats['skipped'] += 1
                if cursor is not None:
                    rel = os.path.relpath(dest_path, output_root).replace(os.sep, '/')
                    record_file_archived(cursor, file_path, src_hash, rel)
                continue
        else:
            src_hash = None
            resolved = dest_path

        # --- Copy and set timestamps ---
        os.makedirs(dest_dir, exist_ok=True)
        try:
            shutil.copy2(src, resolved)
            set_file_times(resolved, timestamp)
            if src_hash is None:
                src_hash = file_md5(resolved)
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

        # Detect platform(s) present in the archive.
        # A mixed iOS+Android archive warns but proceeds with Android paths only.
        # A pure iOS archive is rejected — iOS paths have no restore target.
        has_ios = conn.execute(
            "SELECT 1 FROM archive_copies WHERE original_path LIKE 'Message/%' LIMIT 1"
        ).fetchone()
        has_android = conn.execute(
            "SELECT 1 FROM archive_copies WHERE original_path LIKE 'Media/%' LIMIT 1"
        ).fetchone()
        if has_ios and not has_android:
            logger.error(
                "Restore mode is only supported for Android archives. "
                "iOS original paths cannot be reconstructed into a usable media folder."
            )
            raise SystemExit(1)
        if has_ios and has_android:
            logger.warning(
                "This archive contains both iOS and Android entries. "
                "Only Android entries (Media/...) will be restored."
            )

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
        epilog=(
            'Android: requires msgstore.db or a crypt15 backup + e2e key. '
            'iOS: pass --ios_backup to read directly from an iPhone backup.'
        )
    )
    parser.add_argument('--version', action='version',
                        version=f'WhatsApp Media Archiver v{__version__}')
    parser.add_argument('-msg', '--msgstore',
                        default='msgstore.db',
                        help='Path to msgstore.db[.crypt15] or ChatStorage.sqlite '
                             '(default: current folder). Not needed with --ios_backup.')
    parser.add_argument('-e2e', '--e2e_key',
                        help='E2E decryption key for encrypted '
                             'msgstore.db.crypt15')
    parser.add_argument('-c', '--contacts',
                        help='Path to contacts export file (Android ADB format)')
    parser.add_argument('-wa', '--wa_root',
                        required=False,
                        default=None,
                        help='Root path of WhatsApp folder on disk '
                             '(Android: folder containing Media/; '
                             'iOS pre-extracted: AppDomainGroup folder). '
                             'Not required with --ios_backup or --mode restore.')
    parser.add_argument('--ios_backup',
                        default=None,
                        metavar='PATH',
                        help='Path to iPhone backup directory (contains Manifest.db). '
                             'Mutually exclusive with --wa_root for iOS.')
    parser.add_argument('--ios_contacts',
                        default=None,
                        metavar='PATH',
                        help='Path to ContactsV2.sqlite from an iOS backup '
                             '(optional; auto-extracted from --ios_backup if omitted).')
    parser.add_argument('--business',
                        action='store_true',
                        help='Target WhatsApp Business instead of the regular WhatsApp app. '
                             'Affects the ADB pull path (Android) and the backup domain (iOS).')
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
                             'restore = reconstruct original Media/ tree from archive '
                             '(Android archives only)')
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

    if args.ios_backup and args.wa_root:
        parser.error("--ios_backup and --wa_root are mutually exclusive.")

    if args.ios_backup and args.mode == 'adb':
        parser.error("--ios_backup and --mode adb are mutually exclusive.")

    if args.mode != 'restore' and not args.ios_backup and not args.wa_root:
        parser.error("--wa_root / -wa is required unless --ios_backup or --mode restore")

    # --- Output dir and logging ---
    os.makedirs(args.output, exist_ok=True)
    log_path = args.log or os.path.join(args.output, 'wa_media_archiver.log')
    logger = setup_logging(log_path)

    for _label, _path in [('--output', args.output), ('--wa_root', getattr(args, 'wa_root', None))]:
        if _path and (_path.startswith('\\\\') or _path.startswith('//')):
            logger.warning(
                f"{_label} appears to be a network share ({_path}). "
                "Performance may be degraded and file timestamps may not be preserved."
            )

    if args.mode == 'restore':
        logger.info(f"=== WhatsApp Archiver v{__version__} started (restore mode) ===")
        if args.dry_run:
            logger.info("*** DRY RUN MODE — no files will be copied ***")
        run_restore_mode(args, logger)
        return

    if args.dry_run:
        logger.info("*** DRY RUN MODE — no files will be copied ***")

    logger.info(f"=== WhatsApp Archiver v{__version__} started ===")

    # -------------------------------------------------------------------------
    # iOS backup mode — read directly from the iPhone backup
    # -------------------------------------------------------------------------
    if args.ios_backup:
        if backup_reader.detect_encrypted(args.ios_backup, logger):
            logger.error(
                "Your iPhone backup is encrypted. Open Finder (macOS) or "
                "Apple Devices (Windows), disable backup encryption, create a "
                "new backup, then re-run."
            )
            raise SystemExit(1)

        logger.info("Building manifest map from backup...")
        ios_domain = (backup_reader._WA_BUSINESS_DOMAIN if args.business
                      else backup_reader._WA_DOMAIN)
        manifest_map = backup_reader.build_manifest_map(args.ios_backup, logger, domain=ios_domain)
        if not manifest_map:
            logger.warning(
                f"Manifest map is empty — no WhatsApp files found in the backup at: "
                f"{args.ios_backup}\n"
                f"  Make sure --ios_backup points to the backup root directory "
                f"(the folder that contains Manifest.db), and that the backup "
                f"includes WhatsApp data."
            )
        else:
            logger.info(f"Manifest map built: {len(manifest_map)} WhatsApp file(s).")

        logger.info("Extracting ChatStorage.sqlite from backup...")
        tmp_msgstore = backup_reader.extract_to_temp(manifest_map, 'ChatStorage.sqlite', logger)
        atexit.register(os.unlink, tmp_msgstore)
        args.msgstore = tmp_msgstore

        if args.ios_contacts:
            ios_contacts_path = args.ios_contacts
        elif manifest_map.get('ContactsV2.sqlite'):
            logger.info("Extracting ContactsV2.sqlite from backup...")
            tmp_contacts = backup_reader.extract_to_temp(manifest_map, 'ContactsV2.sqlite', logger)
            atexit.register(os.unlink, tmp_contacts)
            ios_contacts_path = tmp_contacts
        else:
            logger.warning("ContactsV2.sqlite not found in backup; proceeding without contacts.")
            ios_contacts_path = None

        platform = 'ios'
        media_resolver = lambda fp: manifest_map.get(fp)  # noqa: E731

    # -------------------------------------------------------------------------
    # wa_root mode — Android or iOS pre-extracted
    # -------------------------------------------------------------------------
    else:
        ios_contacts_path = args.ios_contacts
        platform = None  # resolved after decryption

        def _wa_root_resolver(fp):
            return os.path.join(args.wa_root, *fp.split('/'))
        media_resolver = _wa_root_resolver

    # --- Contacts (Android ADB format) ---
    contacts = {}

    if args.mode == 'adb':
        if not adb_extractor.check_adb(logger):
            raise SystemExit(1)
        if not adb_extractor.check_device_connected(logger):
            raise SystemExit(1)
        tmp_dir = tempfile.mkdtemp(prefix='wa_adb_')
        atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
        try:
            args.msgstore = adb_extractor.pull_msgstore(tmp_dir, args.business, logger)
            args.contacts = adb_extractor.pull_contacts(tmp_dir, logger)
        except subprocess.CalledProcessError:
            raise SystemExit(1)

    if args.contacts:
        if args.ios_backup:
            logger.warning(
                "--contacts was provided alongside --ios_backup. "
                "The supplied contacts file will be used; iOS auto-extracted contacts "
                "will be ignored. Omit --contacts to use the backup's ContactsV2.sqlite."
            )
        contacts = android_handler.load_contacts(args.contacts, logger)

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
            logger.error(
                "wa-crypt-tools is required for decryption but is not installed.\n"
                "  Run: pip install wa-crypt-tools"
            )
            raise SystemExit(1)

        with open(args.msgstore, 'rb') as msg:
            db = DatabaseFactory.from_file(msg)
            msg.seek(0)
            raw = msg.read()
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

    # Resolve platform for wa_root mode (after possible decryption)
    if platform is None:
        platform = detect_db_platform(args.msgstore)
        logger.info(f"Detected platform: {platform}")

    # --- Parse --since and --limit once, before the platform fork ---
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

    with contextlib.closing(sqlite3.connect(args.msgstore)) as msgstore_conn:
        cursor = msgstore_conn.cursor()

        if platform == 'ios':
            ios_handler.validate_ios_schema(cursor, logger)

            if args.wa_root:
                ios_handler.validate_ios_wa_root(args.wa_root, logger)

            number_map = ios_handler.build_ios_number_map(cursor, logger)

            # Load iOS contacts (from --ios_contacts or auto-extracted temp file)
            if not contacts and ios_contacts_path:
                contacts = ios_handler.load_ios_contacts(ios_contacts_path, logger)

            query = ios_handler.build_ios_query(args.limit, since_ms)
            group_subjects = dict(
                cursor.execute(ios_handler.build_ios_group_subjects_query()).fetchall()
            )

        else:
            android_handler.validate_schema(cursor, logger)
            android_handler.validate_wa_root(args.wa_root, logger)
            number_map = android_handler.build_number_map(cursor, logger)

            query = android_handler.build_query(args.limit, since_ms)
            group_subjects = dict(
                cursor.execute(android_handler.build_group_subjects_query()).fetchall()
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
            folder_index = sync_folder_names(
                contacts, number_map, args.output, folder_index, logger,
                conn=archive_conn if not args.dry_run else None,
                dry_run=args.dry_run,
            )

            # --- Sync folder names for renamed groups ---
            group_index = sync_group_names(
                group_subjects, args.output, group_index, logger,
                conn=archive_conn if not args.dry_run else None,
                dry_run=args.dry_run,
            )

            report_path = os.path.join(args.output, 'missing_media_report.csv')

            # --- Stream main query ---
            logger.info("Executing query...")
            cursor.execute(query)
            stats, updated_index, updated_group_index, missing_rows = process_rows(
                cursor, total_rows, contacts, number_map, folder_index, group_index,
                media_resolver, args.output, logger, args.dry_run,
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