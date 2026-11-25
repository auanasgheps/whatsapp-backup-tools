"""Configuration loading, validation, and policy parsing for wab-sweeper."""

from __future__ import annotations

import calendar
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime

VALID_MEDIA_TYPES = frozenset(
    {"image", "video", "audio", "voice_note", "video_note", "document", "sticker"}
)

DEFAULT_INCLUDE_TYPES = ("image", "video", "audio", "voice_note", "document")

_DURATION_RE = re.compile(r"^(\d+)([dmy])$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)\s*$", re.IGNORECASE)

_SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


@dataclass(frozen=True)
class ChatTarget:
    """Single-chat sweep target with Safety Handshake identity."""

    jid: str
    name: str
    sweep_shared: bool


@dataclass(frozen=True)
class SweeperConfig:
    """Validated configuration for wab-sweeper."""

    archive_dir: str
    dry_run: bool
    log_path: str | None
    device: str | None
    android_user_id: int
    older_than: str | None
    keep_last: str | None
    target_free_bytes: int | None
    sort_by: str
    min_file_size_bytes: int | None
    include_types: frozenset[str]
    protect_starred: bool
    exclude_sent: bool
    sweep_preserved: bool
    chat: ChatTarget | None


def _subtract_months(dt: datetime, months: int) -> datetime:
    total_months = dt.year * 12 + (dt.month - 1) - months
    year = total_months // 12
    month = total_months % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def parse_duration_to_cutoff_ms(value: str, now_ms: int) -> int:
    """Parse a duration ('180d', '6m', '2y') or 'YYYY-MM-DD' date into epoch milliseconds."""
    text = value.strip()
    date_match = _DATE_RE.match(text)
    if date_match:
        year, month, day = (int(g) for g in date_match.groups())
        dt = datetime(year, month, day, tzinfo=UTC)
        return int(dt.timestamp() * 1000)

    dur_match = _DURATION_RE.match(text)
    if not dur_match:
        raise ValueError(
            f"Invalid time policy '{value}'. Expected format like '180d', '6m', '2y', or 'YYYY-MM-DD'."
        )

    amount = int(dur_match.group(1))
    unit = dur_match.group(2).lower()
    if amount <= 0:
        raise ValueError(f"Time policy '{value}' must be positive.")

    now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    if unit == "d":
        cutoff_dt = datetime.fromtimestamp((now_ms - amount * 86400 * 1000) / 1000.0, tz=UTC)
    elif unit == "m":
        cutoff_dt = _subtract_months(now_dt, amount)
    else:
        cutoff_dt = _subtract_months(now_dt, amount * 12)
    return int(cutoff_dt.timestamp() * 1000)


def parse_byte_size(value: str) -> int:
    """Parse a human-readable byte size string (e.g. '10GB', '20MB') into bytes."""
    match = _SIZE_RE.match(value)
    if not match:
        raise ValueError(
            f"Invalid byte size '{value}'. Expected format like '20MB', '10GB', or '500KB'."
        )
    number = float(match.group(1))
    unit = match.group(2).upper()
    size_bytes = int(number * _SIZE_MULTIPLIERS[unit])
    if size_bytes <= 0:
        raise ValueError(f"Byte size '{value}' must be greater than zero.")
    return size_bytes


def _normalize_path(raw_path: str) -> str:
    return os.path.abspath(os.path.normpath(os.path.expanduser(raw_path)))


