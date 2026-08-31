import sys
if sys.version_info < (3, 11):
    print(
        f"ERROR: Python 3.11 or higher is required "
        f"(you are running {sys.version.split()[0]}).\n"
        "  Download the latest Python from https://www.python.org/downloads/",
        file=sys.stderr,
    )
    sys.exit(1)

import atexit
import argparse
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

from . import adb_extractor
from . import android_handler
from . import archive_db
from . import backup_reader
from . import ios_handler
from shared import db, hashing

# ==============================================================================
# WhatsApp Backup Archiver
# Archives WhatsApp media into a structured folder hierarchy using msgstore.db
# (Android) or ChatStorage.sqlite (iOS). Run on a backup copy of your data.
# Requires Python 3.11+.
# ==============================================================================

def _get_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("wabtools")
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
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=tz).strftime('%Y')


def human_datetime(timestamp_ms: int, tz=None) -> str:
    """Convert WhatsApp timestamp (ms) to a human-readable string."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=tz).strftime('%Y-%m-%d %H:%M:%S')


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
    logger = logging.getLogger('wab_archiver')
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
# Source conflict CSV report
# ---------------------------------------------------------------------------

def write_conflict_report(report_path: str, rows: list, logger: logging.Logger):
    """Write a CSV report of files that resolved differently across multiple wa_roots."""
    if not rows:
        logger.info("No source conflicts found.")
        return
    fieldnames = ['file_path', 'chosen_source', 'chosen_size',
                  'rejected_source', 'rejected_size', 'reason']
    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(
        f"Source conflicts report written to: {report_path} "
        f"({len(rows)} conflict(s))"
    )



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
# Core processing
# ---------------------------------------------------------------------------

def _build_missing_row(msg_id, timestamp, file_path, mime_type,
                       chat_subject, sender, key_from_me, message_url, media_name,
                       contacts, number_map, filename='', tz=None) -> dict:
    """Build a missing-media report row for either a null file_path or a missing source file."""
    canonical = number_map.get(sender, sender) if sender else None
    if chat_subject is not None:
        chat_display = chat_subject
        sender_display = 'Me' if (key_from_me == 1 or not sender) \
            else contacts.get(canonical, format_phone(canonical or ''))
    else:
        chat_display = contacts.get(canonical, canonical) if canonical else 'Unknown'
        sender_display = 'Me' if key_from_me == 1 \
            else (contacts.get(canonical, canonical) if canonical else 'Unknown')
    return {
        'message_id':        msg_id,
        'timestamp_human':   human_datetime(timestamp, tz) if timestamp else '',
        'original_filename': filename,
        'mime_type':         mime_type or '',
        'sender':            sender_display,
        'chat_name':         chat_display or '',
        'direction':         'Sent' if key_from_me == 1 else 'Received',
        'file_path':         file_path or '',
        'message_url':       message_url or '',
        'media_name':        media_name or '',
    }


def _route_group(chat_row_id, chat_subject, sender, key_from_me,
                 contacts, number_map, filename, year, output_root,
                 group_index) -> tuple[str, str]:
    """Resolve dest_dir and dest_filename for a group chat message. Mutates group_index."""
    group_name = archive_db.resolve_group_folder(chat_row_id, chat_subject, group_index)
    canonical = number_map.get(sender, sender)
    sender_display = 'Me' if (key_from_me == 1 or not sender) \
        else contacts.get(canonical, format_phone(canonical))
    dest_filename = append_sender_to_filename(filename, sender_display)
    dest_dir = os.path.join(output_root, 'Groups', group_name, year)
    return dest_dir, dest_filename


def _route_contact(sender, key_from_me, contacts, number_map,
                   filename, year, output_root, contact_index) -> tuple[str, str]:
    """Resolve dest_dir and dest_filename for a 1-to-1 chat message. Mutates contact_index."""
    contact_number = number_map.get(sender, sender) if sender else 'unknown'
    contact_display = contacts.get(contact_number, None)
    if contact_display is None and contact_number in contact_index:
        _, stored_display = contact_index[contact_number]
        contact_display = stored_display or None
    folder_name = build_contact_folder_name(
        contact_display or contact_number, contact_number
    )
    contact_index[contact_number] = (folder_name, contact_display or '')
    direction = 'Sent' if key_from_me == 1 else 'Received'
    dest_dir = os.path.join(output_root, 'Contacts', folder_name, year, direction)
    return dest_dir, filename


def _copy_or_skip(src, dest_path, dest_dir, file_path, timestamp,
                  output_root, dry_run, logger, cursor) -> tuple[int, int, int]:
    """
    Copy src to dest, handling dry-run, dedup, and collision.
    Returns (copied, skipped, warnings) deltas.
    """
    if dry_run:
        resolved = resolve_unique_dest(dest_path, src, logger)
        if resolved is None:
            return 0, 1, 0
        logger.info(f"[DRY RUN] Would copy: {src} -> {resolved}")
        return 1, 0, 0

    if os.path.exists(dest_path):
        src_hash = file_md5(src)
        resolved = resolve_unique_dest(dest_path, src, logger, src_hash=src_hash)
        if resolved is None:
            if cursor is not None:
                rel = os.path.relpath(dest_path, output_root).replace(os.sep, '/')
                archive_db.record_file_archived(cursor, file_path, src_hash, rel,
                                                os.path.getsize(dest_path))
            return 0, 1, 0
    else:
        src_hash = None
        resolved = dest_path

    os.makedirs(dest_dir, exist_ok=True)
    try:
        shutil.copy(src, resolved)
    except Exception as e:
        logger.error(f"ERROR copying {src} -> {resolved}: {e}")
        return 0, 0, 1
    try:
        set_file_times(resolved, timestamp)
    except OSError as e:
        logger.debug(f"Could not set timestamps on {resolved}: {e}")
    if src_hash is None:
        src_hash = file_md5(resolved)
    logger.debug(f"COPIED: {src} -> {resolved}")
    if cursor is not None:
        rel = os.path.relpath(resolved, output_root).replace(os.sep, '/')
        archive_db.record_file_archived(cursor, file_path, src_hash, rel,
                                        os.path.getsize(resolved))
    return 1, 0, 0


def process_rows(rows, total: int, contacts, number_map, folder_index, group_index,
                 media_resolver, output_root, logger, dry_run, conn=None, tz=None):
    stats = {'copied': 0, 'skipped': 0, 'missing': 0, 'warnings': 0}
    updated_index = dict(folder_index)
    updated_group_index = dict(group_index)
    missing_rows = []
    cursor = conn.cursor() if conn is not None else None

    for i, (msg_id, timestamp, file_path, mime_type, chat_row_id,
            chat_subject, sender, key_from_me, message_url, media_name) in enumerate(rows, 1):

        if i % 1000 == 0:
            logger.info(f"Progress: {i}/{total} rows processed...")

        is_group = chat_subject is not None
        filename = media_name if media_name else (
            os.path.basename(file_path) if file_path else ''
        )

        if not file_path:
            logger.debug(f"SKIP (no file_path): message_id={msg_id}")
            missing_rows.append(_build_missing_row(
                msg_id, timestamp, None, mime_type, chat_subject, sender,
                key_from_me, message_url, media_name, contacts, number_map,
                filename=filename, tz=tz,
            ))
            stats['missing'] += 1
            continue

        src = media_resolver(file_path)

        if src is None or not os.path.isfile(src):
            logger.warning(f"MISSING source file (message_id={msg_id}): {file_path}")
            missing_rows.append(_build_missing_row(
                msg_id, timestamp, file_path, mime_type, chat_subject, sender,
                key_from_me, message_url, media_name, contacts, number_map,
                filename=filename, tz=tz,
            ))
            stats['missing'] += 1
            continue

        if timestamp is None:
            logger.warning(f"SKIP (no timestamp): message_id={msg_id}, file={file_path}")
            missing_rows.append(_build_missing_row(
                msg_id, timestamp, file_path, mime_type, chat_subject, sender,
                key_from_me, message_url, media_name, contacts, number_map,
                filename=filename, tz=tz,
            ))
            stats['missing'] += 1
            continue

        year = get_year(timestamp, tz)

        if is_group:
            dest_dir, dest_filename = _route_group(
                chat_row_id, chat_subject, sender, key_from_me,
                contacts, number_map, filename, year, output_root, updated_group_index,
            )
        else:
            dest_dir, dest_filename = _route_contact(
                sender, key_from_me, contacts, number_map,
                filename, year, output_root, updated_index,
            )

        dest_path = os.path.join(dest_dir, dest_filename)
        copied, skipped, warnings = _copy_or_skip(
            src, dest_path, dest_dir, file_path, timestamp,
            output_root, dry_run, logger, cursor,
        )
        stats['copied'] += copied
        stats['skipped'] += skipped
        stats['warnings'] += warnings

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
    conn = archive_db.open_archive_db(args.output)
    try:
        archive_db.check_db_health(conn, logger)
        file_count = conn.execute(
            "SELECT COUNT(DISTINCT original_path) FROM archive_copies"
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

        stats = {'restored': 0, 'skipped': 0, 'unrestorable': 0, 'warnings': 0}
        report_rows = []
        restore_report_path = os.path.join(args.output, 'restore_report.csv')

        restore_map: dict[str, list[str]] = {}
        for original_path, archive_path in conn.execute(
            "SELECT original_path, archive_path "
            "FROM archive_copies ORDER BY original_path, rowid"
        ):
            restore_map.setdefault(original_path, []).append(archive_path)

        for original_path, archive_paths in restore_map.items():

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

            if args.dry_run:
                logger.info(f"[DRY RUN] Would restore: {original_path}")
                stats['restored'] += 1
                continue

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
    'output', 'msgstore', 'e2e_key', 'wa_root', 'contacts', 'log',
    'business', 'timezone', 'since', 'ios_backup', 'ios_password', 'ios_contacts',
    'pull_media', 'staging',
}

_EXAMPLE_CONFIG = """\
# This is an example config file. Rename it to config.toml to activate it.
# wab-archiver config
# All paths can be absolute or relative to where you run the script.
# Remove the leading # to activate a setting.
#
# Windows tip: you can paste Windows paths as-is; backslashes are auto-corrected.

