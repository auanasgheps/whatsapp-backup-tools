"""Read-only archive/msgstore planner, Safety Handshake, and 4-Gate Integrity Pipeline."""

from __future__ import annotations

import logging
import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shared import adb

from .config import SweeperConfig, parse_duration_to_cutoff_ms
from .sweeper_db import open_readonly_sqlite

PROTECTED_SYSTEM_FILENAMES = frozenset({".nomedia", ".thumbs", ".Shared"})

PROTECTED_MEDIA_ROOT_FOLDERS = frozenset(
    {
        "WhatsApp Images",
        "WhatsApp Video",
        "WhatsApp Audio",
        "WhatsApp Voice Notes",
        "WhatsApp Video Notes",
        "WhatsApp Animated Gifs",
        "WhatsApp Documents",
        "WhatsApp Stickers",
    }
)


@dataclass(frozen=True)
class ChatSummary:
    """Resolved chat metadata for discovery and Safety Handshake validation."""

    jid: str
    display_name: str
    is_group: bool
    media_count: int
    total_bytes: int
    device_eligible_bytes: int


@dataclass(frozen=True)
class SweepCandidate:
    """A file that passed all 4 integrity gates and policy filters."""

    remote_path: str
    original_path: str
    archive_path: str
    abs_archive_path: str
    size_bytes: int
    file_hash_hex: str
    chat_jid: str
    message_timestamp_ms: int
    media_type: str
    shared_chats: list[str]


@dataclass(frozen=True)
class SkippedItem:
    """A file skipped by a safety gate or policy filter."""

    remote_path: str
    reason: str
    file_size_bytes: int
    details: str


@dataclass(frozen=True)
class SweepPlan:
    """Complete Phase 1 dry-run plan ready for summary display or Phase 2 execution."""

    device_serial: str
    media_root: str
    package_name: str
    remote_paths_on_device: frozenset[str]
    candidates: list[SweepCandidate]
    skipped: list[SkippedItem]
    archive_latest_ts_ms: int | None
    stale_archive_days: float | None


def classify_sweeper_media_type(
    file_path: str, message_type: int | None, mime_type: str | None
) -> str:
    """Classify a WhatsApp media file into one of the 7 sweeper categories."""
    norm = file_path.lower().replace("\\", "/")
    if "/whatsapp voice notes/" in norm or norm.startswith("media/whatsapp voice notes/"):
        return "voice_note"
    if "/whatsapp video notes/" in norm or norm.startswith("media/whatsapp video notes/"):
        return "video_note"
    if "/whatsapp stickers/" in norm or norm.startswith("media/whatsapp stickers/"):
        return "sticker"
    if "/whatsapp audio/" in norm or norm.startswith("media/whatsapp audio/"):
        return "audio"
    if "/whatsapp documents/" in norm or norm.startswith("media/whatsapp documents/"):
        return "document"
    if (
        "/whatsapp video/" in norm
        or norm.startswith("media/whatsapp video/")
        or "/whatsapp animated gifs/" in norm
        or norm.startswith("media/whatsapp animated gifs/")
    ):
        return "video"
    if "/whatsapp images/" in norm or norm.startswith("media/whatsapp images/"):
        return "image"

    if message_type == 2:
        return "voice_note"
    if message_type in (28, 81):
        return "video_note"
    if message_type == 20:
        return "sticker"
    if message_type == 9:
        return "document"
    if message_type in (3, 13, 43):
        return "video"
    if message_type in (1, 42):
        return "image"

    if mime_type:
        low_mime = mime_type.lower()
        if low_mime.startswith("image/"):
            return "image"
        if low_mime.startswith("video/"):
            return "video"
        if low_mime.startswith("audio/"):
            return "audio"
    return "document"


def is_protected_system_path(remote_path: str) -> bool:
    """Return True if remote_path is an immutable WhatsApp system file or status folder."""
    norm = remote_path.replace("\\", "/")
    basename = os.path.basename(norm)
    if basename in PROTECTED_SYSTEM_FILENAMES:
        return True
    if "/.Statuses/" in norm or "/.Shared/" in norm or "/.thumbs/" in norm:
        return True
    return False


