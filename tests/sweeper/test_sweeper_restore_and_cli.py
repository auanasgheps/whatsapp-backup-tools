"""Tests for resumable execution, idempotent undo, restore quota/collisions, and CLI."""

from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

import pytest
from tests.sweeper.conftest import (
    FakeAndroidDevice,
    create_synthetic_archive,
    install_fake_device,
)
from wab_sweeper.config import load_config
from wab_sweeper.executor import execute_restore, execute_sweep, execute_undo_last
from wab_sweeper.main import run_cli
from wab_sweeper.planner import plan_restore, plan_sweep


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test_sweeper_exec")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _setup_sweep_fixture(tmp_path: Path, count: int = 3):
    archive_dir = tmp_path / "archive"
    media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
    messages = []
    media_rows = []
    archive_files = []
    blobs: dict[str, bytes] = {}

    for idx in range(1, count + 1):
        fname = f"IMG-{idx:03d}.jpg"
        orig = f"Media/WhatsApp Images/{fname}"
        arc_rel = f"Contacts/Alice (0015550101)/{fname}"
        content = f"IMAGE-PAYLOAD-{idx}".encode()
        blobs[f"{media_root}/WhatsApp Images/{fname}"] = content
        messages.append((idx, 1, 0, f"k{idx}", 0, 1600000000000 + idx * 1000, 1, 0))
        media_rows.append((idx, 1, orig, "image/jpeg"))
        archive_files.append(
            {
                "original_path": orig,
                "archive_path": arc_rel,
                "raw_bytes": content,
                "timestamp_ms": 1600000000000 + idx * 1000,
            }
        )

    create_synthetic_archive(
        archive_dir,
        contacts=[("15550101", "Alice (0015550101)", "Alice")],
        groups=[],
        jids=[(1, "15550101", "s.whatsapp.net")],
        chats=[(1, 1, None)],
        messages=messages,
        media_rows=media_rows,
        archive_files=archive_files,
    )

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[general]",
                f'archive = "{archive_dir.as_posix()}"',
                "dry_run = true",
                "",
                "[sweep]",
                'older_than = "1y"',
                "",
                "[sweep.chat]",
                'id = "15550101@s.whatsapp.net"',
                'name = "Alice"',
                "sweep_shared = false",
            ]
        ),
        encoding="utf-8",
    )
    return archive_dir, cfg_path, media_root, blobs