output     = "/path/to/archive"         # required
# msgstore = "msgstore.db"
# e2e_key  = ""

# Single WhatsApp source folder (Android: folder containing Media/)
# wa_root  = "/path/to/WhatsApp"

# Multiple source folders — tried in order, best copy wins (Android only)
# wa_root  = ["/path/to/old-archive", "/path/to/current-phone/WhatsApp"]

# contacts = ""
# log      = ""
# business = false
# timezone = ""                         # e.g. Europe/Rome
# since    = ""                         # e.g. 2024-01-01

# Android — pull via ADB  (wab-archiver archive --from-adb; see setup-android.md)
# pull_media = false                    # also pull media files (--pull-media)
# staging    = ""                       # persistent folder for pulled media (--staging)

# iOS
# ios_backup   = ""
# ios_password = ""
# ios_contacts = ""
"""


def _load_toml(path: str) -> dict:
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        return tomllib.loads(raw.decode('utf-8'))
    except tomllib.TOMLDecodeError:
        fixed = re.sub(r'"([^"]*)"', lambda m: '"' + m.group(1).replace('\\', '/') + '"', raw.decode('utf-8'))
        try:
            result = tomllib.loads(fixed)
            with open(path, 'wb') as f:
                f.write(fixed.encode('utf-8'))
            print(
                f"NOTE: Backslashes in paths in {path} were automatically converted "
                "to forward slashes and the file was updated.",
                file=sys.stderr,
            )
            return result
        except tomllib.TOMLDecodeError as e:
            print(f"ERROR: Could not parse config file {path}:\n  {e}", file=sys.stderr)
            sys.exit(1)
    except OSError as e:
        print(f"ERROR: Could not read config file {path}:\n  {e}", file=sys.stderr)
        sys.exit(1)


def _generate_config(script_dir: str):
    dest = os.path.join(script_dir, 'example-config.toml')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(_EXAMPLE_CONFIG)
    print(f"Written: {dest}")
    print("Rename it to config.toml and edit the values to activate it.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_KNOWN_COMMANDS = frozenset({'archive', 'restore', 'config'})


def _inject_default_archive_command():
    """
    Inject 'archive' as the default subcommand when the user passes flags
    directly without specifying a subcommand (e.g. wab-archiver --wa-root … -o …).
    """
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help', '--version'):
        return
    first_pos = next((a for a in sys.argv[1:] if not a.startswith('-')), None)
    if first_pos not in _KNOWN_COMMANDS:
        sys.argv.insert(1, 'archive')


def parse_args() -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _cwd = os.getcwd()

    # Pre-scan for --config before argparse runs (it may appear anywhere).
    _config_path = None
    for _i, _arg in enumerate(sys.argv[1:], 1):
        if _arg.startswith('--config='):
            _config_path = _arg[len('--config='):]
            break
        if _arg == '--config' and _i < len(sys.argv) - 1:
            _config_path = sys.argv[_i + 1]
            break

    # Auto-detect config.toml (skip for the 'config' subcommand).
    _is_config_cmd = len(sys.argv) > 1 and sys.argv[1] == 'config'
    if _config_path is None and not _is_config_cmd:
        _candidates = []
        for d in dict.fromkeys([_script_dir, _cwd]):
            p = os.path.join(d, 'config.toml')
            if os.path.isfile(p):
                _candidates.append(p)

        if len(_candidates) == 1:
            answer = input(
                f"Found config.toml at {_candidates[0]} — use it? [Y/n] "
            ).strip().lower()
            if answer in ('', 'y', 'yes'):
                _config_path = _candidates[0]
        elif len(_candidates) > 1:
            print("ERROR: Multiple config files found — specify one with --config:",
                  file=sys.stderr)
            for p in _candidates:
                print(f"  {p}", file=sys.stderr)
            sys.exit(1)

    # Load and validate config.
    _config = {}
    if _config_path:
        if not _config_path.endswith('.toml'):
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
        if 'since' in _config:
            _since_val = _config['since']
            if isinstance(_since_val, date):
                _config['since'] = _since_val.isoformat()
            elif not isinstance(_since_val, str):
                print(
                    f"ERROR: 'since' in config must be a quoted date string "
                    f"(e.g. since = \"2024-01-01\"), got {type(_since_val).__name__}",
                    file=sys.stderr,
                )
                sys.exit(1)
        if 'wa_root' in _config:
            _wa_val = _config.pop('wa_root')
            if isinstance(_wa_val, str):
                _config['wa_roots'] = [_wa_val]
            elif isinstance(_wa_val, list):
                _config['wa_roots'] = _wa_val
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
        prog='wab-archiver',
        description='Archive WhatsApp media into a structured folder hierarchy.',
    )
    parser.add_argument('--version', action='version',
                        version=f'WhatsApp Backup Tools — Archiver v{_get_version()}')

    subs = parser.add_subparsers(dest='command', metavar='command')
    subs.required = True

    # --- archive (default command) ---
    archive_p = subs.add_parser(
        'archive',
        help='Build or update an archive from a source (default command).',
        description=(
            'Archive WhatsApp media from an Android device, iOS backup, or via ADB. '
            'Exactly one source is required: --wa-root, --ios-backup, or --from-adb.'
        ),
    )
    _src = archive_p.add_mutually_exclusive_group()
    _src.add_argument(
        '--wa-root', dest='wa_roots', action='append', default=None, metavar='PATH',
        help='Root path of WhatsApp folder on disk '
             '(Android: folder containing Media/; '
             'iOS pre-extracted: AppDomainGroup folder). '
             'Repeat to search multiple source folders (Android only).',
    )
    _src.add_argument(
        '--ios-backup', dest='ios_backup', default=None, metavar='PATH',
        help='Path to iPhone backup directory (contains Manifest.db).',
    )
    _src.add_argument(
        '--from-adb', dest='from_adb', action='store_true',
        help='Pull msgstore and contacts from a connected Android device via ADB.',
    )
    archive_p.add_argument(
        '--msgstore', default='msgstore.db', metavar='PATH',
        help='Path to msgstore.db[.crypt15] or ChatStorage.sqlite (default: current folder).',
    )
    archive_p.add_argument(
        '--e2e-key', dest='e2e_key', default=None, metavar='KEY',
        help='E2E decryption key for encrypted msgstore.db.crypt15.',
    )
    archive_p.add_argument(
        '-c', '--contacts', default=None, metavar='PATH',
        help='Path to contacts export file (Android ADB format).',
    )
    archive_p.add_argument(
        '--ios-password', dest='ios_password', default=None, metavar='PASSWORD',
        help='Password for an encrypted iPhone backup.',
    )
    archive_p.add_argument(
        '--ios-contacts', dest='ios_contacts', default=None, metavar='PATH',
        help='Path to ContactsV2.sqlite from an iOS backup '
             '(optional; auto-extracted from --ios-backup if omitted).',
    )
    archive_p.add_argument(
        '--business', action='store_true',
        help='Target WhatsApp Business instead of the regular app. '
             'Affects the ADB pull path (Android) and backup domain (iOS).',
    )
    archive_p.add_argument(
        '--pull-media', dest='pull_media', action='store_true',
        help='Pull WhatsApp media files from the device via ADB. '
             'Only valid with --from-adb. Requires --staging.',
    )
    archive_p.add_argument(
        '--staging', dest='staging', default=None, metavar='PATH',
        help='Persistent local directory where ADB-pulled media is staged. '
             'Required with --pull-media.',
    )
    archive_p.add_argument(
        '-o', '--output', default=None, metavar='PATH',
        help='Output root folder for the archive.',
    )
    archive_p.add_argument(
        '-l', '--log', default=None, metavar='PATH',
        help='Log file path (default: <output>/wab-archiver.log).',
    )
    archive_p.add_argument(
        '--timezone', default=None, metavar='TZ',
        help='IANA timezone for year folders and report timestamps '
             '(e.g. Europe/Rome, America/New_York, UTC). '
             'Default: machine local time. '
             'Windows users: requires pip install tzdata.',
    )
    archive_p.add_argument(
        '--config', default=None, metavar='PATH',
        help='Path to a TOML config file. '
             'Auto-detected as config.toml in the script folder or cwd if present.',
    )
    archive_p.add_argument(
        '--dry-run', action='store_true',
        help='Simulate the run without copying any files.',
    )
    archive_p.add_argument(
        '--limit', type=int, default=None, metavar='N',
        help='Limit the number of rows extracted per query block (e.g. --limit 250 for testing).',
    )
    archive_p.add_argument(
        '--since', default=None, metavar='DATE',
        help='Only include messages on or after this date (YYYY-MM-DD). Combines with --limit.',
    )
    # Apply config defaults to the archive subparser.
    archive_p.set_defaults(**_config)

    # --- restore ---
    restore_p = subs.add_parser(
        'restore',
        help='Reconstruct the original Media/ tree from an archive (Android only).',
    )
    restore_p.add_argument(
        '-o', '--output', default=None, metavar='PATH',
        help='Path to the archive to restore.',
    )
    restore_p.add_argument(
        '-l', '--log', default=None, metavar='PATH',
        help='Log file path.',
    )
    restore_p.add_argument(
        '--dry-run', action='store_true',
        help='Simulate the restore without copying any files.',
    )
    restore_p.add_argument(
        '--config', default=None, metavar='PATH',
        help='Path to a TOML config file.',
    )
    _restore_config = {k: v for k, v in _config.items() if k in ('output', 'log')}
    restore_p.set_defaults(**_restore_config)

    # --- config ---
    config_p = subs.add_parser('config', help='Manage configuration.')
    config_p.add_argument(
        'action', choices=['generate'],
        help='generate: write example-config.toml to the script folder.',
    )

    # -------------------------------------------------------------------------
    # Parse
    # -------------------------------------------------------------------------
    args = parser.parse_args()

    if args.command == 'config':
        _generate_config(_script_dir)
        return args  # _generate_config calls sys.exit(0); unreachable

    if args.command == 'restore':
        if not args.output:
            restore_p.error("the following arguments are required: -o/--output")
        return args

    # --- archive validation ---
    if not args.output:
        archive_p.error("the following arguments are required: -o/--output")

    _n_sources = sum([bool(args.wa_roots), bool(args.ios_backup), args.from_adb])
    if _n_sources == 0:
        archive_p.error("one of --wa-root, --ios-backup, or --from-adb is required")
    if _n_sources > 1:
        archive_p.error("--wa-root, --ios-backup, and --from-adb are mutually exclusive")

    if args.contacts and args.ios_backup:
        archive_p.error(
            "--contacts is for Android ADB exports and cannot be used with --ios-backup. "
            "iOS contacts are loaded automatically from the backup (ContactsV2.sqlite). "
            "Use --ios-contacts to supply a pre-extracted ContactsV2.sqlite instead."
        )
    if args.pull_media and not args.from_adb:
        archive_p.error("--pull-media requires --from-adb.")
    if args.pull_media and not args.staging:
        archive_p.error("--pull-media requires --staging.")

    return args


def _warn_network_paths(args: argparse.Namespace, logger: logging.Logger):
    """Warn if output or any wa_root appear to be UNC network share paths."""
    roots = args.wa_roots or []
    for label, path in [('--output', args.output)] + [('--wa-root', r) for r in roots]:
        if path and (path.startswith('\\\\') or path.startswith('//')):
            logger.warning(
                f"{label} appears to be a network share ({path}). "
                "Performance may be degraded and file timestamps may not be preserved."
            )


def _prepare_input(args: argparse.Namespace, logger: logging.Logger):
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
        is_encrypted = backup_reader.detect_encrypted(args.ios_backup, logger)
        if is_encrypted and not args.ios_password:
            logger.error(
                "Your iPhone backup is encrypted. Re-run with --ios-password <password>."
            )
            raise SystemExit(1)

        if is_encrypted:
            msgstore_path, ios_contacts_path, media_resolver = backup_reader.extract_encrypted(
                args.ios_backup, args.ios_password, args.output,
                args.ios_contacts, args.business, logger,
            )
        else:
            manifest_map, msgstore_path, ios_contacts_path = backup_reader.extract_plaintext(
                args.ios_backup, args.output,
                args.ios_contacts, args.business, logger,
            )
            media_resolver = lambda fp: manifest_map.get(fp)  # noqa: E731

        args.msgstore = msgstore_path
        platform = 'ios'

    # -------------------------------------------------------------------------
    # wa_root mode
    # -------------------------------------------------------------------------
    else:
        ios_contacts_path = args.ios_contacts
        platform = None

        _conflict_rows = []
        _root_hits = {root: 0 for root in args.wa_roots}
        _zero_byte_count = [0]

        def _multi_root_resolver(fp):
            rel_parts = fp.split('/')
            candidates = [
                os.path.join(root, *rel_parts)
                for root in args.wa_roots
                if os.path.exists(os.path.join(root, *rel_parts))
            ]
            valid = [c for c in candidates if os.path.getsize(c) > 0]
            if len(valid) < len(candidates):
                _zero_byte_count[0] += len(candidates) - len(valid)
            if not valid:
                return os.path.join(args.wa_roots[0], *rel_parts)
            if len(valid) == 1:
                _root_hits[_owning_root(valid[0])] += 1
                return valid[0]
            valid.sort(key=os.path.getsize, reverse=True)
            top_size = os.path.getsize(valid[0])
            second_size = os.path.getsize(valid[1])
            if top_size > second_size:
                chosen = valid[0]
                reason = 'largest_wins'
            else:
                md5s = [file_md5(c) for c in valid]
                if len(set(md5s)) == 1:
                    chosen = valid[0]
                    _root_hits[_owning_root(chosen)] += 1
                    return chosen
                chosen = valid[0]
                reason = 'same_size_first_root'
            _conflict_rows.append({
                'file_path':       fp,
                'chosen_source':   chosen,
                'chosen_size':     os.path.getsize(chosen),
                'rejected_source': '; '.join(valid[1:]),
                'rejected_size':   '; '.join(str(os.path.getsize(c)) for c in valid[1:]),
                'reason':          reason,
            })
            logger.warning(
                f"CONFLICT {fp} — {reason}, using: {chosen}"
            )
            _root_hits[_owning_root(chosen)] += 1
            return chosen

        def _owning_root(path):
            norm_path = os.path.normcase(os.path.normpath(path))
            for root in args.wa_roots:
                norm_root = os.path.normcase(os.path.normpath(root))
                if norm_path.startswith(norm_root + os.sep):
                    return root
            return args.wa_roots[0]

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
        tmp_dir = tempfile.mkdtemp(prefix='wab_adb_')
        atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
        try:
            args.msgstore = adb_extractor.pull_msgstore(tmp_dir, args.business, logger)
            args.contacts = adb_extractor.pull_contacts(tmp_dir, logger)
        except subprocess.CalledProcessError:
            raise SystemExit(1)

        if args.pull_media:
            staging_dir = args.staging
            conn_for_pull = archive_db.open_archive_db(args.output)
            try:
                pulled, skipped, conflicts = adb_extractor.pull_media(
                    staging_dir, args.business, conn_for_pull, logger,
                )
            except (RuntimeError, KeyboardInterrupt) as e:
                logger.error(f"ADB media pull stopped: {e}")
                conn_for_pull.close()
                raise SystemExit(1)
            conn_for_pull.close()

            if conflicts:
                report_path = os.path.join(args.output, 'adb_conflicts_report.csv')
                adb_extractor.write_adb_conflicts_report(report_path, conflicts, logger)

            if not args.wa_roots:
                args.wa_roots = [staging_dir]

    # --- Load contacts ---
    contacts = {}
    if args.contacts:
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
            db_file = DatabaseFactory.from_file(msg)
            raw = msg.read()
        key = KeyFactory.new(args.e2e_key)
        if key is None:
            logger.error(
                f"Could not load decryption key from: {args.e2e_key}\n"
                "  Make sure --e2e_key points to a valid WhatsApp key file."
            )
            raise SystemExit(1)
        try:
            decrypted = db_file.decrypt(key, raw)
        except Exception as e:
            logger.error(
                f"Decryption failed: {e}\n"
                "  The key file may not match this backup."
            )
            raise SystemExit(1)
        try:
            output_file = zlib.decompress(decrypted)
        except zlib.error:
            output_file = decrypted
        args.msgstore = os.path.join(args.output, 'msgstore.db')
        if os.path.exists(args.msgstore):
            logger.warning(f"Overwriting existing {args.msgstore} with decrypted database.")
        with open(args.msgstore, 'wb') as out:
            out.write(output_file)
        logger.info("Decryption complete.")

    return platform, media_resolver, ios_contacts_path, contacts


def check_dependencies(args: argparse.Namespace, logger: logging.Logger):
    """Check all required external dependencies upfront and report everything missing at once."""
    import importlib.util
    issues = []

    if getattr(args, 'from_adb', False) and shutil.which('adb') is None:
        issues.append(
            "adb not found on PATH\n"
            "      macOS/Linux: brew install android-platform-tools\n"
            "      Windows:     download platform-tools from developer.android.com/tools/releases/platform-tools"
        )
    if getattr(args, 'e2e_key', None) and importlib.util.find_spec('wa_crypt_tools') is None:
        issues.append(
            "wa-crypt-tools is not installed\n"
            "      Run: pip install wa-crypt-tools"
        )
    if getattr(args, 'ios_password', None) and importlib.util.find_spec('iphone_backup_decrypt') is None:
        issues.append(
            "iphone-backup-decrypt is not installed\n"
            "      Run: pip install iphone-backup-decrypt"
        )
    if getattr(args, 'timezone', None) and sys.platform == 'win32' \
            and importlib.util.find_spec('tzdata') is None:
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
    platform, media_resolver, ios_contacts_path, contacts = _prepare_input(args, logger)

    logger.info(f"Connecting to: {args.msgstore}")
    if not os.path.isfile(args.msgstore):
        logger.error(f"Database file not found: {args.msgstore}")
        raise SystemExit(1)

    if platform is None:
        platform = detect_db_platform(args.msgstore)
        logger.info(f"Detected platform: {platform}")

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    tz = None
    if args.timezone:
        try:
            tz = ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError:
            msg = f"Unknown timezone: '{args.timezone}'."
            if sys.platform == 'win32':
                msg += "\n  On Windows, the IANA timezone database must be installed: pip install tzdata"
            msg += "\n  Use an IANA name, e.g. Europe/Rome, America/New_York, UTC."
            logger.error(msg)
            raise SystemExit(1)
        logger.info(f"Timezone: {args.timezone}")

    since_ms = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, '%Y-%m-%d')
            if tz:
                since_dt = since_dt.replace(tzinfo=tz)
            since_ms = int(since_dt.timestamp() * 1000)
        except ValueError:
            logger.error(f"Invalid --since date '{args.since}'. Expected format: YYYY-MM-DD")
            raise SystemExit(1)
        logger.info(f"SINCE filter active: on or after {args.since}.")
    if args.limit:
        logger.info(f"LIMIT active: {args.limit // 2} rows per chat type "
                    f"({args.limit} max total rows).")

    with contextlib.closing(sqlite3.connect(args.msgstore)) as msgstore_conn:
        cursor = msgstore_conn.cursor()

        if platform == 'ios':
            ios_handler.validate_ios_schema(cursor, logger)

            if args.wa_roots:
                ios_handler.validate_ios_wa_root(args.wa_roots[0], logger)

            number_map = ios_handler.build_ios_number_map(cursor, logger)

            if not contacts and ios_contacts_path:
                contacts = ios_handler.load_ios_contacts(ios_contacts_path, logger)

            pushname_map = ios_handler.build_ios_pushname_map(cursor, logger)
            for key, name in pushname_map.items():
                contacts.setdefault(key, name)

            query = ios_handler.build_ios_query(args.limit, since_ms)
            group_subjects = dict(
                cursor.execute(ios_handler.build_ios_group_subjects_query()).fetchall()
            )

        else:
            android_handler.validate_schema(cursor, logger)
            android_handler.validate_wa_root(args.wa_roots, logger)
            number_map = android_handler.build_number_map(cursor, logger)

            hd_dedup = android_handler._check_hd_association(cursor, logger)
            query = android_handler.build_query(args.limit, since_ms, hd_dedup)
            group_subjects = dict(
                cursor.execute(android_handler.build_group_subjects_query()).fetchall()
            )

        total_rows = cursor.execute(
            f"SELECT COUNT(*) FROM ({query})"
        ).fetchone()[0]
        logger.info(f"Query returned {total_rows} rows.")

        archive_conn = archive_db.open_archive_db(args.output)
        try:
            archive_db.check_db_health(archive_conn, logger)
            folder_index = archive_db.load_contacts_from_db(archive_conn)
            logger.info(f"Contact index loaded: {len(folder_index)} known contact(s).")

            group_index = archive_db.load_groups_from_db(archive_conn)
            logger.info(f"Group index loaded: {len(group_index)} known group(s).")

            folder_index = archive_db.sync_folder_names(
                contacts, number_map, args.output, folder_index, logger,
                conn=archive_conn if not args.dry_run else None,
                dry_run=args.dry_run,
            )

            group_index = archive_db.sync_group_names(
                group_subjects, args.output, group_index, logger,
                conn=archive_conn if not args.dry_run else None,
                dry_run=args.dry_run,
            )

            report_path = os.path.join(args.output, 'missing_media_report.csv')

            logger.info("Executing query...")
            cursor.execute(query)
            stats, updated_index, updated_group_index, missing_rows = process_rows(
                cursor, total_rows, contacts, number_map, folder_index, group_index,
                media_resolver, args.output, logger, args.dry_run,
                conn=archive_conn if not args.dry_run else None,
                tz=tz,
            )

            if not args.dry_run:
                archive_conn.commit()
                archive_db.save_contacts_to_db(archive_conn, updated_index)
                archive_db.save_groups_to_db(archive_conn, updated_group_index)
                archive_conn.execute("ANALYZE")
                archive_conn.commit()
                logger.info(f"Archive DB saved: {len(updated_index)} contact(s), "
                            f"{len(updated_group_index)} group(s).")

            if not args.dry_run:
                write_missing_report(report_path, missing_rows, logger)
            else:
                logger.info(
                    f"[DRY RUN] Would write missing media report with "
                    f"{len(missing_rows)} entries to: {report_path}"
                )

            dup_report_path = os.path.join(args.output, 'duplicate_media_report.csv')
            conflict_report_path = os.path.join(args.output, 'source_conflicts_report.csv')
            if not args.dry_run:
                write_duplicate_report(dup_report_path, archive_conn, logger)
                if hasattr(media_resolver, 'conflict_rows'):
                    write_conflict_report(conflict_report_path, media_resolver.conflict_rows, logger)

        finally:
            archive_conn.close()

    db_path = os.path.join(args.output, '.wa_media_archiver.db')
    log_path = args.log or os.path.join(args.output, 'wab-archiver.log')
    logger.info("=== Run complete ===")
    if args.dry_run:
        logger.info("  (DRY RUN — no files were written)")
    logger.info(f"  {'Would copy' if args.dry_run else 'Copied':<20}: {stats['copied']}")
    logger.info(f"  Skipped             : {stats['skipped']}")
    logger.info(f"  Missing source files: {stats['missing']}")
    logger.info(f"  Errors / Warnings   : {stats['warnings']}")
    if hasattr(media_resolver, 'root_hits'):
        for root, count in media_resolver.root_hits.items():
            logger.info(f"  Source {root:<14}: {count} file(s) used")
        n_conflicts = len(media_resolver.conflict_rows)
        n_zero = media_resolver.zero_byte_count[0]
        if n_conflicts:
            logger.info(f"  Conflicts (diff content): {n_conflicts}  → see source_conflicts_report.csv")
        if n_zero:
            logger.info(f"  0-byte skipped      : {n_zero}")
    logger.info(f"  Log file            : {log_path}")
    if not args.dry_run:
        logger.info(f"  {'Missing media report':<20}: {report_path}")
        logger.info(f"  {'Duplicate media report':<20}: {dup_report_path}")
        if hasattr(media_resolver, 'conflict_rows') and media_resolver.conflict_rows:
            logger.info(f"  {'Conflicts report':<20}: {conflict_report_path}")
        logger.info(f"  {'Archive DB':<20}: {db_path}")


def main():
    args = parse_args()
    os.makedirs(args.output, exist_ok=True)
    log_path = args.log or os.path.join(args.output, 'wab-archiver.log')
    logger = setup_logging(log_path)
    _warn_network_paths(args, logger)

    if args.command == 'restore':
        logger.info(f"=== WhatsApp Backup Tools — Archiver v{_get_version()} started (restore mode) ===")
        if args.dry_run:
            logger.info("*** DRY RUN MODE — no files will be copied ***")
        run_restore_mode(args, logger)
        return

    if args.dry_run:
        logger.info("*** DRY RUN MODE — no files will be copied ***")
    logger.info(f"=== WhatsApp Backup Tools — Archiver v{_get_version()} started ===")
    run_forward_mode(args, logger)


if __name__ == '__main__':
    main()
