"""Tests for 4-Gate Integrity Pipeline, Chat Discovery, Safety Handshake, and Policy Rules."""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path

import pytest
from shared import adb
from tests.sweeper.conftest import (
    FakeAndroidDevice,
    create_synthetic_archive,
    install_fake_device,
)
from wab_sweeper.config import load_config
from wab_sweeper.planner import list_archive_chats, plan_sweep


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("test_sweeper_pipeline")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _base_archive(tmp_path: Path, *, exif_stamped: int = 0, write_disk: bool = True):
    archive_dir = tmp_path / "archive"
    img_raw = b"ORIGINAL-JPEG-BYTES-001"
    img_disk = b"EXIF-STAMPED-JPEG-BYTES-001" if exif_stamped else img_raw
    vid_raw = b"ORIGINAL-MP4-BYTES-002"
    ptt_raw = b"ORIGINAL-OPUS-BYTES-003"
    shared_raw = b"SHARED-JPEG-BYTES-004"
    starred_raw = b"STARRED-JPEG-BYTES-005"
    preserved_raw = b"PRESERVED-JPEG-BYTES-006"

    create_synthetic_archive(
        archive_dir,
        contacts=[
            ("15550101", "Alice (0015550101)", "Alice 🌸"),
            ("15550102", "Bob (0015550102)", "Bob"),
        ],
        groups=[("2", "Family Group 🏡", "Family Group 🏡")],
        jids=[
            (1, "15550101", "s.whatsapp.net"),
            (2, "120363000000000001", "g.us"),
            (3, "999000111222", "lid"),
            (4, "15550102", "s.whatsapp.net"),
        ],
        jid_map_rows=[(3, 4)],  # LID 3 -> Bob (4)
        chats=[
            (1, 1, None),
            (2, 2, "Family Group 🏡"),
            (3, 3, None),  # Bob via LID
        ],
        messages=[
            # Alice: old image (1), old video (2), old voice note (3), shared (4), starred (5), preserved (6)
            (1, 1, 0, "k1", 0, 1600000000000, 1, 0),
            (2, 1, 0, "k2", 0, 1600000000000, 3, 0),
            (3, 1, 0, "k3", 0, 1600000000000, 2, 0),
            (4, 1, 0, "k4", 0, 1600000000000, 1, 0),
            (5, 1, 0, "k5", 0, 1600000000000, 1, 1),  # starred=1
            (6, 1, 0, "k6", 0, 1600000000000, 1, 0),  # preserved
            # Family Group also references shared image (k7)
            (7, 2, 0, "k7", 1, 1600000000000, 1, 0),
            # Bob (via LID) has a newer image
            (8, 3, 0, "k8", 0, 1700000000000, 1, 0),
        ],
        media_rows=[
            (1, 1, "Media/WhatsApp Images/IMG-001.jpg", "image/jpeg"),
            (2, 1, "Media/WhatsApp Video/VID-002.mp4", "video/mp4"),
            (3, 1, "Media/WhatsApp Voice Notes/202037/PTT-003.opus", "audio/ogg"),
            (4, 1, "Media/WhatsApp Images/IMG-SHARED.jpg", "image/jpeg"),
            (5, 1, "Media/WhatsApp Images/IMG-STARRED.jpg", "image/jpeg"),
            (6, 1, "Media/WhatsApp Images/IMG-PRESERVED.jpg", "image/jpeg"),
            (7, 2, "Media/WhatsApp Images/IMG-SHARED.jpg", "image/jpeg"),
            (8, 3, "Media/WhatsApp Images/IMG-BOB.jpg", "image/jpeg"),
        ],
        archive_files=[
            {
                "original_path": "Media/WhatsApp Images/IMG-001.jpg",
                "archive_path": "Contacts/Alice (0015550101)/IMG-001.jpg",
                "raw_bytes": img_raw,
                "disk_bytes": img_disk,
                "write_disk": write_disk,
                "exif_stamped": exif_stamped,
            },
            {
                "original_path": "Media/WhatsApp Video/VID-002.mp4",
                "archive_path": "Contacts/Alice (0015550101)/VID-002.mp4",
                "raw_bytes": vid_raw,
            },
            {
                "original_path": "Media/WhatsApp Voice Notes/202037/PTT-003.opus",
                "archive_path": "Contacts/Alice (0015550101)/PTT-003.opus",
                "raw_bytes": ptt_raw,
            },
            {
                "original_path": "Media/WhatsApp Images/IMG-SHARED.jpg",
                "archive_path": "Contacts/Alice (0015550101)/IMG-SHARED.jpg",
                "raw_bytes": shared_raw,
            },
            {
                "original_path": "Media/WhatsApp Images/IMG-STARRED.jpg",
                "archive_path": "Contacts/Alice (0015550101)/IMG-STARRED.jpg",
                "raw_bytes": starred_raw,
            },
            {
                "original_path": "Media/WhatsApp Images/IMG-PRESERVED.jpg",
                "archive_path": "Contacts/Alice (0015550101)/IMG-PRESERVED.jpg",
                "raw_bytes": preserved_raw,
            },
            {
                "original_path": "Media/WhatsApp Images/IMG-BOB.jpg",
                "archive_path": "Contacts/Bob (0015550102)/IMG-BOB.jpg",
                "raw_bytes": b"BOB-BYTES",
            },
        ],
        preserved_rows=[
            (
                "15550101@s.whatsapp.net",
                "k6",
                "Media/WhatsApp Images/IMG-PRESERVED.jpg",
                "Contacts/Alice (0015550101)/IMG-PRESERVED.jpg",
            )
        ],
    )
    return archive_dir, {
        "img_raw": img_raw,
        "vid_raw": vid_raw,
        "ptt_raw": ptt_raw,
        "shared_raw": shared_raw,
        "starred_raw": starred_raw,
        "preserved_raw": preserved_raw,
    }


