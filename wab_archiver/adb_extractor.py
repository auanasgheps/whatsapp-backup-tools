import os
import shutil
import subprocess

_MSGSTORE_PATH = (
    '/storage/emulated/0/Android/media/com.whatsapp'
    '/WhatsApp/Databases/msgstore.db.crypt15'
)
_MSGSTORE_BUSINESS_PATH = (
    '/storage/emulated/0/Android/media/com.whatsapp.w4b'
    '/WhatsApp Business/Databases/msgstore.db.crypt15'
)
_CONTACTS_URI = 'content://com.android.contacts/data'
_CONTACTS_PROJECTION = 'display_name:data1'


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