def load_config(config_path: str) -> SweeperConfig:
    """Load and validate a wab-sweeper TOML configuration file."""
    abs_cfg = _normalize_path(config_path)
    if not os.path.isfile(abs_cfg):
        raise FileNotFoundError(f"Configuration file not found: {abs_cfg}")

    with open(abs_cfg, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general")
    if not isinstance(general, dict):
        raise ValueError("Configuration missing required [general] section.")

    archive_raw = general.get("archive")
    if not archive_raw or not isinstance(archive_raw, str):
        raise ValueError("Configuration [general].archive must be a non-empty directory path.")
    archive_dir = _normalize_path(archive_raw)

    dry_run = general.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError("Configuration [general].dry_run must be a boolean.")

    log_raw = general.get("log")
    log_path = _normalize_path(log_raw) if isinstance(log_raw, str) and log_raw.strip() else None

    device_raw = general.get("device")
    device = device_raw.strip() if isinstance(device_raw, str) and device_raw.strip() else None

    android_user_id = general.get("android_user_id", 0)
    if (
        not isinstance(android_user_id, int)
        or isinstance(android_user_id, bool)
        or android_user_id < 0
    ):
        raise ValueError("Configuration [general].android_user_id must be a non-negative integer.")

    sweep = raw.get("sweep", {})
    if not isinstance(sweep, dict):
        raise ValueError("Configuration [sweep] must be a table.")

    older_than = sweep.get("older_than")
    keep_last = sweep.get("keep_last")
    if older_than is not None and keep_last is not None:
        raise ValueError(
            "Configuration [sweep].older_than and [sweep].keep_last are mutually exclusive."
        )

    # Validate time policy syntax eagerly
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if older_than is not None:
        if not isinstance(older_than, str):
            raise ValueError("Configuration [sweep].older_than must be a string.")
        parse_duration_to_cutoff_ms(older_than, now_ms)
    if keep_last is not None:
        if not isinstance(keep_last, str):
            raise ValueError("Configuration [sweep].keep_last must be a string.")
        parse_duration_to_cutoff_ms(keep_last, now_ms)

    target_free_raw = sweep.get("target_free")
    target_free_bytes = (
        parse_byte_size(target_free_raw) if isinstance(target_free_raw, str) else None
    )
    if target_free_raw is not None and not isinstance(target_free_raw, str):
        raise ValueError("Configuration [sweep].target_free must be a size string like '10GB'.")

    sort_by = sweep.get("sort_by", "oldest")
    if sort_by not in ("oldest", "largest"):
        raise ValueError("Configuration [sweep].sort_by must be 'oldest' or 'largest'.")

    min_size_raw = sweep.get("min_file_size")
    min_file_size_bytes = parse_byte_size(min_size_raw) if isinstance(min_size_raw, str) else None
    if min_size_raw is not None and not isinstance(min_size_raw, str):
        raise ValueError("Configuration [sweep].min_file_size must be a size string like '20MB'.")

    include_raw = sweep.get("include_types", list(DEFAULT_INCLUDE_TYPES))
    if not isinstance(include_raw, list) or not all(isinstance(t, str) for t in include_raw):
        raise ValueError(
            "Configuration [sweep].include_types must be a list of media type strings."
        )
    invalid_inc = set(include_raw) - VALID_MEDIA_TYPES
    if invalid_inc:
        raise ValueError(
            f"Unknown media type(s) in include_types: {sorted(invalid_inc)}. "
            f"Valid types: {sorted(VALID_MEDIA_TYPES)}"
        )

    exclude_raw = sweep.get("exclude_types", [])
    if not isinstance(exclude_raw, list) or not all(isinstance(t, str) for t in exclude_raw):
        raise ValueError(
            "Configuration [sweep].exclude_types must be a list of media type strings."
        )
    invalid_exc = set(exclude_raw) - VALID_MEDIA_TYPES
    if invalid_exc:
        raise ValueError(
            f"Unknown media type(s) in exclude_types: {sorted(invalid_exc)}. "
            f"Valid types: {sorted(VALID_MEDIA_TYPES)}"
        )

    include_types = frozenset(include_raw) - frozenset(exclude_raw)

    protect_starred = sweep.get("protect_starred", True)
    exclude_sent = sweep.get("exclude_sent", False)
    sweep_preserved = sweep.get("sweep_preserved", False)
    for flag_name, flag_val in (
        ("protect_starred", protect_starred),
        ("exclude_sent", exclude_sent),
        ("sweep_preserved", sweep_preserved),
    ):
        if not isinstance(flag_val, bool):
            raise ValueError(f"Configuration [sweep].{flag_name} must be a boolean.")

    chat_target: ChatTarget | None = None
    chat_raw = sweep.get("chat")
    if chat_raw is not None:
        if not isinstance(chat_raw, dict):
            raise ValueError("Configuration [sweep.chat] must be a single table (max 1 chat).")
        chat_id = chat_raw.get("id")
        chat_name = chat_raw.get("name")
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ValueError("Configuration [sweep.chat].id must be a non-empty JID string.")
        if not isinstance(chat_name, str) or not chat_name.strip():
            raise ValueError("Configuration [sweep.chat].name must be a non-empty display name.")
        sweep_shared = chat_raw.get("sweep_shared", False)
        if not isinstance(sweep_shared, bool):
            raise ValueError("Configuration [sweep.chat].sweep_shared must be a boolean.")
        chat_target = ChatTarget(
            jid=chat_id.strip(), name=chat_name.strip(), sweep_shared=sweep_shared
        )

    return SweeperConfig(
        archive_dir=archive_dir,
        dry_run=dry_run,
        log_path=log_path,
        device=device,
        android_user_id=android_user_id,
        older_than=older_than,
        keep_last=keep_last,
        target_free_bytes=target_free_bytes,
        sort_by=sort_by,
        min_file_size_bytes=min_file_size_bytes,
        include_types=include_types,
        protect_starred=protect_starred,
        exclude_sent=exclude_sent,
        sweep_preserved=sweep_preserved,
        chat=chat_target,
    )


def render_default_config() -> str:
    """Return the default annotated config.toml template for 'wab-sweeper init-config'."""
    return """# =============================================================================
# wab-sweeper configuration file (config.toml)
# =============================================================================

[general]
archive = "C:/WhatsApp_Archive"   # Path to archive containing .wa_media_archiver.db & msgstore.db
dry_run = true                    # Always defaults to true for safety
log = "wab_sweeper.log"
# device = "emulator-5554"        # Optional: target specific serial if multiple connected
android_user_id = 0               # 0 for primary user, 999 for Samsung Dual Messenger / Dual Apps

[sweep]
# Time policies (mutually exclusive)
older_than = "2y"                 # Formats: 180d, 6m, 2y, or YYYY-MM-DD
# keep_last = "1y"

# Size & Quota policies
# target_free = "10GB"            # Sweeps eligible files until 10GB reclaimed
sort_by = "oldest"                # "oldest" (default) or "largest" when target_free is set
# min_file_size = "20MB"          # Only sweep bulky files (e.g., large videos)

# Category filters (voice_note included by default)
include_types = ["image", "video", "audio", "voice_note", "document"]
# exclude_types = ["voice_note"]  # Uncomment to protect voice recordings

# Safety whitelisting
protect_starred = true            # NEVER sweep starred media
exclude_sent = false              # Set to true to keep all outbound media
sweep_preserved = false           # Keep forensic/revoked messages safe (Default: false)

# Targeted chat (OPTIONAL — omit to sweep globally across all chats; max 1 chat if set)
# [sweep.chat]
# id = "491701234567@s.whatsapp.net"
# name = "Anna"
# sweep_shared = false            # Set to true to override cross-chat protection
"""