def _write_config(
    cfg_path: Path,
    archive_dir: Path,
    *,
    chat_id: str | None = "15550101@s.whatsapp.net",
    chat_name: str | None = "Alice 🌸",
    sweep_shared: bool = False,
    sweep_preserved: bool = False,
    exclude_types: list[str] | None = None,
    android_user_id: int = 0,
) -> Path:
    lines = [
        "[general]",
        f'archive = "{archive_dir.as_posix()}"',
        "dry_run = true",
        f"android_user_id = {android_user_id}",
        "",
        "[sweep]",
        'older_than = "1y"',
        "protect_starred = true",
        f"sweep_preserved = {'true' if sweep_preserved else 'false'}",
    ]
    if exclude_types:
        quoted = ", ".join(f'"{t}"' for t in exclude_types)
        lines.append(f"exclude_types = [{quoted}]")
    if chat_id and chat_name:
        lines.extend(
            [
                "",
                "[sweep.chat]",
                f'id = "{chat_id}"',
                f'name = "{chat_name}"',
                f"sweep_shared = {'true' if sweep_shared else 'false'}",
            ]
        )
    cfg_path.write_text("\n".join(lines), encoding="utf-8")
    return cfg_path


class TestFourGatePipelineAndSafety:
    def test_unarchived_missing_and_hash_mismatch_protected(self, tmp_path: Path, monkeypatch):
        archive_dir, blobs = _base_archive(tmp_path, write_disk=False)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        # IMG-001 has write_disk=False -> Gate 2 missing_archive_file
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-001.jpg", blobs["img_raw"])
        # VID-002 has modified bytes on phone -> Gate 4 hash_mismatch
        dev.put_file(f"{media_root}/WhatsApp Video/VID-002.mp4", b"CORRUPTED-OR-MODIFIED-VID")
        # PTT-003 is valid -> eligible
        dev.put_file(f"{media_root}/WhatsApp Voice Notes/202037/PTT-003.opus", blobs["ptt_raw"])
        # Unarchived file on phone -> Gate 1 not_in_archive
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-UNARCHIVED.jpg", b"BRAND-NEW-PHOTO")

        cfg = load_config(str(_write_config(tmp_path / "config.toml", archive_dir)))
        plan = plan_sweep(cfg, _make_logger())

        eligible_paths = {c.remote_path for c in plan.candidates}
        assert eligible_paths == {f"{media_root}/WhatsApp Voice Notes/202037/PTT-003.opus"}

        reasons = {s.remote_path.rsplit("/", 1)[-1]: s.reason for s in plan.skipped}
        assert reasons["IMG-001.jpg"] == "missing_archive_file"
        assert reasons["VID-002.mp4"] == "hash_mismatch"
        assert reasons["IMG-UNARCHIVED.jpg"] == "not_in_archive"

    def test_exif_stamped_passes_gate4_via_files_md5(self, tmp_path: Path, monkeypatch):
        archive_dir, blobs = _base_archive(tmp_path, exif_stamped=1, write_disk=True)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        # Phone holds original unstamped bytes; archive disk holds larger EXIF-stamped bytes
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-001.jpg", blobs["img_raw"])

        cfg = load_config(str(_write_config(tmp_path / "config.toml", archive_dir)))
        plan = plan_sweep(cfg, _make_logger())

        assert [c.remote_path for c in plan.candidates] == [
            f"{media_root}/WhatsApp Images/IMG-001.jpg"
        ]

    def test_shared_starred_preserved_and_voice_note_exclusion(self, tmp_path: Path, monkeypatch):
        archive_dir, blobs = _base_archive(tmp_path)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        media_root = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media"
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-001.jpg", blobs["img_raw"])
        dev.put_file(f"{media_root}/WhatsApp Voice Notes/202037/PTT-003.opus", blobs["ptt_raw"])
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-SHARED.jpg", blobs["shared_raw"])
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-STARRED.jpg", blobs["starred_raw"])
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-PRESERVED.jpg", blobs["preserved_raw"])

        # Default: shared protected, starred protected, preserved protected, voice_note included
        cfg = load_config(str(_write_config(tmp_path / "config.toml", archive_dir)))
        plan = plan_sweep(cfg, _make_logger())
        eligible = {c.remote_path.rsplit("/", 1)[-1] for c in plan.candidates}
        assert eligible == {"IMG-001.jpg", "PTT-003.opus"}

        reasons = {s.remote_path.rsplit("/", 1)[-1]: s.reason for s in plan.skipped}
        assert reasons["IMG-SHARED.jpg"] == "shared_with_other_chat"
        assert reasons["IMG-STARRED.jpg"] == "starred_message"
        assert reasons["IMG-PRESERVED.jpg"] == "preserved_message_protected"

        # Now override sweep_shared=true, sweep_preserved=true, exclude voice_note
        cfg_override = load_config(
            str(
                _write_config(
                    tmp_path / "config2.toml",
                    archive_dir,
                    sweep_shared=True,
                    sweep_preserved=True,
                    exclude_types=["voice_note"],
                )
            )
        )
        plan2 = plan_sweep(cfg_override, _make_logger())
        eligible2 = {c.remote_path.rsplit("/", 1)[-1] for c in plan2.candidates}
        assert eligible2 == {"IMG-001.jpg", "IMG-SHARED.jpg", "IMG-PRESERVED.jpg"}


