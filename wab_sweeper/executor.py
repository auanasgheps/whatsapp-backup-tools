"""Batched ADB sweep deletion, precision restore, and idempotent undo executor."""

from __future__ import annotations

import logging
import os
import posixpath
import uuid
from datetime import UTC, datetime
from typing import Any

from shared import adb, hashing

from .config import SweeperConfig
from .planner import (
    PROTECTED_MEDIA_ROOT_FOLDERS,
    PROTECTED_SYSTEM_FILENAMES,
    SweepPlan,
    build_remote_media_root,
    detect_business_and_serial,
    is_protected_system_path,
)
from .reports import (
    write_restore_conflicts_report,
    write_swept_media_report,
    write_unprotected_skipped_report,
)
from .sweeper_db import (
    create_restore_batch,
    create_sweep_batch,
    get_last_undoable_sweep_id,
    get_latest_incomplete_restore_id,
    get_latest_incomplete_sweep_id,
    get_pending_restore_entries,
    get_staged_sweep_entries,
    get_undo_candidates,
    mark_restore_entry_status,
    mark_sweep_entries_status,
    mark_sweep_entries_undone,
    open_readonly_sqlite,
    open_sweeper_db,
)

_DELETE_BATCH_SIZE = 250
_STORAGE_SAFETY_BUFFER_BYTES = 1024**3  # 1.0 GiB


