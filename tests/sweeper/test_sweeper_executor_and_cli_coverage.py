"""Targeted unit tests for wab_sweeper.executor, wab_sweeper.main, and __main__."""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path

import pytest
from shared import adb
from tests.sweeper.conftest import (
    FakeAndroidDevice,
    create_synthetic_archive,
    install_fake_device,
)
from wab_sweeper.config import load_config
from wab_sweeper.executor import (
    _apply_permissions_and_timestamps,
    _is_protected_folder,
    _prune_empty_leaf_dirs,
    _push_items_to_device,
    execute_restore,
    execute_sweep,
    execute_undo_last,
    write_dry_run_skipped_report,
)
from wab_sweeper.main import _format_bytes, run_cli
from wab_sweeper.planner import plan_restore, plan_sweep
from wab_sweeper.sweeper_db import create_restore_batch, open_sweeper_db


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test_sweeper_exec_cli_cov")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _setup_archive_and_config(tmp_path: Path, count: int, sweep_shared: bool):
    archive_dir = tmp_path / "archive"
    media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
    messages = []
    media_rows = []
    archive_files = []
    blobs: dict[str, bytes] = {}

    for idx in range(1, count + 1):
        fname = f"PTT-{idx:03d}.opus"
        orig = f"Media/WhatsApp Voice Notes/202037/{fname}"
        arc_rel = f"Contacts/Alice (0015550101)/{fname}"
        content = f"VOICE-PAYLOAD-{idx}".encode()
        blobs[f"{media_root}/WhatsApp Voice Notes/202037/{fname}"] = content
        messages.append((idx, 1, 0, f"k{idx}", 0, 1600000000000 + idx * 1000, 2, 0))
        media_rows.append((idx, 1, orig, "audio/ogg"))
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

    log_file = tmp_path / "logs" / "sweeper.log"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[general]",
                f'archive = "{archive_dir.as_posix()}"',
                "dry_run = true",
                f'log = "{log_file.as_posix()}"',
                "",
                "[sweep]",
                'older_than = "1y"',
                "",
                "[sweep.chat]",
                'id = "15550101@s.whatsapp.net"',
                'name = "Alice"',
                f"sweep_shared = {'true' if sweep_shared else 'false'}",
            ]
        ),
        encoding="utf-8",
    )
    return archive_dir, cfg_path, media_root, blobs, log_file


