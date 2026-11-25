"""Shared Android Debug Bridge (ADB) transport and device inspection primitives."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

_TRANSIENT_ERRORS = (
    "error: closed",
    "Connection reset by peer",
    "Broken pipe",
    "device offline",
)
_ADB_TIMEOUT = 120  # seconds per adb call; WiFi ADB can hang silently
_ADB_RETRIES = 2


def build_adb_cmd(device_serial: str | None, args: list[str]) -> list[str]:
    """Build an adb command list, scoping to device_serial when provided."""
    if device_serial:
        return ["adb", "-s", device_serial, *args]
    return ["adb", *args]


def check_adb(logger: logging.Logger) -> bool:
    """Return True if the adb binary is available on PATH."""
    if shutil.which("adb"):
        return True
    logger.error(
        "ADB not found on PATH. Install Android SDK Platform Tools and add it to your PATH.\n"
        "  Download: https://developer.android.com/tools/releases/platform-tools"
    )
    return False


def _parse_adb_devices(stdout: str) -> list[tuple[str, str]]:
    """Parse 'adb devices' output into a list of (serial, state) tuples."""
    devices = []
    for line in stdout.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("*") or trimmed.startswith("List of devices"):
            continue
        parts = trimmed.split()
        if len(parts) >= 2:
            serial = parts[0]
            if parts[1] == "no" and len(parts) >= 3 and parts[2].startswith("permission"):
                state = "no permissions"
            else:
                state = parts[1]
            devices.append((serial, state))
    return devices


def _query_adb_devices(logger: logging.Logger) -> list[tuple[str, str]] | None:
    """Execute 'adb devices' and return parsed (serial, state) pairs, or None on failure."""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"'adb devices' failed: {e.stderr.decode(errors='replace').strip()}")
        return None
    return _parse_adb_devices(result.stdout.decode(errors="replace"))


def _confirm_restart_prompt(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        response = input(prompt)
        return response.strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _prompt_user_to_continue(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        input(prompt)
        return True
    except (EOFError, KeyboardInterrupt):
        return False


def _restart_adb_server(logger: logging.Logger) -> None:
    logger.info("Restarting ADB server...")
    try:
        subprocess.run(
            ["adb", "kill-server"],
            check=True,
            capture_output=True,
            timeout=_ADB_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"'adb kill-server' warning: {e}")
    time.sleep(2)


def _validate_single_device_state(
    serial: str, state: str, logger: logging.Logger, allow_auth_prompt: bool
) -> bool:
    """Validate that a single device is in the 'device' (authorized) state."""
    if state == "device":
        logger.info(f"Android device connected and authorized: {serial}")
        return True

    if state == "unauthorized":
        logger.warning(
            f"Android device '{serial}' is connected but unauthorized "
            "(the USB debugging authorization prompt was not accepted on the phone)."
        )
        if allow_auth_prompt:
            prompt_msg = (
                "Restart ADB server to re-trigger authorization prompt on your phone? [y/N]: "
            )
            if _confirm_restart_prompt(prompt_msg):
                logger.info("Restarting ADB server... Please ensure your phone screen is unlocked.")
                _restart_adb_server(logger)
                _query_adb_devices(logger)
                continue_prompt = (
                    "Unlock your phone, tap 'Allow USB debugging' (check 'Always allow'), "
                    "then press Enter to continue: "
                )
                if not _prompt_user_to_continue(continue_prompt):
                    logger.error("Authorization check cancelled by user.")
                    return False
                devices_after = _query_adb_devices(logger)
                if devices_after:
                    for s_after, st_after in devices_after:
                        if s_after == serial and st_after == "device":
                            logger.info(f"Android device connected and authorized: {serial}")
                            return True
                logger.error(
                    f"Device '{serial}' is still unauthorized.\n"
                    "  1. Unlock your phone screen and look for the 'Allow USB debugging' prompt\n"
                    "  2. In Settings > Developer options, tap 'Revoke USB debugging authorizations'\n"
                    "  3. Ensure USB connection mode is set to 'File Transfer (MTP)' rather than 'Charge only'"
                )
                return False

        logger.error(
            f"Android device '{serial}' is connected but unauthorized.\n"
            "  Unlock your phone and accept the 'Allow USB debugging' prompt "
            "(check 'Always allow from this computer')."
        )
        return False

    if state == "offline":
        logger.error(
            f"Android device '{serial}' is connected but offline.\n"
            "  Reconnect the USB cable, toggle USB debugging off and on in Developer options, "
            "or restart the ADB server ('adb kill-server')."
        )
        return False

    if state == "no permissions":
        logger.error(
            f"Android device '{serial}' detected, but ADB lacks host permissions to access it.\n"
            "  Check your USB permissions or driver (on Linux, check udev rules / plugdev group)."
        )
        return False

    if state in ("recovery", "sideload", "bootloader"):
        logger.error(
            f"Android device '{serial}' is in '{state}' mode.\n"
            "  Normal Android OS is required for WhatsApp operations."
        )
        return False

    logger.error(
        f"Android device '{serial}' is in unexpected state '{state}'.\n"
        "  Reconnect the device and ensure USB debugging is enabled."
    )
    return False


def check_device_connected(logger: logging.Logger) -> bool:
    """Verify that exactly one authorized Android device is connected."""
    devices = _query_adb_devices(logger)
    if devices is None:
        return False

    if not devices:
        logger.error(
            "No Android device detected over USB or Wi-Fi. Ensure:\n"
            "  1. The device is connected via a data-capable USB cable\n"
            "  2. USB debugging is enabled in Settings > Developer options"
        )
        return False

    if len(devices) > 1:
        summary = ", ".join(f"{s} ({st})" for s, st in devices)
        logger.error(
            f"Multiple Android devices detected: {summary}.\n"
            "  Disconnect all devices except the one you wish to archive."
        )
        return False

    serial, state = devices[0]
    return _validate_single_device_state(serial, state, logger, True)


def resolve_connected_device(preferred_serial: str | None, logger: logging.Logger) -> str:
    """Verify device connectivity and return the target serial number.

    If preferred_serial is given, validates that specific device is connected and
    authorized. Otherwise requires exactly one connected device. Raises RuntimeError
    if no suitable device is available.
    """
    devices = _query_adb_devices(logger)
    if devices is None:
        raise RuntimeError("Failed to query connected ADB devices.")
    if not devices:
        raise RuntimeError(
            "No Android device detected over USB or Wi-Fi. "
            "Ensure USB debugging is enabled and the cable is connected."
        )

    if preferred_serial:
        matched = [(s, st) for s, st in devices if s == preferred_serial]
        if not matched:
            available = ", ".join(f"{s} ({st})" for s, st in devices)
            raise RuntimeError(
                f"Requested device '{preferred_serial}' is not connected. "
                f"Connected devices: {available}"
            )
        serial, state = matched[0]
        if not _validate_single_device_state(serial, state, logger, False):
            raise RuntimeError(f"Device '{serial}' is not ready (state: {state}).")
        return serial

    if len(devices) > 1:
        summary = ", ".join(f"{s} ({st})" for s, st in devices)
        raise RuntimeError(
            f"Multiple Android devices detected: {summary}. "
            "Specify '--device <serial>' or set 'device' in config.toml."
        )

    serial, state = devices[0]
    if not _validate_single_device_state(serial, state, logger, False):
        raise RuntimeError(f"Device '{serial}' is not ready (state: {state}).")
    return serial


def get_device_serial(logger: logging.Logger) -> str:
    """Return the serial number of the connected ADB device."""
    result = subprocess.run(
        ["adb", "get-serialno"],
        capture_output=True,
        timeout=_ADB_TIMEOUT,
    )
    serial = result.stdout.decode(errors="replace").strip()
    if result.returncode != 0 or not serial or serial == "unknown":
        raise RuntimeError(
            f"Could not get device serial: {result.stderr.decode(errors='replace').strip()}"
        )
    logger.debug(f"Device serial: {serial}")
    return serial


def probe_md5_binary_for_device(device_serial: str | None, logger: logging.Logger) -> str:
    """Return the name of the md5 binary available on the device ('md5sum' or 'md5')."""
    cmd = build_adb_cmd(device_serial, ["shell", "which md5sum || which md5"])
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=_ADB_TIMEOUT,
    )
    path = result.stdout.decode(errors="replace").strip().splitlines()
    if not path or result.returncode == 127:
        raise RuntimeError(
            "Neither md5sum nor md5 found on device — cannot verify file hashes over ADB."
        )
    binary = os.path.basename(path[0])
    logger.debug(f"md5 binary on device: {binary}")
    return binary


def probe_md5_binary(logger: logging.Logger) -> str:
    """Return the name of the md5 binary available on the default connected device."""
    return probe_md5_binary_for_device(None, logger)


def _run_adb(cmd: list[str], logger: logging.Logger) -> subprocess.CompletedProcess:
    """Run an ADB command with retries on transient connection errors."""
    last_exc = None
    for attempt in range(_ADB_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=_ADB_TIMEOUT,
            )
            if result.returncode == 0:
                return result
            stderr = result.stderr.decode(errors="replace")
            if any(e in stderr for e in _TRANSIENT_ERRORS):
                if attempt < _ADB_RETRIES:
                    logger.warning(
                        f"Transient ADB error (attempt {attempt + 1}/{_ADB_RETRIES + 1}): "
                        f"{stderr.strip()}"
                    )
                    time.sleep(2)
                    continue
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        except subprocess.TimeoutExpired as e:
            last_exc = e
            if attempt < _ADB_RETRIES:
                logger.warning(
                    f"ADB command timed out (attempt {attempt + 1}/{_ADB_RETRIES + 1}), retrying..."
                )
                time.sleep(2)
                continue
            raise RuntimeError(
                f"ADB command timed out after {_ADB_RETRIES + 1} attempts: {' '.join(cmd)}"
            ) from last_exc
    raise subprocess.CalledProcessError(1, cmd)


def _classify_adb_error(stderr: str) -> str:
    """Return a human-readable explanation of an ADB failure."""
    if "does not exist" in stderr:
        return "Remote path not found"
    if "Permission denied" in stderr or "open failed" in stderr:
        return "Permission denied — file not accessible without root"
    if "device not found" in stderr or "device offline" in stderr:
        return "Device disconnected or offline"
    if "No space left on device" in stderr:
        return "Host disk is full"
    return stderr.strip()


def enumerate_remote_files_for_device(
    wa_media_root: str, device_serial: str | None, logger: logging.Logger
) -> list[tuple[str, int]]:
    """Return list of (remote_path, size_bytes) for all files under wa_media_root."""
    logger.info(f"Enumerating remote files under {wa_media_root}...")
    cmd = build_adb_cmd(
        device_serial,
        ["shell", f'find "{wa_media_root}" -type f -printf "%p\\t%s\\n"'],
    )
    result = _run_adb(cmd, logger)
    entries = []
    for line in result.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if "Permission denied" in line or "No such file or directory" in line:
            logger.debug(f"find: skipped inaccessible path: {line}")
            continue
        parts = line.rsplit("\t", 1)
        if len(parts) != 2:
            continue
        remote_path, size_str = parts
        try:
            entries.append((remote_path.strip(), int(size_str.strip())))
        except ValueError:
            logger.debug(f"find: could not parse size in line: {line!r}")
    logger.info(f"Found {len(entries)} files on device.")
    return entries


def enumerate_remote_files(wa_media_root: str, logger: logging.Logger) -> list[tuple[str, int]]:
    """Return list of (remote_path, size_bytes) for all files on the default device."""
    return enumerate_remote_files_for_device(wa_media_root, None, logger)


def parse_md5_output(out: str) -> bytes:
    """Parse GNU md5sum or Toybox/BSD md5 output into raw 16-byte digest."""
    text = out.strip()
    if not text:
        raise ValueError("Empty MD5 output from device.")
    if text.upper().startswith("MD5") and "=" in text:
        hex_str = text.rsplit("=", 1)[1].strip()
    elif ":" in text and not text.startswith("/"):
        hex_str = text.split(":", 1)[1].strip()
    else:
        hex_str = text.split()[0]
    return bytes.fromhex(hex_str)


def remote_md5_for_device(
    remote_path: str, md5_binary: str, device_serial: str | None, logger: logging.Logger
) -> bytes:
    """Return the MD5 hash of a remote file as bytes."""
    cmd = build_adb_cmd(device_serial, ["shell", f'{md5_binary} "{remote_path}"'])
    result = _run_adb(cmd, logger)
    return parse_md5_output(result.stdout.decode(errors="replace"))


def _remote_md5(remote_path: str, md5_binary: str, logger: logging.Logger) -> bytes:
    """Return the MD5 hash of a remote file on the default device as bytes."""
    return remote_md5_for_device(remote_path, md5_binary, None, logger)


def get_android_sdk_level(device_serial: str | None, logger: logging.Logger) -> int | None:
    """Query ro.build.version.sdk on the connected device. Returns None if unavailable."""
    cmd = build_adb_cmd(device_serial, ["shell", "getprop", "ro.build.version.sdk"])
    try:
        result = _run_adb(cmd, logger)
    except Exception as e:
        logger.debug(f"Could not query Android SDK level: {e}")
        return None
    raw = result.stdout.decode(errors="replace").strip()
    if raw.isdigit():
        return int(raw)
    return None


def get_remote_free_kb(device_serial: str | None, remote_path: str, logger: logging.Logger) -> int:
    """Return available storage in KiB on the device filesystem hosting remote_path."""
    cmd = build_adb_cmd(device_serial, ["shell", f"df -k '{remote_path}' | tail -1"])
    result = _run_adb(cmd, logger)
    line = result.stdout.decode(errors="replace").strip()
    parts = line.split()
    # Standard df -k columns: Filesystem  1K-blocks  Used  Available  Use%  Mounted-on
    if len(parts) < 4:
        raise RuntimeError(f"Unexpected 'df -k' output for '{remote_path}': {line!r}")
    try:
        return int(parts[3])
    except ValueError as e:
        raise RuntimeError(f"Could not parse available KiB from 'df -k' output: {line!r}") from e


def check_active_whatsapp_backup(
    device_serial: str | None, package_name: str, logger: logging.Logger
) -> bool:
    """Return True if an active WhatsApp backup service appears to be running on device."""
    cmd = build_adb_cmd(
        device_serial,
        ["shell", f"dumpsys activity services {package_name}"],
    )
    try:
        result = _run_adb(cmd, logger)
    except Exception as e:
        logger.debug(f"Could not inspect WhatsApp services via dumpsys: {e}")
        return False
    stdout = result.stdout.decode(errors="replace")
    for line in stdout.splitlines():
        if "backup" in line.lower() and (
            "servicerecord" in line.lower() or "running" in line.lower()
        ):
            return True
    return False


def set_usb_wake_lock(device_serial: str | None, enabled: bool, logger: logging.Logger) -> bool:
    """Best-effort toggle of USB stay-awake mode. Never raises on OEM restrictions."""
    mode = "usb" if enabled else "false"
    cmd = build_adb_cmd(device_serial, ["shell", "svc", "power", "stayon", mode])
    try:
        _run_adb(cmd, logger)
        return True
    except Exception as e:
        logger.debug(f"Best-effort 'svc power stayon {mode}' failed (non-fatal): {e}")
        return False


def run_adb_shell_script(
    device_serial: str | None, script: str, logger: logging.Logger
) -> subprocess.CompletedProcess:
    """Execute a POSIX shell script on the device via stdin with transient retry handling."""
    cmd = build_adb_cmd(device_serial, ["shell", "sh"])
    payload = script.replace("\r\n", "\n").encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(_ADB_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                timeout=_ADB_TIMEOUT,
            )
            if result.returncode == 0:
                return result
            stderr = result.stderr.decode(errors="replace")
            if any(e in stderr for e in _TRANSIENT_ERRORS) and attempt < _ADB_RETRIES:
                logger.warning(
                    f"Transient ADB shell error (attempt {attempt + 1}/{_ADB_RETRIES + 1}): "
                    f"{stderr.strip()}"
                )
                time.sleep(2)
                continue
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        except subprocess.TimeoutExpired as e:
            last_exc = e
            if attempt < _ADB_RETRIES:
                logger.warning(
                    f"ADB shell script timed out (attempt {attempt + 1}/{_ADB_RETRIES + 1}), retrying..."
                )
                time.sleep(2)
                continue
            raise RuntimeError(
                f"ADB shell script timed out after {_ADB_RETRIES + 1} attempts"
            ) from last_exc
    raise subprocess.CalledProcessError(1, cmd)


def push_file(
    device_serial: str | None, local_path: str, remote_path: str, logger: logging.Logger
) -> subprocess.CompletedProcess:
    """Push a local file to remote_path on the device via ADB."""
    cmd = build_adb_cmd(device_serial, ["push", local_path, remote_path])
    return _run_adb(cmd, logger)