def _sh_quote(value: str) -> str:
    """POSIX single-quote a string safely for sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def _is_protected_folder(remote_dir: str, media_root: str) -> bool:
    norm_dir = remote_dir.rstrip("/")
    norm_root = media_root.rstrip("/")
    if not norm_dir or norm_dir == norm_root or len(norm_dir) <= len(norm_root):
        return True
    rel = norm_dir[len(norm_root) :].lstrip("/")
    if not rel or rel in PROTECTED_MEDIA_ROOT_FOLDERS:
        return True
    if os.path.basename(norm_dir) in PROTECTED_SYSTEM_FILENAMES:
        return True
    return False


def _prune_empty_leaf_dirs(
    device_serial: str,
    deleted_paths: list[str],
    media_root: str,
    logger: logging.Logger,
) -> None:
    """Best-effort POSIX rmdir on leaf directories of deleted files (deepest first)."""
    candidate_dirs: set[str] = set()
    for rp in deleted_paths:
        parent = posixpath.dirname(rp)
        while parent and not _is_protected_folder(parent, media_root):
            candidate_dirs.add(parent)
            parent = posixpath.dirname(parent)

    if not candidate_dirs:
        return

    sorted_dirs = sorted(candidate_dirs, key=lambda d: d.count("/"), reverse=True)
    for idx in range(0, len(sorted_dirs), _DELETE_BATCH_SIZE):
        chunk = sorted_dirs[idx : idx + _DELETE_BATCH_SIZE]
        lines = [f"rmdir -- {_sh_quote(d)} 2>/dev/null || true" for d in chunk]
        try:
            adb.run_adb_shell_script(device_serial, "\n".join(lines) + "\n", logger)
        except Exception as e:
            logger.debug(f"Leaf directory pruning warning (non-fatal): {e}")


def _verify_no_active_backup(device_serial: str, package_name: str, logger: logging.Logger) -> None:
    if adb.check_active_whatsapp_backup(device_serial, package_name, logger):
        raise RuntimeError(
            f"An active WhatsApp backup ({package_name}) is currently running on device "
            f"'{device_serial}'. Wait for the backup to finish before mutating media."
        )


def execute_sweep(
    cfg: SweeperConfig,
    plan: SweepPlan,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Execute Phase 2 batched deletion with resumable state and CSV audit reports."""
    ts_tag = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    _verify_no_active_backup(plan.device_serial, plan.package_name, logger)

    conn = open_sweeper_db(cfg.archive_dir)
    deleted_paths: list[str] = []
    failed_paths: list[str] = []
    sweep_id = ""

    try:
        # Resume incomplete sweep if staged rows exist for this device, else create a new batch
        existing_id = get_latest_incomplete_sweep_id(conn, plan.device_serial)
        if existing_id:
            sweep_id = existing_id
            logger.info(f"Resuming interrupted sweep batch '{sweep_id}'...")
            staged_rows = get_staged_sweep_entries(conn, sweep_id, plan.device_serial)
            remote_set = {c.remote_path for c in plan.candidates}
            reconciled_deleted = [
                r["remote_path"]
                for r in staged_rows
                if r["remote_path"] not in plan.remote_paths_on_device
            ]
            reconciled_failed = [
                r["remote_path"]
                for r in staged_rows
                if r["remote_path"] in plan.remote_paths_on_device
                and r["remote_path"] not in remote_set
            ]
            now_iso = datetime.now(UTC).isoformat()
            mark_sweep_entries_status(
                conn, sweep_id, plan.device_serial, reconciled_deleted, "deleted", now_iso
            )
            mark_sweep_entries_status(
                conn, sweep_id, plan.device_serial, reconciled_failed, "failed", now_iso
            )
            to_delete = [r["remote_path"] for r in staged_rows if r["remote_path"] in remote_set]
            # Also stage any new candidates not yet in this sweep_id
            staged_paths = {r["remote_path"] for r in staged_rows}
            new_entries = [
                {
                    "remote_path": c.remote_path,
                    "archive_path": c.archive_path,
                    "chat_jid": c.chat_jid,
                    "size_bytes": c.size_bytes,
                }
                for c in plan.candidates
                if c.remote_path not in staged_paths
            ]
            if new_entries:
                create_sweep_batch(
                    conn,
                    sweep_id,
                    plan.device_serial,
                    new_entries,
                    datetime.now(UTC).isoformat(),
                )
                to_delete.extend(e["remote_path"] for e in new_entries)
        else:
            sweep_id = f"sweep_{ts_tag}_{uuid.uuid4().hex[:8]}"
            entries = [
                {
                    "remote_path": c.remote_path,
                    "archive_path": c.archive_path,
                    "chat_jid": c.chat_jid,
                    "size_bytes": c.size_bytes,
                }
                for c in plan.candidates
            ]
            create_sweep_batch(
                conn, sweep_id, plan.device_serial, entries, datetime.now(UTC).isoformat()
            )
            to_delete = [c.remote_path for c in plan.candidates]

        # Defense-in-depth: filter out any protected system path
        safe_targets = [rp for rp in to_delete if not is_protected_system_path(rp)]

        adb.set_usb_wake_lock(plan.device_serial, True, logger)
        try:
            for idx in range(0, len(safe_targets), _DELETE_BATCH_SIZE):
                chunk = safe_targets[idx : idx + _DELETE_BATCH_SIZE]
                script_lines = []
                for rp in chunk:
                    q = _sh_quote(rp)
                    script_lines.append(
                        f"p={q}\n"
                        f'if [ ! -e "$p" ]; then\n'
                        f"  printf 'OK\\t%s\\n' \"$p\"\n"
                        f"else\n"
                        f'  rm -- "$p" 2>/dev/null\n'
                        f'  if [ ! -e "$p" ]; then\n'
                        f"    printf 'OK\\t%s\\n' \"$p\"\n"
                        f"  else\n"
                        f"    printf 'FAIL\\t%s\\n' \"$p\"\n"
                        f"  fi\n"
                        f"fi"
                    )
                result = adb.run_adb_shell_script(
                    plan.device_serial, "\n".join(script_lines) + "\n", logger
                )
                stdout = result.stdout.decode(errors="replace")
                chunk_ok: list[str] = []
                chunk_fail: list[str] = []
                for line in stdout.splitlines():
                    if line.startswith("OK\t"):
                        chunk_ok.append(line[3:])
                    elif line.startswith("FAIL\t"):
                        chunk_fail.append(line[5:])

                ok_set = set(chunk_ok)
                for rp in chunk:
                    if rp not in ok_set and rp not in chunk_fail:
                        chunk_fail.append(rp)

                now_iso = datetime.now(UTC).isoformat()
                mark_sweep_entries_status(
                    conn, sweep_id, plan.device_serial, chunk_ok, "deleted", now_iso
                )
                mark_sweep_entries_status(
                    conn, sweep_id, plan.device_serial, chunk_fail, "failed", now_iso
                )
                deleted_paths.extend(chunk_ok)
                failed_paths.extend(chunk_fail)

            _prune_empty_leaf_dirs(plan.device_serial, deleted_paths, plan.media_root, logger)
        finally:
            adb.set_usb_wake_lock(plan.device_serial, False, logger)
    finally:
        conn.close()

    deleted_set = set(deleted_paths)
    swept_rows = [
        {
            "remote_path": c.remote_path,
            "archive_path": c.archive_path,
            "file_size_bytes": c.size_bytes,
            "file_hash": c.file_hash_hex,
            "chat_jid": c.chat_jid,
            "message_timestamp": c.message_timestamp_ms,
            "shared_chats": ";".join(c.shared_chats),
        }
        for c in plan.candidates
        if c.remote_path in deleted_set
    ]
    skipped_rows = [
        {
            "remote_path": s.remote_path,
            "reason": s.reason,
            "file_size_bytes": s.file_size_bytes,
            "details": s.details,
        }
        for s in plan.skipped
    ]

    swept_report = write_swept_media_report(cfg.archive_dir, ts_tag, swept_rows)
    skipped_report = write_unprotected_skipped_report(cfg.archive_dir, ts_tag, skipped_rows)

    return {
        "sweep_id": sweep_id,
        "deleted_count": len(deleted_paths),
        "failed_count": len(failed_paths),
        "swept_report": swept_report,
        "skipped_report": skipped_report,
    }


