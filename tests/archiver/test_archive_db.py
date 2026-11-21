"""Tests for wab_archiver."""

from __future__ import annotations

import hashlib
import os
import sqlite3

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


import wab_archiver.archive_db as arc


class TestRecordFileArchived:
    def test_creates_file_and_copy_records(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = b"\x01" * 16
        arc.record_file_archived(
            cursor,
            "orig.jpg",
            md5,
            "Contacts/Alice (00111)/2024/Received/orig.jpg",
            1024,
            0,
            1700000000000,
        )
        conn.commit()
        row = conn.execute(
            "SELECT md5, size FROM files WHERE original_path = 'orig.jpg'"
        ).fetchone()
        assert row[0] == md5
        assert row[1] == 1024
        copy = conn.execute(
            "SELECT archive_path, timestamp_ms FROM archive_copies WHERE original_path = 'orig.jpg'"
        ).fetchone()
        assert "Contacts/Alice (00111)/2024/Received/orig.jpg" == copy[0]
        assert copy[1] == 1700000000000
        conn.close()

    def test_duplicate_archive_path_silently_ignored(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = b"\x01" * 16
        arc.record_file_archived(cursor, "orig.jpg", md5, "Contacts/path.jpg", 512, 0, None)
        arc.record_file_archived(cursor, "orig.jpg", md5, "Contacts/path.jpg", 512, 0, None)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_copies WHERE original_path = 'orig.jpg'"
        ).fetchone()[0]
        assert count == 1
        conn.close()

    def test_multiple_archive_paths_for_same_original(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = b"\x01" * 16
        arc.record_file_archived(cursor, "orig.jpg", md5, "path1.jpg", 256, 0, None)
        arc.record_file_archived(cursor, "orig.jpg", md5, "path2.jpg", 256, 0, None)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_copies WHERE original_path = 'orig.jpg'"
        ).fetchone()[0]
        assert count == 2
        conn.close()


class TestCheckDbHealth:
    def test_healthy_db_passes(self, tmp_path, logger):
        conn = arc.open_archive_db(str(tmp_path))
        arc.check_db_health(conn, logger)  # should not raise
        conn.close()

    def test_archive_db_user_version_and_reactions_cache(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == 3
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        assert auto_vacuum == 2  # INCREMENTAL
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "reactions_cache" in tables
        assert "recent_messages" in tables
        conn.close()


class TestContactAndGroupPersistence:
    def test_save_and_load_contacts_round_trip(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        index = {"391234567890": ("Alice (00391234567890)", "Alice")}
        arc.save_contacts_to_db(conn, index)
        conn.commit()
        loaded = arc.load_contacts_from_db(conn)
        conn.close()
        assert loaded == index

    def test_update_existing_contact(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        arc.save_contacts_to_db(conn, {"111": ("Old Name (00111)", "Old Name")})
        arc.save_contacts_to_db(conn, {"111": ("New Name (00111)", "New Name")})
        conn.commit()
        loaded = arc.load_contacts_from_db(conn)
        conn.close()
        assert loaded["111"] == ("New Name (00111)", "New Name")

    def test_save_and_load_groups_round_trip(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        index = {"42": {"folder": "Family Chat", "subject": "Family Chat"}}
        arc.save_groups_to_db(conn, index)
        conn.commit()
        loaded = arc.load_groups_from_db(conn)
        conn.close()
        assert loaded == index


class TestArchiveDbMigrationV2:
    def test_v1_to_v2_migration_adds_is_hardlink_and_updates_version(self, tmp_path):
        db_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE files (
                original_path TEXT PRIMARY KEY,
                md5 BLOB NOT NULL,
                size INTEGER
            );
            CREATE TABLE archive_copies (
                original_path TEXT NOT NULL REFERENCES files(original_path),
                archive_path TEXT NOT NULL,
                PRIMARY KEY (original_path, archive_path)
            );
            PRAGMA user_version = 1;
        """)
        conn.execute(
            "INSERT INTO files VALUES ('Media/1.jpg', X'0102030405060708090a0b0c0d0e0f10', 100)"
        )
        conn.execute("INSERT INTO archive_copies VALUES ('Media/1.jpg', 'Contacts/Alice/1.jpg')")
        conn.commit()
        conn.close()

        migrated_conn = arc.open_archive_db(str(tmp_path))
        assert migrated_conn.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {r[1] for r in migrated_conn.execute("PRAGMA table_info(archive_copies)").fetchall()}
        assert "is_hardlink" in cols
        recent_cols = {
            r[1] for r in migrated_conn.execute("PRAGMA table_info(recent_messages)").fetchall()
        }
        assert "is_view_once" in recent_cols

        row = migrated_conn.execute(
            "SELECT original_path, archive_path, is_hardlink FROM archive_copies"
        ).fetchone()
        assert row == ("Media/1.jpg", "Contacts/Alice/1.jpg", 0)
        migrated_conn.close()

    def test_fresh_db_is_v3_with_is_hardlink_and_view_once(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {r[1] for r in conn.execute("PRAGMA table_info(archive_copies)").fetchall()}
        assert "is_hardlink" in cols
        recent_cols = {r[1] for r in conn.execute("PRAGMA table_info(recent_messages)").fetchall()}
        assert "is_view_once" in recent_cols
        conn.close()

    def test_archive_db_migration_v2_to_v3_adds_is_view_once(self, tmp_path):
        db_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE files (
                original_path TEXT PRIMARY KEY,
                md5 BLOB NOT NULL,
                size INTEGER
            );
            CREATE TABLE archive_copies (
                original_path TEXT NOT NULL REFERENCES files(original_path),
                archive_path TEXT NOT NULL,
                is_hardlink INTEGER NOT NULL DEFAULT 0,
                timestamp_ms INTEGER,
                PRIMARY KEY (original_path, archive_path)
            );
            CREATE TABLE recent_messages (
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                sender TEXT NOT NULL,
                from_me INTEGER NOT NULL,
                archive_path TEXT,
                media_type TEXT NOT NULL DEFAULT 'text',
                media_name TEXT,
                text_body TEXT NOT NULL,
                quoted_text TEXT,
                quoted_sender TEXT,
                quoted_ts INTEGER,
                reactions TEXT,
                is_edited INTEGER NOT NULL DEFAULT 0,
                edited_ts INTEGER,
                PRIMARY KEY (chat_id, chat_type, msg_id)
            );
            PRAGMA user_version = 2;
        """)
        conn.execute(
            "INSERT INTO recent_messages VALUES ('10', 'contact', 1, 1000, 'Alice', 0, NULL, 'text', NULL, 'Hi', NULL, NULL, NULL, NULL, 0, NULL)"
        )
        conn.commit()
        conn.close()

        migrated = arc.open_archive_db(str(tmp_path))
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {r[1] for r in migrated.execute("PRAGMA table_info(recent_messages)").fetchall()}
        assert "is_view_once" in cols
        row = migrated.execute(
            "SELECT msg_id, is_view_once FROM recent_messages WHERE msg_id = 1"
        ).fetchone()
        assert row == (1, 0)
        migrated.close()

    def test_load_canonical_copies_retrieves_existing_files(self, tmp_path):
        conn = arc.open_archive_db(str(tmp_path))
        md5 = hashlib.md5(b"payload").digest()
        file1 = tmp_path / "Contacts" / "Alice" / "img.jpg"
        file1.parent.mkdir(parents=True, exist_ok=True)
        file1.write_bytes(b"payload")

        cur = conn.cursor()
        arc.record_file_archived(cur, "Media/img.jpg", md5, "Contacts/Alice/img.jpg", 7, 0, None)
        # Add a missing file to verify it's not treated as canonical
        arc.record_file_archived(
            cur, "Media/missing.jpg", b"fake", "Contacts/Bob/missing.jpg", 10, 0, None
        )
        conn.commit()

        canonical_map = arc.load_canonical_copies(conn, str(tmp_path))
        assert md5 in canonical_map
        assert canonical_map[md5] == str(file1)
        assert b"fake" not in canonical_map
        conn.close()


class TestSyncFolderNames:
    def test_unchanged_name_leaves_folder_intact(self, tmp_path, logger):
        contacts_root = tmp_path / "Contacts"
        contacts_root.mkdir()
        (contacts_root / "Alice (00111)").mkdir()
        folder_index = {"111": ("Alice (00111)", "Alice")}
        contacts = {"111": "Alice"}
        result = arc.sync_folder_names(contacts, {}, str(tmp_path), folder_index, logger)
        assert result["111"][0] == "Alice (00111)"
        assert (contacts_root / "Alice (00111)").exists()

    def test_changed_name_renames_folder_on_disk(self, tmp_path, logger):
        contacts_root = tmp_path / "Contacts"
        contacts_root.mkdir()
        (contacts_root / "OldName (00111)").mkdir()
        folder_index = {"111": ("OldName (00111)", "OldName")}
        contacts = {"111": "NewName"}
        result = arc.sync_folder_names(contacts, {}, str(tmp_path), folder_index, logger)
        assert result["111"][0] == "NewName (00111)"
        assert not (contacts_root / "OldName (00111)").exists()
        assert (contacts_root / "NewName (00111)").exists()

    def test_rename_skipped_when_target_exists_index_updated(self, tmp_path, logger):
        contacts_root = tmp_path / "Contacts"
        contacts_root.mkdir()
        (contacts_root / "OldName (00111)").mkdir()
        (contacts_root / "NewName (00111)").mkdir()  # target already exists
        folder_index = {"111": ("OldName (00111)", "OldName")}
        contacts = {"111": "NewName"}
        result = arc.sync_folder_names(contacts, {}, str(tmp_path), folder_index, logger)
        # Index is updated to the new name even though the on-disk rename was skipped,
        # so future runs don't re-detect the mismatch and re-fire the warning.
        assert result["111"][0] == "NewName (00111)"

    def test_name_changed_no_folder_on_disk_index_updated(self, tmp_path, logger):
        # Name changed but folder has never been created; index should update anyway
        folder_index = {"111": ("OldName (00111)", "OldName")}
        contacts = {"111": "NewName"}
        result = arc.sync_folder_names(contacts, {}, str(tmp_path), folder_index, logger)
        assert result["111"][0] == "NewName (00111)"

    def test_new_contact_registered_in_index(self, tmp_path, logger):
        result = arc.sync_folder_names({"111": "Alice"}, {}, str(tmp_path), {}, logger)
        assert "111" in result

    def test_number_map_resolves_old_to_canonical(self, tmp_path, logger):
        # Contact is known by old number '111'; canonical is '222'
        contacts = {"111": "Alice"}
        number_map = {"111": "222"}
        result = arc.sync_folder_names(contacts, number_map, str(tmp_path), {}, logger)
        # Registered under canonical '222', folder uses canonical number
        assert "222" in result
        assert result["222"][0] == "Alice (00222)"


class TestSyncGroupNames:
    def test_unchanged_subject_leaves_folder_intact(self, tmp_path, logger):
        groups_root = tmp_path / "Groups"
        groups_root.mkdir()
        (groups_root / "Family").mkdir()
        group_index = {"42": {"folder": "Family", "subject": "Family"}}
        result = arc.sync_group_names({"42": "Family"}, str(tmp_path), group_index, logger)
        assert result["42"]["folder"] == "Family"
        assert (groups_root / "Family").exists()

    def test_changed_subject_renames_folder_on_disk(self, tmp_path, logger):
        groups_root = tmp_path / "Groups"
        groups_root.mkdir()
        (groups_root / "OldName").mkdir()
        group_index = {"42": {"folder": "OldName", "subject": "OldName"}}
        result = arc.sync_group_names({"42": "NewName"}, str(tmp_path), group_index, logger)
        assert result["42"]["folder"] == "NewName"
        assert not (groups_root / "OldName").exists()
        assert (groups_root / "NewName").exists()

    def test_rename_skipped_when_target_exists_index_updated(self, tmp_path, logger):
        groups_root = tmp_path / "Groups"
        groups_root.mkdir()
        (groups_root / "OldName").mkdir()
        (groups_root / "NewName").mkdir()  # target already exists
        group_index = {"42": {"folder": "OldName", "subject": "OldName"}}
        result = arc.sync_group_names({"42": "NewName"}, str(tmp_path), group_index, logger)
        # Index is updated even when the on-disk rename is skipped,
        # so future runs don't re-detect the mismatch and repeat the warning.
        assert result["42"]["folder"] == "NewName"

    def test_new_group_not_in_index_is_skipped(self, tmp_path, logger):
        # New groups are not yet in group_index; sync_group_names skips them
        result = arc.sync_group_names({"99": "Brand New Group"}, str(tmp_path), {}, logger)
        assert "99" not in result


class TestResolveGroupFolder:
    def test_new_group_assigned_sanitized_name(self):
        index = {}
        folder = arc.resolve_group_folder("42", "Family Chat", index)
        assert folder == "Family Chat"
        assert index["42"]["folder"] == "Family Chat"

    def test_existing_group_returns_stable_name(self):
        # Even if subject has changed, the stable folder from the index is returned.
        index = {"42": {"folder": "OldName", "subject": "OldName"}}
        folder = arc.resolve_group_folder("42", "NewName", index)
        assert folder == "OldName"

    def test_none_subject_gets_unknown_fallback(self):
        index = {}
        folder = arc.resolve_group_folder("99", None, index)
        assert "Unknown Group" in folder
        assert "99" in folder

    def test_name_collision_with_existing_group_disambiguated(self):
        index = {"10": {"folder": "Friends", "subject": "Friends"}}
        folder = arc.resolve_group_folder("20", "Friends", index)
        assert folder == "Friends (2)"

    def test_special_chars_in_subject_sanitized(self):
        index = {}
        folder = arc.resolve_group_folder("1", "Chat: Family/Work", index)
        assert "/" not in folder
        assert ":" not in folder