def to_original_db_path(remote_path: str) -> str | None:
    """Convert an on-device absolute path to the 'Media/...' relative key used in DBs."""
    norm = remote_path.replace("\\", "/")
    marker = "/Media/"
    idx = norm.find(marker)
    if idx != -1:
        return "Media/" + norm[idx + len(marker) :]
    if norm.startswith("Media/"):
        return norm
    return None


def detect_business_and_serial(
    arc_conn: sqlite3.Connection,
) -> tuple[bool, str | None]:
    """Inspect .wa_media_archiver.db for WhatsApp Business indicators and bound device serial."""
    tables = {
        row[0] for row in arc_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    is_business = False
    bound_serial: str | None = None

    if "adb_pull_state" in tables:
        biz_row = arc_conn.execute(
            "SELECT 1 FROM adb_pull_state "
            "WHERE remote_path LIKE '%com.whatsapp.w4b%' "
            "OR remote_path LIKE '%WhatsApp Business%' LIMIT 1"
        ).fetchone()
        if biz_row is not None:
            is_business = True

        serial_rows = arc_conn.execute(
            "SELECT DISTINCT device_serial FROM adb_pull_state "
            "WHERE device_serial IS NOT NULL AND device_serial != ''"
        ).fetchall()
        if len(serial_rows) == 1:
            bound_serial = str(serial_rows[0]["device_serial"])

    if not is_business and "files" in tables:
        row = arc_conn.execute(
            "SELECT 1 FROM files WHERE original_path LIKE '%WhatsApp Business%' LIMIT 1"
        ).fetchone()
        if row is not None:
            is_business = True

    return is_business, bound_serial


def build_remote_media_root(
    android_user_id: int, is_business: bool, sdk_level: int | None
) -> tuple[str, str]:
    """Return (media_root, package_name) for the target Android user and WhatsApp flavor."""
    package_name = "com.whatsapp.w4b" if is_business else "com.whatsapp"
    wa_folder = "WhatsApp Business" if is_business else "WhatsApp"
    if sdk_level is not None and sdk_level <= 29:
        media_root = f"/storage/emulated/{android_user_id}/{wa_folder}/Media"
    else:
        media_root = (
            f"/storage/emulated/{android_user_id}/Android/media/{package_name}/{wa_folder}/Media"
        )
    return media_root, package_name


def _load_chat_identities(
    arc_conn: sqlite3.Connection, msg_conn: sqlite3.Connection
) -> dict[int, tuple[str, str, bool]]:
    """Map msgstore chat._id -> (canonical_jid, display_name, is_group)."""
    contacts_map: dict[str, str] = {}
    groups_map: dict[str, str] = {}

    arc_tables = {
        row[0] for row in arc_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "contacts" in arc_tables:
        for row in arc_conn.execute("SELECT number, display_name FROM contacts"):
            if row["display_name"]:
                contacts_map[str(row["number"])] = str(row["display_name"])
    if "groups" in arc_tables:
        for row in arc_conn.execute("SELECT chat_row_id, subject FROM groups"):
            if row["subject"]:
                groups_map[str(row["chat_row_id"])] = str(row["subject"])

    # Resolve LID -> phone JID from msgstore jid_map
    lid_to_jid: dict[int, tuple[str, str, str]] = {}
    msg_tables = {
        row[0] for row in msg_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "jid_map" in msg_tables:
        for row in msg_conn.execute(
            """
            SELECT jm.lid_row_id, j.user, j.server, j.raw_string
            FROM jid_map jm
            JOIN jid j ON j._id = jm.jid_row_id
            """
        ):
            user = str(row["user"] or "")
            server = str(row["server"] or "")
            raw = str(row["raw_string"] or f"{user}@{server}")
            lid_to_jid[int(row["lid_row_id"])] = (user, server, raw)

    # Number change consolidation (old -> current)
    number_map: dict[str, str] = {}
    if "message_system_number_change" in msg_tables:
        for row in msg_conn.execute(
            """
            SELECT j_old.user AS old_user, j_new.user AS new_user
            FROM message_system_number_change mnc
            JOIN jid j_old ON j_old._id = mnc.old_jid_row_id
            JOIN jid j_new ON j_new._id = mnc.new_jid_row_id
            WHERE j_old.user IS NOT NULL AND j_new.user IS NOT NULL
            """
        ):
            number_map[str(row["old_user"])] = str(row["new_user"])

    identities: dict[int, tuple[str, str, bool]] = {}
    for row in msg_conn.execute(
        """
        SELECT c._id AS chat_id, c.jid_row_id, c.subject, j.user, j.server, j.raw_string
        FROM chat c
        JOIN jid j ON j._id = c.jid_row_id
        """
    ):
        chat_id = int(row["chat_id"])
        jid_row_id = int(row["jid_row_id"])
        user = str(row["user"] or "")
        server = str(row["server"] or "")
        raw_jid = str(row["raw_string"] or f"{user}@{server}")

        if server == "lid" and jid_row_id in lid_to_jid:
            user, server, raw_jid = lid_to_jid[jid_row_id]

        is_group = server == "g.us"
        if is_group:
            subj = row["subject"]
            display = str(subj).strip() if subj else groups_map.get(str(chat_id), "")
            if not display:
                display = f"Group ({user})"
        else:
            canonical_user = number_map.get(user, user)
            display = contacts_map.get(canonical_user) or contacts_map.get(user) or ""
            if not display and row["subject"]:
                display = str(row["subject"]).strip()
            if not display:
                display = user or raw_jid

        identities[chat_id] = (raw_jid, display, is_group)

    return identities


def list_archive_chats(archive_dir: str, search: str | None) -> list[ChatSummary]:
    """List chats in the archive with media counts and sizes, optionally filtered by search."""
    arc_conn = open_readonly_sqlite(os.path.join(archive_dir, ".wa_media_archiver.db"))
    msg_conn = open_readonly_sqlite(os.path.join(archive_dir, "msgstore.db"))
    try:
        identities = _load_chat_identities(arc_conn, msg_conn)

        archived_sizes: dict[str, int] = {}
        for row in arc_conn.execute("SELECT original_path, size FROM files"):
            if row["size"] is not None:
                archived_sizes[str(row["original_path"])] = int(row["size"])

        chat_files: dict[str, set[str]] = {}
        for row in msg_conn.execute(
            """
            SELECT mm.chat_row_id, mm.file_path
            FROM message_media mm
            WHERE mm.file_path IS NOT NULL AND mm.file_path LIKE 'Media/WhatsApp %'
            """
        ):
            cid = int(row["chat_row_id"])
            if cid not in identities:
                continue
            canonical_jid = identities[cid][0]
            chat_files.setdefault(canonical_jid, set()).add(str(row["file_path"]))

        # Deduplicate by canonical_jid
        jid_meta: dict[str, tuple[str, bool]] = {}
        for cid, (cjid, cname, is_grp) in identities.items():
            if cjid not in jid_meta:
                jid_meta[cjid] = (cname, is_grp)

        summaries: list[ChatSummary] = []
        needle = unicodedata.normalize("NFC", search.strip()).casefold() if search else None
        for cjid, (cname, is_grp) in jid_meta.items():
            files = chat_files.get(cjid, set())
            if not files:
                continue
            if needle:
                name_norm = unicodedata.normalize("NFC", cname).casefold()
                jid_norm = cjid.casefold()
                if needle not in name_norm and needle not in jid_norm:
                    continue
            total_bytes = sum(archived_sizes.get(fp, 0) for fp in files)
            eligible_bytes = sum(archived_sizes.get(fp, 0) for fp in files if fp in archived_sizes)
            summaries.append(
                ChatSummary(
                    jid=cjid,
                    display_name=cname,
                    is_group=is_grp,
                    media_count=len(files),
                    total_bytes=total_bytes,
                    device_eligible_bytes=eligible_bytes,
                )
            )

        summaries.sort(key=lambda s: (-s.total_bytes, s.display_name.casefold()))
        return summaries
    finally:
        arc_conn.close()
        msg_conn.close()


def validate_safety_handshake(
    cfg: SweeperConfig,
    arc_conn: sqlite3.Connection,
    msg_conn: sqlite3.Connection,
) -> dict[int, tuple[str, str, bool]]:
    """Verify [sweep.chat].id maps to [sweep.chat].name using Unicode NFC normalization."""
    identities = _load_chat_identities(arc_conn, msg_conn)
    if cfg.chat is None:
        return identities

    matching = [(cjid, cname) for cjid, cname, _ in identities.values() if cjid == cfg.chat.jid]
    if not matching:
        raise ValueError(
            "[SAFETY HANDSHAKE FAILED]\n"
            f"Config specifies JID '{cfg.chat.jid}' with name '{cfg.chat.name}'.\n"
            "However, this JID does not exist in the archive.\n"
            "Sweep aborted to protect data."
        )

    resolved_name = matching[0][1]
    expected_nfc = unicodedata.normalize("NFC", cfg.chat.name)
    resolved_nfc = unicodedata.normalize("NFC", resolved_name)
    if expected_nfc != resolved_nfc:
        raise ValueError(
            "[SAFETY HANDSHAKE FAILED]\n"
            f"Config specifies JID '{cfg.chat.jid}' with name '{cfg.chat.name}'.\n"
            f"However, archive maps this JID to '{resolved_name}'.\n"
            "Sweep aborted to protect data."
        )
    return identities


def _load_preserved_paths(archive_dir: str) -> set[str]:
    """Load original_path and archive_path strings from preserved_messages.db if present."""
    pres_path = os.path.join(archive_dir, "preserved_messages.db")
    if not os.path.isfile(pres_path):
        return set()
    conn = open_readonly_sqlite(pres_path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "preserved_messages" not in tables:
            return set()
        protected: set[str] = set()
        for row in conn.execute(
            "SELECT original_path, archive_path FROM preserved_messages "
            "WHERE original_path IS NOT NULL OR archive_path IS NOT NULL"
        ):
            if row["original_path"]:
                protected.add(str(row["original_path"]).replace("\\", "/"))
            if row["archive_path"]:
                protected.add(str(row["archive_path"]).replace("\\", "/"))
        return protected
    finally:
        conn.close()


def _load_archive_catalog(
    arc_conn: sqlite3.Connection, archive_dir: str
) -> dict[str, dict[str, Any]]:
    """Load catalog of original_path -> {md5, size, copies: [(archive_path, abs_path, exif_stamped, timestamp_ms)]}."""
    catalog: dict[str, dict[str, Any]] = {}
    for row in arc_conn.execute(
        """
        SELECT f.original_path, f.md5, f.size,
               ac.archive_path, ac.exif_stamped, ac.timestamp_ms
        FROM files f
        JOIN archive_copies ac ON ac.original_path = f.original_path
        """
    ):
        orig = str(row["original_path"]).replace("\\", "/")
        entry = catalog.get(orig)
        if entry is None:
            entry = {
                "md5": bytes(row["md5"]) if row["md5"] is not None else b"",
                "size": row["size"],
                "copies": [],
            }
            catalog[orig] = entry
        arc_rel = str(row["archive_path"]).replace("\\", "/")
        abs_arc = os.path.join(archive_dir, *arc_rel.split("/"))
        entry["copies"].append(
            {
                "archive_path": arc_rel,
                "abs_path": abs_arc,
                "exif_stamped": int(row["exif_stamped"] or 0),
                "timestamp_ms": row["timestamp_ms"],
            }
        )
    return catalog


def _load_msgstore_media_refs(
    msg_conn: sqlite3.Connection, identities: dict[int, tuple[str, str, bool]]
) -> tuple[dict[str, list[dict[str, Any]]], int | None]:
    """Load all message_media references keyed by original_path, plus archive_latest_ts_ms."""
    refs: dict[str, list[dict[str, Any]]] = {}
    latest_ts: int | None = None

    row_max = msg_conn.execute("SELECT MAX(timestamp) AS max_ts FROM message").fetchone()
    if row_max and row_max["max_ts"] is not None:
        latest_ts = int(row_max["max_ts"])

    for row in msg_conn.execute(
        """
        SELECT mm.file_path, mm.mime_type, mm.chat_row_id,
               m.timestamp, m.message_type, m.from_me, m.starred
        FROM message_media mm
        JOIN message m ON m._id = mm.message_row_id
        WHERE mm.file_path IS NOT NULL
        """
    ):
        fp = str(row["file_path"]).replace("\\", "/")
        cid = int(row["chat_row_id"])
        chat_jid = identities[cid][0] if cid in identities else f"chat_{cid}"
        refs.setdefault(fp, []).append(
            {
                "chat_jid": chat_jid,
                "timestamp_ms": int(row["timestamp"] or 0),
                "message_type": row["message_type"],
                "mime_type": row["mime_type"],
                "from_me": int(row["from_me"] or 0),
                "starred": int(row["starred"] or 0),
            }
        )
    return refs, latest_ts


def plan_sweep(cfg: SweeperConfig, logger: logging.Logger) -> SweepPlan:
    """Execute Phase 1: Remote inventory, Safety Handshake, and 4-Gate Verification."""
    arc_conn = open_readonly_sqlite(os.path.join(cfg.archive_dir, ".wa_media_archiver.db"))
    msg_conn = open_readonly_sqlite(os.path.join(cfg.archive_dir, "msgstore.db"))
    try:
        identities = validate_safety_handshake(cfg, arc_conn, msg_conn)
        is_business, bound_serial = detect_business_and_serial(arc_conn)
        preferred_serial = cfg.device or bound_serial
        device_serial = adb.resolve_connected_device(preferred_serial, logger)
        sdk_level = adb.get_android_sdk_level(device_serial, logger)
        media_root, package_name = build_remote_media_root(
            cfg.android_user_id, is_business, sdk_level
        )
        md5_binary = adb.probe_md5_binary_for_device(device_serial, logger)
        remote_files = adb.enumerate_remote_files_for_device(media_root, device_serial, logger)

        catalog = _load_archive_catalog(arc_conn, cfg.archive_dir)
        refs_by_path, archive_latest_ts_ms = _load_msgstore_media_refs(msg_conn, identities)
        preserved_paths = _load_preserved_paths(cfg.archive_dir)
    finally:
        arc_conn.close()
        msg_conn.close()

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    stale_days: float | None = None
    if archive_latest_ts_ms is not None and archive_latest_ts_ms > 0:
        delta_days = (now_ms - archive_latest_ts_ms) / 86_400_000.0
        if delta_days > 7.0:
            stale_days = delta_days
            logger.warning(
                f"Archive is {delta_days:.1f} days old. Consider running 'wab-archiver' first "
                "to archive recent media."
            )

    cutoff_ms: int | None = None
    if cfg.older_than is not None:
        cutoff_ms = parse_duration_to_cutoff_ms(cfg.older_than, now_ms)
    elif cfg.keep_last is not None:
        cutoff_ms = parse_duration_to_cutoff_ms(cfg.keep_last, now_ms)

    candidates: list[SweepCandidate] = []
    skipped: list[SkippedItem] = []

    for remote_path, remote_size in remote_files:
        if is_protected_system_path(remote_path):
            continue

        orig_path = to_original_db_path(remote_path)
        if orig_path is None:
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason="not_in_archive",
                    file_size_bytes=remote_size,
                    details="Path is outside WhatsApp Media hierarchy",
                )
            )
            continue

        msg_refs = refs_by_path.get(orig_path, [])

        # Gate 1: Cataloged in Archive DB
        cat_entry = catalog.get(orig_path)
        if cat_entry is None:
            if (
                archive_latest_ts_ms is not None
                and msg_refs
                and any(r["timestamp_ms"] > archive_latest_ts_ms for r in msg_refs)
            ):
                reason = "post_archive_cutoff"
                details = "Message timestamp is newer than archive cutoff"
            else:
                reason = "not_in_archive"
                details = "File not cataloged in .wa_media_archiver.db"
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason=reason,
                    file_size_bytes=remote_size,
                    details=details,
                )
            )
            continue

        # Target chat pre-check before touching disk/ADB hash
        chat_jids = sorted({r["chat_jid"] for r in msg_refs})
        if cfg.chat is not None:
            if cfg.chat.jid not in chat_jids:
                continue

        # Gate 2: Local Physical Durability
        valid_copy = None
        for copy in cat_entry["copies"]:
            if os.path.isfile(copy["abs_path"]) and os.path.getsize(copy["abs_path"]) > 0:
                valid_copy = copy
                break
        if valid_copy is None:
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason="missing_archive_file",
                    file_size_bytes=remote_size,
                    details="Local archive file is missing or zero-byte on disk",
                )
            )
            continue

        # Policy filters: time, size, category, exclude_sent, starred, preserved, shared
        if cfg.min_file_size_bytes is not None and remote_size < cfg.min_file_size_bytes:
            continue

        sample_ref = msg_refs[0] if msg_refs else None
        media_type = classify_sweeper_media_type(
            orig_path,
            sample_ref["message_type"] if sample_ref else None,
            sample_ref["mime_type"] if sample_ref else None,
        )
        if media_type not in cfg.include_types:
            continue

        if cutoff_ms is not None:
            if msg_refs:
                max_msg_ts = max(r["timestamp_ms"] for r in msg_refs)
                if max_msg_ts >= cutoff_ms:
                    continue
            elif (
                valid_copy["timestamp_ms"] is not None
                and int(valid_copy["timestamp_ms"]) >= cutoff_ms
            ):
                continue

        if cfg.exclude_sent and msg_refs and all(r["from_me"] == 1 for r in msg_refs):
            continue

        if cfg.protect_starred and any(r["starred"] == 1 for r in msg_refs):
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason="starred_message",
                    file_size_bytes=remote_size,
                    details="Message is starred/favorited",
                )
            )
            continue

        if not cfg.sweep_preserved and (
            orig_path in preserved_paths or valid_copy["archive_path"] in preserved_paths
        ):
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason="preserved_message_protected",
                    file_size_bytes=remote_size,
                    details="Referenced by preserved_messages.db",
                )
            )
            continue

        if cfg.chat is not None:
            other_chats = [c for c in chat_jids if c != cfg.chat.jid]
            if other_chats and not cfg.chat.sweep_shared:
                skipped.append(
                    SkippedItem(
                        remote_path=remote_path,
                        reason="shared_with_other_chat",
                        file_size_bytes=remote_size,
                        details=f"Also referenced by: {', '.join(other_chats)}",
                    )
                )
                continue

        # Gate 4: Content Invariance & EXIF-Stamp Awareness
        expected_md5: bytes = cat_entry["md5"]
        if valid_copy["exif_stamped"] == 0:
            local_size = os.path.getsize(valid_copy["abs_path"])
            if local_size != remote_size:
                skipped.append(
                    SkippedItem(
                        remote_path=remote_path,
                        reason="hash_mismatch",
                        file_size_bytes=remote_size,
                        details=f"Size mismatch (remote={remote_size}, archive={local_size})",
                    )
                )
                continue

        try:
            remote_md5_bytes = adb.remote_md5_for_device(
                remote_path, md5_binary, device_serial, logger
            )
        except Exception as e:
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason="hash_mismatch",
                    file_size_bytes=remote_size,
                    details=f"Remote MD5 probe failed: {e}",
                )
            )
            continue

        if not expected_md5 or remote_md5_bytes != expected_md5:
            skipped.append(
                SkippedItem(
                    remote_path=remote_path,
                    reason="hash_mismatch",
                    file_size_bytes=remote_size,
                    details=(
                        f"MD5 mismatch (remote={remote_md5_bytes.hex()}, "
                        f"archive={expected_md5.hex()})"
                    ),
                )
            )
            continue

        primary_chat = (
            cfg.chat.jid if cfg.chat is not None else (chat_jids[0] if chat_jids else "unknown")
        )
        msg_ts = (
            min(r["timestamp_ms"] for r in msg_refs)
            if msg_refs
            else int(valid_copy["timestamp_ms"] or 0)
        )

        candidates.append(
            SweepCandidate(
                remote_path=remote_path,
                original_path=orig_path,
                archive_path=valid_copy["archive_path"],
                abs_archive_path=valid_copy["abs_path"],
                size_bytes=remote_size,
                file_hash_hex=expected_md5.hex(),
                chat_jid=primary_chat,
                message_timestamp_ms=msg_ts,
                media_type=media_type,
                shared_chats=chat_jids,
            )
        )

    # Sort and apply target_free quota if configured
    if cfg.sort_by == "largest":
        candidates.sort(key=lambda c: (-c.size_bytes, c.message_timestamp_ms))
    else:
        candidates.sort(key=lambda c: (c.message_timestamp_ms, c.remote_path))

    if cfg.target_free_bytes is not None:
        quota_selected: list[SweepCandidate] = []
        accumulated = 0
        for item in candidates:
            quota_selected.append(item)
            accumulated += item.size_bytes
            if accumulated >= cfg.target_free_bytes:
                break
        candidates = quota_selected

    return SweepPlan(
        device_serial=device_serial,
        media_root=media_root,
        package_name=package_name,
        remote_paths_on_device=frozenset(rp for rp, _ in remote_files),
        candidates=candidates,
        skipped=skipped,
        archive_latest_ts_ms=archive_latest_ts_ms,
        stale_archive_days=stale_days,
    )


