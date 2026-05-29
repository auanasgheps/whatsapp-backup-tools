import contextlib
import logging
import os
import plistlib
import shutil
import sqlite3
import tempfile

# ==============================================================================
# backup_reader.py — iPhone backup format parsing
# Knows nothing about WhatsApp schema; handles only the backup file structure.
# ==============================================================================

_WA_DOMAIN          = 'AppDomainGroup-group.net.whatsapp.WhatsApp.shared'
_WA_BUSINESS_DOMAIN = 'AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared'


def detect_encrypted(backup_dir: str, logger: logging.Logger) -> bool:
    """
    Return True if the iPhone backup is encrypted.
    Reads the IsEncrypted flag from Manifest.plist in the backup directory.
    """
    if not os.path.isdir(backup_dir):
        logger.error(
            f"--ios_backup path does not exist or is not a directory: {backup_dir}"
        )
        raise SystemExit(1)

    info_path = os.path.join(backup_dir, 'Info.plist')
    if not os.path.isfile(info_path):
        manifest_path = os.path.join(backup_dir, 'Manifest.plist')
        if not os.path.isfile(manifest_path):
            logger.error(
                f"Neither Info.plist nor Manifest.plist found in: {backup_dir}\n"
                f"  Make sure --ios_backup points to the backup directory "
                f"(the folder that contains Manifest.db)."
            )
            raise SystemExit(1)
        plist_path = manifest_path
    else:
        plist_path = info_path

    with open(plist_path, 'rb') as f:
        data = plistlib.load(f)

    # Manifest.plist uses 'IsEncrypted'; Info.plist does not carry this flag.
    # If we only have Info.plist, fall back to checking Manifest.plist if present.
    is_encrypted = data.get('IsEncrypted', False)
    if not is_encrypted and plist_path == info_path:
        manifest_path = os.path.join(backup_dir, 'Manifest.plist')
        if os.path.isfile(manifest_path):
            with open(manifest_path, 'rb') as f:
                manifest_data = plistlib.load(f)
            is_encrypted = manifest_data.get('IsEncrypted', False)

    return bool(is_encrypted)


def build_manifest_map(backup_dir: str,
                       logger: logging.Logger,
                       domain: str = _WA_DOMAIN) -> dict[str, str]:
    """
    Build a {relativePath: absolute_hash_file_path} map for all WhatsApp files
    in the backup. Reads Manifest.db once at startup; all subsequent lookups
    are O(1) dict access.

    Hash files are stored at <backup_dir>/<fileID[:2]>/<fileID>.
    """
    if not os.path.isdir(backup_dir):
        logger.error(
            f"--ios_backup path does not exist or is not a directory: {backup_dir}"
        )
        raise SystemExit(1)

    manifest_db = os.path.join(backup_dir, 'Manifest.db')
    if not os.path.isfile(manifest_db):
        logger.error(
            f"Manifest.db not found in: {backup_dir}\n"
            f"  Make sure --ios_backup points to the backup root directory."
        )
        raise SystemExit(1)

    with contextlib.closing(sqlite3.connect(manifest_db)) as conn:
        rows = conn.execute(
            "SELECT fileID, relativePath "
            "FROM Files "
            "WHERE domain = ? "
            "  AND relativePath IS NOT NULL",
            (domain,),
        ).fetchall()

    result = {}
    for file_id, relative_path in rows:
        abs_path = os.path.join(backup_dir, file_id[:2], file_id)
        result[relative_path] = abs_path

    return result


def extract_to_temp(manifest_map: dict[str, str],
                    relative_path: str,
                    logger: logging.Logger) -> str:
    """
    Copy a single file from the backup hash tree to a NamedTemporaryFile.
    Returns the temp file path. Caller is responsible for deleting it on exit.

    Raises SystemExit with a clear message if the file is not in the manifest
    or the hash file does not exist on disk.
    """
    src = manifest_map.get(relative_path)
    if src is None:
        logger.error(
            f"'{relative_path}' not found in backup manifest.\n"
            f"  This file may not exist in the WhatsApp domain of this backup."
        )
        raise SystemExit(1)

    if not os.path.isfile(src):
        logger.error(
            f"Backup hash file not found on disk: {src}\n"
            f"  The backup may be incomplete or corrupt."
        )
        raise SystemExit(1)

    suffix = os.path.splitext(relative_path)[1] or '.tmp'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    shutil.copy2(src, tmp.name)
    return tmp.name
