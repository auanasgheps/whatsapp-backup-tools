"""Unit tests for wab_sweeper.config, wab_sweeper.sweeper_db, and wab_sweeper.reports."""

from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wab_sweeper.config import (
    load_config,
    parse_byte_size,
    parse_duration_to_cutoff_ms,
)
from wab_sweeper.reports import write_unprotected_skipped_report
from wab_sweeper.sweeper_db import (
    create_restore_batch,
    create_sweep_batch,
    get_latest_incomplete_restore_id,
    get_pending_restore_entries,
    mark_restore_entry_status,
    mark_sweep_entries_status,
    mark_sweep_entries_undone,
    open_readonly_sqlite,
    open_sweeper_db,
)


def _write_toml(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestTimeAndSizeParsers:
    def test_parse_duration_date_and_units(self):
        # 2026-06-15T12:00:00Z
        now_ms = int(datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)

        assert parse_duration_to_cutoff_ms("2025-01-01", now_ms) == int(
            datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000
        )
        assert parse_duration_to_cutoff_ms("10d", now_ms) == now_ms - 10 * 86400 * 1000
        assert parse_duration_to_cutoff_ms("2m", now_ms) == int(
            datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC).timestamp() * 1000
        )
        assert parse_duration_to_cutoff_ms("1y", now_ms) == int(
            datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC).timestamp() * 1000
        )

    def test_parse_duration_month_clamp_and_errors(self):
        # 2024-03-31 minus 1 month clamps to 2024-02-29 (leap year)
        now_ms = int(datetime(2024, 3, 31, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)
        assert parse_duration_to_cutoff_ms("1m", now_ms) == int(
            datetime(2024, 2, 29, 0, 0, 0, tzinfo=UTC).timestamp() * 1000
        )

        with pytest.raises(ValueError, match="Invalid time policy"):
            parse_duration_to_cutoff_ms("2w", now_ms)
        with pytest.raises(ValueError, match="must be positive"):
            parse_duration_to_cutoff_ms("0d", now_ms)

    def test_parse_byte_size_units_and_errors(self):
        assert parse_byte_size("10B") == 10
        assert parse_byte_size("2KB") == 2 * 1024
        assert parse_byte_size("1.5MB") == int(1.5 * 1024**2)
        assert parse_byte_size("3GB") == 3 * 1024**3
        assert parse_byte_size("1TB") == 1024**4

        with pytest.raises(ValueError, match="Invalid byte size"):
            parse_byte_size("100XB")
        with pytest.raises(ValueError, match="greater than zero"):
            parse_byte_size("0MB")


class TestConfigValidation:
    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="Configuration file not found"):
            load_config(str(tmp_path / "does_not_exist.toml"))

    def test_full_valid_options(self, tmp_path: Path):
        archive = tmp_path / "arc"
        cfg_file = _write_toml(
            tmp_path / "valid.toml",
            f"""
[general]
archive = "{archive.as_posix()}"
dry_run = false
log = "sweeper.log"
device = "SIM-DEV-002"
android_user_id = 10

[sweep]
keep_last = "6m"
target_free = "5GB"
sort_by = "largest"
min_file_size = "10MB"
include_types = ["image", "video"]
exclude_types = ["image"]
protect_starred = false
exclude_sent = true
sweep_preserved = true

[sweep.chat]
id = "15550101@s.whatsapp.net"
name = "Alice"
sweep_shared = true
""",
        )
        cfg = load_config(cfg_file)
        assert cfg.dry_run is False
        assert cfg.log_path is not None
        assert cfg.device == "SIM-DEV-002"
        assert cfg.android_user_id == 10
        assert cfg.keep_last == "6m"
        assert cfg.target_free_bytes == 5 * 1024**3
        assert cfg.sort_by == "largest"
        assert cfg.min_file_size_bytes == 10 * 1024**2
        assert cfg.include_types == frozenset({"video"})
        assert cfg.protect_starred is False
        assert cfg.exclude_sent is True
        assert cfg.sweep_preserved is True
        assert cfg.chat is not None and cfg.chat.sweep_shared is True

    @pytest.mark.parametrize(
        ("toml_body", "err_match"),
        [
            ('[sweep]\nolder_than = "1y"\n', "missing required \\[general\\]"),
            ('[general]\narchive = ""\n', "must be a non-empty directory path"),
            ('[general]\narchive = "/tmp/a"\ndry_run = "yes"\n', "dry_run must be a boolean"),
            (
                '[general]\narchive = "/tmp/a"\nandroid_user_id = -1\n',
                "android_user_id must be a non-negative integer",
            ),
            (
                '[general]\narchive = "/tmp/a"\nandroid_user_id = true\n',
                "android_user_id must be a non-negative integer",
            ),
            ('sweep = "bad"\n[general]\narchive = "/tmp/a"\n', "\\[sweep\\] must be a table"),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nolder_than = "1y"\nkeep_last = "6m"\n',
                "mutually exclusive",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nolder_than = 123\n',
                "older_than must be a string",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nkeep_last = 123\n',
                "keep_last must be a string",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\ntarget_free = 100\n',
                "target_free must be a size string",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nsort_by = "newest"\n',
                "sort_by must be 'oldest' or 'largest'",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nmin_file_size = 50\n',
                "min_file_size must be a size string",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\ninclude_types = "image"\n',
                "include_types must be a list",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\ninclude_types = ["unknown"]\n',
                "Unknown media type\\(s\\) in include_types",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nexclude_types = "video"\n',
                "exclude_types must be a list",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nexclude_types = ["bogus"]\n',
                "Unknown media type\\(s\\) in exclude_types",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nprotect_starred = "true"\n',
                "protect_starred must be a boolean",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep]\nchat = "invalid"\n',
                "\\[sweep.chat\\] must be a single table",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep.chat]\nid = ""\nname = "Alice"\n',
                "\\[sweep.chat\\]\\.id must be a non-empty",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep.chat]\nid = "15550101@s.whatsapp.net"\nname = ""\n',
                "\\[sweep.chat\\]\\.name must be a non-empty",
            ),
            (
                '[general]\narchive = "/tmp/a"\n[sweep.chat]\nid = "15550101@s.whatsapp.net"\nname = "Alice"\nsweep_shared = "no"\n',
                "\\[sweep.chat\\]\\.sweep_shared must be a boolean",
            ),
        ],
    )
    def test_invalid_config_errors(self, tmp_path: Path, toml_body: str, err_match: str):
        cfg_file = _write_toml(tmp_path / "bad.toml", toml_body)
        with pytest.raises(ValueError, match=err_match):
            load_config(cfg_file)


