import logging
import os
import re
import sqlite3


# ---------------------------------------------------------------------------
# Private helpers (copies of the equivalents in wa_media_archiver.py)
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def _escape_like(s: str) -> str:
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _format_phone(number: str) -> str:
    return '00' + number if number else ''


def _build_contact_folder_name(display_name: str, number: str) -> str:
    formatted = _format_phone(number)
    if not display_name or display_name == number:
        label = f"Unknown ({formatted})"
    else:
        label = f"{display_name} ({formatted})"
    return _sanitize_filename(label)


# ---------------------------------------------------------------------------
# Open / schema
# ---------------------------------------------------------------------------

def open_archive_db(output_root: str) -> sqlite3.Connection:
    db_path = os.path.join(output_root, '.wa_media_archiver.db')
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number        TEXT PRIMARY KEY,
            folder        TEXT NOT NULL,
            display_name  TEXT NOT NULL DEFAULT ''
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


def check_db_health(conn: sqlite3.Connection, logger: logging.Logger):
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


# ---------------------------------------------------------------------------
# Contacts persistence
# ---------------------------------------------------------------------------

def load_contacts_from_db(conn: sqlite3.Connection) -> dict:
    return {row[0]: (row[1], row[2])
            for row in conn.execute("SELECT number, folder, display_name FROM contacts")}


def save_contacts_to_db(conn: sqlite3.Connection, index: dict):
    conn.executemany(
        "INSERT OR REPLACE INTO contacts (number, folder, display_name) VALUES (?, ?, ?)",
        ((number, folder, display_name) for number, (folder, display_name) in index.items())
    )


# ---------------------------------------------------------------------------
# Groups persistence
# ---------------------------------------------------------------------------

def load_groups_from_db(conn: sqlite3.Connection) -> dict:
    return {
        row[0]: {'folder': row[1], 'subject': row[2]}
        for row in conn.execute(
            "SELECT chat_row_id, folder, subject FROM groups"
        )
    }


def save_groups_to_db(conn: sqlite3.Connection, index: dict):
    conn.executemany(
        "INSERT OR REPLACE INTO groups (chat_row_id, folder, subject) VALUES (?, ?, ?)",
        ((key, val['folder'], val['subject']) for key, val in index.items())
    )


# ---------------------------------------------------------------------------
# File archive tracking
# ---------------------------------------------------------------------------

def record_file_archived(cursor: sqlite3.Cursor,
                         original_path: str, md5: bytes, archive_path: str):
    cursor.execute(
        "INSERT INTO files (original_path, md5) VALUES (?, ?) "
        "ON CONFLICT(original_path) DO UPDATE SET md5 = excluded.md5",
        (original_path, md5)
    )
    cursor.execute(
        "INSERT OR IGNORE INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
        (original_path, archive_path)
    )


# ---------------------------------------------------------------------------
# Folder name sync and resolution
# ---------------------------------------------------------------------------

def _unique_group_name(desired: str, existing: set) -> str:
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
    groups_root = os.path.join(output_root, 'Groups')
    updated = dict(group_index)

    for key, current_subject in group_subjects.items():
        if key not in updated:
            continue

        entry = updated[key]
        old_folder = entry['folder']
        old_subject = entry.get('subject', '')

        if current_subject == old_subject:
            continue

        desired = _sanitize_filename(current_subject) if current_subject \
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
            else:
                if dry_run:
                    logger.info(f"[DRY RUN] Would rename group folder: {old_folder} -> {new_folder}")
                    continue
                else:
                    os.rename(old_path, new_path)
                    logger.info(f"RENAMED group folder: {old_folder} -> {new_folder}")
                    if conn is not None:
                        old_prefix = f"Groups/{old_folder}/"
                        new_prefix = f"Groups/{new_folder}/"
                        conn.execute(
                            "UPDATE archive_copies "
                            "SET archive_path = ? || SUBSTR(archive_path, ?) "
                            "WHERE archive_path LIKE ? ESCAPE '\\'",
                            (new_prefix, len(old_prefix) + 1,
                             f"{_escape_like(old_prefix)}%")
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
    key = str(chat_row_id)
    if key in group_index:
        return group_index[key]['folder']

    desired = _sanitize_filename(chat_subject) if chat_subject \
        else f"Unknown Group ({chat_row_id})"
    existing = {v['folder'] for v in group_index.values()}
    folder = _unique_group_name(desired, existing)
    group_index[key] = {'folder': folder, 'subject': chat_subject or ''}
    return folder


def sync_folder_names(contacts: dict, number_map: dict, output_root: str,
                      folder_index: dict, logger: logging.Logger,
                      conn: sqlite3.Connection | None = None,
                      dry_run: bool = False) -> dict:
    contacts_root = os.path.join(output_root, 'Contacts')
    updated_index = dict(folder_index)

    for number, display_name in contacts.items():
        canonical = number_map.get(number, number)
        new_folder = _build_contact_folder_name(display_name, canonical)
        entry = folder_index.get(canonical)
        old_folder = entry[0] if entry is not None else None

        if old_folder is None:
            updated_index[canonical] = (new_folder, display_name)
            continue

        if old_folder == new_folder:
            updated_index[canonical] = (new_folder, display_name)
            continue

        old_path = os.path.join(contacts_root, old_folder)
        new_path = os.path.join(contacts_root, new_folder)

        if os.path.exists(old_path):
            if os.path.exists(new_path):
                logger.warning(
                    f"RENAME skipped — target already exists: "
                    f"{old_folder} -> {new_folder}"
                )
            else:
                if dry_run:
                    logger.info(f"[DRY RUN] Would rename contact folder: {old_folder} -> {new_folder}")
                    continue
                else:
                    os.rename(old_path, new_path)
                    logger.info(f"RENAMED contact folder: {old_folder} -> {new_folder}")
                    if conn is not None:
                        old_prefix = f"Contacts/{old_folder}/"
                        new_prefix = f"Contacts/{new_folder}/"
                        conn.execute(
                            "UPDATE archive_copies "
                            "SET archive_path = ? || SUBSTR(archive_path, ?) "
                            "WHERE archive_path LIKE ? ESCAPE '\\'",
                            (new_prefix, len(old_prefix) + 1,
                             f"{_escape_like(old_prefix)}%")
                        )
        else:
            logger.debug(
                f"Folder name changed but no folder on disk yet: "
                f"{old_folder} -> {new_folder}"
            )

        updated_index[canonical] = (new_folder, display_name)

    return updated_index