def plan_restore(
    cfg: SweeperConfig, logger: logging.Logger
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """Plan a precision restore: find archived files matching config missing on device."""
    arc_conn = open_readonly_sqlite(os.path.join(cfg.archive_dir, ".wa_media_archiver.db"))
    msg_conn = open_readonly_sqlite(os.path.join(cfg.archive_dir, "msgstore.db"))
    try:
        identities = validate_safety_handshake(cfg, arc_conn, msg_conn)
        is_business, bound_serial = detect_business_and_serial(arc_conn)
        preferred_serial = cfg.device or bound_serial
        device_serial = adb.resolve_connected_device(preferred_serial, logger)
        sdk_level = adb.get_android_sdk_level(device_serial, logger)
        media_root, package_name = build_remote_media_root(
            cfg.android_user_id, is_business, sdk_level
        )
        remote_files = adb.enumerate_remote_files_for_device(media_root, device_serial, logger)
        catalog = _load_archive_catalog(arc_conn, cfg.archive_dir)
        refs_by_path, _ = _load_msgstore_media_refs(msg_conn, identities)
    finally:
        arc_conn.close()
        msg_conn.close()

    remote_by_orig: dict[str, str] = {}
    for rp, _ in remote_files:
        orig = to_original_db_path(rp)
        if orig:
            remote_by_orig[orig] = rp

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    cutoff_ms: int | None = None
    if cfg.older_than is not None:
        cutoff_ms = parse_duration_to_cutoff_ms(cfg.older_than, now_ms)
    elif cfg.keep_last is not None:
        cutoff_ms = parse_duration_to_cutoff_ms(cfg.keep_last, now_ms)

    restore_items: list[dict[str, Any]] = []
    for orig_path, cat_entry in catalog.items():
        if orig_path in remote_by_orig:
            continue

        valid_copy = None
        for copy in cat_entry["copies"]:
            if os.path.isfile(copy["abs_path"]) and os.path.getsize(copy["abs_path"]) > 0:
                valid_copy = copy
                break
        if valid_copy is None:
            continue

        msg_refs = refs_by_path.get(orig_path, [])
        chat_jids = sorted({r["chat_jid"] for r in msg_refs})
        if cfg.chat is not None and cfg.chat.jid not in chat_jids:
            continue

        sample_ref = msg_refs[0] if msg_refs else None
        media_type = classify_sweeper_media_type(
            orig_path,
            sample_ref["message_type"] if sample_ref else None,
            sample_ref["mime_type"] if sample_ref else None,
        )
        if media_type not in cfg.include_types:
            continue

        if cutoff_ms is not None:
            if msg_refs:
                max_msg_ts = max(r["timestamp_ms"] for r in msg_refs)
                if max_msg_ts >= cutoff_ms:
                    continue
            elif (
                valid_copy["timestamp_ms"] is not None
                and int(valid_copy["timestamp_ms"]) >= cutoff_ms
            ):
                continue

        if cfg.exclude_sent and msg_refs and all(r["from_me"] == 1 for r in msg_refs):
            continue

        rel_under_media = (
            orig_path[len("Media/") :] if orig_path.startswith("Media/") else orig_path
        )
        target_remote_path = f"{media_root}/{rel_under_media}"
        size_bytes = os.path.getsize(valid_copy["abs_path"])
        if cfg.min_file_size_bytes is not None and size_bytes < cfg.min_file_size_bytes:
            continue
        restore_items.append(
            {
                "remote_path": target_remote_path,
                "archive_path": valid_copy["archive_path"],
                "abs_archive_path": valid_copy["abs_path"],
                "size_bytes": size_bytes,
                "expected_md5": cat_entry["md5"],
                "timestamp_ms": valid_copy["timestamp_ms"],
            }
        )

    return device_serial, media_root, package_name, restore_items