def write_dry_run_skipped_report(archive_dir: str, plan: SweepPlan) -> str | None:
    """Write unprotected_skipped_report_<timestamp>.csv during a dry-run scan."""
    if not plan.skipped:
        return None
    ts_tag = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    skipped_rows = [
        {
            "remote_path": s.remote_path,
            "reason": s.reason,
            "file_size_bytes": s.file_size_bytes,
            "details": s.details,
        }
        for s in plan.skipped
    ]
    return write_unprotected_skipped_report(archive_dir, ts_tag, skipped_rows)


def _check_remote_quota(
    device_serial: str,
    media_root: str,
    required_bytes: int,
    logger: logging.Logger,
) -> None:
    """Verify device has required_bytes + 1.0 GiB safety buffer free before pushing."""
    probe_path = media_root
    free_kb = adb.get_remote_free_kb(device_serial, probe_path, logger)
    free_bytes = free_kb * 1024
    needed_with_buffer = required_bytes + _STORAGE_SAFETY_BUFFER_BYTES
    if free_bytes < needed_with_buffer:
        raise RuntimeError(
            f"Insufficient storage on device '{device_serial}': "
            f"available={free_bytes / (1024**3):.2f} GiB, "
            f"required={required_bytes / (1024**3):.2f} GiB + 1.00 GiB safety buffer."
        )


def _apply_permissions_and_timestamps(
    device_serial: str,
    restored_items: list[dict[str, Any]],
    logger: logging.Logger,
) -> None:
    """Batch chmod 664 and best-effort touch -d on restored files."""
    if not restored_items:
        return
    for idx in range(0, len(restored_items), _DELETE_BATCH_SIZE):
        chunk = restored_items[idx : idx + _DELETE_BATCH_SIZE]
        chmod_lines = [f"chmod 664 -- {_sh_quote(item['remote_path'])}" for item in chunk]
        adb.run_adb_shell_script(device_serial, "\n".join(chmod_lines) + "\n", logger)

        touch_lines = []
        for item in chunk:
            ts_ms = item.get("timestamp_ms")
            if ts_ms and int(ts_ms) > 0:
                iso_ts = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                touch_lines.append(
                    f"touch -d {_sh_quote(iso_ts)} -- {_sh_quote(item['remote_path'])} 2>/dev/null || true"
                )
        if touch_lines:
            try:
                adb.run_adb_shell_script(device_serial, "\n".join(touch_lines) + "\n", logger)
            except Exception as e:
                logger.debug(f"Best-effort timestamp restoration warning (non-fatal): {e}")