class TestExecutorEdgeCases:
    def test_protected_folder_and_prune_warning(self, tmp_path: Path, monkeypatch):
        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        assert _is_protected_folder(media_root, media_root) is True
        assert _is_protected_folder(f"{media_root}/WhatsApp Images", media_root) is True
        assert _is_protected_folder(f"{media_root}/WhatsApp Images/.nomedia", media_root) is True
        assert (
            _is_protected_folder(f"{media_root}/WhatsApp Voice Notes/202037", media_root) is False
        )

        monkeypatch.setattr(
            adb,
            "run_adb_shell_script",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rmdir fail")),
        )
        # Should log debug warning and not raise
        _prune_empty_leaf_dirs(
            "SIM-DEV-001",
            [f"{media_root}/WhatsApp Voice Notes/202037/PTT-001.opus"],
            media_root,
            _make_logger(),
        )

    def test_sweep_readonly_failure_and_dry_run_report(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs, _ = _setup_archive_and_config(
            tmp_path, count=2, sweep_shared=False
        )
        rp_list = sorted(blobs.keys())
        dev = FakeAndroidDevice(tmp_path / "phone", readonly_paths={rp_list[0]})
        install_fake_device(monkeypatch, dev)
        for rp, data in blobs.items():
            dev.put_file(rp, data)
        # Add unarchived file so plan.skipped is non-empty
        dev.put_file(f"{media_root}/WhatsApp Images/UNARCHIVED.jpg", b"UNARCHIVED")

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan = plan_sweep(cfg, logger)

        dry_report = write_dry_run_skipped_report(str(archive_dir), plan)
        assert dry_report is not None and Path(dry_report).is_file()

        res = execute_sweep(cfg, plan, logger)
        assert res["deleted_count"] == 1
        assert res["failed_count"] == 1

    def test_push_items_empty_missing_and_identical(self, tmp_path: Path, monkeypatch):
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)
        logger = _make_logger()

        # Empty items early return
        assert _push_items_to_device(
            "SIM-DEV-001", "/m", str(tmp_path), [], logger, None, None, None
        ) == (
            [],
            [],
        )
        _apply_permissions_and_timestamps("SIM-DEV-001", [], logger)

        # Missing local archive file raises FileNotFoundError
        with pytest.raises(FileNotFoundError, match="Local archive file missing"):
            _push_items_to_device(
                "SIM-DEV-001",
                "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media",
                str(tmp_path),
                [
                    {
                        "remote_path": "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/NOPE.jpg",
                        "archive_path": "Contacts/Alice/NOPE.jpg",
                        "abs_archive_path": str(tmp_path / "NOPE.jpg"),
                        "size_bytes": 10,
                        "expected_md5": b"\x00" * 16,
                        "timestamp_ms": 1600000000000,
                    }
                ],
                logger,
                None,
                None,
                None,
            )

    def test_restore_resume_and_identical_skip(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, media_root, blobs, _ = _setup_archive_and_config(
            tmp_path, count=2, sweep_shared=False
        )
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        serial, mroot, pkg, items = plan_restore(cfg, logger)
        assert len(items) == 2

        # Seed an incomplete restore batch in .wab-sweeper.db with 1st item staged
        sconn = open_sweeper_db(str(archive_dir))
        create_restore_batch(
            sconn,
            "restore_incomplete_1",
            serial,
            [
                {
                    "remote_path": items[0]["remote_path"],
                    "archive_path": items[0]["archive_path"],
                    "size_bytes": items[0]["size_bytes"],
                }
            ],
            "2026-01-01T00:00:00+00:00",
        )
        sconn.close()

        res = execute_restore(cfg, serial, mroot, pkg, items, logger)
        assert res["restore_id"] == "restore_incomplete_1"
        assert res["restored_count"] == 2
        for rp, data in blobs.items():
            assert dev.read_bytes(rp) == data

        # Now test _push_items_to_device when file already exists with identical MD5
        pushed_before = list(dev._pushed_files)
        verified, conflicts = _push_items_to_device(
            serial, mroot, str(archive_dir), items, logger, None, None, None
        )
        assert len(verified) == 2
        assert conflicts == []
        assert dev._pushed_files == pushed_before

    def test_undo_dry_run(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, _, blobs, _ = _setup_archive_and_config(
            tmp_path, count=1, sweep_shared=False
        )
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)
        for rp, data in blobs.items():
            dev.put_file(rp, data)

        cfg = load_config(str(cfg_path))
        logger = _make_logger()
        plan = plan_sweep(cfg, logger)
        execute_sweep(cfg, plan, logger)

        dry_undo = execute_undo_last(str(archive_dir), None, 0, logger, True)
        assert dry_undo["sweep_id"] is not None
        assert dry_undo["candidate_count"] == 1
        assert dry_undo["restored_count"] == 0


class TestCliCommandsAndEntryPoint:
    def test_format_bytes_large_units(self):
        assert _format_bytes(500) == "500 B"
        assert _format_bytes(2 * 1024**3) == "2.00 GB"
        assert _format_bytes(3 * 1024**4) == "3.00 TB"

    def test_list_chats_and_init_config_force(self, tmp_path: Path, capsys):
        archive_dir, cfg_path, _, _, _ = _setup_archive_and_config(
            tmp_path, count=1, sweep_shared=False
        )
        assert run_cli(["list-chats", "--archive", str(archive_dir)]) == 0
        out = capsys.readouterr().out
        assert "Alice" in out

        assert (
            run_cli(["list-chats", "--archive", str(archive_dir), "--search", "nonexistent"]) == 0
        )
        assert "No matching chats" in capsys.readouterr().out

        # Existing file without --force fails
        assert run_cli(["init-config", "--output", str(cfg_path)]) == 1
        # With --force overwrites
        assert run_cli(["init-config", "--output", str(cfg_path), "--force"]) == 0

    def test_cli_adb_unavailable_and_empty_plans(self, tmp_path: Path, monkeypatch):
        archive_dir, cfg_path, _, blobs, log_file = _setup_archive_and_config(
            tmp_path, count=1, sweep_shared=True
        )
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        # ADB unavailable returns 1 for sweep, restore, undo
        monkeypatch.setattr(adb, "check_adb", lambda logger: False)
        assert run_cli(["sweep", "--config", str(cfg_path)]) == 1
        assert run_cli(["restore", "--config", str(cfg_path)]) == 1
        assert run_cli(["undo", "--last", "--archive", str(archive_dir)]) == 1

        # Restore ADB available
        monkeypatch.setattr(adb, "check_adb", lambda logger: True)

        # Sweep --execute with no candidates on device
        assert run_cli(["sweep", "--config", str(cfg_path), "--execute", "--yes"]) == 0
        assert log_file.is_file()

        # Restore dry-run then --execute
        assert run_cli(["restore", "--config", str(cfg_path)]) == 0
        assert run_cli(["restore", "--config", str(cfg_path), "--execute"]) == 0
        # Second restore --execute finds everything already on device
        assert run_cli(["restore", "--config", str(cfg_path), "--execute"]) == 0

        # Sweep with --sweep-shared and EOFError on confirmation
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
        assert run_cli(["sweep", "--config", str(cfg_path), "--execute"]) == 1

        # Sweep with --yes executes deletion
        assert run_cli(["sweep", "--config", str(cfg_path), "--execute", "--yes"]) == 0

        # Undo dry-run then --execute with --config, then no undoable left
        assert run_cli(["undo", "--last", "--config", str(cfg_path)]) == 0
        assert run_cli(["undo", "--last", "--config", str(cfg_path), "--execute"]) == 0
        assert run_cli(["undo", "--last", "--config", str(cfg_path), "--execute"]) == 0

    def test_module_main_entry_point(self, tmp_path: Path, monkeypatch):
        out = tmp_path / "m_cfg.toml"
        monkeypatch.setattr(sys, "argv", ["wab-sweeper", "init-config", "--output", str(out)])
        with pytest.raises(SystemExit) if False else pytest.MonkeyPatch().context():
            # __main__.py calls main() without sys.exit; verify it executes cleanly
            runpy.run_module("wab_sweeper", run_name="__main__")
        assert out.is_file()
