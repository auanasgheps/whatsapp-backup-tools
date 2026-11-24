"""Tests for wab_archiver."""

from __future__ import annotations

import argparse
import os
import plistlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import wab_archiver.backup_reader as br
import wab_archiver.main as wa
from tests.archiver.conftest import (
    _create_synthetic_crypt15,
    _requires_wa_crypt_tools,
)


class TestDetectDbPlatform:
    def test_ios_db_detected(self, tmp_path):
        db_path = str(tmp_path / "ios.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert wa.detect_db_platform(db_path) == "ios"

    def test_android_db_detected(self, tmp_path):
        db_path = str(tmp_path / "android.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE message (_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert wa.detect_db_platform(db_path) == "android"


class TestDetectEncrypted:
    def test_unencrypted_manifest_plist_returns_false(self, tmp_path, logger):
        with open(tmp_path / "Manifest.plist", "wb") as f:
            plistlib.dump({"IsEncrypted": False}, f)
        assert br.detect_encrypted(str(tmp_path), logger) is False

    def test_encrypted_manifest_plist_returns_true(self, tmp_path, logger):
        with open(tmp_path / "Manifest.plist", "wb") as f:
            plistlib.dump({"IsEncrypted": True}, f)
        assert br.detect_encrypted(str(tmp_path), logger) is True

    def test_info_plist_without_flag_falls_back_to_manifest(self, tmp_path, logger):
        # Info.plist exists but has no IsEncrypted key → fall back to Manifest.plist
        with open(tmp_path / "Info.plist", "wb") as f:
            plistlib.dump({"DeviceName": "iPhone"}, f)
        with open(tmp_path / "Manifest.plist", "wb") as f:
            plistlib.dump({"IsEncrypted": True}, f)
        assert br.detect_encrypted(str(tmp_path), logger) is True

    def test_missing_plist_files_exits(self, tmp_path, logger):
        # No Info.plist or Manifest.plist → SystemExit
        with pytest.raises(SystemExit):
            br.detect_encrypted(str(tmp_path), logger)

    def test_nonexistent_directory_exits(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            br.detect_encrypted(str(tmp_path / "nonexistent"), logger)


class TestExtractToTemp:
    def test_copies_file_to_temp_and_returns_path(self, tmp_path, logger):
        hash_dir = tmp_path / "ab"
        hash_dir.mkdir()
        fake_file = hash_dir / ("ab" + "c" * 38)
        fake_file.write_bytes(b"sqlite data")
        manifest_map = {"ChatStorage.sqlite": str(fake_file)}

        temp_path = br.extract_to_temp(manifest_map, "ChatStorage.sqlite", logger)
        try:
            assert os.path.isfile(temp_path)
            with open(temp_path, "rb") as f:
                assert f.read() == b"sqlite data"
        finally:
            os.unlink(temp_path)

    def test_preserves_file_extension(self, tmp_path, logger):
        src = tmp_path / "srcfile"
        src.write_bytes(b"data")
        manifest_map = {"ContactsV2.sqlite": str(src)}

        temp_path = br.extract_to_temp(manifest_map, "ContactsV2.sqlite", logger)
        try:
            assert temp_path.endswith(".sqlite")
        finally:
            os.unlink(temp_path)

    def test_file_not_in_manifest_exits(self, logger):
        with pytest.raises(SystemExit):
            br.extract_to_temp({}, "ChatStorage.sqlite", logger)

    def test_hash_file_missing_from_disk_exits(self, tmp_path, logger):
        manifest_map = {"ChatStorage.sqlite": str(tmp_path / "nonexistent_hash")}
        with pytest.raises(SystemExit):
            br.extract_to_temp(manifest_map, "ChatStorage.sqlite", logger)


class TestExtractPlaintext:
    def _make_backup(self, tmp_path):
        """Minimal fake backup: Manifest.db + hash files for ChatStorage and a media file."""
        import sqlite3 as _sq

        tmp_path.mkdir(parents=True, exist_ok=True)
        manifest_db = tmp_path / "Manifest.db"
        conn = _sq.connect(str(manifest_db))
        conn.execute("""
            CREATE TABLE Files (
                fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER
            )
        """)
        # ChatStorage.sqlite
        chat_id = "aa" + "b" * 38
        conn.execute(
            "INSERT INTO Files VALUES (?,?,?,1)",
            (chat_id, "AppDomainGroup-group.net.whatsapp.WhatsApp.shared", "ChatStorage.sqlite"),
        )
        # Media file
        media_id = "cc" + "d" * 38
        conn.execute(
            "INSERT INTO Files VALUES (?,?,?,1)",
            (
                media_id,
                "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
                "Message/Media/WhatsApp Images/IMG001.jpg",
            ),
        )
        conn.commit()
        conn.close()
        # Create hash directories and files
        for fid, content in [(chat_id, b"SQLITE"), (media_id, b"JPEGDATA")]:
            d = tmp_path / fid[:2]
            d.mkdir(exist_ok=True)
            (d / fid).write_bytes(content)
        return tmp_path

    def test_returns_manifest_map_msgstore_path(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / "backup")
        output_dir = tmp_path / "output"
        manifest_map, msgstore_path, contacts_path = br.extract_plaintext(
            str(backup_dir), str(output_dir), None, False, logger
        )
        assert "ChatStorage.sqlite" in manifest_map
        assert "Message/Media/WhatsApp Images/IMG001.jpg" in manifest_map
        assert os.path.isfile(msgstore_path)
        assert msgstore_path == str(output_dir / "Whatsapp Databases" / "ChatStorage.sqlite")

    def test_contacts_none_when_not_in_backup(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / "backup")
        output_dir = tmp_path / "output"
        _, _, contacts_path = br.extract_plaintext(
            str(backup_dir), str(output_dir), None, False, logger
        )
        assert contacts_path is None

    def test_contacts_override_respected(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / "backup")
        output_dir = tmp_path / "output"
        fake_contacts = str(tmp_path / "contacts.sqlite")
        _, _, contacts_path = br.extract_plaintext(
            str(backup_dir), str(output_dir), fake_contacts, False, logger
        )
        assert contacts_path == fake_contacts

    def test_overwrite_debug_logged(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / "backup")
        output_dir = tmp_path / "output"
        db_dir = output_dir / "Whatsapp Databases"
        db_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create the DB file so overwrite warning fires
        (db_dir / "ChatStorage.sqlite").write_bytes(b"OLD")
        import unittest.mock as mock

        with mock.patch.object(logger, "debug") as mock_debug:
            br.extract_plaintext(str(backup_dir), str(output_dir), None, False, logger)
        debug_msgs = " ".join(str(c) for c in mock_debug.call_args_list)
        assert "Overwriting" in debug_msgs or "overwriting" in debug_msgs.lower()


class TestExtractEncrypted:
    def _make_mock_backup(self, tmp_path):
        """Return a mock EncryptedBackup that extracts fake files."""
        import unittest.mock as mock

        def fake_extract_file(*, relative_path, domain_like, output_filename):
            if relative_path == "ChatStorage.sqlite":
                with open(output_filename, "wb") as f:
                    f.write(b"SQLITE")
            elif relative_path == "ContactsV2.sqlite":
                with open(output_filename, "wb") as f:
                    f.write(b"CONTACTS")
            elif relative_path == "Message/Media/WhatsApp Images/IMG001.jpg":
                with open(output_filename, "wb") as f:
                    f.write(b"MEDIAFILE")
            else:
                raise FileNotFoundError(relative_path)

        mock_backup = mock.MagicMock()
        mock_backup.test_decryption.return_value = True
        mock_backup.extract_file.side_effect = fake_extract_file
        return mock_backup

    def _patch_library(self, mock_backup):
        """
        Return a context manager that injects a fake iphone_backup_decrypt
        module into sys.modules. Works whether or not the real package is
        installed (avoids ModuleNotFoundError on CI without the package).
        """
        import sys
        import types
        import unittest.mock as mock

        fake_mod = types.ModuleType("iphone_backup_decrypt")
        fake_mod.EncryptedBackup = mock.MagicMock(return_value=mock_backup)
        fake_mod.DomainLike = mock.MagicMock()
        return mock.patch.dict(sys.modules, {"iphone_backup_decrypt": fake_mod})

    def test_returns_resolver_and_msgstore(self, tmp_path, logger):
        mock_backup = self._make_mock_backup(tmp_path)
        output_dir = tmp_path / "output"

        with self._patch_library(mock_backup):
            msgstore_path, contacts_path, resolver = br.extract_encrypted(
                str(tmp_path / "backup"),
                "secret",
                str(output_dir),
                None,
                False,
                logger,
            )

        assert os.path.isfile(msgstore_path)
        assert msgstore_path == str(output_dir / "Whatsapp Databases" / "ChatStorage.sqlite")
        assert contacts_path == str(output_dir / "Whatsapp Databases" / "ContactsV2.sqlite")
        # Resolver decrypts on demand
        media_path = resolver("Message/Media/WhatsApp Images/IMG001.jpg")
        assert media_path is not None
        assert os.path.isfile(media_path)
        # Second call returns the cached path without re-decrypting
        mock_backup.extract_file.reset_mock()
        media_path2 = resolver("Message/Media/WhatsApp Images/IMG001.jpg")
        assert media_path2 == media_path
        mock_backup.extract_file.assert_not_called()

    def test_resolver_returns_none_for_missing_file(self, tmp_path, logger):
        mock_backup = self._make_mock_backup(tmp_path)
        output_dir = tmp_path / "output"

        with self._patch_library(mock_backup):
            _, _, resolver = br.extract_encrypted(
                str(tmp_path / "backup"),
                "secret",
                str(output_dir),
                None,
                False,
                logger,
            )

        result = resolver("Message/Media/nonexistent.jpg")
        assert result is None

    def test_wrong_password_exits(self, tmp_path, logger):
        import unittest.mock as mock

        mock_backup = mock.MagicMock()
        mock_backup.test_decryption.side_effect = ValueError("incorrect passphrase")
        output_dir = tmp_path / "output"

        with self._patch_library(mock_backup):
            with pytest.raises(SystemExit):
                br.extract_encrypted(
                    str(tmp_path / "backup"),
                    "wrongpw",
                    str(output_dir),
                    None,
                    False,
                    logger,
                )

    def test_missing_library_exits(self, tmp_path, logger):
        import sys
        import unittest.mock as mock

        output_dir = tmp_path / "output"

        with mock.patch.dict(sys.modules, {"iphone_backup_decrypt": None}):
            with pytest.raises(SystemExit):
                br.extract_encrypted(
                    str(tmp_path / "backup"),
                    "secret",
                    str(output_dir),
                    None,
                    False,
                    logger,
                )

    def test_contacts_override_skips_extraction(self, tmp_path, logger):
        mock_backup = self._make_mock_backup(tmp_path)
        output_dir = tmp_path / "output"
        fake_contacts = str(tmp_path / "my_contacts.sqlite")

        with self._patch_library(mock_backup):
            _, contacts_path, _ = br.extract_encrypted(
                str(tmp_path / "backup"),
                "secret",
                str(output_dir),
                fake_contacts,
                False,
                logger,
            )

        assert contacts_path == fake_contacts
        # extract_file should only have been called for ChatStorage, not ContactsV2
        calls = [c.kwargs.get("relative_path") for c in mock_backup.extract_file.call_args_list]
        assert "ContactsV2.sqlite" not in calls


class TestDecryptMsgstore:
    @pytest.fixture
    def synthetic_db(self, tmp_path):
        db_path = tmp_path / "plain.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, msg TEXT)")
        conn.execute("INSERT INTO test VALUES (1, 'synthetic_test_value')")
        conn.commit()
        conn.close()
        return db_path.read_bytes()

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_success_with_hex_key(self, tmp_path, logger, synthetic_db):
        key_hex = "11" * 32
        encrypted_bytes = _create_synthetic_crypt15(synthetic_db, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)

        output_path = tmp_path / "out" / "msgstore.db"
        res = wa.decrypt_msgstore(str(crypt_path), key_hex, str(output_path), logger)

        assert res == str(output_path)
        assert output_path.exists()
        assert output_path.read_bytes() == synthetic_db

        conn = sqlite3.connect(str(output_path))
        row = conn.execute("SELECT msg FROM test WHERE id = 1").fetchone()
        assert row[0] == "synthetic_test_value"
        conn.close()

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_success_with_binary_key_file(self, tmp_path, logger, synthetic_db):
        from wa_crypt_tools.lib.key.key15 import Key15

        key_hex = "11" * 32
        encrypted_bytes = _create_synthetic_crypt15(synthetic_db, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)

        key_file = tmp_path / "encrypted_backup.key"
        key_obj = Key15(keyarray=bytes.fromhex(key_hex))
        key_obj.file_dump(key_file)

        output_path = tmp_path / "msgstore.db"
        res = wa.decrypt_msgstore(str(crypt_path), str(key_file), str(output_path), logger)

        assert res == str(output_path)
        assert output_path.read_bytes() == synthetic_db

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_success_with_text_hex_key_file(self, tmp_path, logger, synthetic_db):
        key_hex = "11" * 32
        encrypted_bytes = _create_synthetic_crypt15(synthetic_db, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)

        key_txt = tmp_path / "hex_key.txt"
        key_txt.write_text(f"  {key_hex}\n", encoding="utf-8")

        output_path = tmp_path / "msgstore.db"
        res = wa.decrypt_msgstore(str(crypt_path), str(key_txt), str(output_path), logger)

        assert res == str(output_path)
        assert output_path.read_bytes() == synthetic_db

    def test_decrypt_msgstore_missing_dependency(self, tmp_path, logger):
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(b"dummy")
        output_path = tmp_path / "msgstore.db"

        with patch.dict(sys.modules, {"wa_crypt_tools.lib.db.dbfactory": None}):
            with pytest.raises(SystemExit):
                wa.decrypt_msgstore(str(crypt_path), "11" * 32, str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_invalid_key_raises(self, tmp_path, logger, synthetic_db):
        key_hex = "11" * 32
        encrypted_bytes = _create_synthetic_crypt15(synthetic_db, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)
        output_path = tmp_path / "msgstore.db"

        with pytest.raises(SystemExit):
            wa.decrypt_msgstore(str(crypt_path), "invalid_key_string", str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_corrupt_file_raises(self, tmp_path, logger):
        crypt_path = tmp_path / "corrupted.crypt15"
        crypt_path.write_bytes(b"")
        output_path = tmp_path / "msgstore.db"

        with pytest.raises(SystemExit):
            wa.decrypt_msgstore(str(crypt_path), "11" * 32, str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_wrong_key_fails(self, tmp_path, logger, synthetic_db):
        key_hex = "11" * 32
        wrong_key_hex = "22" * 32
        encrypted_bytes = _create_synthetic_crypt15(synthetic_db, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)
        output_path = tmp_path / "msgstore.db"

        with pytest.raises(SystemExit):
            wa.decrypt_msgstore(str(crypt_path), wrong_key_hex, str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_overwrites_existing_warning(self, tmp_path, logger, synthetic_db):
        key_hex = "11" * 32
        encrypted_bytes = _create_synthetic_crypt15(synthetic_db, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)

        output_path = tmp_path / "msgstore.db"
        output_path.write_bytes(b"prior_existing_bytes")

        with patch.object(logger, "warning") as mock_warn:
            wa.decrypt_msgstore(str(crypt_path), key_hex, str(output_path), logger)
            assert output_path.read_bytes() == synthetic_db
            mock_warn.assert_called_once()
            assert "Overwriting existing" in mock_warn.call_args[0][0]

    @_requires_wa_crypt_tools
    def test_decrypt_key_candidate_oserror(self, tmp_path, logger):
        key_file = tmp_path / "inaccessible.key"
        key_file.write_text("something", encoding="utf-8")
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(b"dummy")
        output_path = tmp_path / "msgstore.db"

        real_open = open

        def mock_open_fn(file, *args, **kwargs):
            if str(file) == str(key_file):
                raise OSError("Permission denied")
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_fn):
            with pytest.raises(SystemExit):
                wa.decrypt_msgstore(str(crypt_path), str(key_file), str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_db_factory_returns_none(self, tmp_path, logger):
        key_hex = "11" * 32
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(b"some_bytes")
        output_path = tmp_path / "msgstore.db"

        with patch("wa_crypt_tools.lib.db.dbfactory.DatabaseFactory.from_file", return_value=None):
            with pytest.raises(SystemExit):
                wa.decrypt_msgstore(str(crypt_path), key_hex, str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_msgstore_read_oserror(self, tmp_path, logger):
        key_hex = "11" * 32
        crypt_path = tmp_path / "nonexistent.crypt15"
        output_path = tmp_path / "msgstore.db"

        with pytest.raises(SystemExit):
            wa.decrypt_msgstore(str(crypt_path), key_hex, str(output_path), logger)

    @_requires_wa_crypt_tools
    def test_decrypt_payload_decrypt_exception(self, tmp_path, logger):
        key_hex = "11" * 32
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(b"some_bytes")
        output_path = tmp_path / "msgstore.db"

        mock_db = MagicMock()
        mock_db.decrypt.side_effect = RuntimeError("Crypto error")
        with patch(
            "wa_crypt_tools.lib.db.dbfactory.DatabaseFactory.from_file", return_value=mock_db
        ):
            with pytest.raises(SystemExit):
                wa.decrypt_msgstore(str(crypt_path), key_hex, str(output_path), logger)


class TestPrepareInputDecryption:
    def test_prepare_input_missing_e2e_key_raises(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=None,
            from_adb=False,
            contacts=None,
            msgstore="msgstore.db.crypt15",
            e2e_key=None,
            output=str(tmp_path),
        )
        with pytest.raises(SystemExit):
            wa._prepare_input(args, None, logger)

    @_requires_wa_crypt_tools
    def test_prepare_input_calls_decrypt_msgstore(self, tmp_path, logger):
        db_path = tmp_path / "plain.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO test VALUES (1, 'val1')")
        conn.commit()
        conn.close()
        plain_bytes = db_path.read_bytes()

        key_hex = "11" * 32
        encrypted_bytes = _create_synthetic_crypt15(plain_bytes, key_hex)
        crypt_path = tmp_path / "msgstore.db.crypt15"
        crypt_path.write_bytes(encrypted_bytes)

        args = argparse.Namespace(
            ios_backup=None,
            from_adb=False,
            contacts=None,
            msgstore=str(crypt_path),
            e2e_key=key_hex,
            output=str(tmp_path),
        )

        platform, resolver, ios_contacts, contacts = wa._prepare_input(args, None, logger)
        expected_db = os.path.join(str(tmp_path), "Whatsapp Databases", "msgstore.db")
        assert args.msgstore == expected_db
        assert os.path.isfile(expected_db)
        with open(expected_db, "rb") as f:
            assert f.read() == plain_bytes
