import sys

if sys.version_info < (3, 11):
    print(
        f"ERROR: Python 3.11 or higher is required "
        f"(you are running {sys.version.split()[0]}).\n"
        "  Download the latest Python from https://www.python.org/downloads/",
        file=sys.stderr,
    )
    sys.exit(1)

import argparse
import atexit
import contextlib
import csv
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import tomllib
import zlib
from datetime import date, datetime

from shared import db, hashing

from . import (
    adb_extractor,
    android_handler,
    archive_db,
    backup_reader,
    increment_merger,
    ios_handler,
    preservation,
)
from .progress import ProgressReporter

# ==============================================================================
# WhatsApp Backup Archiver
# Archives WhatsApp media into a structured folder hierarchy using msgstore.db
# (Android) or ChatStorage.sqlite (iOS). Run on a backup copy of your data.
# Requires Python 3.11+.
# ==============================================================================


def _get_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("whatsapp-backup-tools")
    except Exception:
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


# ---------------------------------------------------------------------------
# Helpers (shared)
# ---------------------------------------------------------------------------

sanitize_filename = db.sanitize_filename
escape_like = db.escape_like
format_phone = db.format_phone
build_contact_folder_name = db.build_contact_folder_name
file_md5 = hashing.file_md5


def set_file_times(path: str, timestamp_ms: int):
    """Set file access and modification time from a WhatsApp timestamp (ms)."""
    ts = timestamp_ms / 1000.0
    os.utime(path, (ts, ts))


def get_year(timestamp_ms: int, tz=None) -> str:
    """Extract year string from a WhatsApp timestamp (ms)."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=tz).strftime("%Y")


def human_datetime(timestamp_ms: int, tz=None) -> str:
    """Convert WhatsApp timestamp (ms) to a human-readable string."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def append_sender_to_filename(filename: str, sender: str) -> str:
    """
    Append sender name to a filename before the extension.
    e.g. IMG-20260512-WA0003.jpg -> IMG-20260512-WA0003_JohnDoe.jpg
    """
    root, ext = os.path.splitext(filename)
    safe_sender = sanitize_filename(sender)
    return f"{root}_{safe_sender}{ext}"


def resolve_unique_dest(
    dest_path: str, src_path: str, logger: logging.Logger, src_hash: bytes | None = None
) -> str | None:
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