class TestResumabilityAndUndoIdempotency:
    def test_mid_sweep_disconnect_resumes_cleanly(self, tmp_path: Path, monkeypatch):
        # Create 260 files so deletion spans 2 batches (250 + 10)
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=260)
        dev = FakeAndroidDevice(tmp_path / "phone", disconnect_after_scripts=1)
        install_fake_device(monkeypatch, dev)

        for rp, data in blobs.items():
            dev.put_file(rp, data)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan1 = plan_sweep(cfg, logger)
        assert len(plan1.candidates) == 260

        with pytest.raises(RuntimeError, match="Simulated USB disconnect"):
            execute_sweep(cfg, plan1, logger)

        # Verify 250 deleted, 10 still staged in .wab-sweeper.db
        conn = sqlite3.connect(archive_dir / ".wab-sweeper.db")
        counts = dict(
            conn.execute("SELECT status, COUNT(*) FROM adb_sweep_state GROUP BY status").fetchall()
        )
        conn.close()
        assert counts == {"deleted": 250, "staged": 10}

        # Reconnect and resume
        dev.disconnect_after_scripts = None
        plan2 = plan_sweep(cfg, logger)
        assert len(plan2.candidates) == 10
        res = execute_sweep(cfg, plan2, logger)
        assert res["deleted_count"] == 10

        conn = sqlite3.connect(archive_dir / ".wab-sweeper.db")
        counts_after = dict(
            conn.execute("SELECT status, COUNT(*) FROM adb_sweep_state GROUP BY status").fetchall()
        )
        conn.close()
        assert counts_after == {"deleted": 260}

    def test_resume_does_not_mark_existing_excluded_file_as_deleted(
        self, tmp_path: Path, monkeypatch
    ):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=2)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)
        for rp, data in blobs.items():
            dev.put_file(rp, data)

        # Stage 2 files as interrupted from an earlier run, while both still exist on device
        from wab_sweeper.sweeper_db import create_sweep_batch, open_sweeper_db

        paths = sorted(blobs.keys())
        sconn = open_sweeper_db(str(archive_dir))
        create_sweep_batch(
            sconn,
            "sweep_interrupted",
            dev.serial,
            [
                {
                    "remote_path": rp,
                    "archive_path": f"Contacts/Alice (0015550101)/{rp.rsplit('/', 1)[-1]}",
                    "chat_jid": "15550101@s.whatsapp.net",
                    "size_bytes": len(blobs[rp]),
                }
                for rp in paths
            ],
            "2026-01-01T00:00:00+00:00",
        )
        sconn.close()

        # Restrict config on resume so neither file is eligible (min_file_size = 1MB)
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8").replace(
                'older_than = "1y"', 'older_than = "1y"\nmin_file_size = "1MB"'
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan = plan_sweep(cfg, logger)
        assert plan.candidates == []
        res = execute_sweep(cfg, plan, logger)
        assert res["deleted_count"] == 0

        # Both files still exist on device -> must be marked 'failed', never 'deleted'
        conn = sqlite3.connect(archive_dir / ".wab-sweeper.db")
        statuses = dict(conn.execute("SELECT remote_path, status FROM adb_sweep_state").fetchall())
        conn.close()
        assert all(st == "failed" for st in statuses.values())

    def test_undo_last_restores_and_is_idempotent(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=3)
        dev = FakeAndroidDevice(tmp_path / "phone", wakelock_restricted=True)
        install_fake_device(monkeypatch, dev)

        for rp, data in blobs.items():
            dev.put_file(rp, data)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan = plan_sweep(cfg, logger)
        res = execute_sweep(cfg, plan, logger)
        assert res["deleted_count"] == 3
        for rp in blobs:
            assert not dev.exists(rp)

        # First undo restores all 3 files + applies chmod 664 and touch -d
        undo1 = execute_undo_last(str(archive_dir), None, 0, logger, False)
        assert undo1["restored_count"] == 3
        for rp, data in blobs.items():
            assert dev.exists(rp)
            assert dev.read_bytes(rp) == data
        assert len(dev._chmod_calls) == 3
        assert all(mode == "664" for mode, _ in dev._chmod_calls)
        assert len(dev._touch_calls) == 3

        # Second undo is strictly idempotent (0 files to restore)
        undo2 = execute_undo_last(str(archive_dir), None, 0, logger, False)
        assert undo2["sweep_id"] is None
        assert undo2["restored_count"] == 0


