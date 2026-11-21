"""Shared fixtures and test helpers for preservation test suites."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _make_dummy_archive_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number       TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_row_id  TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            subject      TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path TEXT PRIMARY KEY,
            archive_path  TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
