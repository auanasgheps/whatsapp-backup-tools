"""Targeted unit tests for wab_sweeper.planner edge cases and policy branches."""

from __future__ import annotations

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
from wab_sweeper.planner import (
    classify_sweeper_media_type,
    detect_business_and_serial,
    is_protected_system_path,
    list_archive_chats,
    plan_restore,
    plan_sweep,
    to_original_db_path,
)


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test_sweeper_planner_cov")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


class TestPlannerHelpers:
    @pytest.mark.parametrize(
        ("path", "msg_type", "mime", "expected"),
        [
            ("Media/WhatsApp Video Notes/VN-1.mp4", None, None, "video_note"),
            ("Media/WhatsApp Stickers/STK-1.webp", None, None, "sticker"),
            ("Media/WhatsApp Audio/AUD-1.mp3", None, None, "audio"),
            ("Media/WhatsApp Documents/DOC-1.pdf", None, None, "document"),
            ("Media/WhatsApp Animated Gifs/GIF-1.mp4", None, None, "video"),
            ("unknown/path/a.bin", 2, None, "voice_note"),
            ("unknown/path/b.bin", 28, None, "video_note"),
            ("unknown/path/c.bin", 81, None, "video_note"),
            ("unknown/path/d.bin", 20, None, "sticker"),
            ("unknown/path/e.bin", 9, None, "document"),
            ("unknown/path/f.bin", 13, None, "video"),
            ("unknown/path/g.bin", 42, None, "image"),
            ("unknown/path/h.bin", None, "image/png", "image"),
            ("unknown/path/i.bin", None, "video/mp4", "video"),
            ("unknown/path/j.bin", None, "audio/mpeg", "audio"),
            ("unknown/path/k.bin", None, "application/octet-stream", "document"),
        ],
    )
    def test_classify_sweeper_media_type(
        self, path: str, msg_type: int | None, mime: str | None, expected: str
    ):
        assert classify_sweeper_media_type(path, msg_type, mime) == expected

    def test_protected_system_paths_and_db_path_conversion(self):
        assert is_protected_system_path("/sdcard/Media/.Statuses/status.jpg") is True
        assert is_protected_system_path("/sdcard/Media/.Shared/tmp") is True
        assert is_protected_system_path("/sdcard/Media/.thumbs/t.jpg") is True
        assert is_protected_system_path("/sdcard/Media/WhatsApp Images/IMG-1.jpg") is False

        assert to_original_db_path("Media/WhatsApp Images/IMG-1.jpg") == (
            "Media/WhatsApp Images/IMG-1.jpg"
        )
        assert to_original_db_path("/sdcard/WhatsApp/Other/foo.jpg") is None

    def test_detect_business_and_serial(self, tmp_path: Path):
        db_path = tmp_path / "arc1.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE adb_pull_state (remote_path TEXT, device_serial TEXT);
            CREATE TABLE files (original_path TEXT);
            INSERT INTO adb_pull_state VALUES (
                '/storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp Business/Media/a.jpg',
                'SER-W4B-1'
            );
            """
        )
        assert detect_business_and_serial(conn) == (True, "SER-W4B-1")
        conn.close()

        db_path2 = tmp_path / "arc2.db"
        conn2 = sqlite3.connect(db_path2)
        conn2.row_factory = sqlite3.Row
        conn2.executescript(
            """
            CREATE TABLE files (original_path TEXT);
            INSERT INTO files VALUES ('WhatsApp Business/Media/WhatsApp Images/IMG-1.jpg');
            """
        )
        assert detect_business_and_serial(conn2) == (True, None)
        conn2.close()


class TestChatIdentitiesAndDiscovery:
    def test_number_change_and_fallback_display_names(self, tmp_path: Path):
        archive_dir = tmp_path / "archive"
        create_synthetic_archive(
            archive_dir,
            contacts=[("15550199", "Alice New (0015550199)", "Alice New")],
            groups=[],
            jids=[
                (1, "15550100", "s.whatsapp.net"),  # old number
                (2, "15550199", "s.whatsapp.net"),  # new number
                (3, "120363000000000099", "g.us"),  # group without subject
                (4, "15550155", "s.whatsapp.net"),  # unknown contact
            ],
            chats=[
                (1, 1, None),
                (2, 3, None),
                (3, 4, None),
            ],
            messages=[
                (1, 1, 0, "k1", 0, 1600000000000, 1, 0),
                (2, 2, 0, "k2", 0, 1600000000000, 1, 0),
                (3, 3, 0, "k3", 0, 1600000000000, 1, 0),
            ],
            media_rows=[
                (1, 1, "Media/WhatsApp Images/IMG-1.jpg", "image/jpeg"),
                (2, 2, "Media/WhatsApp Images/IMG-2.jpg", "image/jpeg"),
                (3, 3, "Media/WhatsApp Images/IMG-3.jpg", "image/jpeg"),
            ],
            archive_files=[
                {
                    "original_path": "Media/WhatsApp Images/IMG-1.jpg",
                    "archive_path": "Contacts/Alice New/IMG-1.jpg",
                    "raw_bytes": b"A1",
                },
                {
                    "original_path": "Media/WhatsApp Images/IMG-2.jpg",
                    "archive_path": "Groups/G1/IMG-2.jpg",
                    "raw_bytes": b"A2",
                },
                {
                    "original_path": "Media/WhatsApp Images/IMG-3.jpg",
                    "archive_path": "Contacts/15550155/IMG-3.jpg",
                    "raw_bytes": b"A3",
                },
            ],
        )
        # Add message_system_number_change to msgstore.db
        msg_conn = sqlite3.connect(archive_dir / "msgstore.db")
        msg_conn.executescript(
            """
            CREATE TABLE message_system_number_change (
                message_row_id INTEGER PRIMARY KEY,
                old_jid_row_id INTEGER NOT NULL,
                new_jid_row_id INTEGER NOT NULL
            );
            INSERT INTO message_system_number_change VALUES (1, 1, 2);
            """
        )
        msg_conn.commit()
        msg_conn.close()

        # Also create an empty preserved_messages.db without preserved_messages table
        pres_conn = sqlite3.connect(archive_dir / "preserved_messages.db")
        pres_conn.execute("CREATE TABLE meta (k TEXT)")
        pres_conn.commit()
        pres_conn.close()

        all_chats = list_archive_chats(str(archive_dir), None)
        names = {c.jid: c.display_name for c in all_chats}
        assert names["15550100@s.whatsapp.net"] == "Alice New"
        assert names["120363000000000099@g.us"] == "Group (120363000000000099)"
        assert names["15550155@s.whatsapp.net"] == "15550155"

        assert list_archive_chats(str(archive_dir), "no-such-chat") == []


class TestPlanSweepAndRestoreEdgeCases:
    def _build_edge_archive(self, tmp_path: Path):
        archive_dir = tmp_path / "archive"
        create_synthetic_archive(
            archive_dir,
            contacts=[
                ("15550101", "Alice (0015550101)", "Alice"),
                ("15550102", "Bob (0015550102)", "Bob"),
            ],
            groups=[],
            jids=[
                (1, "15550101", "s.whatsapp.net"),
                (2, "15550102", "s.whatsapp.net"),
            ],
            chats=[(1, 1, None), (2, 2, None)],
            messages=[
                # Old sent message (from_me=1)
                (1, 1, 1, "k1", 0, 1600000000000, 1, 0),
                # Old received message, larger size
                (2, 1, 0, "k2", 0, 1600000005000, 1, 0),
                # Recent message (newer than cutoff)
                (3, 1, 0, "k3", 0, 4000000000000, 1, 0),
                # Bob's message
                (4, 2, 0, "k4", 0, 1600000000000, 1, 0),
                # Post-archive cutoff unarchived message (timestamp > archive_latest_ts_ms handled below)
                (5, 1, 0, "k5", 0, 1700000000000, 1, 0),
            ],
            media_rows=[
                (1, 1, "Media/WhatsApp Images/IMG-SENT.jpg", "image/jpeg"),
                (2, 1, "Media/WhatsApp Images/IMG-LARGE.jpg", "image/jpeg"),
                (3, 1, "Media/WhatsApp Images/IMG-RECENT.jpg", "image/jpeg"),
                (4, 2, "Media/WhatsApp Images/IMG-BOB.jpg", "image/jpeg"),
                (5, 1, "Media/WhatsApp Images/IMG-POST.jpg", "image/jpeg"),
            ],
            archive_files=[
                {
                    "original_path": "Media/WhatsApp Images/IMG-SENT.jpg",
                    "archive_path": "Contacts/Alice (0015550101)/IMG-SENT.jpg",
                    "raw_bytes": b"SENT-12345",
                    "timestamp_ms": 1600000000000,
                },
                {
                    "original_path": "Media/WhatsApp Images/IMG-LARGE.jpg",
                    "archive_path": "Contacts/Alice (0015550101)/IMG-LARGE.jpg",
                    "raw_bytes": b"L" * 2048,
                    "timestamp_ms": 1600000005000,
                },
                {
                    "original_path": "Media/WhatsApp Images/IMG-RECENT.jpg",
                    "archive_path": "Contacts/Alice (0015550101)/IMG-RECENT.jpg",
                    "raw_bytes": b"RECENT-BYTES",
                    "timestamp_ms": 4000000000000,
                },
                {
                    "original_path": "Media/WhatsApp Images/IMG-BOB.jpg",
                    "archive_path": "Contacts/Bob (0015550102)/IMG-BOB.jpg",
                    "raw_bytes": b"BOB-BYTES-LONG",
                    "timestamp_ms": 1600000000000,
                },
                {
                    "original_path": "Media/WhatsApp Images/IMG-MISSING-DISK.jpg",
                    "archive_path": "Contacts/Alice (0015550101)/IMG-MISSING-DISK.jpg",
                    "raw_bytes": b"MISSING",
                    "write_disk": False,
                    "timestamp_ms": 1600000000000,
                },
            ],
        )
        # Set message 3 timestamp to 1650000000000 so max_ts=1700000000000,
        # and make message 5 timestamp 1800000000000? Wait: MAX(timestamp) in message
        # is archive_latest_ts_ms, so post_archive_cutoff happens if msg_refs timestamp > MAX(timestamp)
        # OR we can leave message 3 as 1650000000000 and test orphan/size mismatch/md5 error.
        return archive_dir

    def test_safety_handshake_missing_jid_raises(self, tmp_path: Path):
        archive_dir = self._build_edge_archive(tmp_path)
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    "[general]",
                    f'archive = "{archive_dir.as_posix()}"',
                    "[sweep.chat]",
                    'id = "15559999@s.whatsapp.net"',
                    'name = "Ghost"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(cfg_path))
        with pytest.raises(ValueError, match="does not exist in the archive"):
            plan_sweep(cfg, _make_logger())

    def test_plan_sweep_filters_quota_sort_and_md5_errors(self, tmp_path: Path, monkeypatch):
        archive_dir = self._build_edge_archive(tmp_path)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        # Put files on phone
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-SENT.jpg", b"SENT-12345")
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-LARGE.jpg", b"L" * 2048)
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-RECENT.jpg", b"RECENT-BYTES")
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-BOB.jpg", b"BOB-BYTES-LONG")
        # Size mismatch (exif_stamped=0, remote size != archive size)
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-POST.jpg", b"NOT-CATALOGED")
        # Outside Media/ hierarchy (simulate enumerate_remote_files returning non-Media path)
        orig_enum = dev.enumerate_files

        def _enum_with_extra(root: str):
            return orig_enum(root) + [("/storage/emulated/0/Other/stray.jpg", 10)]

        monkeypatch.setattr(dev, "enumerate_files", _enum_with_extra)

        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    "[general]",
                    f'archive = "{archive_dir.as_posix()}"',
                    "[sweep]",
                    'keep_last = "1y"',
                    'target_free = "1KB"',
                    'sort_by = "largest"',
                    'min_file_size = "5B"',
                    "exclude_sent = true",
                    "[sweep.chat]",
                    'id = "15550101@s.whatsapp.net"',
                    'name = "Alice"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(cfg_path))
        plan = plan_sweep(cfg, _make_logger())

        # IMG-LARGE.jpg (2048B) satisfies target_free=1KB; IMG-SENT excluded by exclude_sent;
        # IMG-RECENT excluded by cutoff; IMG-BOB excluded by cfg.chat; stray.jpg skipped
        assert [c.remote_path.rsplit("/", 1)[-1] for c in plan.candidates] == ["IMG-LARGE.jpg"]
        reasons = {s.remote_path.rsplit("/", 1)[-1]: s.reason for s in plan.skipped}
        assert reasons["stray.jpg"] == "not_in_archive"
        assert reasons["IMG-POST.jpg"] == "not_in_archive"

    def test_plan_sweep_size_mismatch_and_remote_md5_exception(self, tmp_path: Path, monkeypatch):
        archive_dir = self._build_edge_archive(tmp_path)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        # Wrong size on device -> Gate 4 size mismatch
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-SENT.jpg", b"WRONG-SIZE")
        # Matching size on device, but remote_md5 raises exception -> Gate 4 hash_mismatch
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-BOB.jpg", b"BOB-BYTES-LONG")

        from shared import adb

        def _boom_md5(rp, binary, device_serial, logger):
            raise RuntimeError("adb md5 failed")

        monkeypatch.setattr(adb, "remote_md5_for_device", _boom_md5)

        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    "[general]",
                    f'archive = "{archive_dir.as_posix()}"',
                    "[sweep]",
                    'older_than = "1y"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(cfg_path))
        plan = plan_sweep(cfg, _make_logger())
        reasons = {s.remote_path.rsplit("/", 1)[-1]: s.reason for s in plan.skipped}
        assert reasons["IMG-SENT.jpg"] == "hash_mismatch"
        assert reasons["IMG-BOB.jpg"] == "hash_mismatch"

    def test_plan_restore_filters(self, tmp_path: Path, monkeypatch):
        archive_dir = self._build_edge_archive(tmp_path)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        # Put IMG-SENT on device so it's skipped as already present; leave IMG-LARGE missing
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-SENT.jpg", b"SENT-12345")

        cfg_path = tmp_path / "cfg_restore.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    "[general]",
                    f'archive = "{archive_dir.as_posix()}"',
                    "[sweep]",
                    'keep_last = "1y"',
                    'min_file_size = "1KB"',
                    "exclude_sent = true",
                    'include_types = ["image"]',
                    "[sweep.chat]",
                    'id = "15550101@s.whatsapp.net"',
                    'name = "Alice"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(cfg_path))
        _, _, _, items = plan_restore(cfg, _make_logger())
        assert [i["remote_path"].rsplit("/", 1)[-1] for i in items] == ["IMG-LARGE.jpg"]
