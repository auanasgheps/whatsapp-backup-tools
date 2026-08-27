import csv
import datetime
import os
import shutil
import sqlite3
import subprocess
import time

_MSGSTORE_PATH = (
    '/storage/emulated/0/Android/media/com.whatsapp'
    '/WhatsApp/Databases/msgstore.db.crypt15'
)
_MSGSTORE_BUSINESS_PATH = (
    '/storage/emulated/0/Android/media/com.whatsapp.w4b'
    '/WhatsApp Business/Databases/msgstore.db.crypt15'
)
_WA_MEDIA_ROOT = '/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media'
_WA_BUSINESS_MEDIA_ROOT = (
    '/storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp Business/Media'
)
_CONTACTS_URI = 'content://com.android.contacts/data'
_CONTACTS_PROJECTION = 'display_name:data1'

_TRANSIENT_ERRORS = (
    'error: closed',
    'Connection reset by peer',
    'Broken pipe',
    'device offline',
)
_ADB_TIMEOUT = 120  # seconds per adb call; WiFi ADB can hang silently
_ADB_RETRIES = 2


def check_adb(logger) -> bool:
    if shutil.which('adb'):
        return True
    logger.error(
        "ADB not found on PATH. Install Android SDK Platform Tools and add it to your PATH.\n"
        "  Download: https://developer.android.com/tools/releases/platform-tools"
    )
    return False


