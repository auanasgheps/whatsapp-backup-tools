"""CLI entry point, subcommand dispatch, and interactive confirmation for wab-sweeper."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from shared import adb

from .config import SweeperConfig, load_config, render_default_config
from .executor import (
    execute_restore,
    execute_sweep,
    execute_undo_last,
    write_dry_run_skipped_report,
)
from .planner import SweepPlan, list_archive_chats, plan_restore, plan_sweep


def _format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{num_bytes} B"


def _setup_logger(log_path: str | None, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("wab_sweeper")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(stream_handler)

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)

    return logger


def _print_chat_list(archive_dir: str, search: str | None) -> int:
    summaries = list_archive_chats(archive_dir, search)
    header_filter = f' (Filter: "{search}")' if search else ""
    print("=" * 80)
    print(f"MATCHING CHATS IN ARCHIVE{header_filter}")
    print("=" * 80)
    if not summaries:
        print("No matching chats with media found.")
        return 0
    for idx, item in enumerate(summaries, 1):
        kind = "Group" if item.is_group else "Contact"
        print(f"[{idx}] {item.display_name}")
        print(f"    JID         : {item.jid} ({kind})")
        print(f"    Media Count : {item.media_count:,} files")
        print(
            f"    Total Space : {_format_bytes(item.total_bytes)} "
            f"(archived: ~{_format_bytes(item.device_eligible_bytes)} eligible)"
        )
        print("    Config Snippet:")
        print("    --------------------------------------------------")
        print("    [sweep.chat]")
        print(f'    id = "{item.jid}"')
        print(f'    name = "{item.display_name}"')
        print("    --------------------------------------------------")
    return 0


def _render_sweep_summary(cfg: SweeperConfig, plan: SweepPlan, execute: bool) -> str:
    total_bytes = sum(c.size_bytes for c in plan.candidates)
    skip_bytes = sum(s.file_size_bytes for s in plan.skipped)

    cat_counts: dict[str, tuple[int, int]] = {}
    for c in plan.candidates:
        cnt, sz = cat_counts.get(c.media_type, (0, 0))
        cat_counts[c.media_type] = (cnt + 1, sz + c.size_bytes)

    reason_counts: dict[str, tuple[int, int]] = {}
    for s in plan.skipped:
        cnt, sz = reason_counts.get(s.reason, (0, 0))
        reason_counts[s.reason] = (cnt + 1, sz + s.file_size_bytes)

    scope = (
        f"Single Chat — {cfg.chat.name} ({cfg.chat.jid})"
        if cfg.chat is not None
        else "Global (All Chats)"
    )
    filters = []
    if cfg.older_than:
        filters.append(f"Older than {cfg.older_than}")
    elif cfg.keep_last:
        filters.append(f"Keep last {cfg.keep_last}")
    if cfg.protect_starred:
        filters.append("Protect Starred")
    if cfg.chat is not None:
        filters.append("Sweep Shared" if cfg.chat.sweep_shared else "Protect Shared")
    if not cfg.sweep_preserved:
        filters.append("Protect Preserved")

    mode_label = "READY TO EXECUTE" if execute else "DRY-RUN PREVIEW"
    lines = [
        "=" * 80,
        f"SWEEP EXECUTION SUMMARY ({mode_label})",
        "=" * 80,
        f"Target Scope            : {scope}",
        f"Active Filters          : {' | '.join(filters) if filters else 'Default'}",
        "-" * 80,
        f"ELIGIBLE FOR DELETION   : {len(plan.candidates):,} files ({_format_bytes(total_bytes)})",
    ]
    for cat, (cnt, sz) in sorted(cat_counts.items()):
        lines.append(f"  - {cat:<19}: {cnt:,} files ({_format_bytes(sz)})")
    lines.append("-" * 80)
    lines.append(
        f"PROTECTED / SKIPPED     : {len(plan.skipped):,} files ({_format_bytes(skip_bytes)})"
    )
    for reason, (cnt, sz) in sorted(reason_counts.items()):
        lines.append(f"  - {reason:<19}: {cnt:,} files ({_format_bytes(sz)})")
    lines.append("=" * 80)
    return "\n".join(lines)


def _confirm_sweep_execution(cfg: SweeperConfig, plan: SweepPlan, yes: bool) -> bool:
    if yes:
        return True
    total_bytes = sum(c.size_bytes for c in plan.candidates)
    required_token = (
        "CONFIRM-SHARED" if (cfg.chat is not None and cfg.chat.sweep_shared) else "CONFIRM"
    )
    if required_token == "CONFIRM-SHARED":
        shared_chats = sorted(
            {chat for c in plan.candidates for chat in c.shared_chats if chat != cfg.chat.jid}
        )
        if shared_chats:
            print(f"WARNING: Shared media also affects: {', '.join(shared_chats)}")
    prompt = (
        f"Type '{required_token}' to delete {len(plan.candidates):,} files "
        f"({_format_bytes(total_bytes)}) from device (or Ctrl+C to abort): "
    )
    try:
        entered = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return entered == required_token


def _apply_cli_overrides(cfg: SweeperConfig, args: argparse.Namespace) -> SweeperConfig:
    device = args.device if getattr(args, "device", None) else cfg.device
    sweep_preserved = cfg.sweep_preserved or bool(getattr(args, "sweep_preserved", False))
    chat = cfg.chat
    if chat is not None and getattr(args, "sweep_shared", False):
        from dataclasses import replace

        chat = replace(chat, sweep_shared=True)
    from dataclasses import replace

    return replace(cfg, device=device, sweep_preserved=sweep_preserved, chat=chat)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wab-sweeper",
        description="Android WhatsApp Media Management & Precision Restitution CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-chats", help="Discover chats in the archive")
    p_list.add_argument("--archive", required=True, help="Path to archive directory")
    p_list.add_argument("--search", default=None, help="Case-insensitive substring filter")

    p_init = sub.add_parser("init-config", help="Write a default config.toml template")
    p_init.add_argument("--output", default="config.toml", help="Destination TOML file path")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config file")

    p_sweep = sub.add_parser("sweep", help="Scan and optionally delete archived media from device")
    p_sweep.add_argument("--config", required=True, help="Path to config.toml")
    p_sweep.add_argument(
        "--execute", action="store_true", help="Actually delete files (default: dry-run)"
    )
    p_sweep.add_argument("--yes", "-y", action="store_true", help="Skip interactive confirmation")
    p_sweep.add_argument("--device", default=None, help="Override target ADB device serial")
    p_sweep.add_argument("--sweep-shared", action="store_true", help="Allow sweeping shared media")
    p_sweep.add_argument(
        "--sweep-preserved", action="store_true", help="Allow sweeping preserved media"
    )
    p_sweep.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    p_restore = sub.add_parser("restore", help="Push archived media back to device")
    p_restore.add_argument("--config", required=True, help="Path to config.toml")
    p_restore.add_argument(
        "--execute", action="store_true", help="Actually push files (default: dry-run)"
    )
    p_restore.add_argument("--yes", "-y", action="store_true", help="Skip interactive confirmation")
    p_restore.add_argument("--device", default=None, help="Override target ADB device serial")
    p_restore.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    p_undo = sub.add_parser("undo", help="Rollback the most recent sweep run")
    p_undo.add_argument(
        "--last", action="store_true", required=True, help="Undo the most recent sweep"
    )
    p_undo.add_argument("--archive", default=".", help="Path to archive directory")
    p_undo.add_argument("--config", default=None, help="Optional config.toml to resolve archive")
    p_undo.add_argument(
        "--execute", action="store_true", help="Actually restore files (default: dry-run)"
    )
    p_undo.add_argument("--device", default=None, help="Target ADB device serial")
    p_undo.add_argument(
        "--android-user-id", type=int, default=0, help="Android user ID (default: 0)"
    )
    p_undo.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    return parser


def run_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-chats":
        return _print_chat_list(os.path.abspath(args.archive), args.search)

    if args.command == "init-config":
        out_path = os.path.abspath(args.output)
        if os.path.exists(out_path) and not args.force:
            print(
                f"ERROR: '{out_path}' already exists. Pass --force to overwrite.", file=sys.stderr
            )
            return 1
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_default_config())
        print(f"Wrote default configuration to {out_path}")
        return 0

    if args.command == "sweep":
        cfg = _apply_cli_overrides(load_config(args.config), args)
        logger = _setup_logger(cfg.log_path, args.verbose)
        if not adb.check_adb(logger):
            return 1
        plan = plan_sweep(cfg, logger)
        do_execute = bool(args.execute)
        print(_render_sweep_summary(cfg, plan, do_execute))

        if not do_execute:
            write_dry_run_skipped_report(cfg.archive_dir, plan)
            print("Dry-run complete. Re-run with '--execute' to delete eligible files.")
            return 0

        if not plan.candidates:
            write_dry_run_skipped_report(cfg.archive_dir, plan)
            print("No eligible files to sweep.")
            return 0

        if not _confirm_sweep_execution(cfg, plan, args.yes):
            print("Sweep aborted by user. Zero device mutations performed.")
            return 1

        result = execute_sweep(cfg, plan, logger)
        print(
            f"Sweep '{result['sweep_id']}' complete: "
            f"{result['deleted_count']:,} deleted, {result['failed_count']:,} failed."
        )
        return 0 if result["failed_count"] == 0 else 1

    if args.command == "restore":
        cfg = _apply_cli_overrides(load_config(args.config), args)
        logger = _setup_logger(cfg.log_path, args.verbose)
        if not adb.check_adb(logger):
            return 1
        device_serial, media_root, package_name, items = plan_restore(cfg, logger)
        total_bytes = sum(int(i["size_bytes"]) for i in items)
        print(
            f"Restore plan for device '{device_serial}': "
            f"{len(items):,} file(s) ({_format_bytes(total_bytes)}) missing on device."
        )
        if not args.execute:
            print("Dry-run complete. Re-run with '--execute' to push files to device.")
            return 0
        if not items:
            print("All matching archived files are already present on device.")
            return 0
        result = execute_restore(cfg, device_serial, media_root, package_name, items, logger)
        print(f"Restore '{result['restore_id']}' complete: {result['restored_count']:,} restored.")
        return 0

    if args.command == "undo":
        archive_dir = os.path.abspath(args.archive)
        log_path = None
        android_user_id = args.android_user_id
        preferred_device = args.device
        if args.config:
            cfg = load_config(args.config)
            archive_dir = cfg.archive_dir
            log_path = cfg.log_path
            android_user_id = cfg.android_user_id
            preferred_device = preferred_device or cfg.device
        logger = _setup_logger(log_path, args.verbose)
        if not adb.check_adb(logger):
            return 1
        result = execute_undo_last(
            archive_dir,
            preferred_device,
            android_user_id,
            logger,
            not args.execute,
        )
        if not result["sweep_id"]:
            print("No undoable sweep found in .wab-sweeper.db.")
            return 0
        if not args.execute:
            print(
                f"[DRY-RUN] Would undo sweep '{result['sweep_id']}': "
                f"{result['candidate_count']:,} file(s) to restore."
            )
            return 0
        print(
            f"Undo of sweep '{result['sweep_id']}' complete: "
            f"{result['restored_count']:,} file(s) restored."
        )
        return 0

    return 1


def main() -> int:
    """Console entry point for wab-sweeper."""
    return run_cli(sys.argv[1:])