def probe_hardlink_support(target_dir: str, logger: logging.Logger) -> bool:
    """
    Test whether the filesystem hosting target_dir supports creating hard links.
    Creates an ephemeral probe file, attempts os.link, and removes both files.
    Returns True if supported, or False if unsupported (e.g. FAT32, exFAT, unsupported SMB)
    with a warning logged.
    """
    os.makedirs(target_dir, exist_ok=True)
    probe_src = os.path.join(target_dir, f".wa_hardlink_probe_{os.getpid()}_{id(target_dir)}.tmp")
    probe_dst = probe_src + ".link"
    try:
        with open(probe_src, "wb") as f:
            f.write(b"probe")
        try:
            os.link(probe_src, probe_dst)
            return True
        except OSError as e:
            logger.warning(
                f"Target filesystem at '{target_dir}' does not support hard links ({e}). "
                "Hardlinking will be disabled and standard copy mode will be used."
            )
            return False
    except OSError as e:
        logger.warning(
            f"Failed to perform hardlink support probe at '{target_dir}' ({e}). "
            "Hardlinking will be disabled and standard copy mode will be used."
        )
        return False
    finally:
        for p in (probe_dst, probe_src):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(log_path: str, debug: bool) -> logging.Logger:
    """Configure logging to both file (DEBUG if debug is True else INFO) and console (INFO)."""
    logger = logging.getLogger("wab_archiver")
    target_level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(target_level)

    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(target_level)
        fh.setFormatter(fmt)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)

        logger.addHandler(fh)
        logger.addHandler(ch)
    else:
        logger.setLevel(target_level)
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                h.setLevel(target_level)
            elif isinstance(h, logging.StreamHandler):
                h.setLevel(logging.INFO)
    return logger


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_db_platform(path: str) -> str:
    """
    Inspect a WhatsApp database and return 'ios' or 'android'.
    iOS databases contain ZWAMESSAGE; Android databases do not.
    """
    try:
        with contextlib.closing(sqlite3.connect(path)) as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        return "ios" if "ZWAMESSAGE" in tables else "android"
    except sqlite3.DatabaseError as e:
        logger = logging.getLogger("wab_archiver")
        logger.error(f"Database file '{path}' is not a valid SQLite database: {e}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Source conflict CSV report
# ---------------------------------------------------------------------------


def write_conflict_report(report_path: str, rows: list, logger: logging.Logger):
    """Write a CSV report of files that resolved differently across multiple wa_roots."""
    if not rows:
        logger.debug("No source conflicts found.")
        return
    fieldnames = [
        "file_path",
        "chosen_source",
        "chosen_size",
        "rejected_source",
        "rejected_size",
        "reason",
    ]
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.debug(f"Source conflicts report written to: {report_path} ({len(rows)} conflict(s))")


def write_missing_report(report_path: str, rows: list, logger: logging.Logger):
    """Write the collected missing media rows to a CSV file."""
    if not rows:
        logger.debug("No missing media entries to report.")
        return
    fieldnames = [
        "message_id",
        "timestamp_human",
        "original_filename",
        "mime_type",
        "sender",
        "chat_name",
        "direction",
        "file_path",
        "message_url",
        "media_name",
    ]
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.debug(f"Missing media report written to: {report_path} ({len(rows)} entries)")


# ---------------------------------------------------------------------------
# Duplicate media CSV report
# ---------------------------------------------------------------------------

_DUPLICATE_SECTIONS = [
    ("image", "PHOTOS & IMAGES"),
    ("video", "VIDEOS"),
    ("document", "DOCUMENTS"),
    ("audio", "AUDIO & VOICE NOTES"),
    ("gif", "ANIMATED GIFS"),
    ("vcard", "CONTACTS"),
    ("sticker", "STICKERS"),
    ("other", "OTHER FILES"),
]


def _determine_duplicate_group_media_type(grp_rows: list) -> str:
    """Determine canonical media type for a duplicate group from candidate paths."""
    for _, _, _, orig_p in grp_rows:
        if orig_p:
            mt = db.classify_media_type(orig_p, None)
            if mt in ("sticker", "gif", "vcard"):
                return mt
    for arch_p, _, _, _ in grp_rows:
        mt = db.classify_media_type(arch_p, None)
        if mt in ("sticker", "gif", "vcard"):
            return mt

    first_arch_p, _, _, first_orig_p = grp_rows[0]
    return db.classify_media_type(first_arch_p, first_orig_p)


class DuplicateReportSummary(tuple):
    """2-tuple (group_count, file_count) with section breakdown metadata."""

    def __new__(cls, group_count: int, file_count: int, sections: list):
        obj = super().__new__(cls, (group_count, file_count))
        obj.sections = sections
        return obj


def write_duplicate_report(
    report_path: str,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    tz,
):
    """Write a CSV report of media files with identical content at multiple paths, grouped into sections."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(archive_copies)")}
    ts_expr = "MIN(ac.timestamp_ms)" if "timestamp_ms" in cols else "NULL"
    rows = conn.execute(f"""
        WITH dup_hashes AS (
            SELECT f.md5, COUNT(DISTINCT ac.archive_path) AS total
            FROM files f
            JOIN archive_copies ac ON ac.original_path = f.original_path
            GROUP BY f.md5
            HAVING total >= 2
        )
        SELECT dh.md5, dh.total, ac.archive_path, MAX(ac.is_hardlink) AS is_hardlink, {ts_expr} AS timestamp_ms, MIN(f.original_path) AS orig_path
        FROM dup_hashes dh
        JOIN files f ON f.md5 = dh.md5
        JOIN archive_copies ac ON ac.original_path = f.original_path
        GROUP BY dh.md5, dh.total, ac.archive_path
        ORDER BY dh.total DESC, dh.md5, ac.archive_path ASC
    """).fetchall()

    if not rows:
        logger.debug("No duplicate media found.")
        return DuplicateReportSummary(0, 0, [])

    groups_by_md5: dict[bytes, dict] = {}
    for md5_bytes, file_count, archived_path, is_hardlink, timestamp_ms, orig_path in rows:
        if md5_bytes not in groups_by_md5:
            groups_by_md5[md5_bytes] = {
                "file_count": file_count,
                "md5_bytes": md5_bytes,
                "rows": [],
            }
        groups_by_md5[md5_bytes]["rows"].append(
            (archived_path, is_hardlink, timestamp_ms, orig_path)
        )

    known_section_keys = {sec[0] for sec in _DUPLICATE_SECTIONS}
    sections: dict[str, list[dict]] = {sec[0]: [] for sec in _DUPLICATE_SECTIONS}
    for grp in groups_by_md5.values():
        m_type = _determine_duplicate_group_media_type(grp["rows"])
        grp["media_type"] = m_type
        sec_key = m_type if m_type in known_section_keys else "other"
        sections[sec_key].append(grp)

    for sec_key in sections:
        sections[sec_key].sort(key=lambda g: (-g["file_count"], g["md5_bytes"]))

    fieldnames = [
        "media_type",
        "group_id",
        "file_count",
        "archived_path",
        "md5_hex",
        "link_status",
        "original_timestamp",
    ]
    current_group_id = 0
    total_files = 0
    output_root = os.path.dirname(os.path.abspath(report_path))
    first_section = True

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        raw_writer = csv.writer(f)
        dict_writer = csv.DictWriter(f, fieldnames=fieldnames)
        dict_writer.writeheader()

        for sec_key, sec_title in _DUPLICATE_SECTIONS:
            sec_groups = sections.get(sec_key, [])
            if not sec_groups:
                continue

            sec_file_count = sum(len(g["rows"]) for g in sec_groups)
            group_label = "duplicate group" if len(sec_groups) == 1 else "duplicate groups"
            file_label = "file" if sec_file_count == 1 else "files"

            if not first_section:
                raw_writer.writerow([])
            first_section = False

            divider_text = f"# --- {sec_title} ({len(sec_groups)} {group_label}, {sec_file_count} {file_label}) ---"
            raw_writer.writerow([divider_text, "", "", "", "", "", ""])

            for grp in sec_groups:
                current_group_id += 1
                md5_hex = grp["md5_bytes"].hex()
                file_count = grp["file_count"]
                m_type = grp["media_type"]

                for archived_path, is_hardlink, timestamp_ms, _ in grp["rows"]:
                    total_files += 1
                    abs_file = os.path.join(output_root, *archived_path.split("/"))
                    is_hl = 1 if is_hardlink else 0
                    if os.path.isfile(abs_file):
                        try:
                            if os.stat(abs_file).st_nlink > 1:
                                is_hl = 1
                        except OSError:
                            pass
                    link_status = "hardlink" if is_hl else "duplicate_copy"
                    dict_writer.writerow(
                        {
                            "media_type": m_type,
                            "group_id": current_group_id,
                            "file_count": file_count,
                            "archived_path": archived_path,
                            "md5_hex": md5_hex,
                            "link_status": link_status,
                            "original_timestamp": human_datetime(timestamp_ms, tz)
                            if timestamp_ms
                            else "",
                        }
                    )

    logger.debug(
        f"Duplicate media report written to: {report_path} "
        f"({current_group_id} group(s), {total_files} file(s))"
    )
    section_breakdown = []
    for sec_key, sec_title in _DUPLICATE_SECTIONS:
        sec_groups = sections.get(sec_key, [])
        if sec_groups:
            sec_file_count = sum(len(g["rows"]) for g in sec_groups)
            section_breakdown.append((sec_title, len(sec_groups), sec_file_count))
            logger.debug(
                f"  - {sec_title:<22}: {len(sec_groups)} group(s), {sec_file_count} file(s)"
            )
    return DuplicateReportSummary(current_group_id, total_files, section_breakdown)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _build_missing_row(
    msg_id,
    timestamp,
    file_path,
    mime_type,
    chat_subject,
    sender,
    key_from_me,
    message_url,
    media_name,
    contacts,
    number_map,
    filename="",
    tz=None,
) -> dict:
    """Build a missing-media report row for either a null file_path or a missing source file."""
    canonical = number_map.get(sender, sender) if sender else None
    if chat_subject is not None:
        chat_display = chat_subject
        sender_display = (
            "Me"
            if (key_from_me == 1 or not sender)
            else contacts.get(canonical, format_phone(canonical or ""))
        )
    else:
        chat_display = contacts.get(canonical, canonical) if canonical else "Unknown"
        sender_display = (
            "Me"
            if key_from_me == 1
            else (contacts.get(canonical, canonical) if canonical else "Unknown")
        )
    return {
        "message_id": msg_id,
        "timestamp_human": human_datetime(timestamp, tz) if timestamp else "",
        "original_filename": filename,
        "mime_type": mime_type or "",
        "sender": sender_display,
        "chat_name": chat_display or "",
        "direction": "Sent" if key_from_me == 1 else "Received",
        "file_path": file_path or "",
        "message_url": message_url or "",
        "media_name": media_name or "",
    }


def _route_group(
    chat_row_id,
    chat_subject,
    sender,
    key_from_me,
    contacts,
    number_map,
    filename,
    year,
    output_root,
    group_index,
) -> tuple[str, str]:
    """Resolve dest_dir and dest_filename for a group chat message. Mutates group_index."""
    group_name = archive_db.resolve_group_folder(chat_row_id, chat_subject, group_index)
    canonical = number_map.get(sender, sender)
    sender_display = (
        "Me"
        if (key_from_me == 1 or not sender)
        else contacts.get(canonical, format_phone(canonical))
    )
    dest_filename = append_sender_to_filename(filename, sender_display)
    dest_dir = os.path.join(output_root, "Groups", group_name, year)
    return dest_dir, dest_filename


def _route_contact(
    sender, key_from_me, contacts, number_map, filename, year, output_root, contact_index
) -> tuple[str, str]:
    """Resolve dest_dir and dest_filename for a 1-to-1 chat message. Mutates contact_index."""
    contact_number = number_map.get(sender, sender) if sender else "unknown"
    contact_display = contacts.get(contact_number, None)
    if contact_display is None and contact_number in contact_index:
        _, stored_display = contact_index[contact_number]
        contact_display = stored_display or None
    folder_name = build_contact_folder_name(contact_display or contact_number, contact_number)
    contact_index[contact_number] = (folder_name, contact_display or "")
    direction = "Sent" if key_from_me == 1 else "Received"
    dest_dir = os.path.join(output_root, "Contacts", folder_name, year, direction)
    return dest_dir, filename


class CopyResult(tuple):
    """3-tuple preserving (copied, skipped, warnings) contract with extra is_hardlink attribute."""

    def __new__(cls, copied: int, skipped: int, warnings: int, is_hardlink: int):
        instance = super().__new__(cls, (copied, skipped, warnings))
        instance.is_hardlink = is_hardlink
        return instance


def _copy_or_skip(
    src: str,
    dest_path: str,
    dest_dir: str,
    file_path: str,
    timestamp: int | None,
    output_root: str,
    dry_run: bool,
    logger: logging.Logger,
    cursor: sqlite3.Cursor | None,
    link_duplicates: bool,
    hash_to_canonical: dict[bytes, str],
) -> tuple[int, int, int]:
    """
    Copy or hardlink src to dest, handling dry-run, dedup, and collision.
    Returns (copied, skipped, warnings) deltas.
    """
    if dry_run:
        resolved = resolve_unique_dest(dest_path, src, logger)
        if resolved is None:
            return CopyResult(0, 1, 0, 0)
        if link_duplicates:
            src_hash = file_md5(src)
            canonical = hash_to_canonical.get(src_hash)
            if canonical and canonical != resolved:
                logger.info(f"[DRY RUN] Would link: {canonical} -> {resolved}")
                return CopyResult(1, 0, 0, 1)
            hash_to_canonical[src_hash] = resolved
        logger.info(f"[DRY RUN] Would copy: {src} -> {resolved}")
        return CopyResult(1, 0, 0, 0)

    if os.path.exists(dest_path):
        src_hash = file_md5(src)
        resolved = resolve_unique_dest(dest_path, src, logger, src_hash=src_hash)
        if resolved is None:
            if cursor is not None:
                rel = os.path.relpath(dest_path, output_root).replace(os.sep, "/")
                is_hardlink_val = 1 if os.stat(dest_path).st_nlink > 1 else 0
                archive_db.record_file_archived(
                    cursor,
                    file_path,
                    src_hash,
                    rel,
                    os.path.getsize(dest_path),
                    is_hardlink_val,
                    timestamp,
                )
            if src_hash not in hash_to_canonical:
                hash_to_canonical[src_hash] = dest_path
            return CopyResult(0, 1, 0, 0)
    else:
        src_hash = None
        resolved = dest_path

    os.makedirs(dest_dir, exist_ok=True)
    is_hardlink = 0

    if link_duplicates:
        if src_hash is None:
            src_hash = file_md5(src)
        canonical = hash_to_canonical.get(src_hash)
        if canonical and canonical != resolved and os.path.isfile(canonical):
            try:
                os.link(canonical, resolved)
                is_hardlink = 1
                logger.debug(f"LINKED: {canonical} -> {resolved}")
            except OSError as e:
                logger.warning(
                    f"Could not hard link {canonical} -> {resolved} ({e}). Falling back to copy."
                )
                try:
                    shutil.copy(src, resolved)
                    logger.debug(f"COPIED (fallback): {src} -> {resolved}")
                except Exception as copy_err:
                    logger.error(f"ERROR copying {src} -> {resolved}: {copy_err}")
                    return CopyResult(0, 0, 1, 0)
        else:
            try:
                shutil.copy(src, resolved)
                logger.debug(f"COPIED: {src} -> {resolved}")
            except Exception as e:
                logger.error(f"ERROR copying {src} -> {resolved}: {e}")
                return CopyResult(0, 0, 1, 0)
            hash_to_canonical[src_hash] = resolved
    else:
        try:
            shutil.copy(src, resolved)
            logger.debug(f"COPIED: {src} -> {resolved}")
        except Exception as e:
            logger.error(f"ERROR copying {src} -> {resolved}: {e}")
            return CopyResult(0, 0, 1, 0)

    if timestamp is not None:
        try:
            set_file_times(resolved, timestamp)
        except OSError as e:
            logger.debug(f"Could not set timestamps on {resolved}: {e}")

    if src_hash is None:
        src_hash = file_md5(resolved)
    if src_hash not in hash_to_canonical:
        hash_to_canonical[src_hash] = resolved

    if cursor is not None:
        rel = os.path.relpath(resolved, output_root).replace(os.sep, "/")
        archive_db.record_file_archived(
            cursor, file_path, src_hash, rel, os.path.getsize(resolved), is_hardlink, timestamp
        )
    return CopyResult(1, 0, 0, is_hardlink)


def process_rows(
    rows,
    total: int,
    contacts,
    number_map,
    folder_index,
    group_index,
    media_resolver,
    output_root,
    logger,
    dry_run,
    conn,
    tz,
    link_duplicates: bool,
    hash_to_canonical: dict[bytes, str],
):
    stats = {
        "copied": 0,
        "skipped": 0,
        "missing": 0,
        "warnings": 0,
        "hardlinked": 0,
        "skipped_preexisting": 0,
        "skipped_duplicate": 0,
        "skipped_empty_vcard": 0,
    }
    written_dest_paths: set[str] = set()
    updated_index = dict(folder_index)
    updated_group_index = dict(group_index)
    missing_rows = []
    cursor = conn.cursor() if conn is not None else None

    progress = ProgressReporter("Progress", total, logger, sys.stderr, 0.2)
    thumb_dir_ensured = False

    for i, row in enumerate(rows, 1):
        (
            msg_id,
            timestamp,
            file_path,
            mime_type,
            chat_row_id,
            chat_subject,
            sender,
            key_from_me,
            message_url,
            media_name,
        ) = row[:10]
        thumb_path = row[10] if len(row) > 10 else None
        is_group = chat_subject is not None
        filename = media_name if media_name else (os.path.basename(file_path) if file_path else "")

        def _extract_thumb_if_missing():
            nonlocal thumb_dir_ensured
            if thumb_path:
                src_thumb = media_resolver(thumb_path)
                if src_thumb and os.path.isfile(src_thumb):
                    thumb_dest_dir = os.path.join(output_root, "Whatsapp Databases", ".thumbnails")
                    thumb_filename = os.path.basename(thumb_path)
                    dest_thumb = os.path.join(thumb_dest_dir, thumb_filename)
                    if not dry_run:
                        if not thumb_dir_ensured:
                            os.makedirs(thumb_dest_dir, exist_ok=True)
                            thumb_dir_ensured = True
                        try:
                            if not os.path.exists(dest_thumb):
                                shutil.copy2(src_thumb, dest_thumb)
                            if cursor is not None:
                                rel = os.path.relpath(dest_thumb, output_root).replace(os.sep, "/")
                                archive_db.record_file_archived(
                                    cursor,
                                    thumb_path,
                                    b"",
                                    rel,
                                    os.path.getsize(dest_thumb),
                                    0,
                                    timestamp,
                                )
                        except OSError:
                            pass

        if not file_path:
            logger.debug(f"SKIP (no file_path): message_id={msg_id}")
            _extract_thumb_if_missing()
            missing_rows.append(
                _build_missing_row(
                    msg_id,
                    timestamp,
                    None,
                    mime_type,
                    chat_subject,
                    sender,
                    key_from_me,
                    message_url,
                    media_name,
                    contacts,
                    number_map,
                    filename=filename,
                    tz=tz,
                )
            )
            stats["missing"] += 1
            progress.update(
                i,
                {
                    "Copied": stats["copied"],
                    "Skipped": stats["skipped"],
                    "Missing": stats["missing"],
                },
            )
            continue

        src = media_resolver(file_path)

        if src is None or not os.path.isfile(src):
            logger.warning(f"MISSING source file (message_id={msg_id}): {file_path}")
            _extract_thumb_if_missing()
            missing_rows.append(
                _build_missing_row(
                    msg_id,
                    timestamp,
                    file_path,
                    mime_type,
                    chat_subject,
                    sender,
                    key_from_me,
                    message_url,
                    media_name,
                    contacts,
                    number_map,
                    filename=filename,
                    tz=tz,
                )
            )
            stats["missing"] += 1
            progress.update(
                i,
                {
                    "Copied": stats["copied"],
                    "Skipped": stats["skipped"],
                    "Missing": stats["missing"],
                },
            )
            continue

        if timestamp is None:
            logger.warning(f"SKIP (no timestamp): message_id={msg_id}, file={file_path}")
            missing_rows.append(
                _build_missing_row(
                    msg_id,
                    timestamp,
                    file_path,
                    mime_type,
                    chat_subject,
                    sender,
                    key_from_me,
                    message_url,
                    media_name,
                    contacts,
                    number_map,
                    filename=filename,
                    tz=tz,
                )
            )
            stats["missing"] += 1
            progress.update(
                i,
                {
                    "Copied": stats["copied"],
                    "Skipped": stats["skipped"],
                    "Missing": stats["missing"],
                },
            )
            continue

        year = get_year(timestamp, tz)

        if is_group:
            dest_dir, dest_filename = _route_group(
                chat_row_id,
                chat_subject,
                sender,
                key_from_me,
                contacts,
                number_map,
                filename,
                year,
                output_root,
                updated_group_index,
            )
        else:
            dest_dir, dest_filename = _route_contact(
                sender,
                key_from_me,
                contacts,
                number_map,
                filename,
                year,
                output_root,
                updated_index,
            )

        dest_path = os.path.join(dest_dir, dest_filename)
        dest_existed_before = dest_path not in written_dest_paths and os.path.exists(dest_path)
        res = _copy_or_skip(
            src,
            dest_path,
            dest_dir,
            file_path,
            timestamp,
            output_root,
            dry_run,
            logger,
            cursor,
            link_duplicates,
            hash_to_canonical,
        )
        copied, skipped, warnings = res
        stats["copied"] += copied
        stats["skipped"] += skipped
        stats["warnings"] += warnings
        if copied:
            written_dest_paths.add(dest_path)
            if getattr(res, "is_hardlink", 0):
                stats["hardlinked"] += 1
        elif skipped:
            if dest_existed_before:
                stats["skipped_preexisting"] += 1
                written_dest_paths.add(dest_path)
            else:
                stats["skipped_duplicate"] += 1
        progress.update(
            i, {"Copied": stats["copied"], "Skipped": stats["skipped"], "Missing": stats["missing"]}
        )

    progress.finish(
        {"Copied": stats["copied"], "Skipped": stats["skipped"], "Missing": stats["missing"]}
    )
    return stats, updated_index, updated_group_index, missing_rows


def resolve_unique_vcard_dest(
    dest_path: str, vcard_hash: bytes, logger: logging.Logger
) -> str | None:
    """Determine whether to write, skip, or rename for an in-memory vCard payload."""
    if not os.path.exists(dest_path):
        return dest_path

    dst_hash = file_md5(dest_path)
    if vcard_hash == dst_hash:
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


def process_vcard_rows(
    rows: list,
    contacts: dict,
    number_map: dict,
    folder_index: dict,
    group_index: dict,
    output_root: str,
    logger: logging.Logger,
    dry_run: bool,
    conn: sqlite3.Connection | None,
    tz,
) -> tuple[dict, dict, dict]:
    """
    Extract vCard contacts to .vcf files in the archive output folder as regular media.
    Routes to Groups/<Group>/<Year>/<Filename> or Contacts/<Contact>/<Year>/<Sent|Received>/<Filename>.
    Tracks files in files and archive_copies tables in .wa_media_archiver.db.
    """
    stats = {
        "copied": 0,
        "skipped": 0,
        "warnings": 0,
        "hardlinked": 0,
        "skipped_preexisting": 0,
        "skipped_duplicate": 0,
        "skipped_empty_vcard": 0,
    }
    written_dest_paths: set[str] = set()
    updated_index = dict(folder_index)
    updated_group_index = dict(group_index)
    cursor = conn.cursor() if conn is not None else None

    progress = ProgressReporter("Contacts", len(rows), logger, sys.stderr, 0.2)

    for i, row in enumerate(rows, 1):
        if isinstance(row, dict):
            msg_id = row["message_id"]
            timestamp_ms = int(row["timestamp_ms"])
            chat_row_id = row["chat_row_id"]
            chat_subject = row["chat_subject"]
            sender = row["sender"]
            key_from_me = row["key_from_me"]
            contact_name = row["contact_name"]
            vcard_text = row["vcard_text"]
        else:
            (
                msg_id,
                timestamp_ms,
                chat_row_id,
                chat_subject,
                sender,
                key_from_me,
                contact_name,
                vcard_text,
            ) = row
            timestamp_ms = int(timestamp_ms)

        if not vcard_text or not vcard_text.strip():
            stats["skipped"] += 1
            stats["skipped_empty_vcard"] += 1
            progress.update(i, {"Copied": stats["copied"], "Skipped": stats["skipped"]})
            continue

        is_group = chat_subject is not None
        disp_name = db.resolve_vcard_display_name(contact_name, vcard_text)
        filename = db.sanitize_filename(disp_name) + ".vcf"

        year = get_year(timestamp_ms, tz)

        if is_group:
            dest_dir, dest_filename = _route_group(
                chat_row_id,
                chat_subject,
                sender,
                key_from_me,
                contacts,
                number_map,
                filename,
                year,
                output_root,
                updated_group_index,
            )
        else:
            dest_dir, dest_filename = _route_contact(
                sender,
                key_from_me,
                contacts,
                number_map,
                filename,
                year,
                output_root,
                updated_index,
            )

        dest_path = os.path.join(dest_dir, dest_filename)
        vcard_bytes = vcard_text.strip().encode("utf-8") + b"\r\n"
        vcard_hash = hashing.bytes_md5(vcard_bytes)
        vcard_size = len(vcard_bytes)
        virtual_orig_path = f"vcard:{msg_id}"

        if dry_run:
            resolved = resolve_unique_vcard_dest(dest_path, vcard_hash, logger)
            if resolved is None:
                stats["skipped"] += 1
                if dest_path in written_dest_paths:
                    stats["skipped_duplicate"] += 1
                else:
                    stats["skipped_preexisting"] += 1
                    written_dest_paths.add(dest_path)
            else:
                logger.info(f"[DRY RUN] Would write vcard: {resolved}")
                stats["copied"] += 1
                written_dest_paths.add(resolved)
            progress.update(i, {"Copied": stats["copied"], "Skipped": stats["skipped"]})
            continue

        if os.path.exists(dest_path):
            resolved = resolve_unique_vcard_dest(dest_path, vcard_hash, logger)
            if resolved is None:
                if cursor is not None:
                    rel = os.path.relpath(dest_path, output_root).replace(os.sep, "/")
                    archive_db.record_file_archived(
                        cursor, virtual_orig_path, vcard_hash, rel, vcard_size, 0, timestamp_ms
                    )
                stats["skipped"] += 1
                if dest_path in written_dest_paths:
                    stats["skipped_duplicate"] += 1
                else:
                    stats["skipped_preexisting"] += 1
                    written_dest_paths.add(dest_path)
                progress.update(i, {"Copied": stats["copied"], "Skipped": stats["skipped"]})
                continue
        else:
            resolved = dest_path

        os.makedirs(dest_dir, exist_ok=True)
        try:
            with open(resolved, "wb") as f:
                f.write(vcard_bytes)
        except OSError as e:
            logger.error(f"ERROR writing vCard {resolved}: {e}")
            stats["warnings"] += 1
            progress.update(i, {"Copied": stats["copied"], "Skipped": stats["skipped"]})
            continue
        written_dest_paths.add(resolved)

        if timestamp_ms is not None:
            try:
                set_file_times(resolved, timestamp_ms)
            except OSError as e:
                logger.debug(f"Could not set timestamps on {resolved}: {e}")

        logger.debug(f"SAVED VCARD: {resolved}")
        if cursor is not None:
            rel = os.path.relpath(resolved, output_root).replace(os.sep, "/")
            archive_db.record_file_archived(
                cursor, virtual_orig_path, vcard_hash, rel, vcard_size, 0, timestamp_ms
            )
        stats["copied"] += 1
        progress.update(i, {"Copied": stats["copied"], "Skipped": stats["skipped"]})

    progress.finish({"Copied": stats["copied"], "Skipped": stats["skipped"]})
    return stats, updated_index, updated_group_index


# ---------------------------------------------------------------------------
# Restore mode
# ---------------------------------------------------------------------------


def run_restore_mode(args, logger):
    """
    Reconstruct the original WhatsApp Media/ folder structure from the archive.
    Queries .wa_media_archiver.db to determine original file paths and their archive copies.
    The reconstructed tree is written to <output>/Media/.
    """
    conn = archive_db.open_archive_db(args.output)
    try:
        archive_db.check_db_health(conn, logger)
        file_count = conn.execute(
            "SELECT COUNT(DISTINCT original_path) FROM archive_copies "
            "WHERE original_path NOT LIKE 'vcard:%'"
        ).fetchone()[0]
        if file_count == 0:
            logger.error(
                "No restore data found in the archive database.\n"
                "  No files have been archived yet. Re-run the archiver (forward mode) "
                "to build the restore data, then run restore mode again."
            )
            raise SystemExit(1)

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

        stats = {"restored": 0, "skipped": 0, "unrestorable": 0, "warnings": 0}
        report_rows = []
        restore_report_path = os.path.join(args.output, "restore_report.csv")

        restore_map: dict[str, list[str]] = {}
        for original_path, archive_path in conn.execute(
            "SELECT original_path, archive_path FROM archive_copies "
            "WHERE original_path NOT LIKE 'vcard:%' "
            "ORDER BY original_path, rowid"
        ):
            restore_map.setdefault(original_path, []).append(archive_path)

        progress = ProgressReporter("Restore", len(restore_map), logger, sys.stderr, 0.2)

        for i, (original_path, archive_paths) in enumerate(restore_map.items(), 1):
            src = None
            src_rel = None
            for rel_path in archive_paths:
                candidate = os.path.join(args.output, *rel_path.split("/"))
                if os.path.isfile(candidate):
                    src = candidate
                    src_rel = rel_path
                    break

            if src is None:
                logger.warning(f"UNRESTORABLE (no archive copy found): {original_path}")
                stats["unrestorable"] += 1
                report_rows.append(
                    {
                        "original_path": original_path,
                        "source_archive_path": "",
                        "status": "unrestorable",
                    }
                )
                progress.update(
                    i,
                    {
                        "Restored": stats["restored"],
                        "Skipped": stats["skipped"],
                        "Unrestorable": stats["unrestorable"],
                    },
                )
                continue

            dest = os.path.join(args.output, *original_path.split("/"))

            if os.path.exists(dest):
                if file_md5(src) == file_md5(dest):
                    logger.debug(f"SKIP (identical already restored): {original_path}")
                    stats["skipped"] += 1
                    progress.update(
                        i,
                        {
                            "Restored": stats["restored"],
                            "Skipped": stats["skipped"],
                            "Unrestorable": stats["unrestorable"],
                        },
                    )
                    continue
                logger.warning(
                    f"{'[DRY RUN] ' if args.dry_run else ''}"
                    f"COLLISION at restore destination (different content): {dest}"
                )
                stats["warnings"] += 1
                if not args.dry_run:
                    report_rows.append(
                        {
                            "original_path": original_path,
                            "source_archive_path": src_rel,
                            "status": "collision_skipped",
                        }
                    )
                progress.update(
                    i,
                    {
                        "Restored": stats["restored"],
                        "Skipped": stats["skipped"],
                        "Unrestorable": stats["unrestorable"],
                    },
                )
                continue

            if args.dry_run:
                logger.info(f"[DRY RUN] Would restore: {original_path}")
                stats["restored"] += 1
                progress.update(
                    i,
                    {
                        "Restored": stats["restored"],
                        "Skipped": stats["skipped"],
                        "Unrestorable": stats["unrestorable"],
                    },
                )
                continue

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.copy2(src, dest)
                logger.debug(f"RESTORED: {original_path}")
                stats["restored"] += 1
            except Exception as e:
                logger.error(f"ERROR restoring {original_path}: {e}")
                stats["warnings"] += 1
                report_rows.append(
                    {
                        "original_path": original_path,
                        "source_archive_path": src_rel,
                        "status": f"error: {e}",
                    }
                )
            progress.update(
                i,
                {
                    "Restored": stats["restored"],
                    "Skipped": stats["skipped"],
                    "Unrestorable": stats["unrestorable"],
                },
            )

        progress.finish(
            {
                "Restored": stats["restored"],
                "Skipped": stats["skipped"],
                "Unrestorable": stats["unrestorable"],
            }
        )

        arch_db_source = os.path.join(args.output, "Whatsapp Databases", "msgstore.db")
        if os.path.isfile(arch_db_source):
            db_dest = os.path.join(args.output, "Databases", "msgstore.db")
            if os.path.exists(db_dest) and file_md5(arch_db_source) == file_md5(db_dest):
                logger.debug("SKIP database (identical already restored): Databases/msgstore.db")
                stats["skipped"] += 1
            elif args.dry_run:
                logger.info("[DRY RUN] Would restore database: Databases/msgstore.db")
                stats["restored"] += 1
            else:
                os.makedirs(os.path.dirname(db_dest), exist_ok=True)
                try:
                    shutil.copy2(arch_db_source, db_dest)
                    logger.info("RESTORED database: Databases/msgstore.db")
                    stats["restored"] += 1
                except Exception as e:
                    logger.error(f"ERROR restoring database Databases/msgstore.db: {e}")
                    stats["warnings"] += 1
                    report_rows.append(
                        {
                            "original_path": "Databases/msgstore.db",
                            "source_archive_path": "Whatsapp Databases/msgstore.db",
                            "status": f"error: {e}",
                        }
                    )

        if not args.dry_run:
            if report_rows:
                fieldnames = ["original_path", "source_archive_path", "status"]
                with open(restore_report_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(report_rows)
                logger.info(
                    f"Restore report written to: {restore_report_path} "
                    f"({len(report_rows)} issue(s))"
                )
            else:
                logger.info("No restore issues to report.")

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
# Config file
# ---------------------------------------------------------------------------

_VALID_CONFIG_KEYS = {
    "output",
    "msgstore",
    "e2e_key",
    "wa_root",
    "contacts",
    "log",
    "debug",
    "business",
    "timezone",
    "since",
    "ios_backup",
    "ios_password",
    "ios_contacts",
    "from_adb",
    "pull_media",
    "staging",
    "link_duplicates",
    "preserve_deleted",
}

_EXAMPLE_CONFIG = """\
# =============================================================================
# wab-archiver config
#
# Rename this file to config.toml to activate it.
# All paths can be absolute or relative to where you run the command.
# Remove the leading '#' to uncomment and activate any setting.
#
# Windows tip: You can paste Windows paths as-is (e.g. C:\\Users\\...);
# backslashes are automatically converted to forward slashes.
# =============================================================================

# -----------------------------------------------------------------------------
# Archive Destination & General Settings
# -----------------------------------------------------------------------------
output     = "/path/to/archive"         # Destination folder for the archive (required)
# log      = ""                         # Custom log file path (default: <output>/wab-archiver.log)
# debug    = false                      # Enable debug logging in the log file (default: false, INFO level)
# timezone = ""                         # IANA timezone (e.g. Europe/Rome, UTC) for year folders & reports
# business = false                      # Target WhatsApp Business instead of regular WhatsApp

# -----------------------------------------------------------------------------
# Archiving Features & Filtering
# -----------------------------------------------------------------------------
# preserve_deleted = true               # Preserve messages/chats deleted on phone or revoked by sender across runs
# link_duplicates  = true               # Save disk space by hardlinking duplicate media across chats
# since            = ""                 # Only archive media from messages on or after this date (YYYY-MM-DD)

# -----------------------------------------------------------------------------
# Source: Android (Local Files & Folders)
# -----------------------------------------------------------------------------
# msgstore = "msgstore.db"              # Path to msgstore.db or msgstore.db.crypt15
# e2e_key  = ""                         # 64-char hex key for encrypted .crypt15 backups
# contacts = ""                         # Path to contacts CSV or exported wa_contacts

# Single WhatsApp source folder (folder containing Media/)
# wa_root  = "/path/to/WhatsApp"

# Multiple source folders — searched in order, best copy wins
# wa_root  = ["/path/to/old-archive", "/path/to/current-phone/WhatsApp"]

# -----------------------------------------------------------------------------
# Source: Android (ADB Mode — Automatic USB Pull)
# See docs/archiver/setup-android.md
# -----------------------------------------------------------------------------
# from_adb   = false                    # Pull msgstore and contacts automatically via ADB
# pull_media = false                    # Also pull media files from phone storage via ADB
# staging    = ""                       # Permanent local phone backup folder (copied to output)

# -----------------------------------------------------------------------------
# Source: iOS (iPhone Backup)
# See docs/archiver/setup-ios.md
# -----------------------------------------------------------------------------
# ios_backup   = ""                     # Path to iPhone backup folder (containing Manifest.db)
# ios_password = ""                     # Password for encrypted iPhone backup
# ios_contacts = ""                     # Path to ContactsV2.sqlite (auto-extracted from backup if omitted)
"""


def _flatten_config(data: dict) -> dict:
    """Recursively flatten TOML sections into a flat configuration dict."""
    flat = {}
    for k, v in data.items():
        if isinstance(v, dict):
            flat.update(_flatten_config(v))
        else:
            flat[k] = v
    return flat


def _load_toml(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return _flatten_config(tomllib.loads(raw.decode("utf-8")))
    except tomllib.TOMLDecodeError:
        fixed = re.sub(
            r'"([^"]*)"', lambda m: '"' + m.group(1).replace("\\", "/") + '"', raw.decode("utf-8")
        )
        try:
            result = tomllib.loads(fixed)
            with open(path, "wb") as f:
                f.write(fixed.encode("utf-8"))
            print(
                f"NOTE: Backslashes in paths in {path} were automatically converted "
                "to forward slashes and the file was updated.",
                file=sys.stderr,
            )
            return _flatten_config(result)
        except tomllib.TOMLDecodeError as e:
            print(f"ERROR: Could not parse config file {path}:\n  {e}", file=sys.stderr)
            sys.exit(1)
    except OSError as e:
        print(f"ERROR: Could not read config file {path}:\n  {e}", file=sys.stderr)
        sys.exit(1)


def _generate_config(script_dir: str):
    dest = os.path.join(script_dir, "example-config.toml")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(_EXAMPLE_CONFIG)
    print(f"Written: {dest}")
    print("Rename it to config.toml and edit the values to activate it.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_KNOWN_COMMANDS = frozenset({"archive", "restore", "config"})


def _inject_default_archive_command():
    """
    Inject 'archive' as the default subcommand when the user passes flags
    directly without specifying a subcommand (e.g. wab-archiver --wa-root … -o …).
    """
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "--version"):
        return
    if len(sys.argv) == 1 or sys.argv[1] not in _KNOWN_COMMANDS:
        sys.argv.insert(1, "archive")


def parse_args() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _cwd = os.getcwd()

    # Pre-scan for --config before argparse runs (it may appear anywhere).
    _config_path = None
    for _i, _arg in enumerate(sys.argv[1:], 1):
        if _arg.startswith("--config="):
            _config_path = _arg[len("--config=") :]
            break
        if _arg == "--config" and _i < len(sys.argv) - 1:
            _config_path = sys.argv[_i + 1]
            break

    # Auto-detect config.toml (skip for the 'config' subcommand).
    _is_config_cmd = len(sys.argv) > 1 and sys.argv[1] == "config"
    if _config_path is None and not _is_config_cmd:
        _candidates = []
        for d in dict.fromkeys([_script_dir, _cwd]):
            p = os.path.join(d, "config.toml")
            if os.path.isfile(p):
                _candidates.append(p)

        if len(_candidates) == 1:
            answer = (
                input(f"Found config.toml at {_candidates[0]} — use it? [Y/n] ").strip().lower()
            )
            if answer in ("", "y", "yes"):
                _config_path = _candidates[0]
        elif len(_candidates) > 1:
            print(
                "ERROR: Multiple config files found — specify one with --config:", file=sys.stderr
            )
            for p in _candidates:
                print(f"  {p}", file=sys.stderr)
            sys.exit(1)

    # Load and validate config.
    _config = {}
    if _config_path:
        if not _config_path.endswith(".toml"):
            print(
                f"ERROR: Config file must be a .toml file, got: {_config_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        _config = _load_toml(_config_path)
        _unknown = set(_config) - _VALID_CONFIG_KEYS
        if _unknown:
            print(
                f"ERROR: Unknown key(s) in config file {_config_path}:\n"
                + "\n".join(f"  {k}" for k in sorted(_unknown))
                + "\n  Check for typos. Valid keys: "
                + ", ".join(sorted(_VALID_CONFIG_KEYS)),
                file=sys.stderr,
            )
            sys.exit(1)
        if "since" in _config:
            _since_val = _config["since"]
            if isinstance(_since_val, date):
                _config["since"] = _since_val.isoformat()
            elif not isinstance(_since_val, str):
                print(
                    f"ERROR: 'since' in config must be a quoted date string "
                    f'(e.g. since = "2024-01-01"), got {type(_since_val).__name__}',
                    file=sys.stderr,
                )
                sys.exit(1)
        if "wa_root" in _config:
            _wa_val = _config.pop("wa_root")
            if isinstance(_wa_val, str):
                _config["wa_roots"] = [_wa_val]
            elif isinstance(_wa_val, list):
                _config["wa_roots"] = _wa_val
            else:
                print(
                    f"ERROR: 'wa_root' in config must be a string or list of strings, "
                    f"got {type(_wa_val).__name__}",
                    file=sys.stderr,
                )
                sys.exit(1)

    # Inject default subcommand so flags can be passed without typing 'archive'.
    _inject_default_archive_command()

    # -------------------------------------------------------------------------
    # Build parser
    # -------------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        prog="wab-archiver",
        description="Archive WhatsApp media into a structured folder hierarchy.",
    )
    parser.add_argument(
        "--version", action="version", version=f"WhatsApp Backup Tools — Archiver v{_get_version()}"
    )

    subs = parser.add_subparsers(dest="command", metavar="command")
    subs.required = True

    # --- archive (default command) ---
    archive_p = subs.add_parser(
        "archive",
        help="Build or update an archive from a source (default command).",
        description=("Archive WhatsApp media from an Android device, iOS backup, or via ADB."),
    )
    _src = archive_p.add_mutually_exclusive_group()
    _src.add_argument(
        "--wa-root",
        dest="wa_roots",
        action="append",
        default=None,
        metavar="PATH",
        help="Root path of WhatsApp folder on disk "
        "(Android: folder containing Media/; "
        "iOS pre-extracted: AppDomainGroup folder). "
        "Repeat to search multiple source folders (Android only).",
    )
    _src.add_argument(
        "--ios-backup",
        dest="ios_backup",
        default=None,
        metavar="PATH",
        help="Path to iPhone backup directory (contains Manifest.db).",
    )
    archive_p.add_argument(
        "--from-adb",
        dest="from_adb",
        action="store_true",
        help="Pull msgstore and contacts from a connected Android device via ADB.",
    )
    archive_p.add_argument(
        "--msgstore",
        default="msgstore.db",
        metavar="PATH",
        help="Path to msgstore.db[.crypt15] or ChatStorage.sqlite (default: current folder).",
    )
    archive_p.add_argument(
        "--e2e-key",
        dest="e2e_key",
        default=None,
        metavar="KEY",
        help="E2E decryption key for encrypted msgstore.db.crypt15.",
    )
    archive_p.add_argument(
        "-c",
        "--contacts",
        default=None,
        metavar="PATH",
        help="Path to an existing contacts file (e.g. wa_contacts exported via ADB, or a phone,name CSV). "
        "Auto-extracted when using --from-adb.",
    )
    archive_p.add_argument(
        "--ios-password",
        dest="ios_password",
        default=None,
        metavar="PASSWORD",
        help="Password for an encrypted iPhone backup.",
    )
    archive_p.add_argument(
        "--ios-contacts",
        dest="ios_contacts",
        default=None,
        metavar="PATH",
        help="Path to ContactsV2.sqlite from an iOS backup "
        "(optional; auto-extracted from --ios-backup if omitted).",
    )
    archive_p.add_argument(
        "--business",
        action="store_true",
        help="Target WhatsApp Business instead of the regular app. "
        "Affects the ADB pull path (Android) and backup domain (iOS).",
    )
    archive_p.add_argument(
        "--pull-media",
        dest="pull_media",
        action="store_true",
        help="Pull WhatsApp media files from the device via ADB. "
        "Only valid with --from-adb. Requires --staging.",
    )
    archive_p.add_argument(
        "--staging",
        dest="staging",
        default=None,
        metavar="PATH",
        help="Persistent local folder where ADB-pulled media is stored. "
        "Acts as a permanent copy of phone storage and wa_root; "
        "files are copied to output and never deleted. Required with --pull-media.",
    )
    archive_p.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="PATH",
        help="Output root folder for the archive.",
    )
    archive_p.add_argument(
        "-l",
        "--log",
        default=None,
        metavar="PATH",
        help="Log file path (default: <output>/wab-archiver.log).",
    )
    archive_p.add_argument(
        "--timezone",
        default=None,
        metavar="TZ",
        help="IANA timezone for year folders and report timestamps "
        "(e.g. Europe/Rome, America/New_York, UTC). "
        "Default: machine local time. "
        "Windows users: requires pip install tzdata.",
    )
    archive_p.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to a TOML config file. "
        "Auto-detected as config.toml in the script folder or cwd if present.",
    )
    archive_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the run without copying any files.",
    )
    archive_p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Limit the number of rows extracted per query block (e.g. --limit 250 for testing).",
    )
    archive_p.add_argument(
        "--since",
        default=None,
        metavar="DATE",
        help="Only archive media from messages sent or received on or after this date (YYYY-MM-DD). Combines with --limit.",
    )
    archive_p.add_argument(
        "--link-duplicates",
        action="store_true",
        dest="link_duplicates",
        default=False,
        help="Hardlink duplicate media files across chats instead of copying bytes.",
    )
    archive_p.add_argument(
        "-P",
        "--preserve-deleted",
        action="store_true",
        dest="preserve_deleted",
        default=False,
        help="Preserve messages deleted or revoked on device into preserved_messages.db.",
    )
    archive_p.add_argument(
        "-v",
        "--debug",
        "--verbose",
        action="store_true",
        dest="debug",
        default=False,
        help="Enable debug logging in the log file (default: false, INFO level).",
    )
    # Apply config defaults to the archive subparser.
    archive_p.set_defaults(**_config)

    # --- restore ---
    restore_p = subs.add_parser(
        "restore",
        help="Reconstruct the original Media/ tree from an archive (Android only).",
    )
    restore_p.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="PATH",
        help="Path to the archive to restore.",
    )
    restore_p.add_argument(
        "-l",
        "--log",
        default=None,
        metavar="PATH",
        help="Log file path.",
    )
    restore_p.add_argument(
        "-v",
        "--debug",
        "--verbose",
        action="store_true",
        dest="debug",
        default=False,
        help="Enable debug logging in the log file (default: false, INFO level).",
    )
    restore_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the restore without copying any files.",
    )
    restore_p.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to a TOML config file.",
    )
    _restore_config = {k: v for k, v in _config.items() if k in ("output", "log", "debug")}
    restore_p.set_defaults(**_restore_config)

    # --- config ---
    config_p = subs.add_parser("config", help="Manage configuration.")
    config_p.add_argument(
        "action",
        choices=["generate"],
        help="generate: write example-config.toml to the script folder.",
    )

    # -------------------------------------------------------------------------
    # Parse
    # -------------------------------------------------------------------------
    args = parser.parse_args()

    if args.command == "config":
        _generate_config(_script_dir)
        return args  # _generate_config calls sys.exit(0); unreachable

    if args.command == "restore":
        if not args.output:
            restore_p.error("the following arguments are required: -o/--output")
        return args

    # --- archive validation ---
    if not args.output:
        archive_p.error("the following arguments are required: -o/--output")

    if args.ios_backup:
        if args.wa_roots:
            archive_p.error("--wa-root and --ios-backup are mutually exclusive")
        if args.from_adb:
            archive_p.error("--from-adb and --ios-backup are mutually exclusive")
        if args.pull_media:
            archive_p.error("--pull-media and --ios-backup are mutually exclusive")
        if args.contacts:
            archive_p.error(
                "--contacts is for Android ADB exports and cannot be used with --ios-backup. "
                "iOS contacts are loaded automatically from the backup (ContactsV2.sqlite). "
                "Use --ios-contacts to supply a pre-extracted ContactsV2.sqlite instead."
            )
    else:
        if args.pull_media and not args.from_adb:
            archive_p.error("--pull-media requires --from-adb.")
        if args.pull_media and not args.staging:
            archive_p.error("--pull-media requires --staging.")
        if args.pull_media and args.wa_roots:
            archive_p.error("--wa-root and --pull-media are mutually exclusive.")
        if not args.wa_roots and not args.pull_media:
            archive_p.error("either --wa-root or --pull-media (with --staging) is required")

    return args


def _warn_network_paths(args: argparse.Namespace, logger: logging.Logger):
    """Warn if output or any wa_root appear to be UNC network share paths."""
    roots = getattr(args, "wa_roots", None) or []
    output = getattr(args, "output", None)
    for label, path in [("--output", output)] + [("--wa-root", r) for r in roots]:
        if path and (path.startswith("\\\\") or path.startswith("//")):
            logger.warning(
                f"{label} appears to be a network share ({path}). "
                "Performance may be degraded and file timestamps may not be preserved."
            )


def decrypt_msgstore(
    encrypted_path: str,
    e2e_key: str,
    output_path: str,
    logger: logging.Logger,
) -> str:
    """Decrypt an encrypted WhatsApp msgstore database (.crypt15) using wa-crypt-tools.

    Args:
        encrypted_path: Path to the encrypted msgstore file.
        e2e_key: Path to the encryption key file or 64-character hex key string.
        output_path: Destination path for the decrypted SQLite database.
        logger: Logger instance.

    Returns:
        The output path to the decrypted database file.
    """
    try:
        from wa_crypt_tools.lib.db.dbfactory import DatabaseFactory
        from wa_crypt_tools.lib.key.keyfactory import KeyFactory
    except ImportError:
        logger.error(
            "wa-crypt-tools is required for decryption but is not installed.\n"
            "  Run: pip install wa-crypt-tools"
        )
        raise SystemExit(1)

    key = KeyFactory.new(e2e_key)
    if key is None and os.path.isfile(e2e_key):
        try:
            with open(e2e_key, encoding="utf-8") as f:
                hex_candidate = f.read().strip()
            if len(hex_candidate) == 64:
                key = KeyFactory.new(hex_candidate)
        except OSError:
            pass

    if key is None:
        logger.error(
            f"Could not load decryption key from: {e2e_key}\n"
            "  Make sure --e2e-key points to a valid WhatsApp key file or 64-character hex key."
        )
        raise SystemExit(1)

    logger.info("Decrypting msgstore...")
    try:
        with open(encrypted_path, "rb") as msg_file:
            try:
                db_file = DatabaseFactory.from_file(msg_file)
            except Exception as e:
                logger.error(
                    f"Failed to parse encrypted database header from '{encrypted_path}': {e}"
                )
                raise SystemExit(1)

            if db_file is None:
                logger.error(
                    f"Failed to parse encrypted database header from '{encrypted_path}'.\n"
                    "  Make sure the file is a valid encrypted WhatsApp backup."
                )
                raise SystemExit(1)

            encrypted_payload = msg_file.read()
    except OSError as e:
        logger.error(f"Failed to read encrypted database file '{encrypted_path}': {e}")
        raise SystemExit(1)

    try:
        decrypted = db_file.decrypt(key, encrypted_payload)
    except Exception as e:
        logger.error(f"Decryption failed: {e}\n  The key file may not match this backup.")
        raise SystemExit(1)

    try:
        output_file = zlib.decompressobj().decompress(decrypted)
    except zlib.error:
        output_file = decrypted

    if not output_file.startswith(b"SQLite format 3\x00"):
        logger.error(
            f"Decryption failed for '{encrypted_path}': decrypted output is not a valid SQLite database.\n"
            "  The key file may not match this backup, or the file is corrupted."
        )
        raise SystemExit(1)

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(output_path):
        logger.warning(f"Overwriting existing {output_path} with decrypted database.")
    with open(output_path, "wb") as out:
        out.write(output_file)
    logger.info("Decryption complete.")
    return output_path


def _decrypt_msgstore_if_needed(args: argparse.Namespace, logger: logging.Logger) -> None:
    """Decrypt encrypted msgstore and merge incremental diffs if present."""
    if hasattr(args, "msgstore") and args.msgstore:
        is_encrypted = args.msgstore.endswith((".crypt15", ".crypt14"))
        staged_incs = getattr(args, "_staged_increments", None) or []
        local_incs = []
        if os.path.isfile(args.msgstore):
            msgstore_dir = os.path.dirname(os.path.abspath(args.msgstore))
            local_incs = increment_merger.find_increment_files(msgstore_dir, args.msgstore)
            local_incs = [
                f for f in local_incs if os.path.abspath(f) != os.path.abspath(args.msgstore)
            ]

        all_increments = staged_incs if staged_incs else local_incs
        if all_increments:
            all_increments = increment_merger.filter_active_increments(
                all_increments, getattr(args, "msgstore", None)
            )

        if not is_encrypted and not all_increments:
            return

        if not getattr(args, "e2e_key", None):
            logger.error("Encrypted backup/increments detected but no --e2e-key provided. Exiting.")
            raise SystemExit(1)

        db_dir = os.path.join(args.output, "Whatsapp Databases")
        os.makedirs(db_dir, exist_ok=True)
        decrypted_db = os.path.join(db_dir, "msgstore.db")
        old_db = os.path.join(db_dir, "msgstore.db.old")
        incoming_db = os.path.join(db_dir, "msgstore.db.incoming")
        merge_staging_dir = os.path.join(db_dir, ".staging_merge")

        if os.path.exists(incoming_db):
            with contextlib.suppress(OSError):
                os.remove(incoming_db)

        try:
            if all_increments:
                logger.info(f"Detected {len(all_increments)} incremental backup(s) to merge.")
                base_decrypted = os.path.join(db_dir, "msgstore.db.base_decrypted")
                if os.path.exists(base_decrypted):
                    with contextlib.suppress(OSError):
                        os.remove(base_decrypted)

                try:
                    if is_encrypted:
                        decrypt_msgstore(args.msgstore, args.e2e_key, base_decrypted, logger)
                    else:
                        shutil.copy2(args.msgstore, base_decrypted)

                    increment_merger.merge_increments(
                        base_decrypted,
                        all_increments,
                        args.e2e_key,
                        incoming_db,
                        merge_staging_dir,
                        logger,
                    )
                finally:
                    if os.path.exists(base_decrypted):
                        with contextlib.suppress(OSError):
                            os.remove(base_decrypted)
            else:
                decrypt_msgstore(args.msgstore, args.e2e_key, incoming_db, logger)
        except BaseException:
            if os.path.exists(incoming_db):
                with contextlib.suppress(OSError):
                    os.remove(incoming_db)
            raise

        has_previous_db = os.path.isfile(decrypted_db)

        if getattr(args, "preserve_deleted", False):
            if has_previous_db:
                if os.path.exists(old_db):
                    with contextlib.suppress(OSError):
                        os.remove(old_db)
                os.replace(decrypted_db, old_db)
                os.replace(incoming_db, decrypted_db)
                args.msgstore = decrypted_db

                try:
                    archive_db_path = os.path.join(args.output, ".wa_media_archiver.db")
                    args._preservation_stats = preservation.preserve_deleted_messages(
                        old_db,
                        decrypted_db,
                        args.output,
                        archive_db_path,
                        "android",
                        logger,
                    )
                except (Exception, KeyboardInterrupt) as e:
                    logger.error(
                        f"Preservation failed ({e}). Restoring previous database from {old_db}."
                    )
                    if os.path.exists(decrypted_db):
                        with contextlib.suppress(OSError):
                            os.remove(decrypted_db)
                    with contextlib.suppress(OSError):
                        os.replace(old_db, decrypted_db)
                    raise
                finally:
                    if os.path.exists(old_db):
                        with contextlib.suppress(OSError):
                            os.remove(old_db)
            else:
                logger.info(
                    "Preserve-deleted: First run detected in output directory; establishing baseline database snapshot (0 messages preserved across 0 chats)."
                )
                args._preservation_stats = preservation.PreservationResult(messages=0, chats=0)
                os.replace(incoming_db, decrypted_db)
                args.msgstore = decrypted_db
        else:
            if has_previous_db:
                logger.warning(f"Overwriting existing {decrypted_db} with decrypted database.")
            os.replace(incoming_db, decrypted_db)
            args.msgstore = decrypted_db

        # Clear stale viewer caches in archive db if present
        archive_db_path = os.path.join(args.output, ".wa_media_archiver.db")
        if os.path.exists(archive_db_path):
            with contextlib.closing(sqlite3.connect(archive_db_path)) as arc_c:
                try:
                    arc_c.execute("DELETE FROM recent_messages")
                    arc_c.execute("DELETE FROM reactions_cache")
                    arc_c.commit()
                except sqlite3.OperationalError:
                    pass

        # Only after successful merge and rotation, clean up temporary staging diff files
        if os.path.exists(merge_staging_dir):
            shutil.rmtree(merge_staging_dir, ignore_errors=True)
        if staged_incs:
            for sf in staged_incs:
                with contextlib.suppress(OSError):
                    os.remove(sf)
            args._staged_increments = []


def _prepare_input(args: argparse.Namespace, since_ms: int | None, logger: logging.Logger):
    """
    Resolve all input sources into a ready-to-query state.

    Handles iOS backup extraction, ADB pull, contacts loading, and decryption.
    Returns (platform, media_resolver, ios_contacts_path, contacts).
    platform is None for wa_root mode — caller resolves it after this returns.
    """
    # -------------------------------------------------------------------------
    # iOS backup mode
    # -------------------------------------------------------------------------
    if args.ios_backup:
        existing_chat_storage = os.path.join(
            args.output, "Whatsapp Databases", "ChatStorage.sqlite"
        )
        old_ios_db = os.path.join(args.output, "Whatsapp Databases", "ChatStorage.sqlite.old")
        has_previous_ios_db = os.path.isfile(existing_chat_storage)

        is_encrypted = backup_reader.detect_encrypted(args.ios_backup, logger)
        if is_encrypted and not args.ios_password:
            logger.error("Your iPhone backup is encrypted. Re-run with --ios-password <password>.")
            raise SystemExit(1)

        if getattr(args, "preserve_deleted", False):
            if has_previous_ios_db:
                if os.path.exists(old_ios_db):
                    with contextlib.suppress(OSError):
                        os.remove(old_ios_db)
                os.replace(existing_chat_storage, old_ios_db)

                try:
                    if is_encrypted:
                        msgstore_path, ios_contacts_path, media_resolver = (
                            backup_reader.extract_encrypted(
                                args.ios_backup,
                                args.ios_password,
                                args.output,
                                args.ios_contacts,
                                args.business,
                                logger,
                            )
                        )
                    else:
                        manifest_map, msgstore_path, ios_contacts_path = (
                            backup_reader.extract_plaintext(
                                args.ios_backup,
                                args.output,
                                args.ios_contacts,
                                args.business,
                                logger,
                            )
                        )
                        media_resolver = lambda fp: manifest_map.get(fp)  # noqa: E731

                    archive_db_path = os.path.join(args.output, ".wa_media_archiver.db")
                    args._preservation_stats = preservation.preserve_deleted_messages(
                        old_ios_db,
                        msgstore_path,
                        args.output,
                        archive_db_path,
                        "ios",
                        logger,
                    )
                except (Exception, KeyboardInterrupt) as e:
                    logger.error(
                        f"Preservation failed ({e}). Restoring previous database from {old_ios_db}."
                    )
                    if os.path.exists(existing_chat_storage):
                        with contextlib.suppress(OSError):
                            os.remove(existing_chat_storage)
                    with contextlib.suppress(OSError):
                        os.replace(old_ios_db, existing_chat_storage)
                    raise
                finally:
                    if os.path.exists(old_ios_db):
                        with contextlib.suppress(OSError):
                            os.remove(old_ios_db)
            else:
                logger.info(
                    "Preserve-deleted: First run detected in output directory; establishing baseline database snapshot (0 messages preserved across 0 chats)."
                )
                args._preservation_stats = preservation.PreservationResult(messages=0, chats=0)
                if is_encrypted:
                    msgstore_path, ios_contacts_path, media_resolver = (
                        backup_reader.extract_encrypted(
                            args.ios_backup,
                            args.ios_password,
                            args.output,
                            args.ios_contacts,
                            args.business,
                            logger,
                        )
                    )
                else:
                    manifest_map, msgstore_path, ios_contacts_path = (
                        backup_reader.extract_plaintext(
                            args.ios_backup,
                            args.output,
                            args.ios_contacts,
                            args.business,
                            logger,
                        )
                    )
                    media_resolver = lambda fp: manifest_map.get(fp)  # noqa: E731
        else:
            if is_encrypted:
                msgstore_path, ios_contacts_path, media_resolver = backup_reader.extract_encrypted(
                    args.ios_backup,
                    args.ios_password,
                    args.output,
                    args.ios_contacts,
                    args.business,
                    logger,
                )
            else:
                manifest_map, msgstore_path, ios_contacts_path = backup_reader.extract_plaintext(
                    args.ios_backup,
                    args.output,
                    args.ios_contacts,
                    args.business,
                    logger,
                )
                media_resolver = lambda fp: manifest_map.get(fp)  # noqa: E731

        args.msgstore = msgstore_path
        platform = "ios"

    # -------------------------------------------------------------------------
    # wa_root mode
    # -------------------------------------------------------------------------
    else:
        ios_contacts_path = getattr(args, "ios_contacts", None)
        platform = None

        wa_roots = getattr(args, "wa_roots", None) or []
        _conflict_rows = []
        _root_hits = {root: 0 for root in wa_roots}
        _zero_byte_count = [0]

        def _multi_root_resolver(fp):
            rel_parts = fp.split("/")
            candidates = [
                os.path.join(root, *rel_parts)
                for root in (getattr(args, "wa_roots", None) or [])
                if os.path.exists(os.path.join(root, *rel_parts))
            ]
            valid = [c for c in candidates if os.path.getsize(c) > 0]
            if len(valid) < len(candidates):
                _zero_byte_count[0] += len(candidates) - len(valid)
            if not valid:
                return os.path.join(args.wa_roots[0], *rel_parts)
            if len(valid) == 1:
                _k = _owning_root(valid[0])
                _root_hits[_k] = _root_hits.get(_k, 0) + 1
                return valid[0]
            valid.sort(key=os.path.getsize, reverse=True)
            top_size = os.path.getsize(valid[0])
            second_size = os.path.getsize(valid[1])
            if top_size > second_size:
                chosen = valid[0]
                reason = "largest_wins"
            else:
                md5s = [file_md5(c) for c in valid]
                if len(set(md5s)) == 1:
                    chosen = valid[0]
                    _k = _owning_root(chosen)
                    _root_hits[_k] = _root_hits.get(_k, 0) + 1
                    return chosen
                chosen = valid[0]
                reason = "same_size_first_root"
            _conflict_rows.append(
                {
                    "file_path": fp,
                    "chosen_source": chosen,
                    "chosen_size": os.path.getsize(chosen),
                    "rejected_source": "; ".join(valid[1:]),
                    "rejected_size": "; ".join(str(os.path.getsize(c)) for c in valid[1:]),
                    "reason": reason,
                }
            )
            logger.warning(f"CONFLICT {fp} — {reason}, using: {chosen}")
            _k = _owning_root(chosen)
            _root_hits[_k] = _root_hits.get(_k, 0) + 1
            return chosen

        def _owning_root(path):
            roots = getattr(args, "wa_roots", None) or []
            norm_path = os.path.normcase(os.path.normpath(path))
            for root in roots:
                norm_root = os.path.normcase(os.path.normpath(root))
                if norm_path.startswith(norm_root + os.sep):
                    return root
            return roots[0] if roots else ""

        media_resolver = _multi_root_resolver
        media_resolver.conflict_rows = _conflict_rows
        media_resolver.root_hits = _root_hits
        media_resolver.zero_byte_count = _zero_byte_count

    # --- ADB pull ---
    if args.from_adb:
        if not adb_extractor.check_adb(logger):
            raise SystemExit(1)
        if not adb_extractor.check_device_connected(logger):
            raise SystemExit(1)
        tmp_dir = tempfile.mkdtemp(prefix="wab_adb_")
        atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
        try:
            args.msgstore, args._staged_increments = adb_extractor.pull_msgstore_and_increments(
                tmp_dir, args.business, logger
            )
            args._staging_dir = tmp_dir
            args.contacts = adb_extractor.pull_contacts(tmp_dir, logger)
        except subprocess.CalledProcessError:
            raise SystemExit(1)

        _decrypt_msgstore_if_needed(args, logger)

        if args.pull_media:
            target_paths = None
            if since_ms is not None:
                with contextlib.closing(sqlite3.connect(args.msgstore)) as check_conn:
                    check_cur = check_conn.cursor()
                    hd_dedup = android_handler._check_hd_association(check_cur, logger)
                    target_paths = android_handler.get_media_file_paths(
                        check_cur, since_ms, hd_dedup
                    )
                    logger.info(
                        f"Filtering ADB media pull to {len(target_paths):,} file(s) "
                        f"referenced on or after {args.since}."
                    )

            staging_dir = args.staging
            conn_for_pull = archive_db.open_archive_db(args.output)
            try:
                pulled, skipped, conflicts = adb_extractor.pull_media(
                    staging_dir,
                    args.business,
                    conn_for_pull,
                    logger,
                    target_paths,
                )
            except (RuntimeError, KeyboardInterrupt) as e:
                logger.error(f"ADB media pull stopped: {e}")
                conn_for_pull.close()
                raise SystemExit(1)
            conn_for_pull.close()

            if conflicts:
                report_path = os.path.join(args.output, "adb_conflicts_report.csv")
                adb_extractor.write_adb_conflicts_report(report_path, conflicts, logger)

            if not args.wa_roots:
                args.wa_roots = [staging_dir]

    # --- Load contacts ---
    contacts = {}
    if args.contacts:
        contacts = android_handler.load_contacts(args.contacts, logger)

    # --- Decrypt if needed ---
    if not getattr(args, "from_adb", False):
        _decrypt_msgstore_if_needed(args, logger)

    return platform, media_resolver, ios_contacts_path, contacts


def check_dependencies(args: argparse.Namespace, logger: logging.Logger):
    """Check all required external dependencies upfront and report everything missing at once."""
    import importlib.util

    issues = []

    if getattr(args, "from_adb", False) and shutil.which("adb") is None:
        issues.append(
            "adb not found on PATH\n"
            "      macOS/Linux: brew install android-platform-tools\n"
            "      Windows:     download platform-tools from developer.android.com/tools/releases/platform-tools"
        )
    if getattr(args, "e2e_key", None) and importlib.util.find_spec("wa_crypt_tools") is None:
        issues.append("wa-crypt-tools is not installed\n      Run: pip install wa-crypt-tools")
    if (
        getattr(args, "ios_password", None)
        and importlib.util.find_spec("iphone_backup_decrypt") is None
    ):
        issues.append(
            "iphone-backup-decrypt is not installed\n      Run: pip install iphone-backup-decrypt"
        )
    if (
        getattr(args, "timezone", None)
        and sys.platform == "win32"
        and importlib.util.find_spec("tzdata") is None
    ):
        issues.append(
            "tzdata is not installed (required for --timezone on Windows)\n"
            "      Run: pip install tzdata"
        )

    if issues:
        msg = "Missing dependencies for the requested operation:\n\n"
        msg += "\n\n".join(f"  • {i}" for i in issues)
        logger.error(msg)
        raise SystemExit(1)


def run_forward_mode(args: argparse.Namespace, logger: logging.Logger):
    """Execute a full forward archival run."""
    check_dependencies(args, logger)

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz = None
    if args.timezone:
        try:
            tz = ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError:
            msg = f"Unknown timezone: '{args.timezone}'."
            if sys.platform == "win32":
                msg += "\n  On Windows, the IANA timezone database must be installed: pip install tzdata"
            msg += "\n  Use an IANA name, e.g. Europe/Rome, America/New_York, UTC."
            logger.error(msg)
            raise SystemExit(1)
        logger.info(f"Timezone: {args.timezone}")

    since_ms = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d")
            if tz:
                since_dt = since_dt.replace(tzinfo=tz)
            since_ms = int(since_dt.timestamp() * 1000)
        except ValueError:
            logger.error(f"Invalid --since date '{args.since}'. Expected format: YYYY-MM-DD")
            raise SystemExit(1)
        logger.info(f"SINCE filter active: on or after {args.since}.")
    if args.limit:
        logger.info(
            f"LIMIT active: {args.limit // 2} rows per chat type "
            f"({(args.limit // 2) * 2} max total rows)."
        )

    platform, media_resolver, ios_contacts_path, contacts = _prepare_input(args, since_ms, logger)

    logger.info(f"Connecting to: {args.msgstore}")
    if not os.path.isfile(args.msgstore):
        logger.error(f"Database file not found: {args.msgstore}")
        raise SystemExit(1)

    if platform is None:
        platform = detect_db_platform(args.msgstore)
        logger.info(f"Detected platform: {platform}")

    pres_stats = getattr(
        args, "_preservation_stats", preservation.PreservationResult(messages=0, chats=0)
    )

    if getattr(args, "preserve_deleted", False) and not args.ios_backup:
        target_name = "msgstore.db" if platform == "android" else "ChatStorage.sqlite"
        dest_db = os.path.join(args.output, "Whatsapp Databases", target_name)
        if not getattr(args, "dry_run", False):
            os.makedirs(os.path.dirname(dest_db), exist_ok=True)
            if os.path.isfile(dest_db):
                if os.path.abspath(args.msgstore) != os.path.abspath(dest_db):
                    archive_db_path = os.path.join(args.output, ".wa_media_archiver.db")
                    old_db = dest_db + ".old"
                    if os.path.exists(old_db):
                        with contextlib.suppress(OSError):
                            os.remove(old_db)
                    os.replace(dest_db, old_db)
                    shutil.copy2(args.msgstore, dest_db)
                    try:
                        pres_stats = preservation.preserve_deleted_messages(
                            old_db,
                            dest_db,
                            args.output,
                            archive_db_path,
                            platform or "android",
                            logger,
                        )
                    except (Exception, KeyboardInterrupt) as e:
                        logger.error(
                            f"Preservation failed ({e}). Restoring previous database from {old_db}."
                        )
                        if os.path.exists(dest_db):
                            with contextlib.suppress(OSError):
                                os.remove(dest_db)
                        with contextlib.suppress(OSError):
                            os.replace(old_db, dest_db)
                        raise
                    finally:
                        if os.path.exists(old_db):
                            with contextlib.suppress(OSError):
                                os.remove(old_db)
                    args.msgstore = dest_db
            else:
                logger.info(
                    "Preserve-deleted: First run detected in output directory; establishing baseline database snapshot (0 messages preserved across 0 chats)."
                )
                pres_stats = preservation.PreservationResult(messages=0, chats=0)
                if os.path.abspath(args.msgstore) != os.path.abspath(dest_db):
                    shutil.copy2(args.msgstore, dest_db)
                    args.msgstore = dest_db
        else:
            logger.info(
                "Preserve-deleted: [DRY RUN] Skipping database preservation (0 messages preserved across 0 chats)."
            )

    with contextlib.closing(sqlite3.connect(args.msgstore)) as msgstore_conn:
        cursor = msgstore_conn.cursor()

        try:
            if platform == "ios":
                ios_handler.validate_ios_schema(cursor, logger)

                if args.wa_roots:
                    ios_handler.validate_ios_wa_root(args.wa_roots[0], logger)

                number_map = ios_handler.build_ios_number_map(cursor, logger)

                if not contacts and ios_contacts_path:
                    contacts = ios_handler.load_ios_contacts(ios_contacts_path, logger)

                pushname_map = ios_handler.build_ios_pushname_map(cursor, logger)
                for key, name in pushname_map.items():
                    contacts.setdefault(key, name)

                candidate_dirs = [
                    os.path.join(args.output, "Whatsapp Databases"),
                    args.output,
                    os.path.dirname(os.path.abspath(args.msgstore)),
                ]
                if args.wa_roots:
                    candidate_dirs.extend(args.wa_roots)
                ext_db_path = ios_handler.find_ios_ext_db(candidate_dirs)
                hd_dedup = ios_handler.check_ios_hd_association(cursor, ext_db_path, logger)

                has_thumb = ios_handler.has_ios_thumb_support(cursor)
                query = ios_handler.build_ios_query(args.limit, since_ms, hd_dedup, has_thumb)
                vcard_query = (
                    ios_handler.build_ios_vcard_query(args.limit, since_ms)
                    if ios_handler.has_ios_vcard_support(cursor)
                    else None
                )
                group_subjects = dict(
                    cursor.execute(ios_handler.build_ios_group_subjects_query()).fetchall()
                )

            else:
                android_handler.validate_schema(cursor, logger)
                android_handler.validate_wa_root(args.wa_roots, logger)
                number_map = android_handler.build_number_map(cursor, logger)

                hd_dedup = android_handler._check_hd_association(cursor, logger)
                query = android_handler.build_query(args.limit, since_ms, hd_dedup)
                vcard_query = (
                    android_handler.build_vcard_query(args.limit, since_ms)
                    if android_handler.has_vcard_support(cursor)
                    else None
                )
                group_subjects = dict(
                    cursor.execute(android_handler.build_group_subjects_query()).fetchall()
                )

            total_rows = cursor.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]
            logger.info(f"Query returned {total_rows} rows.")
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if "no such column" in err_msg or "no such table" in err_msg:
                from shared.schema_validator import (
                    build_github_issue_url,
                    extract_database_fingerprint,
                    format_schema_banner,
                )

                fingerprint = extract_database_fingerprint(cursor, platform)
                issue_url = build_github_issue_url(
                    platform, [], {}, f"{fingerprint}; query error={e}", _get_version()
                )
                banner = format_schema_banner(platform, [], {}, issue_url)
                logger.error(banner)
                raise SystemExit(1)
            raise

        archive_conn = archive_db.open_archive_db(args.output)
        try:
            archive_db.check_db_health(archive_conn, logger)
            folder_index = archive_db.load_contacts_from_db(archive_conn)
            logger.info(f"Contact index loaded: {len(folder_index)} known contact(s).")

            group_index = archive_db.load_groups_from_db(archive_conn)
            logger.info(f"Group index loaded: {len(group_index)} known group(s).")

            folder_index = archive_db.sync_folder_names(
                contacts,
                number_map,
                args.output,
                folder_index,
                logger,
                conn=archive_conn if not args.dry_run else None,
                dry_run=args.dry_run,
            )

            group_index = archive_db.sync_group_names(
                group_subjects,
                args.output,
                group_index,
                logger,
                conn=archive_conn if not args.dry_run else None,
                dry_run=args.dry_run,
            )

            report_path = os.path.join(args.output, "missing_media_report.csv")

            link_duplicates = bool(getattr(args, "link_duplicates", False))
            if link_duplicates:
                if not probe_hardlink_support(args.output, logger):
                    link_duplicates = False

            if archive_conn is not None:
                hash_to_canonical = archive_db.load_canonical_copies(archive_conn, args.output)
            else:
                hash_to_canonical = {}

            logger.info("Executing query...")
            cursor.execute(query)
            stats, updated_index, updated_group_index, missing_rows = process_rows(
                cursor,
                total_rows,
                contacts,
                number_map,
                folder_index,
                group_index,
                media_resolver,
                args.output,
                logger,
                args.dry_run,
                conn=archive_conn if not args.dry_run else None,
                tz=tz,
                link_duplicates=link_duplicates,
                hash_to_canonical=hash_to_canonical,
            )

            if vcard_query:
                vcard_rows = cursor.execute(vcard_query).fetchall()
                if vcard_rows:
                    logger.info(f"Processing {len(vcard_rows)} contact card(s)...")
                    v_stats, updated_index, updated_group_index = process_vcard_rows(
                        vcard_rows,
                        contacts,
                        number_map,
                        updated_index,
                        updated_group_index,
                        args.output,
                        logger,
                        args.dry_run,
                        conn=archive_conn if not args.dry_run else None,
                        tz=tz,
                    )
                    stats["copied"] = stats.get("copied", 0) + v_stats.get("copied", 0)
                    stats["skipped"] = stats.get("skipped", 0) + v_stats.get("skipped", 0)
                    stats["warnings"] = stats.get("warnings", 0) + v_stats.get("warnings", 0)
                    stats["hardlinked"] = stats.get("hardlinked", 0) + v_stats.get("hardlinked", 0)
                    stats["skipped_preexisting"] = stats.get(
                        "skipped_preexisting", 0
                    ) + v_stats.get("skipped_preexisting", 0)
                    stats["skipped_duplicate"] = stats.get("skipped_duplicate", 0) + v_stats.get(
                        "skipped_duplicate", 0
                    )
                    stats["skipped_empty_vcard"] = stats.get(
                        "skipped_empty_vcard", 0
                    ) + v_stats.get("skipped_empty_vcard", 0)

            if not args.dry_run:
                archive_conn.commit()
                archive_db.save_contacts_to_db(archive_conn, updated_index)
                archive_db.save_groups_to_db(archive_conn, updated_group_index)
                archive_conn.execute("ANALYZE")
                archive_conn.commit()
                logger.info(
                    f"Archive DB saved: {len(updated_index)} contact(s), "
                    f"{len(updated_group_index)} group(s)."
                )

            if not args.dry_run:
                write_missing_report(report_path, missing_rows, logger)
            else:
                logger.debug(
                    f"[DRY RUN] Would write missing media report with "
                    f"{len(missing_rows)} entries to: {report_path}"
                )

            dup_report_path = os.path.join(args.output, "duplicate_media_report.csv")
            conflict_report_path = os.path.join(args.output, "source_conflicts_report.csv")
            dup_summary = DuplicateReportSummary(0, 0, [])
            if not args.dry_run:
                dup_summary = write_duplicate_report(dup_report_path, archive_conn, logger, tz)
                if hasattr(media_resolver, "conflict_rows"):
                    write_conflict_report(
                        conflict_report_path, media_resolver.conflict_rows, logger
                    )

        finally:
            archive_conn.close()

    db_path = os.path.join(args.output, ".wa_media_archiver.db")
    log_path = args.log or os.path.join(args.output, "wab-archiver.log")
    logger.info("=== Run complete ===")
    if args.dry_run:
        logger.info("  (DRY RUN — no files were written)")
    copied_label = "Would copy" if args.dry_run else "Copied"
    hardlinked_count = stats.get("hardlinked", 0)
    if hardlinked_count > 0:
        logger.info(
            f"  {copied_label:<20}: {stats['copied']} ({hardlinked_count} linked duplicates)"
        )
    else:
        logger.info(f"  {copied_label:<20}: {stats['copied']}")

    n_skipped = stats["skipped"]
    n_pre = stats.get("skipped_preexisting", 0)
    n_dup = stats.get("skipped_duplicate", 0)
    n_empty = stats.get("skipped_empty_vcard", 0)
    if n_skipped > 0 and (n_pre > 0 or n_dup > 0 or n_empty > 0):
        parts = []
        if n_pre > 0:
            parts.append(f"{n_pre} already archived")
        if n_dup > 0:
            parts.append(f"{n_dup} same-chat duplicates")
        if n_empty > 0:
            parts.append(f"{n_empty} empty vCards")
        logger.info(f"  {'Skipped':<20}: {n_skipped} ({', '.join(parts)})")
    else:
        logger.info(f"  {'Skipped':<20}: {n_skipped}")

    logger.info(f"  {'Errors / Warnings':<20}: {stats['warnings']}")
    if getattr(args, "preserve_deleted", False):
        pres_label = "Would preserve" if args.dry_run else "Preserved"
        logger.info(
            f"  {pres_label:<20}: {pres_stats.messages} message(s) across {pres_stats.chats} chat(s)"
        )
    if hasattr(media_resolver, "root_hits"):
        for root, count in media_resolver.root_hits.items():
            logger.info(f"  Source {root:<14}: {count} file(s) used")
        n_conflicts = len(media_resolver.conflict_rows)
        n_zero = media_resolver.zero_byte_count[0]
        if n_conflicts:
            logger.info(
                f"  Conflicts (diff content): {n_conflicts}  → see source_conflicts_report.csv"
            )
        if n_zero:
            logger.info(f"  0-byte skipped      : {n_zero}")

    logger.info("")
    logger.info(f"  {'Missing source files':<20}: {stats['missing']}")

    if dup_summary[1] > 0:
        logger.info(
            f"  {'Duplicate media':<20}: {dup_summary[1]} file(s) across {dup_summary[0]} group(s) "
            "→ see duplicate_media_report.csv"
        )
        for sec_title, sec_groups, sec_files in getattr(dup_summary, "sections", []):
            logger.info(f"    - {sec_title:<18}: {sec_groups} group(s), {sec_files} file(s)")

    logger.info("")
    if not args.dry_run:
        logger.info(f"  {'Archive DB':<20}: {db_path}")
        if getattr(args, "preserve_deleted", False):
            pres_db_path = preservation.get_preserved_db_path(args.output)
            if os.path.isfile(pres_db_path):
                logger.info(f"  {'Preserved DB':<20}: {pres_db_path}")
    logger.info(f"  {'Log file':<20}: {log_path}")
    if not args.dry_run:
        if stats["missing"] > 0 and os.path.isfile(report_path):
            logger.info(f"  {'Missing report':<20}: {report_path}")
        if dup_summary[1] > 0 and os.path.isfile(dup_report_path):
            logger.info(f"  {'Duplicate report':<20}: {dup_report_path}")
        if (
            hasattr(media_resolver, "conflict_rows")
            and media_resolver.conflict_rows
            and os.path.isfile(conflict_report_path)
        ):
            logger.info(f"  {'Conflicts report':<20}: {conflict_report_path}")


def main():
    args = parse_args()
    os.makedirs(args.output, exist_ok=True)
    log_path = args.log or os.path.join(args.output, "wab-archiver.log")
    debug_enabled = getattr(args, "debug", False)
    logger = setup_logging(log_path, debug_enabled)
    _warn_network_paths(args, logger)

    if args.command == "restore":
        logger.info(
            f"=== WhatsApp Backup Tools — Archiver v{_get_version()} started (restore mode) ==="
        )
        if args.dry_run:
            logger.info("*** DRY RUN MODE — no files will be copied ***")
        run_restore_mode(args, logger)
        return

    if args.dry_run:
        logger.info("*** DRY RUN MODE — no files will be copied ***")
    logger.info(f"=== WhatsApp Backup Tools — Archiver v{_get_version()} started ===")
    run_forward_mode(args, logger)


if __name__ == "__main__":
    main()
