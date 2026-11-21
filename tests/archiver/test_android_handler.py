"""Tests for wab_archiver."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import wab_archiver.android_handler as android_handler
import wab_archiver.main as wa
from tests.archiver.conftest import (
    _make_msgstore,
    _make_number_change_db,
)
from tests.conftest import make_android_db


class TestBuildQuery:
    def test_documents_included(self):
        query = android_handler.build_query(None, None, False)
        assert "Media/WhatsApp Documents/%" in query

    def test_all_media_types_present(self):
        query = android_handler.build_query(None, None, False)
        assert "Media/WhatsApp %" in query
        assert "Media/.Statuses/%" in query
        assert query.count("Media/WhatsApp %") == 2

    def test_stickers_included(self):
        assert "WhatsApp Stickers" in android_handler._MEDIA_SUBFOLDERS

    def test_limit_clause_included(self):
        query = android_handler.build_query(limit=100, since_ms=None, hd_dedup=False)
        assert "LIMIT 50" in query  # N//2 per block

    def test_no_limit_when_none(self):
        query = android_handler.build_query(limit=None, since_ms=None, hd_dedup=False)
        assert "LIMIT" not in query

    def test_since_clause_included(self):
        query = android_handler.build_query(limit=None, since_ms=1700000000000, hd_dedup=False)
        assert "1700000000000" in query

    def test_both_union_blocks_have_documents(self):
        query = android_handler.build_query(None, None, False)
        # 2 occurrences in CASE WHEN guards (one per union block)
        assert query.count("Media/WhatsApp Documents/%") == 2

    def test_limit_applies_to_combined_result(self):
        # LIMIT N//2 must appear twice (once per block), not on the outer wrapper
        query = android_handler.build_query(limit=50, since_ms=None, hd_dedup=False)
        assert query.count("LIMIT 25") == 2
        assert not query.strip().endswith("LIMIT 50")

    def test_hd_dedup_injects_not_exists(self):
        query = android_handler.build_query(None, None, True)
        assert query.count("NOT EXISTS") == 2  # one per block
        assert "association_type IN (12, 7)" in query

    def test_hd_dedup_false_excludes_not_exists(self):
        query = android_handler.build_query(None, None, False)
        assert "NOT EXISTS" not in query

    def test_check_hd_association_returns_true_when_table_exists(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE message_association (id INTEGER)")
        result = android_handler._check_hd_association(conn.cursor(), logging.getLogger())
        conn.close()
        assert result is True

    def test_check_hd_association_returns_false_when_table_absent(self):
        conn = sqlite3.connect(":memory:")
        result = android_handler._check_hd_association(conn.cursor(), logging.getLogger())
        conn.close()
        assert result is False

    def test_group_subjects_query_has_no_date_filter(self):
        query = android_handler.build_group_subjects_query()
        assert "timestamp >=" not in query

    def test_has_extended_media_support_empty_or_absent(self):
        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        assert android_handler.has_extended_media_support(c) is False
        conn.execute("CREATE TABLE message_media_map (row_id INTEGER)")
        conn.execute("CREATE TABLE extended_media_data (row_id INTEGER)")
        assert android_handler.has_extended_media_support(c) is False
        conn.execute("INSERT INTO message_media_map VALUES (1)")
        assert android_handler.has_extended_media_support(c) is True
        conn.close()

    def test_build_extended_media_query_structure(self):
        query = android_handler.build_extended_media_query(50, 1700000000000)
        assert "message_media_map" in query
        assert "extended_media_data" in query
        assert "LIMIT 25" in query
        assert "1700000000000" in query
        assert "Media/WhatsApp %" in query


class TestGetMediaFilePaths:
    def test_get_media_file_paths_since_filter(self):
        conn = _make_msgstore()
        conn.execute("INSERT INTO chat VALUES (1, 'Group 1', 10)")
        conn.execute("INSERT INTO chat VALUES (2, NULL, 20)")
        conn.execute("INSERT INTO jid VALUES (10, 'group1')")
        conn.execute("INSERT INTO jid VALUES (20, '123456')")
        conn.execute("INSERT INTO message VALUES (1, 1000, 10, 0)")
        conn.execute("INSERT INTO message VALUES (2, 2000, 20, 0)")
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/old.jpg', 'image/jpeg', 1, 1, 'url', NULL)"
        )
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/new.jpg', 'image/jpeg', 2, 2, 'url', NULL)"
        )
        conn.commit()

        paths = android_handler.get_media_file_paths(conn.cursor(), 1500, False)
        assert paths == {"Media/WhatsApp Images/new.jpg"}
        conn.close()

    def test_get_media_file_paths_no_filter_returns_all(self):
        conn = _make_msgstore()
        conn.execute("INSERT INTO chat VALUES (1, 'Group 1', 10)")
        conn.execute("INSERT INTO jid VALUES (10, 'group1')")
        conn.execute("INSERT INTO message VALUES (1, 1000, 10, 0)")
        conn.execute("INSERT INTO message VALUES (2, 2000, 10, 0)")
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/img1.jpg', 'image/jpeg', 1, 1, 'url', NULL)"
        )
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Video/clip.mp4', 'video/mp4', 1, 2, 'url', NULL)"
        )
        conn.commit()

        paths = android_handler.get_media_file_paths(conn.cursor(), None, False)
        assert paths == {"Media/WhatsApp Images/img1.jpg", "Media/WhatsApp Video/clip.mp4"}
        conn.close()

    def test_get_media_file_paths_hd_dedup(self):
        conn = _make_msgstore(extra_tables=["message_association"])
        conn.execute("ALTER TABLE message_association ADD COLUMN parent_message_row_id INTEGER")
        conn.execute("ALTER TABLE message_association ADD COLUMN child_message_row_id INTEGER")
        conn.execute("ALTER TABLE message_association ADD COLUMN association_type INTEGER")
        conn.execute("INSERT INTO chat VALUES (1, 'Group 1', 10)")
        conn.execute("INSERT INTO jid VALUES (10, 'group1')")
        conn.execute("INSERT INTO message VALUES (1, 1000, 10, 0)")
        conn.execute("INSERT INTO message VALUES (2, 1000, 10, 0)")
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/lq.jpg', 'image/jpeg', 1, 1, 'url', NULL)"
        )
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Images/hd.jpg', 'image/jpeg', 1, 2, 'url', NULL)"
        )
        conn.execute("INSERT INTO message_association VALUES (1, 1, 2, 12)")
        conn.commit()

        paths = android_handler.get_media_file_paths(conn.cursor(), None, True)
        assert paths == {"Media/WhatsApp Images/hd.jpg"}
        conn.close()

    def test_get_media_file_paths_includes_stickers(self):
        conn = _make_msgstore()
        conn.execute("INSERT INTO chat VALUES (1, NULL, 20)")
        conn.execute("INSERT INTO jid VALUES (20, '123456')")
        conn.execute("INSERT INTO message VALUES (1, 1000, 20, 0)")
        conn.execute(
            "INSERT INTO message_media VALUES ('Media/WhatsApp Stickers/STK-1.webp', 'image/webp', 1, 1, 'url', NULL)"
        )
        conn.commit()

        paths = android_handler.get_media_file_paths(conn.cursor(), None, False)
        assert "Media/WhatsApp Stickers/STK-1.webp" in paths
        conn.close()


class TestValidateSchema:
    def test_valid_schema_passes(self, logger):
        conn = _make_msgstore()
        cur = conn.cursor()
        android_handler.validate_schema(cur, logger)  # should not raise

    def test_missing_table_aborts(self, logger):
        conn = sqlite3.connect(":memory:")  # completely empty DB
        cur = conn.cursor()
        with pytest.raises(SystemExit):
            android_handler.validate_schema(cur, logger)

    def test_missing_column_aborts(self, logger):
        conn = _make_msgstore(missing_col=("message", "timestamp"))
        cur = conn.cursor()
        with pytest.raises(SystemExit):
            android_handler.validate_schema(cur, logger)

    def test_extra_tables_are_allowed(self, logger):
        conn = _make_msgstore(extra_tables=["props", "message_thumbnail"])
        cur = conn.cursor()
        android_handler.validate_schema(cur, logger)  # should not raise


class TestBuildNumberMap:
    def test_empty_table_returns_empty_map(self, logger):
        conn = _make_number_change_db([])
        result = android_handler.build_number_map(conn.cursor(), logger)
        assert result == {}

    def test_single_change(self, logger):
        conn = _make_number_change_db([("111", "222")])
        result = android_handler.build_number_map(conn.cursor(), logger)
        assert result == {"111": "222"}

    def test_chain_resolved(self, logger):
        # A -> B -> C should resolve A -> C
        conn = _make_number_change_db([("111", "222"), ("222", "333")])
        result = android_handler.build_number_map(conn.cursor(), logger)
        assert result["111"] == "333"
        assert result["222"] == "333"

    def test_missing_table_returns_empty(self, logger):
        conn = sqlite3.connect(":memory:")  # no tables at all
        result = android_handler.build_number_map(conn.cursor(), logger)
        assert result == {}


class TestLoadAndroidContacts:
    def test_parses_display_name_and_number(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text("Row: display_name=Alice, data1=391234567890\n", encoding="utf-8")
        result = android_handler.load_contacts(str(f), logger)
        assert result == {"391234567890": "Alice", "0": "WhatsApp"}

    def test_multiple_contacts(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: display_name=Alice, data1=111\nRow: display_name=Bob, data1=222\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result == {"111": "Alice", "222": "Bob", "0": "WhatsApp"}

    def test_email_addresses_excluded(self, tmp_path, logger):
        # data1 values containing @ are skipped by the regex
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: display_name=Alice, data1=alice@example.com\nRow: display_name=Bob, data1=222\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert "alice@example.com" not in result
        assert result.get("222") == "Bob"

    def test_empty_file_returns_empty(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text("", encoding="utf-8")
        result = android_handler.load_contacts(str(f), logger)
        assert result == {"0": "WhatsApp"}

    def test_missing_file_returns_empty(self, tmp_path, logger):
        result = android_handler.load_contacts(str(tmp_path / "nonexistent.txt"), logger)
        assert result == {}

    def test_none_path_returns_empty(self, logger):
        result = android_handler.load_contacts(None, logger)
        assert result == {}

    def test_empty_contacts_emits_warning(self, tmp_path, caplog):
        import logging as _logging

        f = tmp_path / "contacts.txt"
        f.write_text("", encoding="utf-8")
        log = _logging.getLogger("warn_test")
        log.setLevel(_logging.WARNING)
        with caplog.at_level(_logging.WARNING, logger="warn_test"):
            android_handler.load_contacts(str(f), log)
        assert any("no WhatsApp contacts" in r.message for r in caplog.records)

    def test_parses_projection_with_data4_and_mimetype(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: 0 display_name=Charlie, data1=0170 1234567, data4=+491701234567, "
            "mimetype=vnd.android.cursor.item/phone_v2\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result.get("491701234567") == "Charlie"
        assert result.get("01701234567") == "Charlie"

    def test_phone_number_cleaning(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: 0 display_name=Bob, data1=+1 (555) 123-4567, data4=NULL, "
            "mimetype=vnd.android.cursor.item/phone_v2\n"
            "Row: 1 display_name=Alice, data1=0044 20 7946 0999, data4=NULL, "
            "mimetype=vnd.android.cursor.item/phone_v2\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result.get("15551234567") == "Bob"
        assert result.get("442079460999") == "Alice"

    def test_whatsapp_profile_precedence(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: 0 display_name=David Phone, data1=+15550001111, data4=+15550001111, "
            "mimetype=vnd.android.cursor.item/phone_v2\n"
            "Row: 1 display_name=David WA, data1=15550001111@s.whatsapp.net, data4=NULL, "
            "mimetype=vnd.android.cursor.item/vnd.com.whatsapp.profile\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result.get("15550001111") == "David WA"

    def test_display_name_with_comma(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: 0 display_name=Smith, John, data1=+15559876543, data4=+15559876543, "
            "mimetype=vnd.android.cursor.item/phone_v2\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result.get("15559876543") == "Smith, John"

    def test_csv_and_delimited_format(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "+39 340 1234567, Mario Rossi\nLuigi Verdi: +393409876543\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result.get("393401234567") == "Mario Rossi"
        assert result.get("393409876543") == "Luigi Verdi"

    def test_non_phone_mimetypes_ignored(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: 0 display_name=Eve, data1=eve@company.com, data4=NULL, "
            "mimetype=vnd.android.cursor.item/email_v2\n"
            "Row: 1 display_name=Frank, data1=+15551112222, data4=NULL, "
            "mimetype=vnd.android.cursor.item/phone_v2\n",
            encoding="utf-8",
        )
        result = android_handler.load_contacts(str(f), logger)
        assert "eve@company.com" not in result
        assert "eve" not in result
        assert result.get("15551112222") == "Frank"

    def test_logs_phone_numbers_and_unique_contacts(self, tmp_path, caplog):
        import logging as _logging

        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: display_name=Alice, data1=111\n"
            "Row: display_name=Alice, data1=222\n"
            "Row: display_name=Bob, data1=333\n",
            encoding="utf-8",
        )
        log = _logging.getLogger("test_android_log")
        log.setLevel(_logging.INFO)
        with caplog.at_level(_logging.INFO, logger="test_android_log"):
            android_handler.load_contacts(str(f), log)
        assert any(
            "Loaded 3 phone number(s) across 2 Android contact(s)." in r.message
            for r in caplog.records
        )


class TestValidateWaRoot:
    def test_valid_root_passes(self, tmp_path, logger):
        media = tmp_path / "Media"
        media.mkdir()
        (media / "WhatsApp Images").mkdir()
        android_handler.validate_wa_root([str(tmp_path)], logger)  # should not raise

    def test_nonexistent_path_aborts(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root([str(tmp_path / "does_not_exist")], logger)

    def test_media_folder_passed_directly_aborts(self, tmp_path, logger):
        media = tmp_path / "Media"
        media.mkdir()
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root([str(media)], logger)

    def test_subfolder_of_media_passed_aborts(self, tmp_path, logger):
        images = tmp_path / "WhatsApp Images"
        images.mkdir()
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root([str(images)], logger)

    def test_root_with_no_subfolders_warns_but_continues(self, tmp_path, logger):
        # Media/ exists but is empty — should warn, not abort
        (tmp_path / "Media").mkdir()
        caplog_logger = logging.getLogger("test_warn")
        caplog_logger.setLevel(logging.WARNING)
        handler_called = []

        class Capture(logging.Handler):
            def emit(self, record):
                handler_called.append(record.levelno)

        caplog_logger.addHandler(Capture())
        android_handler.validate_wa_root(
            [str(tmp_path)], caplog_logger
        )  # must not raise SystemExit
        assert logging.WARNING in handler_called

    def test_second_root_valid_when_first_invalid(self, tmp_path, logger):
        # First root missing entirely, second valid — should not abort
        root2 = tmp_path / "root2"
        media2 = root2 / "Media"
        media2.mkdir(parents=True)
        (media2 / "WhatsApp Images").mkdir()
        android_handler.validate_wa_root(
            [str(tmp_path / "nonexistent"), str(root2)], logger
        )  # should not raise

    def test_all_roots_invalid_aborts(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root([str(tmp_path / "a"), str(tmp_path / "b")], logger)


class TestRunForwardModeAndroid:
    def test_run_forward_mode_android_full_pipeline(self, tmp_path, logger):
        db_path = tmp_path / "msgstore.db"
        conn = make_android_db(db_path)
        conn.executescript("""
            INSERT INTO jid (_id, user) VALUES (1, '390111222');
            INSERT INTO jid (_id, user) VALUES (2, '390333444');
            INSERT INTO chat (_id, subject, jid_row_id) VALUES (1, NULL, 1);
            INSERT INTO chat (_id, subject, jid_row_id) VALUES (2, 'Family Group', 2);

            INSERT INTO message (_id, timestamp, sender_jid_row_id, from_me, chat_row_id) VALUES (1, 1718000000000, 1, 0, 1);
            INSERT INTO message_media (_id, file_path, mime_type, chat_row_id, message_row_id, message_url, media_name)
            VALUES (1, 'Media/WhatsApp Images/IMG-001.jpg', 'image/jpeg', 1, 1, 'http://example.com/1', 'IMG-001.jpg');

            INSERT INTO message (_id, timestamp, sender_jid_row_id, from_me, chat_row_id) VALUES (2, 1718001000000, 1, 0, 1);
            INSERT INTO message_media (_id, file_path, mime_type, chat_row_id, message_row_id, message_url, media_name)
            VALUES (2, 'Media/WhatsApp Images/IMG-002.jpg', 'image/jpeg', 1, 2, 'http://example.com/2', 'IMG-002.jpg');

            INSERT INTO message (_id, timestamp, sender_jid_row_id, from_me, chat_row_id) VALUES (3, 1718002000000, 2, 0, 2);
            INSERT INTO message_media (_id, file_path, mime_type, chat_row_id, message_row_id, message_url, media_name)
            VALUES (3, 'Media/WhatsApp Images/IMG-003.jpg', 'image/jpeg', 2, 3, 'http://example.com/3', 'IMG-003.jpg');

            INSERT INTO message (_id, timestamp, sender_jid_row_id, from_me, chat_row_id) VALUES (4, 1718003000000, 1, 0, 1);
            INSERT INTO message_media (_id, file_path, mime_type, chat_row_id, message_row_id, message_url, media_name)
            VALUES (4, 'Media/WhatsApp Images/MISSING.jpg', 'image/jpeg', 1, 4, 'http://example.com/4', 'MISSING.jpg');
        """)
        conn.close()

        root1 = tmp_path / "wa_root1"
        media_dir1 = root1 / "Media" / "WhatsApp Images"
        media_dir1.mkdir(parents=True)
        # IMG-001 in root1 is 0 bytes (zero-byte skipped candidate)
        (media_dir1 / "IMG-001.jpg").write_bytes(b"")
        # IMG-003 in root1 is 100 bytes (larger candidate for conflict)
        (media_dir1 / "IMG-003.jpg").write_bytes(b"large_content_" * 10)

        root2 = tmp_path / "wa_root2"
        media_dir2 = root2 / "Media" / "WhatsApp Images"
        media_dir2.mkdir(parents=True)
        dup_bytes = b"identical_duplicate_media_payload_bytes"
        # IMG-001 in root2 is valid
        (media_dir2 / "IMG-001.jpg").write_bytes(dup_bytes)
        # IMG-002 in root2 has identical content to IMG-001 (causing duplicate hash in archive)
        (media_dir2 / "IMG-002.jpg").write_bytes(dup_bytes)
        # IMG-003 in root2 is 20 bytes (smaller candidate, triggering conflict)
        (media_dir2 / "IMG-003.jpg").write_bytes(b"small_content_20byte")

        output_dir = tmp_path / "archive_output"
        output_dir.mkdir()

        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone=None,
            since=None,
            limit=None,
            ios_backup=None,
            dry_run=False,
            msgstore=str(db_path),
            wa_roots=[str(root1), str(root2)],
            output=str(output_dir),
            contacts=None,
            log=None,
        )

        wa.run_forward_mode(args, logger)

        archive_db_path = output_dir / ".wa_media_archiver.db"
        assert archive_db_path.exists()

        missing_report = output_dir / "missing_media_report.csv"
        assert missing_report.exists()

        dup_report = output_dir / "duplicate_media_report.csv"
        assert dup_report.exists()

        conflict_report = output_dir / "source_conflicts_report.csv"
        assert conflict_report.exists()

    def test_run_forward_mode_preserve_deleted_baseline_and_incremental(self, tmp_path, logger):
        # 1. Setup source db 1 with 2 messages
        db_path1 = tmp_path / "src1_msgstore.db"
        conn1 = make_android_db(db_path1)
        conn1.executescript("""
            INSERT INTO jid (_id, user) VALUES (1, '390111222'), (2, '390333444');
            INSERT INTO chat (_id, subject, jid_row_id) VALUES (1, NULL, 1);
            INSERT INTO message (_id, chat_row_id, timestamp, sender_jid_row_id, from_me, key_id, message_type, text_data) VALUES
                (1, 1, 1718000000000, 2, 0, 'k1', 0, 'First message'),
                (2, 1, 1718001000000, 2, 0, 'k2', 0, 'Message to be deleted');
        """)
        conn1.close()

        root = tmp_path / "wa_root"
        (root / "Media" / "WhatsApp Images").mkdir(parents=True)
        output_dir = tmp_path / "archive_output"
        output_dir.mkdir()

        args = argparse.Namespace(
            from_adb=False,
            e2e_key=None,
            ios_password=None,
            timezone=None,
            since=None,
            limit=None,
            ios_backup=None,
            dry_run=False,
            preserve_deleted=True,
            msgstore=str(db_path1),
            wa_roots=[str(root)],
            output=str(output_dir),
            contacts=None,
            log=None,
        )

        # Run 1: First run with preserve_deleted
        wa.run_forward_mode(args, logger)

        baseline_db = output_dir / "Whatsapp Databases" / "msgstore.db"
        assert baseline_db.exists()
        pres_db = output_dir / "Whatsapp Databases" / "preserved_messages.db"
        assert not pres_db.exists()

        # 2. Setup source db 2 where message 2 ('k2') was deleted
        db_path2 = tmp_path / "src2_msgstore.db"
        conn2 = make_android_db(db_path2)
        conn2.executescript("""
            INSERT INTO jid (_id, user) VALUES (1, '390111222'), (2, '390333444');
            INSERT INTO chat (_id, subject, jid_row_id) VALUES (1, NULL, 1);
            INSERT INTO message (_id, chat_row_id, timestamp, sender_jid_row_id, from_me, key_id, message_type, text_data) VALUES
                (1, 1, 1718000000000, 2, 0, 'k1', 0, 'First message');
        """)
        conn2.close()

        # Run 2: Incremental run with preserve_deleted
        args.msgstore = str(db_path2)
        wa.run_forward_mode(args, logger)

        assert pres_db.exists()
        with sqlite3.connect(str(pres_db)) as pres_conn:
            row = pres_conn.execute(
                "SELECT stanza_id, text_body, preservation_reason FROM preserved_messages"
            ).fetchone()
            assert row == ("k2", "Message to be deleted", "deleted_on_phone")


class TestPrepareInputMultiRoot:
    def test_multi_root_empty_candidates_returns_default(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1), str(root2)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        resolved = resolver("Media/photo.jpg")
        expected = os.path.join(str(root1), "Media", "photo.jpg")
        assert resolved == expected

    def test_multi_root_single_candidate(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        img1 = root1 / "Media" / "photo.jpg"
        img1.parent.mkdir(parents=True)
        img1.write_bytes(b"content_img1")

        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1), str(root2)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        resolved = resolver("Media/photo.jpg")
        assert resolved == str(img1)
        assert resolver.root_hits[str(root1)] == 1
        assert resolver.root_hits[str(root2)] == 0

    def test_multi_root_zero_byte_files_skipped(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        img1 = root1 / "Media" / "photo.jpg"
        img1.parent.mkdir(parents=True)
        img1.write_bytes(b"")

        img2 = root2 / "Media" / "photo.jpg"
        img2.parent.mkdir(parents=True)
        img2.write_bytes(b"valid_data")

        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1), str(root2)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        resolved = resolver("Media/photo.jpg")
        assert resolved == str(img2)
        assert resolver.zero_byte_count[0] == 1
        assert resolver.root_hits[str(root2)] == 1

    def test_multi_root_largest_wins(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        img1 = root1 / "Media" / "photo.jpg"
        img1.parent.mkdir(parents=True)
        img1.write_bytes(b"large_content_bytes_here")

        img2 = root2 / "Media" / "photo.jpg"
        img2.parent.mkdir(parents=True)
        img2.write_bytes(b"small")

        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1), str(root2)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        resolved = resolver("Media/photo.jpg")
        assert resolved == str(img1)
        assert len(resolver.conflict_rows) == 1
        assert resolver.conflict_rows[0]["reason"] == "largest_wins"
        assert resolver.root_hits[str(root1)] == 1

    def test_multi_root_same_size_same_content(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        content = b"same_content_for_both_roots"
        img1 = root1 / "Media" / "photo.jpg"
        img1.parent.mkdir(parents=True)
        img1.write_bytes(content)

        img2 = root2 / "Media" / "photo.jpg"
        img2.parent.mkdir(parents=True)
        img2.write_bytes(content)

        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1), str(root2)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        resolved = resolver("Media/photo.jpg")
        assert resolved == str(img1)
        assert len(resolver.conflict_rows) == 0
        assert resolver.root_hits[str(root1)] == 1

    def test_multi_root_same_size_different_content(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir()
        root2.mkdir()
        img1 = root1 / "Media" / "photo.jpg"
        img1.parent.mkdir(parents=True)
        img1.write_bytes(b"content_version_a")

        img2 = root2 / "Media" / "photo.jpg"
        img2.parent.mkdir(parents=True)
        img2.write_bytes(b"content_version_b")

        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1), str(root2)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        resolved = resolver("Media/photo.jpg")
        assert resolved == str(img1)
        assert len(resolver.conflict_rows) == 1
        assert resolver.conflict_rows[0]["reason"] == "same_size_first_root"

    def test_owning_root_outside_all_roots(self, tmp_path, logger):
        root1 = tmp_path / "root1"
        root1.mkdir()
        args = argparse.Namespace(
            ios_backup=None,
            wa_roots=[str(root1)],
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        platform, resolver, _, _ = wa._prepare_input(args, None, logger)
        other_path = tmp_path / "other" / "file.jpg"
        other_path.parent.mkdir()
        other_path.write_bytes(b"some_bytes")
        with patch("os.path.exists", return_value=True), patch("os.path.getsize", return_value=10):
            with patch("os.path.join", return_value=str(other_path)):
                chosen = resolver("Media/photo.jpg")
                assert chosen == str(other_path)
                assert resolver.root_hits[str(root1)] == 1
