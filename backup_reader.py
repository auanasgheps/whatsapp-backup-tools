import atexit
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


def _save_db_to_output(src: str, output_dir: str, filename: str,
                       logger: logging.Logger) -> str:
    """Copy a SQLite file to the output folder; warn if overwriting. Returns dest path."""
    os.makedirs(output_dir, exist_ok=True)
    dest = os.path.join(output_dir, filename)
    if os.path.exists(dest):
        logger.warning(f"Overwriting existing {dest} with extracted database.")
    shutil.copy2(src, dest)
    return dest


def extract_plaintext(backup_dir: str,
                      output_dir: str,
                      ios_contacts_override: str | None,
                      business: bool,
                      logger: logging.Logger) -> tuple[dict, str, str | None]:
    """
    Extract WhatsApp files from a plaintext (unencrypted) iPhone backup.
    Returns (manifest_map, msgstore_path, ios_contacts_path).

    ChatStorage.sqlite is copied to output_dir for re-run support.
    ContactsV2.sqlite is extracted to a temp file (auto-deleted on exit).
    """
    domain = _WA_BUSINESS_DOMAIN if business else _WA_DOMAIN
    logger.info("Building manifest map from backup...")
    manifest_map = build_manifest_map(backup_dir, logger, domain=domain)
    if not manifest_map:
        logger.warning(
            f"Manifest map is empty — no WhatsApp files found in the backup at: "
            f"{backup_dir}\n"
            f"  Make sure --ios_backup points to the backup root directory "
            f"(the folder that contains Manifest.db), and that the backup "
            f"includes WhatsApp data."
        )
    else:
        logger.info(f"Manifest map built: {len(manifest_map)} WhatsApp file(s).")

    logger.info("Extracting ChatStorage.sqlite from backup...")
    tmp_db = extract_to_temp(manifest_map, 'ChatStorage.sqlite', logger)
    msgstore_path = _save_db_to_output(tmp_db, output_dir, 'ChatStorage.sqlite', logger)
    os.unlink(tmp_db)

    ios_contacts_path = ios_contacts_override
    if ios_contacts_path is None:
        if manifest_map.get('ContactsV2.sqlite'):
            logger.info("Extracting ContactsV2.sqlite from backup...")
            tmp_contacts = extract_to_temp(manifest_map, 'ContactsV2.sqlite', logger)
            atexit.register(os.unlink, tmp_contacts)
            ios_contacts_path = tmp_contacts
        else:
            logger.warning("ContactsV2.sqlite not found in backup; proceeding without contacts.")

    return manifest_map, msgstore_path, ios_contacts_path


def extract_encrypted(backup_dir: str,
                      passphrase: str,
                      output_dir: str,
                      ios_contacts_override: str | None,
                      business: bool,
                      logger: logging.Logger) -> tuple[str, str | None, callable]:
    """
    Decrypt an encrypted iPhone backup and return a lazy media resolver.
    Returns (msgstore_path, ios_contacts_path, media_resolver).

    ChatStorage.sqlite is decrypted to output_dir for re-run support.
    ContactsV2.sqlite is decrypted to a temp file (auto-deleted on exit).
    Media files are decrypted on demand: media_resolver(relative_path) decrypts
    the file the first time it is requested and caches it in a temp dir
    (auto-deleted on exit). This means only the media that is actually referenced
    by the filtered query is decrypted.
    """
    try:
        from iphone_backup_decrypt import EncryptedBackup
    except ImportError:
        logger.error(
            "iphone-backup-decrypt is required for encrypted iOS backups but is not installed.\n"
            "  Run: pip install iphone-backup-decrypt"
        )
        raise SystemExit(1)

    logger.info("Unlocking encrypted backup...")
    try:
        backup = EncryptedBackup(backup_directory=backup_dir, passphrase=passphrase)
        backup.test_decryption()
    except ValueError as e:
        logger.error(f"Failed to unlock backup: {e}\n  Check that --ios_password is correct.")
        raise SystemExit(1)

    # Use a tight domain pattern that targets exactly one app.
    # DomainLike.WHATSAPP ("%net.whatsapp.%") is deliberately avoided — it
    # matches both regular and Business domains, which would mix their data.
    _domain = _WA_BUSINESS_DOMAIN if business else _WA_DOMAIN
    domain_like = f"%{_domain.split('-', 1)[1]}%"

    os.makedirs(output_dir, exist_ok=True)

    # --- ChatStorage.sqlite ---
    logger.info("Decrypting ChatStorage.sqlite...")
    tmp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.sqlite')
    tmp_db.close()
    try:
        backup.extract_file(relative_path='ChatStorage.sqlite',
                            domain_like=domain_like,
                            output_filename=tmp_db.name)
    except FileNotFoundError:
        logger.error("ChatStorage.sqlite not found in encrypted backup.")
        os.unlink(tmp_db.name)
        raise SystemExit(1)
    msgstore_path = _save_db_to_output(tmp_db.name, output_dir, 'ChatStorage.sqlite', logger)
    os.unlink(tmp_db.name)

    # --- ContactsV2.sqlite ---
    ios_contacts_path = ios_contacts_override
    if ios_contacts_path is None:
        tmp_contacts = tempfile.NamedTemporaryFile(delete=False, suffix='.sqlite')
        tmp_contacts.close()
        try:
            backup.extract_file(relative_path='ContactsV2.sqlite',
                                domain_like=domain_like,
                                output_filename=tmp_contacts.name)
            atexit.register(os.unlink, tmp_contacts.name)
            ios_contacts_path = tmp_contacts.name
            logger.info("Decrypted ContactsV2.sqlite.")
        except FileNotFoundError:
            logger.warning("ContactsV2.sqlite not found in encrypted backup; proceeding without contacts.")
            os.unlink(tmp_contacts.name)

    # --- Lazy media resolver ---
    # Decrypt each media file on demand, caching in a temp dir.
    # Only files actually referenced by the (possibly filtered) query are touched.
    tmp_media_dir = tempfile.mkdtemp(prefix='wa_ios_media_')
    atexit.register(shutil.rmtree, tmp_media_dir, ignore_errors=True)
    cache: dict[str, str] = {}

    def _media_resolver(relative_path: str) -> str | None:
        if relative_path in cache:
            return cache[relative_path]
        # Mirror the output path that extract_files(preserve_folders=True) would produce
        dest = os.path.join(tmp_media_dir, *relative_path.split('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            backup.extract_file(relative_path=relative_path,
                                domain_like=domain_like,
                                output_filename=dest)
        except FileNotFoundError:
            return None
        cache[relative_path] = dest
        return dest

    return msgstore_path, ios_contacts_path, _media_resolver
