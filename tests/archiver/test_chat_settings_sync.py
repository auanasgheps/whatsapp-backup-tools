"""Tests for optional chatsettingsbackup.db discovery, ADB pull, and sync."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from wab_archiver import adb_extractor
from wab_archiver import main as archiver_main


def test_pull_chat_settings_handles_missing_and_success(tmp_path):
    logger = logging.getLogger("test_chat_settings")
    with patch("wab_archiver.adb_extractor.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=1, stdout=b"", stderr=b"No such file")
        assert adb_extractor.pull_chat_settings(str(tmp_path), False, logger) is None

    def _fake_pull(cmd, check=False, capture_output=True):
        dest = cmd[3]
        with open(dest, "wb") as f:
            f.write(b"fake-crypt15")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with patch("wab_archiver.adb_extractor.subprocess.run", side_effect=_fake_pull):
        out = adb_extractor.pull_chat_settings(str(tmp_path), False, logger)
        assert out is not None
        assert out.endswith("chatsettingsbackup.db.crypt15")


def test_sync_chat_settings_db_copies_plaintext_and_decrypts_crypt15(tmp_path):
    logger = logging.getLogger("test_chat_settings_sync")
    backups_dir = tmp_path / "source" / "Backups"
    db_dir = tmp_path / "source" / "Databases"
    backups_dir.mkdir(parents=True)
    db_dir.mkdir(parents=True)
    msgstore = db_dir / "msgstore.db"
    msgstore.write_bytes(b"SQLite format 3\x00")

    # 1. Plaintext copy
    plain_src = backups_dir / "chatsettingsbackup.db"
    conn = sqlite3.connect(str(plain_src))
    conn.execute("CREATE TABLE settings (jid TEXT, pinned INTEGER, pinned_time INTEGER)")
    conn.execute("INSERT INTO settings VALUES ('15550101@s.whatsapp.net', 1, 1234)")
    conn.commit()
    conn.close()

    out_dir = tmp_path / "out"
    args = argparse.Namespace(output=str(out_dir), msgstore=str(msgstore), e2e_key=None)
    archiver_main._sync_chat_settings_db(args, logger)
    dest = out_dir / "Whatsapp Databases" / "chatsettingsbackup.db"
    assert dest.is_file()

    # 2. Encrypted .crypt15 without key skips gracefully
    plain_src.unlink()
    enc_src = backups_dir / "chatsettingsbackup.db.crypt15"
    enc_src.write_bytes(b"encrypted")
    dest.unlink()
    archiver_main._sync_chat_settings_db(args, logger)
    assert not dest.exists()

    # 3. Encrypted .crypt15 with key invokes decrypt_msgstore
    args.e2e_key = "0" * 64

    def _fake_decrypt(src, key, incoming, _logger):
        c = sqlite3.connect(incoming)
        c.execute("CREATE TABLE settings (jid TEXT, pinned INTEGER)")
        c.commit()
        c.close()
        return incoming

    with patch("wab_archiver.main.decrypt_msgstore", side_effect=_fake_decrypt):
        archiver_main._sync_chat_settings_db(args, logger)
    assert dest.is_file()