def _push_items_to_device(
    device_serial: str,
    media_root: str,
    archive_dir: str,
    items: list[dict[str, Any]],
    logger: logging.Logger,
    restore_conn: Any,
    restore_id: str | None,
    on_verified: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Push items to device with quota check, collision handling, chmod 664, and touch -d."""
    if not items:
        return [], []

    total_bytes = sum(int(item["size_bytes"]) for item in items)
    _check_remote_quota(device_serial, media_root, total_bytes, logger)

    md5_binary = adb.probe_md5_binary_for_device(device_serial, logger)
    remote_files = adb.enumerate_remote_files_for_device(media_root, device_serial, logger)
    remote_paths_existing = {rp for rp, _ in remote_files}

    ts_tag = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    conflicts: list[dict[str, Any]] = []
    verified_items: list[dict[str, Any]] = []

    adb.set_usb_wake_lock(device_serial, True, logger)
    try:
        for item in items:
            rp = item["remote_path"]
            abs_local = item["abs_archive_path"]
            expected_md5: bytes = item["expected_md5"]

            if not os.path.isfile(abs_local):
                if restore_conn is not None and restore_id is not None:
                    mark_restore_entry_status(
                        restore_conn,
                        restore_id,
                        device_serial,
                        rp,
                        "failed",
                        datetime.now(UTC).isoformat(),
                    )
                raise FileNotFoundError(f"Local archive file missing for restore: {abs_local}")

            if rp in remote_paths_existing:
                remote_md5_bytes = adb.remote_md5_for_device(rp, md5_binary, device_serial, logger)
                if expected_md5 and remote_md5_bytes == expected_md5:
                    # Already present and identical on device — skip silently
                    if restore_conn is not None and restore_id is not None:
                        mark_restore_entry_status(
                            restore_conn,
                            restore_id,
                            device_serial,
                            rp,
                            "verified",
                            datetime.now(UTC).isoformat(),
                        )
                    verified_items.append(item)
                    if on_verified is not None:
                        on_verified(rp)
                    continue

                # Collision: rename remote conflicting file to <filename>_conflict_<ts>.bak
                stem, ext = posixpath.splitext(rp)
                backup_path = f"{stem}_conflict_{ts_tag}.bak"
                mv_script = f"mv -- {_sh_quote(rp)} {_sh_quote(backup_path)}\n"
                adb.run_adb_shell_script(device_serial, mv_script, logger)
                conflicts.append(
                    {
                        "remote_path": rp,
                        "archive_path": item["archive_path"],
                        "action_taken": "backed_up_and_replaced",
                        "backup_path": backup_path,
                    }
                )

            parent_dir = posixpath.dirname(rp)
            adb.run_adb_shell_script(
                device_serial, f"mkdir -p -- {_sh_quote(parent_dir)}\n", logger
            )
            adb.push_file(device_serial, abs_local, rp, logger)
            if restore_conn is not None and restore_id is not None:
                mark_restore_entry_status(
                    restore_conn, restore_id, device_serial, rp, "pushed", None
                )

            verified_items.append(item)
            if restore_conn is not None and restore_id is not None:
                mark_restore_entry_status(
                    restore_conn,
                    restore_id,
                    device_serial,
                    rp,
                    "verified",
                    datetime.now(UTC).isoformat(),
                )
            if on_verified is not None:
                on_verified(rp)

        _apply_permissions_and_timestamps(device_serial, verified_items, logger)
    finally:
        adb.set_usb_wake_lock(device_serial, False, logger)

    conflict_report = write_restore_conflicts_report(archive_dir, ts_tag, conflicts)
    return [item["remote_path"] for item in verified_items], (
        [{"report": conflict_report, "count": len(conflicts)}] if conflict_report else []
    )


def execute_restore(
    cfg: SweeperConfig,
    device_serial: str,
    media_root: str,
    package_name: str,
    restore_items: list[dict[str, Any]],
    logger: logging.Logger,
) -> dict[str, Any]:
    """Execute precision restore using .wab-sweeper.db adb_restore_state for resumability."""
    _verify_no_active_backup(device_serial, package_name, logger)
    conn = open_sweeper_db(cfg.archive_dir)
    ts_tag = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    try:
        existing_id = get_latest_incomplete_restore_id(conn, device_serial)
        if existing_id:
            restore_id = existing_id
            logger.info(f"Resuming interrupted restore batch '{restore_id}'...")
            pending = get_pending_restore_entries(conn, restore_id, device_serial)
            pending_map = {r["remote_path"] for r in pending}
            items_to_push = [i for i in restore_items if i["remote_path"] in pending_map]
            new_items = [i for i in restore_items if i["remote_path"] not in pending_map]
            if new_items:
                create_restore_batch(
                    conn, restore_id, device_serial, new_items, datetime.now(UTC).isoformat()
                )
                items_to_push.extend(new_items)
        else:
            restore_id = f"restore_{ts_tag}_{uuid.uuid4().hex[:8]}"
            create_restore_batch(
                conn, restore_id, device_serial, restore_items, datetime.now(UTC).isoformat()
            )
            items_to_push = restore_items

        restored_paths, conflict_info = _push_items_to_device(
            device_serial,
            media_root,
            cfg.archive_dir,
            items_to_push,
            logger,
            conn,
            restore_id,
            None,
        )
        return {
            "restore_id": restore_id,
            "restored_count": len(restored_paths),
            "conflicts": conflict_info,
        }
    finally:
        conn.close()


def execute_undo_last(
    archive_dir: str,
    preferred_device: str | None,
    android_user_id: int,
    logger: logging.Logger,
    dry_run: bool,
) -> dict[str, Any]:
    """Idempotently reverse the most recent completed sweep_id in .wab-sweeper.db."""
    arc_conn = open_readonly_sqlite(os.path.join(archive_dir, ".wa_media_archiver.db"))
    try:
        is_business, bound_serial = detect_business_and_serial(arc_conn)
        catalog_rows = arc_conn.execute(
            """
            SELECT f.original_path, f.md5, ac.archive_path, ac.timestamp_ms
            FROM files f
            JOIN archive_copies ac ON ac.original_path = f.original_path
            """
        ).fetchall()
        md5_by_archive = {
            str(r["archive_path"]).replace("\\", "/"): (
                bytes(r["md5"]) if r["md5"] is not None else b"",
                r["timestamp_ms"],
            )
            for r in catalog_rows
        }
    finally:
        arc_conn.close()

    device_serial = adb.resolve_connected_device(preferred_device or bound_serial, logger)
    sdk_level = adb.get_android_sdk_level(device_serial, logger)
    media_root, package_name = build_remote_media_root(android_user_id, is_business, sdk_level)

    conn = open_sweeper_db(archive_dir)
    try:
        sweep_id = get_last_undoable_sweep_id(conn, device_serial)
        if not sweep_id:
            return {
                "sweep_id": None,
                "restored_count": 0,
                "candidate_count": 0,
                "conflicts": [],
            }
        candidates = get_undo_candidates(conn, sweep_id, device_serial)
        if dry_run:
            return {
                "sweep_id": sweep_id,
                "restored_count": 0,
                "candidate_count": len(candidates),
                "conflicts": [],
            }

        _verify_no_active_backup(device_serial, package_name, logger)
        items: list[dict[str, Any]] = []
        for row in candidates:
            arc_rel = str(row["archive_path"]).replace("\\", "/")
            abs_arc = os.path.join(archive_dir, *arc_rel.split("/"))
            expected_md5, ts_ms = md5_by_archive.get(arc_rel, (b"", None))
            if not expected_md5 and os.path.isfile(abs_arc):
                expected_md5 = hashing.file_md5(abs_arc)
            items.append(
                {
                    "remote_path": row["remote_path"],
                    "archive_path": arc_rel,
                    "abs_archive_path": abs_arc,
                    "size_bytes": row["size_bytes"],
                    "expected_md5": expected_md5,
                    "timestamp_ms": ts_ms,
                }
            )

        def _mark_one_undone(rp: str) -> None:
            mark_sweep_entries_undone(
                conn,
                sweep_id,
                device_serial,
                [rp],
                datetime.now(UTC).isoformat(),
            )

        restored_paths, conflict_info = _push_items_to_device(
            device_serial,
            media_root,
            archive_dir,
            items,
            logger,
            None,
            None,
            _mark_one_undone,
        )
        return {
            "sweep_id": sweep_id,
            "restored_count": len(restored_paths),
            "candidate_count": len(candidates),
            "conflicts": conflict_info,
        }
    finally:
        conn.close()