class TestSweeperDbAndReports:
    def test_open_readonly_missing_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="Required database not found"):
            open_readonly_sqlite(str(tmp_path / "missing.db"))

    def test_open_sweeper_db_integrity_failure(self, tmp_path: Path, monkeypatch):
        class _BadConn:
            def __init__(self) -> None:
                self.row_factory = None

            def execute(self, sql: str):
                class _Cur:
                    def fetchone(self_inner):
                        return (1,)

                    def fetchall(self_inner):
                        return [("corrupt page 1",)]

                return _Cur()

            def executescript(self, sql: str) -> None:
                pass

            def commit(self) -> None:
                pass

        monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: _BadConn())
        with pytest.raises(sqlite3.DatabaseError, match="Sweeper DB integrity check failed"):
            open_sweeper_db(str(tmp_path))

    def test_restore_state_and_empty_noop_helpers(self, tmp_path: Path):
        conn = open_sweeper_db(str(tmp_path))
        try:
            # Empty remote_paths early returns
            mark_sweep_entries_status(conn, "s1", "DEV1", [], "deleted", "2026-01-01T00:00:00Z")
            mark_sweep_entries_undone(conn, "s1", "DEV1", [], "2026-01-01T00:00:00Z")

            create_restore_batch(
                conn,
                "restore_1",
                "DEV1",
                [
                    {
                        "remote_path": "/sdcard/IMG-1.jpg",
                        "archive_path": "Contacts/Alice/IMG-1.jpg",
                        "size_bytes": 100,
                    },
                    {
                        "remote_path": "/sdcard/IMG-2.jpg",
                        "archive_path": "Contacts/Alice/IMG-2.jpg",
                        "size_bytes": 200,
                    },
                ],
                "2026-01-01T00:00:00Z",
            )
            assert get_latest_incomplete_restore_id(conn, "DEV1") == "restore_1"
            pending = get_pending_restore_entries(conn, "restore_1", "DEV1")
            assert len(pending) == 2

            mark_restore_entry_status(
                conn,
                "restore_1",
                "DEV1",
                "/sdcard/IMG-1.jpg",
                "verified",
                "2026-01-01T00:01:00Z",
            )
            pending_after = get_pending_restore_entries(conn, "restore_1", "DEV1")
            assert [p["remote_path"] for p in pending_after] == ["/sdcard/IMG-2.jpg"]
        finally:
            conn.close()

    def test_db_mutation_rollback_on_error(self, tmp_path: Path):
        conn = open_sweeper_db(str(tmp_path))
        conn.close()  # Closed connection forces sqlite3.ProgrammingError

        with pytest.raises(sqlite3.ProgrammingError):
            create_sweep_batch(conn, "s1", "DEV1", [], "2026-01-01T00:00:00Z")
        with pytest.raises(sqlite3.ProgrammingError):
            mark_sweep_entries_status(
                conn, "s1", "DEV1", ["/sdcard/a.jpg"], "deleted", "2026-01-01T00:00:00Z"
            )
        with pytest.raises(sqlite3.ProgrammingError):
            mark_sweep_entries_undone(conn, "s1", "DEV1", ["/sdcard/a.jpg"], "2026-01-01T00:00:00Z")
        with pytest.raises(sqlite3.ProgrammingError):
            create_restore_batch(conn, "r1", "DEV1", [], "2026-01-01T00:00:00Z")
        with pytest.raises(sqlite3.ProgrammingError):
            mark_restore_entry_status(conn, "r1", "DEV1", "/sdcard/a.jpg", "verified", None)

    def test_write_unprotected_skipped_report(self, tmp_path: Path):
        assert write_unprotected_skipped_report(str(tmp_path), "20260101_000000", []) is None

        path = write_unprotected_skipped_report(
            str(tmp_path),
            "20260101_000000",
            [
                {
                    "remote_path": "/sdcard/IMG-1.jpg",
                    "reason": "hash_mismatch",
                    "file_size_bytes": 42,
                    "details": "MD5 mismatch",
                }
            ],
        )
        assert path is not None
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows == [
            {
                "remote_path": "/sdcard/IMG-1.jpg",
                "reason": "hash_mismatch",
                "file_size_bytes": "42",
                "details": "MD5 mismatch",
            }
        ]