def check_device_connected(logger) -> bool:
    try:
        result = subprocess.run(
            ['adb', 'devices'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"'adb devices' failed: {e.stderr.decode(errors='replace').strip()}")
        return False

    lines = result.stdout.decode(errors='replace').splitlines()
    connected = [line for line in lines if line.strip().endswith('device')]
    if connected:
        return True

    logger.error(
        "No Android device detected. Ensure:\n"
        "  1. USB debugging is enabled on the device (Settings > Developer options)\n"
        "  2. The device is connected via USB\n"
        "  3. You have authorised the connection on the phone when prompted"
    )
    return False


def pull_msgstore(output_dir: str, business: bool = False, logger=None) -> str:
    remote = _MSGSTORE_BUSINESS_PATH if business else _MSGSTORE_PATH
    dest = os.path.join(output_dir, 'msgstore.db.crypt15')
    if logger:
        logger.info("Pulling msgstore backup via ADB...")
    try:
        subprocess.run(
            ['adb', 'pull', remote, dest],
            check=True, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        if logger:
            logger.error(
                f"ADB pull failed.\n"
                f"  {e.stderr.decode(errors='replace').strip()}\n"
                f"  Make sure the device is connected, USB debugging is enabled, "
                f"and the connection is authorised on the phone."
            )
        raise
    return dest


def pull_contacts(output_dir: str, logger=None) -> str:
    dest = os.path.join(output_dir, 'wa_contacts')
    if logger:
        logger.info("Pulling contacts via ADB...")
    try:
        result = subprocess.run(
            ['adb', 'shell', 'content', 'query',
             '--uri', _CONTACTS_URI,
             '--projection', _CONTACTS_PROJECTION],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
    except subprocess.CalledProcessError as e:
        if logger:
            logger.error(
                f"ADB contacts query failed.\n"
                f"  {e.stderr.decode(errors='replace').strip()}"
            )
        raise

    lines = result.stdout.decode(errors='replace').splitlines()
    wa_lines = [line for line in lines if '@s.whatsapp.net' in line]
    with open(dest, 'w', encoding='utf-8') as f:
        f.write('\n'.join(wa_lines) + '\n')
    return dest


# ---------------------------------------------------------------------------
# ADB media pull helpers
# ---------------------------------------------------------------------------

def get_device_serial(logger) -> str:
    """Return the serial number of the connected ADB device."""
    result = subprocess.run(
        ['adb', 'get-serialno'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=_ADB_TIMEOUT,
    )
    serial = result.stdout.decode(errors='replace').strip()
    if result.returncode != 0 or not serial or serial == 'unknown':
        raise RuntimeError(
            f"Could not get device serial: {result.stderr.decode(errors='replace').strip()}"
        )
    logger.debug(f"Device serial: {serial}")
    return serial


def probe_md5_binary(logger) -> str:
    """Return the name of the md5 binary available on the device ('md5sum' or 'md5')."""
    result = subprocess.run(
        ['adb', 'shell', 'which md5sum || which md5'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=_ADB_TIMEOUT,
    )
    path = result.stdout.decode(errors='replace').strip().splitlines()
    if not path or result.returncode == 127:
        raise RuntimeError(
            "Neither md5sum nor md5 found on device — "
            "cannot verify file hashes for ADB pull."
        )
    binary = os.path.basename(path[0])
    logger.debug(f"md5 binary on device: {binary}")
    return binary


def _run_adb(cmd: list, logger) -> subprocess.CompletedProcess:
    """Run an ADB command with retries on transient connection errors."""
    last_exc = None
    for attempt in range(_ADB_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_ADB_TIMEOUT,
            )
            if result.returncode == 0:
                return result
            stderr = result.stderr.decode(errors='replace')
            if any(e in stderr for e in _TRANSIENT_ERRORS):
                if attempt < _ADB_RETRIES:
                    logger.warning(
                        f"Transient ADB error (attempt {attempt + 1}/{_ADB_RETRIES + 1}): "
                        f"{stderr.strip()}"
                    )
                    time.sleep(2)
                    continue
            raise subprocess.CalledProcessError(result.returncode, cmd,
                                                result.stdout, result.stderr)
        except subprocess.TimeoutExpired as e:
            last_exc = e
            if attempt < _ADB_RETRIES:
                logger.warning(
                    f"ADB command timed out (attempt {attempt + 1}/{_ADB_RETRIES + 1}), "
                    f"retrying..."
                )
                time.sleep(2)
                continue
            raise RuntimeError(
                f"ADB command timed out after {_ADB_RETRIES + 1} attempts: {' '.join(cmd)}"
            ) from last_exc
    raise subprocess.CalledProcessError(1, cmd)


def _classify_adb_error(stderr: str) -> str:
    """Return a human-readable explanation of an ADB pull failure."""
    if 'does not exist' in stderr:
        return "Remote path not found"
    if 'Permission denied' in stderr or 'open failed' in stderr:
        return "Permission denied — file not accessible without root"
    if 'device not found' in stderr or 'device offline' in stderr:
        return "Device disconnected or offline"
    if 'No space left on device' in stderr:
        return "Host disk is full"
    return stderr.strip()


def enumerate_remote_files(wa_media_root: str, logger) -> list:
    """Return list of (remote_path, size_bytes) for all files under wa_media_root."""
    logger.info(f"Enumerating remote files under {wa_media_root}...")
    result = _run_adb(
        ['adb', 'shell', f'find {wa_media_root} -type f -printf "%p\\t%s\\n"'],
        logger,
    )
    entries = []
    for line in result.stdout.decode(errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        # Filter find error lines mixed into stdout
        if 'Permission denied' in line or 'No such file or directory' in line:
            logger.debug(f"find: skipped inaccessible path: {line}")
            continue
        parts = line.rsplit('\t', 1)
        if len(parts) != 2:
            continue
        remote_path, size_str = parts
        try:
            entries.append((remote_path.strip(), int(size_str.strip())))
        except ValueError:
            logger.debug(f"find: could not parse size in line: {line!r}")
    logger.info(f"Found {len(entries)} files on device.")
    return entries


def _remote_md5(remote_path: str, md5_binary: str, logger) -> bytes:
    """Return the MD5 hash of a remote file as bytes."""
    result = _run_adb(
        ['adb', 'shell', f'{md5_binary} "{remote_path}"'],
        logger,
    )
    out = result.stdout.decode(errors='replace').strip()
    # md5sum: "<hash>  <path>"; md5 (older): "<hash> <path>" or "<path>: <hash>"
    if ':' in out and not out.startswith('/'):
        # "<path>: <hash>" format
        hex_str = out.split(':', 1)[1].strip()
    else:
        hex_str = out.split()[0]
    return bytes.fromhex(hex_str)


def _staging_path(remote_path: str, remote_root: str, staging_dir: str) -> str:
    """Map a remote absolute path to a local staging path."""
    rel = remote_path[len(remote_root):].lstrip('/')
    return os.path.join(staging_dir, rel.replace('/', os.sep))


def pull_media(staging_dir: str, business: bool,
               conn: sqlite3.Connection, logger) -> tuple:
    """
    Pull WhatsApp media from a connected device to staging_dir.

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

    logger.info(
        f"Found {len(remote_files):,} file(s) to evaluate. "
        "If your WhatsApp media folder is several gigabytes, this can take hours, "
        "especially if you have many voice messages or other small files."
    )

    done_paths = _arc.get_adb_done_paths(conn, device_serial)

    pull_list = []
    skip_count = 0
    conflicts = []

    for remote_path, remote_size in remote_files:
        if remote_path in done_paths:
            skip_count += 1
            continue
        basename = os.path.basename(remote_path)
        if basename not in filename_index:
            pull_list.append((remote_path, remote_size))
            continue
        db_size, db_md5 = filename_index[basename]
        if db_size is not None and db_size != remote_size:
            pull_list.append((remote_path, remote_size))
            continue
        # Size matches (or DB has no size for old record): verify via remote md5
        try:
            remote_md5 = _remote_md5(remote_path, md5_binary, logger)
        except Exception as e:
            logger.warning(f"Could not hash remote file {remote_path}: {e} — will pull")
            pull_list.append((remote_path, remote_size))
            continue
        if remote_md5 == db_md5:
            skip_count += 1
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'done')
        else:
            conflicts.append({
                'remote_path': remote_path,
                'remote_md5': remote_md5.hex(),
                'archived_md5': db_md5.hex(),
            })
            logger.warning(
                f"CONFLICT: {basename} has different content on device vs archive "
                f"(same {'name+size' if db_size == remote_size else 'name'}) — skipped"
            )

    logger.info(
        f"Delta pre-filter: {len(pull_list):,} to pull, "
        f"{skip_count:,} already archived, "
        f"{len(conflicts):,} conflict(s)."
    )

    pulled = 0
    for remote_path, remote_size in pull_list:
        local = _staging_path(remote_path, wa_media_root, staging_dir)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'partial')
        try:
            _run_adb(['adb', 'pull', remote_path, local], logger)
        except KeyboardInterrupt:
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'partial')
            raise
        except Exception as e:
            stderr = ''
            if hasattr(e, 'stderr') and e.stderr:
                stderr = e.stderr.decode(errors='replace')
            reason = _classify_adb_error(stderr)
            if 'device not found' in stderr or 'device offline' in stderr or \
                    'error: closed' in stderr:
                _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'partial')
                raise RuntimeError(
                    f"Device disconnected during pull of {remote_path}: {reason}"
                ) from e
            logger.error(f"Failed to pull {remote_path}: {reason}")
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'partial')
            continue
        if not os.path.exists(local):
            logger.error(
                f"ADB pull reported success but file not found locally: {local}"
            )
            _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'partial')
            continue
        _arc.upsert_adb_pull_state(conn, remote_path, device_serial, 'done')
        pulled += 1
        logger.debug(f"Pulled: {remote_path}")

    logger.info(f"Pull complete: {pulled:,} file(s) pulled.")
    return pulled, skip_count, conflicts


def write_adb_conflicts_report(report_path: str, conflicts: list, logger):
    """Write ADB pull conflicts to a CSV file — requires user intervention."""
    if not conflicts:
        return
    fieldnames = ['remote_path', 'remote_md5', 'archived_md5']
    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(conflicts)
    logger.warning(
        f"ADB conflicts report written to: {report_path} "
        f"({len(conflicts)} file(s) with same name/size but different content — "
        "review and resolve manually)"
    )
