"""CSV audit report writers for wab-sweeper."""

from __future__ import annotations

import csv
import os
from typing import Any


def write_swept_media_report(
    output_dir: str, timestamp_tag: str, rows: list[dict[str, Any]]
) -> str | None:
    """Write swept_media_report_<timestamp>.csv and return its path, or None if empty."""
    if not rows:
        return None
    report_path = os.path.join(output_dir, f"swept_media_report_{timestamp_tag}.csv")
    fieldnames = [
        "remote_path",
        "archive_path",
        "file_size_bytes",
        "file_hash",
        "chat_jid",
        "message_timestamp",
        "shared_chats",
    ]
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "remote_path": row["remote_path"],
                    "archive_path": row["archive_path"],
                    "file_size_bytes": row["file_size_bytes"],
                    "file_hash": row["file_hash"],
                    "chat_jid": row["chat_jid"],
                    "message_timestamp": row["message_timestamp"],
                    "shared_chats": row["shared_chats"],
                }
            )
    return report_path


def write_unprotected_skipped_report(
    output_dir: str, timestamp_tag: str, rows: list[dict[str, Any]]
) -> str | None:
    """Write unprotected_skipped_report_<timestamp>.csv and return its path, or None if empty."""
    if not rows:
        return None
    report_path = os.path.join(output_dir, f"unprotected_skipped_report_{timestamp_tag}.csv")
    fieldnames = ["remote_path", "reason", "file_size_bytes", "details"]
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "remote_path": row["remote_path"],
                    "reason": row["reason"],
                    "file_size_bytes": row["file_size_bytes"],
                    "details": row["details"],
                }
            )
    return report_path


def write_restore_conflicts_report(
    output_dir: str, timestamp_tag: str, rows: list[dict[str, Any]]
) -> str | None:
    """Write restore_conflicts_<timestamp>.csv and return its path, or None if empty."""
    if not rows:
        return None
    report_path = os.path.join(output_dir, f"restore_conflicts_{timestamp_tag}.csv")
    fieldnames = ["remote_path", "archive_path", "action_taken", "backup_path"]
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "remote_path": row["remote_path"],
                    "archive_path": row["archive_path"],
                    "action_taken": row["action_taken"],
                    "backup_path": row["backup_path"],
                }
            )
    return report_path
