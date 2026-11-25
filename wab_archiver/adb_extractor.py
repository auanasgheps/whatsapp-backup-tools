import csv
import logging
import os
import posixpath
import re
import sqlite3
import subprocess
import sys

from shared.adb import (
    _ADB_RETRIES as _ADB_RETRIES,
)
from shared.adb import (
    _ADB_TIMEOUT as _ADB_TIMEOUT,
)
from shared.adb import (
    _TRANSIENT_ERRORS as _TRANSIENT_ERRORS,
)
from shared.adb import (
    _classify_adb_error as _classify_adb_error,
)
from shared.adb import (
    _confirm_restart_prompt as _confirm_restart_prompt,
)
from shared.adb import (
    _parse_adb_devices as _parse_adb_devices,
)
from shared.adb import (
    _prompt_user_to_continue as _prompt_user_to_continue,
)
from shared.adb import (
    _query_adb_devices as _query_adb_devices,
)
from shared.adb import (
    _remote_md5 as _remote_md5,
)
from shared.adb import (
    _restart_adb_server as _restart_adb_server,
)
from shared.adb import (
    _run_adb as _run_adb,
)
from shared.adb import (
    check_adb as check_adb,
)
from shared.adb import (
    check_device_connected as check_device_connected,
)
from shared.adb import (
    enumerate_remote_files as enumerate_remote_files,
)
from shared.adb import (
    get_device_serial as get_device_serial,
)
from shared.adb import (
    probe_md5_binary as probe_md5_binary,
)

from .increment_merger import filter_active_increments
from .progress import ProgressReporter

_MSGSTORE_PATH = (
    "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15"
)
_MSGSTORE_BUSINESS_PATH = (
    "/storage/emulated/0/Android/media/com.whatsapp.w4b"
    "/WhatsApp Business/Databases/msgstore.db.crypt15"
)
_WA_MEDIA_ROOT = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
_WA_BUSINESS_MEDIA_ROOT = (
    "/storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"
)
_CONTACTS_URI = "content://com.android.contacts/data"
_CONTACTS_PROJECTION = "display_name:data1:data4:mimetype"


