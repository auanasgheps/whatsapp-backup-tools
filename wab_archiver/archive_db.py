import logging
import os
import sqlite3

from shared.db import (
    build_contact_folder_name as _build_contact_folder_name,
)
from shared.db import (
    escape_like as _escape_like,
)
from shared.db import (
    sanitize_filename as _sanitize_filename,
)

# ---------------------------------------------------------------------------
# Open / schema
# ---------------------------------------------------------------------------


def open_archive_db(output_root: str) -> sqlite3.Connection:
    db_path = os.path.join(output_root, ".wa_media_archiver.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode = WAL")
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
            md5            BLOB NOT NULL,
            size           INTEGER
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path  TEXT NOT NULL REFERENCES files(original_path),
            archive_path   TEXT NOT NULL,
            is_hardlink    INTEGER NOT NULL DEFAULT 0,
            timestamp_ms   INTEGER,
            PRIMARY KEY (original_path, archive_path)
        );
        CREATE INDEX IF NOT EXISTS idx_files_md5 ON files(md5);
        CREATE TABLE IF NOT EXISTS recent_messages (
            chat_id        TEXT NOT NULL,
            chat_type      TEXT NOT NULL,
            msg_id         INTEGER NOT NULL,
            timestamp_ms   INTEGER NOT NULL,
            sender         TEXT NOT NULL,
            from_me        INTEGER NOT NULL,
            archive_path   TEXT,
            media_type     TEXT NOT NULL DEFAULT 'text',
            media_name     TEXT,
            text_body      TEXT NOT NULL,
            quoted_text    TEXT,
            quoted_sender  TEXT,
            quoted_ts      INTEGER,
            reactions      TEXT,
            is_edited      INTEGER NOT NULL DEFAULT 0,
            edited_ts      INTEGER,
            PRIMARY KEY (chat_id, chat_type, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recent_chat_ts
            ON recent_messages(chat_id, chat_type, timestamp_ms DESC);
        CREATE TABLE IF NOT EXISTS adb_pull_state (
            remote_path    TEXT    NOT NULL,
            device_serial  TEXT    NOT NULL,
            status         TEXT    NOT NULL,
            pulled_at      TEXT,
            PRIMARY KEY (remote_path, device_serial)
        );
        CREATE TABLE IF NOT EXISTS reactions_cache (
            chat_id    TEXT NOT NULL,
            msg_id     INTEGER NOT NULL,
            reactions  TEXT NOT NULL,
            PRIMARY KEY (chat_id, msg_id)
        );
    """)
    # Migrate existing DBs: add size column if absent (safe no-op on new DBs)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if "size" not in existing:
        conn.execute("ALTER TABLE files ADD COLUMN size INTEGER")
    existing_copies = {r[1] for r in conn.execute("PRAGMA table_info(archive_copies)")}
    if "is_hardlink" not in existing_copies:
        conn.execute("ALTER TABLE archive_copies ADD COLUMN is_hardlink INTEGER NOT NULL DEFAULT 0")
    if "timestamp_ms" not in existing_copies:
        conn.execute("ALTER TABLE archive_copies ADD COLUMN timestamp_ms INTEGER")
    existing_recent = {r[1] for r in conn.execute("PRAGMA table_info(recent_messages)")}
    if "is_edited" not in existing_recent:
        conn.execute("ALTER TABLE recent_messages ADD COLUMN is_edited INTEGER NOT NULL DEFAULT 0")
    if "edited_ts" not in existing_recent:
        conn.execute("ALTER TABLE recent_messages ADD COLUMN edited_ts INTEGER")
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        conn.execute("PRAGMA user_version = 2")
    conn.commit()
    return conn


def check_db_health(conn: sqlite3.Connection, logger: logging.Logger):
    results = conn.execute("PRAGMA quick_check").fetchall()
    if results != [("ok",)]:
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
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT number, folder, display_name FROM contacts")
    }


def save_contacts_to_db(conn: sqlite3.Connection, index: dict):
    conn.executemany(
        "INSERT OR REPLACE INTO contacts (number, folder, display_name) VALUES (?, ?, ?)",
        ((number, folder, display_name) for number, (folder, display_name) in index.items()),
    )


# ---------------------------------------------------------------------------
# Groups persistence
# ---------------------------------------------------------------------------


def load_groups_from_db(conn: sqlite3.Connection) -> dict:
    return {
        row[0]: {"folder": row[1], "subject": row[2]}
        for row in conn.execute("SELECT chat_row_id, folder, subject FROM groups")
    }


def save_groups_to_db(conn: sqlite3.Connection, index: dict):
    conn.executemany(
        "INSERT OR REPLACE INTO groups (chat_row_id, folder, subject) VALUES (?, ?, ?)",
        ((key, val["folder"], val["subject"]) for key, val in index.items()),
    )


# ---------------------------------------------------------------------------
# File archive tracking
# ---------------------------------------------------------------------------


def record_file_archived(
    cursor: sqlite3.Cursor,
    original_path: str,
    md5: bytes,
    archive_path: str,
    size: int,
    is_hardlink: int,
    timestamp_ms: int | None,
):
    cursor.execute(
        "INSERT INTO files (original_path, md5, size) VALUES (?, ?, ?) "
        "ON CONFLICT(original_path) DO UPDATE SET md5 = excluded.md5, size = excluded.size",
        (original_path, md5, size),
    )
    cursor.execute(
        "INSERT INTO archive_copies (original_path, archive_path, is_hardlink, timestamp_ms) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(original_path, archive_path) DO UPDATE SET "
        "is_hardlink = excluded.is_hardlink, "
        "timestamp_ms = COALESCE(excluded.timestamp_ms, archive_copies.timestamp_ms)",
        (original_path, archive_path, is_hardlink, timestamp_ms),
    )


def load_canonical_copies(conn: sqlite3.Connection, output_root: str) -> dict[bytes, str]:
    """
    Load a mapping of {md5_bytes: canonical_abs_path} from the archive database
    for files that exist on disk.
    """
    canonical_map = {}
    rows = conn.execute(
        "SELECT f.md5, ac.archive_path FROM files f "
        "JOIN archive_copies ac ON ac.original_path = f.original_path "
        "ORDER BY ac.rowid ASC"
    ).fetchall()
    for md5_bytes, rel_path in rows:
        if md5_bytes not in canonical_map:
            abs_path = os.path.join(output_root, *rel_path.split("/"))
            if os.path.isfile(abs_path):
                canonical_map[md5_bytes] = abs_path
    return canonical_map


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


def sync_group_names(
    group_subjects: dict,
    output_root: str,
    group_index: dict,
    logger: logging.Logger,
    conn: sqlite3.Connection | None = None,
    dry_run: bool = False,
) -> dict:
    groups_root = os.path.join(output_root, "Groups")
    updated = dict(group_index)

    for key, current_subject in group_subjects.items():
        if key not in updated:
            continue

        entry = updated[key]
        old_folder = entry["folder"]
        old_subject = entry.get("subject", "")

        if current_subject == old_subject:
            continue

        desired = (
            _sanitize_filename(current_subject) if current_subject else f"Unknown Group ({key})"
        )
        existing = {v["folder"] for k2, v in updated.items() if k2 != key}
        new_folder = _unique_group_name(desired, existing)

        old_path = os.path.join(groups_root, old_folder)
        new_path = os.path.join(groups_root, new_folder)

        if os.path.exists(old_path):
            if os.path.exists(new_path):
                logger.warning(
                    f"RENAME skipped — target already exists: {old_folder} -> {new_folder}"
                )
            else:
                if dry_run:
                    logger.info(
                        f"[DRY RUN] Would rename group folder: {old_folder} -> {new_folder}"
                    )
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
                            (new_prefix, len(old_prefix) + 1, f"{_escape_like(old_prefix)}%"),
                        )
        else:
            logger.debug(
                f"Group folder name changed but no folder on disk yet: {old_folder} -> {new_folder}"
            )

        updated[key] = {"folder": new_folder, "subject": current_subject}

    return updated


def resolve_group_folder(chat_row_id: int, chat_subject: str | None, group_index: dict) -> str:
    key = str(chat_row_id)
    if key in group_index:
        return group_index[key]["folder"]

    desired = _sanitize_filename(chat_subject) if chat_subject else f"Unknown Group ({chat_row_id})"
    existing = {v["folder"] for v in group_index.values()}
    folder = _unique_group_name(desired, existing)
    group_index[key] = {"folder": folder, "subject": chat_subject or ""}
    return folder


def sync_folder_names(
    contacts: dict,
    number_map: dict,
    output_root: str,
    folder_index: dict,
    logger: logging.Logger,
    conn: sqlite3.Connection | None = None,
    dry_run: bool = False,
) -> dict:
    contacts_root = os.path.join(output_root, "Contacts")
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
                    f"RENAME skipped — target already exists: {old_folder} -> {new_folder}"
                )
            else:
                if dry_run:
                    logger.info(
                        f"[DRY RUN] Would rename contact folder: {old_folder} -> {new_folder}"
                    )
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
                            (new_prefix, len(old_prefix) + 1, f"{_escape_like(old_prefix)}%"),
                        )
        else:
            logger.debug(
                f"Folder name changed but no folder on disk yet: {old_folder} -> {new_folder}"
            )

        updated_index[canonical] = (new_folder, display_name)

    return updated_index


# ---------------------------------------------------------------------------
# ADB pull state
# ---------------------------------------------------------------------------


def build_archive_filename_index(conn: sqlite3.Connection) -> dict:
    """Return {basename: (size_or_none, md5)} for all archived files.

    Used as the delta pre-filter for ADB pull: O(1) lookup by filename.
    If the same basename appears at multiple paths, the last row wins — any
    match is sufficient to identify a file as already archived.
    """
    index = {}
    for original_path, md5, size in conn.execute("SELECT original_path, md5, size FROM files"):
        basename = os.path.basename(original_path.replace("\\", "/"))
        index[basename] = (size, md5)
    return index


def get_adb_done_paths(conn: sqlite3.Connection, device_serial: str) -> set:
    """Return remote paths already successfully pulled for this device."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT remote_path FROM adb_pull_state WHERE device_serial = ? AND status = 'done'",
            (device_serial,),
        )
    }


def get_adb_partial_paths(conn: sqlite3.Connection, device_serial: str) -> list:
    """Return remote paths that were partially pulled (interrupted) for this device."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT remote_path FROM adb_pull_state WHERE device_serial = ? AND status = 'partial'",
            (device_serial,),
        )
    ]


def upsert_adb_pull_state(
    conn: sqlite3.Connection, remote_path: str, device_serial: str, status: str
):
    import datetime

    conn.execute(
        "INSERT INTO adb_pull_state (remote_path, device_serial, status, pulled_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(remote_path, device_serial) DO UPDATE SET "
        "status = excluded.status, pulled_at = excluded.pulled_at",
        (remote_path, device_serial, status, datetime.datetime.now(datetime.UTC).isoformat()),
    )
    conn.commit()


def remove_adb_pull_state(conn: sqlite3.Connection, remote_path: str, device_serial: str):
    conn.execute(
        "DELETE FROM adb_pull_state WHERE remote_path = ? AND device_serial = ?",
        (remote_path, device_serial),
    )
    conn.commit()