class TestRestoreQuotaCollisionsAndHygiene:
    def test_restore_aborts_on_low_device_storage(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=2)
        # Only 500 MB free (< batch + 1.0 GB safety buffer)
        dev = FakeAndroidDevice(tmp_path / "phone", free_kb=500 * 1024)
        install_fake_device(monkeypatch, dev)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        serial, mroot, pkg, items = plan_restore(cfg, logger)
        assert len(items) == 2

        with pytest.raises(RuntimeError, match="Insufficient storage"):
            execute_restore(cfg, serial, mroot, pkg, items, logger)
        assert dev._pushed_files == []

    def test_restore_collision_backup_and_csv_report(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=1)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        target_rp = f"{media_root}/WhatsApp Images/IMG-001.jpg"
        dev.put_file(target_rp, b"DIFFERENT-CONFLICTING-PHONE-FILE")

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        # plan_restore skips paths already on device, so test collision via undo_last
        # by simulating a swept row that now has a conflicting file on phone
        from wab_sweeper.sweeper_db import (
            create_sweep_batch,
            mark_sweep_entries_status,
            open_sweeper_db,
        )

        sconn = open_sweeper_db(str(archive_dir))
        create_sweep_batch(
            sconn,
            "sweep_test_1",
            dev.serial,
            [
                {
                    "remote_path": target_rp,
                    "archive_path": "Contacts/Alice (0015550101)/IMG-001.jpg",
                    "chat_jid": "15550101@s.whatsapp.net",
                    "size_bytes": len(blobs[target_rp]),
                }
            ],
            "2026-01-01T00:00:00+00:00",
        )
        mark_sweep_entries_status(
            sconn, "sweep_test_1", dev.serial, [target_rp], "deleted", "2026-01-01T00:00:01+00:00"
        )
        sconn.close()

        undo_res = execute_undo_last(str(archive_dir), None, 0, logger, False)
        assert undo_res["restored_count"] == 1
        assert dev.read_bytes(target_rp) == blobs[target_rp]

        conflict_csvs = list(archive_dir.glob("restore_conflicts_*.csv"))
        assert len(conflict_csvs) == 1
        rows = list(csv.DictReader(conflict_csvs[0].open(encoding="utf-8")))
        assert len(rows) == 1
        assert rows[0]["action_taken"] == "backed_up_and_replaced"
        assert dev.exists(rows[0]["backup_path"])
        assert dev.read_bytes(rows[0]["backup_path"]) == b"DIFFERENT-CONFLICTING-PHONE-FILE"

    def test_nomedia_and_root_folders_never_deleted(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=1)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        nomedia = f"{media_root}/WhatsApp Images/.nomedia"
        thumbs = f"{media_root}/WhatsApp Images/.thumbs"
        shared_dir_marker = f"{media_root}/.Shared"
        dev.put_file(nomedia, b"")
        dev.put_file(thumbs, b"thumb")
        dev.put_file(shared_dir_marker, b"")
        for rp, data in blobs.items():
            dev.put_file(rp, data)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan = plan_sweep(cfg, logger)
        res = execute_sweep(cfg, plan, logger)
        assert res["deleted_count"] == 1
        assert dev.exists(nomedia)
        assert dev.exists(thumbs)
        assert dev.exists(shared_dir_marker)

    def test_active_whatsapp_backup_aborts_execution(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=1)
        dev = FakeAndroidDevice(tmp_path / "phone", active_backup=True)
        install_fake_device(monkeypatch, dev)
        for rp, data in blobs.items():
            dev.put_file(rp, data)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan = plan_sweep(cfg, logger)
        with pytest.raises(RuntimeError, match="active WhatsApp backup"):
            execute_sweep(cfg, plan, logger)


class TestCliDryRunConfirmationAndReports:
    def test_cli_dry_run_default_and_confirm_prompt(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs = _setup_sweep_fixture(tmp_path, count=2)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)
        for rp, data in blobs.items():
            dev.put_file(rp, data)

        # Dry-run default does not delete
        rc_dry = run_cli(["sweep", "--config", str(cfg_path)])
        assert rc_dry == 0
        assert all(dev.exists(rp) for rp in blobs)

        # Wrong confirmation token aborts without deletion
        monkeypatch.setattr("builtins.input", lambda _: "no")
        rc_abort = run_cli(["sweep", "--config", str(cfg_path), "--execute"])
        assert rc_abort == 1
        assert all(dev.exists(rp) for rp in blobs)

        # Exact 'CONFIRM' executes deletion and writes swept_media_report_*.csv
        monkeypatch.setattr("builtins.input", lambda _: "CONFIRM")
        rc_exec = run_cli(["sweep", "--config", str(cfg_path), "--execute"])
        assert rc_exec == 0
        assert all(not dev.exists(rp) for rp in blobs)
        reports = list(archive_dir.glob("swept_media_report_*.csv"))
        assert len(reports) == 1
        rows = list(csv.DictReader(reports[0].open(encoding="utf-8")))
        assert len(rows) == 2

    def test_init_config_generates_valid_template(self, tmp_path: Path):
        out = tmp_path / "generated.toml"
        rc = run_cli(["init-config", "--output", str(out)])
        assert rc == 0
        cfg = load_config(str(out))
        assert cfg.dry_run is True
        assert cfg.protect_starred is True
        assert cfg.sweep_preserved is False