def pull_msgstore(output_dir: str, business: bool = False, logger=None) -> str:
    remote = _MSGSTORE_BUSINESS_PATH if business else _MSGSTORE_PATH
    dest = os.path.join(output_dir, "msgstore.db.crypt15")
    if logger:
        logger.info("Pulling msgstore backup via ADB...")
    try:
        subprocess.run(
            ["adb", "pull", remote, dest],
            check=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        if logger:
            stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
            if "does not exist" in stderr or "No such file" in stderr:
                logger.error(
                    f"WhatsApp backup file not found on device at '{remote}'.\n"
                    "  Ensure 'End-to-end encrypted backup' is enabled in WhatsApp "
                    "(Settings > Chats > Chat backup) and a backup has been created."
                )
            elif "Permission denied" in stderr or "open failed" in stderr:
                logger.error(f"Permission denied accessing '{remote}' on device.")
            elif "device offline" in stderr or "device not found" in stderr:
                logger.error(f"Android device disconnected or went offline during pull: {stderr}")
            else:
                logger.error(f"ADB pull failed: {stderr}")
        raise
    return dest


def enumerate_remote_backup_files(
    business: bool, logger: logging.Logger
) -> tuple[str | None, list[str]]:
    """Discover the base msgstore.db.crypt15 and any msgstore-increment-*.crypt15 on the device."""

    remote_dir = (
        posixpath.dirname(_MSGSTORE_BUSINESS_PATH)
        if business
        else posixpath.dirname(_MSGSTORE_PATH)
    )

    logger.info(f"Checking for database backups in {remote_dir}...")
    try:
        result = _run_adb(
            ["adb", "shell", f'ls -1 "{remote_dir}"'],
            logger,
        )
    except Exception as e:
        logger.warning(f"Failed to list remote backups in {remote_dir}: {e}")
        return None, []

    lines = result.stdout.decode(errors="replace").splitlines()
    base_file = None
    rotated_base_candidates = []
    remote_inc_candidates = []
    inc_pattern = re.compile(r"msgstore-increment-(\d+)")
    rotated_base_pattern = re.compile(
        r"^msgstore-(\d{4}-\d{2}-\d{2}(?:\.\d+)?)\.db\.crypt(?:14|15)$"
    )

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line in ("msgstore.db.crypt15", "msgstore.db.crypt14"):
            base_file = f"{remote_dir}/{line}"
        elif rotated_base_pattern.match(line):
            rotated_base_candidates.append(f"{remote_dir}/{line}")
        elif inc_pattern.search(line) and (line.endswith(".crypt15") or line.endswith(".crypt14")):
            remote_inc_candidates.append(f"{remote_dir}/{line}")

    if base_file is None and rotated_base_candidates:
        rotated_base_candidates.sort()
        base_file = rotated_base_candidates[-1]

    sorted_inc_paths = filter_active_increments(remote_inc_candidates, base_file)

    if len(remote_inc_candidates) > len(sorted_inc_paths):
        logger.info(
            f"Filtered {len(remote_inc_candidates)} remote increments down to "
            f"{len(sorted_inc_paths)} active generation increment(s)."
        )

    if base_file is None:
        logger.warning(f"Base msgstore backup not found in {remote_dir}.")
    else:
        logger.info(
            f"Found base backup '{posixpath.basename(base_file)}' and "
            f"{len(sorted_inc_paths)} active incremental backup(s)."
        )

    return base_file, sorted_inc_paths


def pull_msgstore_and_increments(
    staging_dir: str, business: bool, logger: logging.Logger
) -> tuple[str, list[str]]:
    """Pull the base msgstore database and all active incremental diffs into staging_dir."""

    base_remote, inc_remotes = enumerate_remote_backup_files(business, logger)
    if base_remote is None:
        local_base = pull_msgstore(staging_dir, business, logger)
        return local_base, []

    os.makedirs(staging_dir, exist_ok=True)
    local_base = os.path.join(staging_dir, posixpath.basename(base_remote))
    logger.info(f"Pulling base msgstore '{posixpath.basename(base_remote)}'...")
    try:
        subprocess.run(
            ["adb", "pull", base_remote, local_base],
            check=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
        logger.error(f"Failed to pull base backup from '{base_remote}' via ADB: {stderr}")
        raise

    local_increments = []
    for inc_remote in inc_remotes:
        inc_name = posixpath.basename(inc_remote)
        local_inc = os.path.join(staging_dir, inc_name)
        logger.info(f"Pulling increment '{inc_name}'...")
        try:
            subprocess.run(
                ["adb", "pull", inc_remote, local_inc],
                check=True,
                stderr=subprocess.PIPE,
            )
            local_increments.append(local_inc)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
            logger.error(f"Failed to pull increment '{inc_remote}' via ADB: {stderr}")
            raise

    return local_base, local_increments


def pull_contacts(output_dir: str, logger=None) -> str:
    dest = os.path.join(output_dir, "wa_contacts")
    if logger:
        logger.info("Pulling contacts via ADB...")
    try:
        result = subprocess.run(
            [
                "adb",
                "shell",
                "content",
                "query",
                "--uri",
                _CONTACTS_URI,
                "--projection",
                _CONTACTS_PROJECTION,
            ],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        if logger:
            logger.error(
                f"ADB contacts query failed.\n  {e.stderr.decode(errors='replace').strip()}"
            )
        raise

    lines = result.stdout.decode(errors="replace").splitlines()
    relevant_lines = [
        line
        for line in lines
        if "vnd.com.whatsapp" in line
        or "@s.whatsapp.net" in line
        or "vnd.android.cursor.item/phone_v2" in line
    ]
    # If no lines matched known mimetypes, write all lines as fallback
    output_lines = relevant_lines if relevant_lines else lines
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines) + "\n")
    return dest


# ---------------------------------------------------------------------------
# ADB media pull helpers
# ---------------------------------------------------------------------------


def _staging_path(remote_path: str, remote_root: str, staging_dir: str) -> str:
    """Map a remote absolute path to a local staging path under Media/."""
    rel = remote_path[len(remote_root) :].lstrip("/")
    return os.path.join(staging_dir, "Media", rel.replace("/", os.sep))


def pull_media(
    staging_dir: str,
    business: bool,
    conn: sqlite3.Connection,
    logger,
    target_paths: set[str] | None,
) -> tuple:
    """
    Pull WhatsApp media from a connected device to staging_dir.

    staging_dir acts as a permanent 1:1 copy of the phone's WhatsApp storage
    (treated as wa_root during archiving; files are copied to the output folder
    and are not deleted).

    Returns (pulled, skipped, conflicts) where conflicts is a list of
    {'remote_path', 'remote_md5', 'archived_md5'} dicts.
    """
    from . import archive_db as _arc

    wa_media_root = _WA_BUSINESS_MEDIA_ROOT if business else _WA_MEDIA_ROOT
    os.makedirs(staging_dir, exist_ok=True)

    device_serial = get_device_serial(logger)
    md5_binary = probe_md5_binary(logger)

    # Clean up any partial files from a previous interrupted run
    partial_paths = _arc.get_adb_partial_paths(conn, device_serial)
    if partial_paths:
        logger.info(f"Cleaning {len(partial_paths)} partial file(s) from previous run...")
    for remote_path in partial_paths:
        local = _staging_path(remote_path, wa_media_root, staging_dir)
        if os.path.exists(local):
            os.remove(local)
            logger.debug(f"Removed partial: {local}")
        _arc.remove_adb_pull_state(conn, remote_path, device_serial)

    # Build filename index from archive DB for delta pre-filter
    filename_index = _arc.build_archive_filename_index(conn)

    # Enumerate remote files
    remote_files = enumerate_remote_files(wa_media_root, logger)
    if not remote_files:
        logger.warning("No media files found on device.")
        return 0, 0, []

    if target_paths is not None:
        prefix_len = len(wa_media_root)
        candidate_files = []
        for remote_path, remote_size in remote_files:
            rel = remote_path[prefix_len:].lstrip("/")
            clean_rel = rel.replace("\\", "/")
            db_path = f"Media/{clean_rel}"
            if db_path in target_paths:
                candidate_files.append((remote_path, remote_size))
        logger.info(
            f"Filtered {len(remote_files):,} remote file(s) to "
            f"{len(candidate_files):,} file(s) matching filter."
        )
        remote_files = candidate_files
        if not remote_files:
            logger.info("No remote files match the filter.")
            return 0, 0, []

    logger.info(
        f"Found {len(remote_files):,} file(s) to evaluate. "
        "If your WhatsApp media folder is several gigabytes, this can take hours, "
        "especially if you have many voice messages or other small files."
    )

    done_paths = _arc.get_adb_done_paths(conn, device_serial)

    pull_list = []
    skip_count = 0
    conflicts = []

    progress_eval = ProgressReporter("Evaluating media", len(remote_files), logger, sys.stderr, 0.2)

    for i, (remote_path, remote_size) in enumerate(remote_files, 1):
        if remote_path in done_paths:
            skip_count += 1
            progress_eval.update(
                i, {"To pull": len(pull_list), "Skipped": skip_count, "Conflicts": len(conflicts)}
            )
            continue
        basename = os.path.basename(remote_path)
        if basename not in filename_index:
            pull_list.append((remote_path, remote_size))
            progress_eval.update(
                i, {"To pull": len(pull_list), "Skipped": skip_count, "Conflicts": len(conflicts)}
            )
            continue
        db_size, db_md5 = filename_index[basename]
        if db_size is not None and db_size != remote_size:
            pull_list.append((remote_path, remote_size))
            progress_eval.update(
                i, {"To pull": len(pull_list), "Skipped": skip_count, "Conflicts": len(conflicts)}
            )
            continue
        # Size matches (or DB has no size for old record): verify via remote md5
        try:
            remote_md5 = _remote_md5(remote_path, md5_binary, logger)
        except Exception as e:
            logger.warning(f"Could not hash remote file {remote_path}: {e} — will pull")
            pull_list.append((remote_path, remote_size))
            progress_eval.update(
                i, {"To pull": len(pull_list), "Skipped": skip_count, "Conflicts": len(conflicts)}
            )
            continue
        if remote_md5 == db_md5:
            skip_count += 1
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "done")
        else:
            conflicts.append(
                {
                    "remote_path": remote_path,
                    "remote_md5": remote_md5.hex(),
                    "archived_md5": db_md5.hex(),
                }
            )
            logger.warning(
                f"CONFLICT: {basename} has different content on device vs archive "
                f"(same {'name+size' if db_size == remote_size else 'name'}) — skipped"
            )
        progress_eval.update(
            i, {"To pull": len(pull_list), "Skipped": skip_count, "Conflicts": len(conflicts)}
        )

    progress_eval.finish(
        {"To pull": len(pull_list), "Skipped": skip_count, "Conflicts": len(conflicts)}
    )

    logger.info(
        f"Delta pre-filter: {len(pull_list):,} to pull, "
        f"{skip_count:,} already archived, "
        f"{len(conflicts):,} conflict(s)."
    )

    pulled = 0
    failed = 0
    progress_pull = ProgressReporter("Pulling media", len(pull_list), logger, sys.stderr, 0.2)

    for i, (remote_path, remote_size) in enumerate(pull_list, 1):
        local = _staging_path(remote_path, wa_media_root, staging_dir)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "partial")
        try:
            _run_adb(["adb", "pull", remote_path, local], logger)
        except KeyboardInterrupt:
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "partial")
            progress_pull.finish({"Pulled": pulled, "Failed": failed})
            raise
        except Exception as e:
            stderr = ""
            if hasattr(e, "stderr") and e.stderr:
                stderr = e.stderr.decode(errors="replace")
            reason = _classify_adb_error(stderr)
            if (
                "device not found" in stderr
                or "device offline" in stderr
                or "error: closed" in stderr
            ):
                _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "partial")
                progress_pull.finish({"Pulled": pulled, "Failed": failed})
                raise RuntimeError(
                    f"Device disconnected during pull of {remote_path}: {reason}"
                ) from e
            logger.error(f"Failed to pull {remote_path}: {reason}")
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "partial")
            failed += 1
            progress_pull.update(i, {"Pulled": pulled, "Failed": failed})
            continue
        if not os.path.exists(local):
            logger.error(f"ADB pull reported success but file not found locally: {local}")
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "partial")
            failed += 1
            progress_pull.update(i, {"Pulled": pulled, "Failed": failed})
            continue
        if os.path.getsize(local) != remote_size:
            logger.warning(
                f"Pulled file is truncated (expected {remote_size} B, "
                f"got {os.path.getsize(local)} B): {remote_path}"
            )
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "partial")
            failed += 1
            progress_pull.update(i, {"Pulled": pulled, "Failed": failed})
            continue
        _arc.upsert_adb_pull_state(conn, remote_path, device_serial, "done")
        pulled += 1
        logger.debug(f"Pulled: {remote_path}")
        progress_pull.update(i, {"Pulled": pulled, "Failed": failed})

    progress_pull.finish({"Pulled": pulled, "Failed": failed})

    logger.info(f"Pull complete: {pulled:,} file(s) pulled.")
    return pulled, skip_count, conflicts


def write_adb_conflicts_report(report_path: str, conflicts: list, logger):
    """Write ADB pull conflicts to a CSV file — requires user intervention."""
    if not conflicts:
        return
    fieldnames = ["remote_path", "remote_md5", "archived_md5"]
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(conflicts)
    logger.warning(
        f"ADB conflicts report written to: {report_path} "
        f"({len(conflicts)} file(s) with same name/size but different content — "
        "review and resolve manually)"
    )