class TestChatDiscoveryAndSafetyHandshake:
    def test_list_chats_search_and_lid_resolution(self, tmp_path: Path):
        archive_dir, _ = _base_archive(tmp_path)

        family = list_archive_chats(str(archive_dir), "family")
        assert len(family) == 1
        assert family[0].jid == "120363000000000001@g.us"
        assert family[0].display_name == "Family Group 🏡"
        assert family[0].is_group is True

        bob = list_archive_chats(str(archive_dir), "bob")
        assert len(bob) == 1
        assert bob[0].jid == "15550102@s.whatsapp.net"
        assert bob[0].display_name == "Bob"

    def test_safety_handshake_nfc_and_mismatch_abort(self, tmp_path: Path, monkeypatch):
        archive_dir, _ = _base_archive(tmp_path)
        dev = FakeAndroidDevice(tmp_path / "phone")
        install_fake_device(monkeypatch, dev)

        # NFD decomposed name should still pass NFC normalization
        nfd_name = unicodedata.normalize("NFD", "Alice 🌸")
        cfg_ok = load_config(
            str(
                _write_config(
                    tmp_path / "cfg_ok.toml",
                    archive_dir,
                    chat_id="15550101@s.whatsapp.net",
                    chat_name=nfd_name,
                )
            )
        )
        plan_ok = plan_sweep(cfg_ok, _make_logger())
        assert plan_ok.device_serial == "SIM-DEV-001"

        # Wrong name for JID must abort immediately
        cfg_bad = load_config(
            str(
                _write_config(
                    tmp_path / "cfg_bad.toml",
                    archive_dir,
                    chat_id="15550101@s.whatsapp.net",
                    chat_name="Family Group 🏡",
                )
            )
        )
        with pytest.raises(ValueError, match="SAFETY HANDSHAKE FAILED"):
            plan_sweep(cfg_bad, _make_logger())


class TestDeviceFlavorsAndMd5Formats:
    def test_android9_legacy_path_and_dual_messenger_user_999(self, tmp_path: Path, monkeypatch):
        archive_dir, blobs = _base_archive(tmp_path)
        dev = FakeAndroidDevice(tmp_path / "phone", sdk_level=28)
        install_fake_device(monkeypatch, dev)

        # Android 9/10 + Dual Messenger user 999
        media_root = "/storage/emulated/999/WhatsApp/Media"
        dev.put_file(f"{media_root}/WhatsApp Images/IMG-001.jpg", blobs["img_raw"])

        cfg = load_config(
            str(_write_config(tmp_path / "cfg9.toml", archive_dir, android_user_id=999))
        )
        plan = plan_sweep(cfg, _make_logger())
        assert plan.media_root == media_root
        assert len(plan.candidates) == 1

    def test_parse_toybox_and_gnu_md5_formats(self):
        expected = bytes.fromhex("d41d8cd98f00b204e9800998ecf8427e")
        assert adb.parse_md5_output("d41d8cd98f00b204e9800998ecf8427e  /sdcard/a.jpg") == expected
        assert (
            adb.parse_md5_output("MD5 (/sdcard/a.jpg) = d41d8cd98f00b204e9800998ecf8427e")
            == expected
        )
