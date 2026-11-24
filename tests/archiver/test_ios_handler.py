"""Tests for wab_archiver."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import wab_archiver.archive_db as arc
import wab_archiver.backup_reader as br
import wab_archiver.ios_handler as ios
import wab_archiver.main as wa
from tests.archiver.conftest import (
    _make_contacts_db,
    _make_ios_msgstore,
    _make_ios_sessions_db,
    _make_manifest_db,
    _make_manifest_db_multi_domain,
)
from tests.conftest import make_ios_db


class TestValidateIosSchema:
    def test_valid_schema_passes(self, logger):
        conn = _make_ios_msgstore()
        ios.validate_ios_schema(conn.cursor(), logger)  # should not raise

    def test_missing_zwamessage_aborts(self, logger):
        conn = _make_ios_msgstore(missing_table="ZWAMESSAGE")
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_missing_zwachatsession_aborts(self, logger):
        conn = _make_ios_msgstore(missing_table="ZWACHATSESSION")
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_missing_zwamediaitem_aborts(self, logger):
        conn = _make_ios_msgstore(missing_table="ZWAMEDIAITEM")
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_missing_column_aborts(self, logger):
        conn = _make_ios_msgstore(missing_col=("ZWAMESSAGE", "ZMESSAGEDATE"))
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_extra_tables_are_allowed(self, logger):
        conn = _make_ios_msgstore()
        conn.execute("CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        ios.validate_ios_schema(conn.cursor(), logger)  # should not raise


class TestValidateIosWaRoot:
    def test_valid_root_with_message_dir_passes(self, tmp_path, logger):
        (tmp_path / "Message").mkdir()
        ios.validate_ios_wa_root(str(tmp_path), logger)  # should not raise

    def test_missing_message_dir_aborts(self, tmp_path, logger):
        # tmp_path exists but has no Message/ subdirectory
        with pytest.raises(SystemExit):
            ios.validate_ios_wa_root(str(tmp_path), logger)

    def test_nonexistent_path_aborts(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            ios.validate_ios_wa_root(str(tmp_path / "nonexistent"), logger)


class TestBuildIosQuery:
    def test_group_sender_jid_without_at_uses_full_jid(self):
        # ZMEMBERJID with no '@' returns the full JID as sender.
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, NULL, 1, 'Group A');
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAGROUPMEMBER  VALUES (1, 'nojid');
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, 1, NULL);
        """)
        rows = conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert len(rows) == 1
        sender = rows[0][6]  # sender column
        assert sender == "nojid"

    def test_1to1_sender_jid_without_at_uses_full_jid(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, 'nojid', NULL, NULL);
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, NULL, NULL);
        """)
        rows = conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert len(rows) == 1
        sender = rows[0][6]  # sender column
        assert sender == "nojid"

    def test_group_sender_normal_jid_strips_at_suffix(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, NULL, 1, 'Group A');
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAGROUPMEMBER  VALUES (1, '447700900123@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, 1, NULL);
        """)
        rows = conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert rows[0][6] == "447700900123"

    def test_1to1_sender_normal_jid_strips_at_suffix(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '447700900456@s.whatsapp.net', NULL, NULL);
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, NULL, NULL);
        """)
        rows = conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert rows[0][6] == "447700900456"

    def test_status_media_excluded_from_group_query(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, NULL, 1, 'Group A');
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Media/390000000001@status/6/d/photo.jpg', NULL, NULL);
            INSERT INTO ZWAGROUPMEMBER  VALUES (1, '447700900123@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, 1, NULL);
        """)
        rows = conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert len(rows) == 0

    def test_status_media_excluded_from_1to1_query(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '447700900456@s.whatsapp.net', NULL, NULL);
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Media/390000000001@status/6/d/photo.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, NULL, NULL);
        """)
        rows = conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert len(rows) == 0


class TestIosLinkThumbnails:
    def test_has_ios_link_thumbnails_support_false_when_absent(self):
        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        assert ios.has_ios_link_thumbnails_support(c) is False
        conn.close()

    def test_has_ios_link_thumbnails_support_true_with_mdi_or_mediaitem(self):
        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        conn.execute("CREATE TABLE ZWAMESSAGEDATAITEM (ZTHUMBNAILPATH TEXT)")
        assert ios.has_ios_link_thumbnails_support(c) is True
        conn.close()

        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        conn.execute("CREATE TABLE ZWAMEDIAITEM (ZXMPPTHUMBPATH TEXT)")
        assert ios.has_ios_link_thumbnails_support(c) is True
        conn.close()

    def test_build_ios_link_thumbnails_query_structure_and_since(self):
        conn = sqlite3.connect(":memory:")
        c = conn.cursor()
        conn.execute("CREATE TABLE ZWAMESSAGEDATAITEM (ZMESSAGE INTEGER, ZTHUMBNAILPATH TEXT)")
        conn.execute("CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER, ZXMPPTHUMBPATH TEXT)")
        conn.execute(
            "CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZMESSAGEDATE REAL, ZMEDIAITEM INTEGER, ZMESSAGETYPE INTEGER)"
        )

        q = ios.build_ios_link_thumbnails_query(c, None)
        assert "ZWAMESSAGEDATAITEM" in q
        assert "ZWAMEDIAITEM" in q
        assert "ZMESSAGEDATE >=" not in q

        q_since = ios.build_ios_link_thumbnails_query(c, 1700000000000)
        assert "ZMESSAGEDATE >=" in q_since
        conn.close()

    def test_process_ios_link_thumbnails_copies_and_records(self, tmp_path):

        wa_path = tmp_path / "ChatStorage.sqlite"
        output_dir = tmp_path / "output"

        # Create source fake thumb file
        source_dir = tmp_path / "backup_files"
        source_dir.mkdir(parents=True)
        fake_thumb = source_dir / "sample.thumb"
        fake_thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZXMPPTHUMBPATH TEXT
            );
            CREATE TABLE ZWAMESSAGEDATAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZTHUMBNAILPATH TEXT
            );

            INSERT INTO ZWAMESSAGE VALUES (10, 700000000.0, 7, 1);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/sample.thumb');
            INSERT INTO ZWAMESSAGEDATAITEM VALUES (1, 10, 'Media/sample.thumb');
        """)
        conn.commit()

        # Archive DB
        output_dir.mkdir(parents=True, exist_ok=True)
        arch_conn = arc.open_archive_db(str(output_dir))

        def resolver(fp):
            if "sample.thumb" in fp:
                return str(fake_thumb)
            return None

        stats = wa.process_ios_link_thumbnails(
            conn.cursor(),
            resolver,
            str(output_dir),
            arch_conn,
            None,
            False,
            logging.getLogger("test"),
        )
        assert stats["copied"] == 1
        assert stats["missing"] == 0

        # Check copied file on disk
        dest_thumb = output_dir / "Whatsapp Databases" / ".thumbnails" / "sample.thumb"
        assert dest_thumb.exists()

        # Check recorded in archive_copies
        rows = arch_conn.execute(
            "SELECT original_path, archive_path FROM archive_copies"
        ).fetchall()
        orig_paths = {r[0] for r in rows}
        assert "Media/sample.thumb" in orig_paths
        assert "Message/Media/sample.thumb" in orig_paths

        conn.close()
        arch_conn.close()


class TestBuildManifestMap:
    def test_returns_expected_mapping(self, tmp_path, logger):
        file_id = "ab" + "c" * 38  # 40-char hex-like string
        _make_manifest_db(tmp_path, [(file_id, "ChatStorage.sqlite")])
        result = br.build_manifest_map(str(tmp_path), logger)
        expected_path = os.path.join(str(tmp_path), file_id[:2], file_id)
        assert result == {"ChatStorage.sqlite": expected_path}

    def test_multiple_files(self, tmp_path, logger):
        rows = [
            ("aa" + "1" * 38, "ChatStorage.sqlite"),
            ("bb" + "2" * 38, "Message/Media/file.jpg"),
        ]
        _make_manifest_db(tmp_path, rows)
        result = br.build_manifest_map(str(tmp_path), logger)
        assert len(result) == 2
        assert "ChatStorage.sqlite" in result
        assert "Message/Media/file.jpg" in result

    def test_empty_backup_returns_empty_dict(self, tmp_path, logger):
        _make_manifest_db(tmp_path, [])
        result = br.build_manifest_map(str(tmp_path), logger)
        assert result == {}

    def test_missing_manifest_db_exits(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            br.build_manifest_map(str(tmp_path), logger)


class TestBuildManifestMapDomain:
    def test_default_domain_excludes_business_files(self, tmp_path, logger):
        _make_manifest_db_multi_domain(
            tmp_path,
            [
                ("aa" + "1" * 38, br._WA_DOMAIN, "ChatStorage.sqlite"),
                ("bb" + "2" * 38, br._WA_BUSINESS_DOMAIN, "ChatStorage.sqlite"),
            ],
        )
        result = br.build_manifest_map(str(tmp_path), logger)
        assert len(result) == 1
        assert list(result.values())[0].endswith("aa" + "1" * 38)

    def test_business_domain_excludes_regular_files(self, tmp_path, logger):
        _make_manifest_db_multi_domain(
            tmp_path,
            [
                ("aa" + "1" * 38, br._WA_DOMAIN, "ChatStorage.sqlite"),
                ("bb" + "2" * 38, br._WA_BUSINESS_DOMAIN, "ChatStorage.sqlite"),
            ],
        )
        result = br.build_manifest_map(str(tmp_path), logger, domain=br._WA_BUSINESS_DOMAIN)
        assert len(result) == 1
        assert list(result.values())[0].endswith("bb" + "2" * 38)

    def test_business_domain_constant_value(self):
        assert br._WA_BUSINESS_DOMAIN == "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared"


class TestLoadIosContacts:
    def test_parses_jid_to_number(self, tmp_path, logger):
        path = _make_contacts_db(tmp_path, [("390000000005@s.whatsapp.net", "Alice")])
        result = ios.load_ios_contacts(path, logger)
        assert result == {"390000000005": "Alice", "0": "WhatsApp"}

    def test_multiple_contacts(self, tmp_path, logger):
        path = _make_contacts_db(
            tmp_path,
            [
                ("111@s.whatsapp.net", "Alice"),
                ("222@s.whatsapp.net", "Bob"),
            ],
        )
        result = ios.load_ios_contacts(path, logger)
        assert result == {"111": "Alice", "222": "Bob", "0": "WhatsApp"}

    def test_non_whatsapp_jids_excluded(self, tmp_path, logger):
        path = _make_contacts_db(
            tmp_path,
            [
                ("111@s.whatsapp.net", "Alice"),
                ("222@other.net", "Bob"),
            ],
        )
        result = ios.load_ios_contacts(path, logger)
        assert "222" not in result
        assert "111" in result

    def test_null_name_excluded(self, tmp_path, logger):
        path = _make_contacts_db(
            tmp_path,
            [
                ("111@s.whatsapp.net", None),
                ("222@s.whatsapp.net", "Bob"),
            ],
        )
        result = ios.load_ios_contacts(path, logger)
        assert "111" not in result
        assert result["222"] == "Bob"

    def test_missing_file_returns_empty(self, tmp_path, logger):
        result = ios.load_ios_contacts(str(tmp_path / "nonexistent.sqlite"), logger)
        assert result == {}

    def test_logs_phone_numbers_and_unique_contacts(self, tmp_path, caplog):
        import logging as _logging

        path = _make_contacts_db(
            tmp_path,
            [
                ("111@s.whatsapp.net", "Alice"),
                ("222@s.whatsapp.net", "Alice"),
                ("333@s.whatsapp.net", "Bob"),
            ],
        )
        log = _logging.getLogger("test_ios_log")
        log.setLevel(_logging.INFO)
        with caplog.at_level(_logging.INFO, logger="test_ios_log"):
            ios.load_ios_contacts(path, log)
        assert any(
            "Loaded 3 phone number(s) across 2 iOS contact(s)." in r.message for r in caplog.records
        )


class TestBuildIosNumberMap:
    def test_single_number_change(self, logger):
        conn = _make_ios_sessions_db([("111@s.whatsapp.net", "222@s.whatsapp.net", 42)])
        result = ios.build_ios_number_map(conn.cursor(), logger)
        assert result == {"111": "222"}

    def test_chain_resolved(self, logger):
        # A -> B -> C: use separate abids per pair, share via timestamps
        conn = _make_ios_sessions_db(
            [
                ("111@s.whatsapp.net", "222@s.whatsapp.net", 1),
                ("222@s.whatsapp.net", "333@s.whatsapp.net", 2),
            ]
        )
        result = ios.build_ios_number_map(conn.cursor(), logger)
        # 222 -> 333 directly; 111 -> 222 -> 333 via chain resolution
        assert result.get("222") == "333"
        assert result.get("111") == "333"

    def test_no_pairs_returns_empty(self, logger):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZCONTACTABID INTEGER,
                ZLASTMESSAGEDATE REAL
            )
        """)
        result = ios.build_ios_number_map(conn.cursor(), logger)
        assert result == {}

    def test_missing_table_returns_empty(self, logger):
        conn = sqlite3.connect(":memory:")
        result = ios.build_ios_number_map(conn.cursor(), logger)
        assert result == {}

    def test_lid_mapped_to_phone_from_chatsession(self, logger):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZCONTACTIDENTIFIER TEXT,
                ZGROUPINFO INTEGER,
                ZCONTACTABID INTEGER,
                ZLASTMESSAGEDATE REAL
            );
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZGROUPINFO, ZCONTACTABID, ZLASTMESSAGEDATE)
            VALUES (1, '123456789012345@lid', '393001112223@s.whatsapp.net', NULL, NULL, 1000.0);
        """)
        result = ios.build_ios_number_map(conn.cursor(), logger)
        assert result == {"123456789012345": "393001112223"}

    def test_load_ios_lid_map_extracts_pairs(self, tmp_path, logger):
        db_path = tmp_path / "ContactsV2.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY,
                ZLID TEXT,
                ZWHATSAPPID TEXT,
                ZFULLNAME TEXT
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZLID, ZWHATSAPPID, ZFULLNAME)
            VALUES (1, '123456789012345@lid', '393001112223@s.whatsapp.net', 'Synthetic Contact');
        """)
        conn.commit()
        conn.close()

        result = ios.load_ios_lid_map(str(db_path), logger)
        assert result == {"123456789012345": "393001112223"}


class TestIosAuxiliaryDatabaseExtraction:
    def test_extract_auxiliary_databases_unencrypted(self, tmp_path):

        from wab_archiver import backup_reader as br

        fake_ext = tmp_path / "fake_ext.sqlite"
        fake_infra = tmp_path / "fake_infra.sqlite"
        fake_lid = tmp_path / "fake_lid.sqlite"
        fake_ext.write_bytes(b"EXT_DATA")
        fake_infra.write_bytes(b"INFRA_DATA")
        fake_lid.write_bytes(b"LID_DATA")

        manifest_map = {
            "ExtChatDB/ExtChatDatabase.sqlite": str(fake_ext),
            "MessagingInfraDB_v2/MessagingInfraDatabase.sqlite": str(fake_infra),
            "LID.sqlite": str(fake_lid),
        }

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        logger = logging.getLogger("test_aux_extract")

        br._extract_auxiliary_databases_unencrypted(manifest_map, str(output_dir), logger)

        assert (output_dir / "ExtChatDatabase.sqlite").read_bytes() == b"EXT_DATA"
        assert (output_dir / "MessagingInfraDatabase.sqlite").read_bytes() == b"INFRA_DATA"
        assert (output_dir / "LID.sqlite").read_bytes() == b"LID_DATA"

    def test_extract_auxiliary_databases_missing_is_graceful(self, tmp_path):

        from wab_archiver import backup_reader as br

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        logger = logging.getLogger("test_aux_missing")

        br._extract_auxiliary_databases_unencrypted({}, str(output_dir), logger)

        assert not (output_dir / "ExtChatDatabase.sqlite").exists()
        assert not (output_dir / "MessagingInfraDatabase.sqlite").exists()

    def test_extract_auxiliary_databases_encrypted_summary_and_stdout_suppression(self, tmp_path):

        from wab_archiver import backup_reader as br

        mock_backup = MagicMock()

        def fake_extract_file(*, relative_path, domain_like, output_filename):
            if relative_path == "ExtChatDB/ExtChatDatabase.sqlite":
                print("WARN: decrypted 123 bytes of 'temp.sqlite', expected 456 bytes!")
                with open(output_filename, "wb") as f:
                    f.write(b"EXT_DATA")
            elif relative_path == "LID.sqlite":
                with open(output_filename, "wb") as f:
                    f.write(b"LID_DATA")
            else:
                raise FileNotFoundError(relative_path)

        mock_backup.extract_file.side_effect = fake_extract_file
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        logger = MagicMock(spec=logging.Logger)

        br._extract_auxiliary_databases_encrypted(
            mock_backup, "%whatsapp%", str(output_dir), logger
        )

        assert (output_dir / "ExtChatDatabase.sqlite").read_bytes() == b"EXT_DATA"
        assert (output_dir / "LID.sqlite").read_bytes() == b"LID_DATA"
        logger.info.assert_called_once_with(
            "Decrypted 2 auxiliary databases: ExtChatDatabase.sqlite, LID.sqlite."
        )
        debug_calls = [c[0][0] for c in logger.debug.call_args_list]
        assert any(
            "iphone_backup_decrypt output for ExtChatDB/ExtChatDatabase.sqlite" in m
            for m in debug_calls
        )
        assert any("WARN: decrypted 123 bytes" in m for m in debug_calls)

    def test_save_db_to_output_overwrite_logs_debug_not_warning(self, tmp_path):

        from wab_archiver import backup_reader as br

        src = tmp_path / "source.sqlite"
        src.write_bytes(b"NEW_DATA")
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        existing = output_dir / "ContactsV2.sqlite"
        existing.write_bytes(b"OLD_DATA")

        logger = MagicMock(spec=logging.Logger)
        dest = br._save_db_to_output(str(src), str(output_dir), "ContactsV2.sqlite", logger)

        assert (output_dir / "ContactsV2.sqlite").read_bytes() == b"NEW_DATA"
        assert dest == str(existing)
        logger.warning.assert_not_called()
        logger.debug.assert_called_once()
        assert "Overwriting existing" in logger.debug.call_args[0][0]


class TestIosHdDeduplication:
    def test_find_ios_ext_db(self, tmp_path):
        from wab_archiver import ios_handler as ios

        d1 = tmp_path / "d1"
        d1.mkdir()
        assert ios.find_ios_ext_db([str(d1)]) is None

        # Test nested ExtChatDB folder
        nested = d1 / "ExtChatDB"
        nested.mkdir()
        nested_file = nested / "ExtChatDatabase.sqlite"
        nested_file.touch()
        assert ios.find_ios_ext_db([str(d1)]) == str(nested_file)

        # Test flat folder
        d2 = tmp_path / "d2"
        d2.mkdir()
        flat_file = d2 / "ExtChatDatabase.sqlite"
        flat_file.touch()
        assert ios.find_ios_ext_db([str(d2)]) == str(flat_file)

    def test_check_ios_hd_association(self, tmp_path):

        from wab_archiver import ios_handler as ios

        logger = logging.getLogger("test_hd_assoc")
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.execute(
            "CREATE TABLE message_parent_association (stanza_id TEXT, parent_stanza_id TEXT, type INTEGER)"
        )
        ext_conn.execute("INSERT INTO message_parent_association VALUES ('child1', 'parent1', 10)")
        ext_conn.commit()
        ext_conn.close()

        main_conn = sqlite3.connect(":memory:")
        cursor = main_conn.cursor()

        # Valid association found
        assert ios.check_ios_hd_association(cursor, str(ext_path), logger) is True

        # Non-existent file
        assert (
            ios.check_ios_hd_association(cursor, str(tmp_path / "nonexistent.sqlite"), logger)
            is False
        )

    def test_build_ios_query_hd_dedup_clause(self):
        from wab_archiver import ios_handler as ios

        query_no_dedup = ios.build_ios_query(None, None, False, False, True, True, True)
        assert "ext.message_parent_association" not in query_no_dedup

        query_with_dedup = ios.build_ios_query(None, None, True, False, True, True, True)
        assert "ext.message_parent_association" in query_with_dedup
        assert "mpa.type IN (10, 5)" in query_with_dedup

    def test_ios_hd_dedup_execution(self, tmp_path):
        from wab_archiver import ios_handler as ios

        main_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        main_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZPARTNERNAME, ZCONTACTJID) VALUES (1, 1, 'Group 1', NULL);
            -- Parent message (LQ)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE) VALUES (10, 'lq.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID)
            VALUES (100, 1000.0, 0, 1, 10, NULL, 'User1', 'STANZA_LQ');

            -- Child message (HD)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE) VALUES (11, 'hd.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID)
            VALUES (101, 1001.0, 0, 1, 11, NULL, 'User1', 'STANZA_HD');
        """)

        ext_conn = sqlite3.connect(str(tmp_path / "ExtChatDatabase.sqlite"))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
            INSERT INTO message_parent_association VALUES ('STANZA_HD', 'STANZA_LQ', 10, 1, 'group1@g.us');
        """)
        ext_conn.close()

        main_conn.execute("ATTACH DATABASE ? AS ext", (str(tmp_path / "ExtChatDatabase.sqlite"),))

        # Without dedup: both rows returned
        rows_no_dedup = main_conn.execute(
            ios.build_ios_query(None, None, False, False, False, False, False)
        ).fetchall()
        assert len(rows_no_dedup) == 2

        # With dedup: only the HD row returned (LQ parent filtered out)
        rows_dedup = main_conn.execute(
            ios.build_ios_query(None, None, True, False, False, False, False)
        ).fetchall()
        assert len(rows_dedup) == 1
        assert rows_dedup[0][0] == 101  # HD message ID
        assert rows_dedup[0][2] == "Message/hd.jpg"

    def test_run_forward_mode_ios(self, tmp_path, logger):
        from wab_archiver.main import parse_args, run_forward_mode

        db_path = tmp_path / "ChatStorage.sqlite"
        conn = make_ios_db(db_path)
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        (wa_root / "Message").mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch(
            "sys.argv",
            [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(db_path),
                "--wa-root",
                str(wa_root),
                "--output",
                str(output_dir),
                "--dry-run",
            ],
        ):
            args = parse_args()
        run_forward_mode(args, logger)

    def test_run_forward_mode_ios_with_ext_db(self, tmp_path, logger):
        from wab_archiver.main import parse_args, run_forward_mode

        db_path = tmp_path / "ChatStorage.sqlite"
        conn = make_ios_db(db_path)
        conn.close()

        ext_db = tmp_path / "ExtChatDatabase.sqlite"
        conn = sqlite3.connect(str(ext_db))
        conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
            INSERT INTO message_parent_association VALUES ('HD1', 'LQ1', 10, 1, 'chat@s.whatsapp.net');
        """)
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        (wa_root / "Message").mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch(
            "sys.argv",
            [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(db_path),
                "--wa-root",
                str(wa_root),
                "--output",
                str(output_dir),
                "--dry-run",
            ],
        ):
            args = parse_args()
        run_forward_mode(args, logger)


class TestIosCallHistoryExtraction:
    def test_find_ios_call_history_db(self, tmp_path):
        from wab_archiver import ios_handler as ios

        d1 = tmp_path / "d1"
        d1.mkdir()
        assert ios.find_ios_call_history_db([str(d1)]) is None

        # Test nested Library/CallHistory/CallHistory.storedata
        ch_dir = d1 / "Library" / "CallHistory"
        ch_dir.mkdir(parents=True)
        storedata = ch_dir / "CallHistory.storedata"
        storedata.touch()
        assert ios.find_ios_call_history_db([str(d1)]) == str(storedata)

        # Test flat folder CallHistory.sqlite
        d2 = tmp_path / "d2"
        d2.mkdir()
        flat_file = d2 / "CallHistory.sqlite"
        flat_file.touch()
        assert ios.find_ios_call_history_db([str(d2)]) == str(flat_file)

    def test_verify_ios_call_history_db(self, tmp_path, logger):
        from wab_archiver import ios_handler as ios

        # Non-existent file
        assert ios.verify_ios_call_history_db(str(tmp_path / "nonexistent.sqlite"), logger) is False

        # Invalid/empty file
        empty_file = tmp_path / "empty.sqlite"
        empty_file.touch()
        assert ios.verify_ios_call_history_db(str(empty_file), logger) is False

        # Missing ZWACDCALLEVENT table
        no_table = tmp_path / "no_table.sqlite"
        with sqlite3.connect(str(no_table)) as conn:
            conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY)")
        assert ios.verify_ios_call_history_db(str(no_table), logger) is False

        # Valid CallHistory database
        valid_db = tmp_path / "valid.sqlite"
        with sqlite3.connect(str(valid_db)) as conn:
            conn.execute(
                "CREATE TABLE ZWACDCALLEVENT (Z_PK INTEGER PRIMARY KEY, ZDATE REAL, ZDURATION INTEGER)"
            )
        assert ios.verify_ios_call_history_db(str(valid_db), logger) is True


class TestIosThumbnailExtraction:
    def test_has_ios_thumb_support(self):
        from wab_archiver import ios_handler as ios

        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT)")
        assert ios.has_ios_thumb_support(cursor) is False

        cursor.execute("ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZXMPPTHUMBPATH TEXT")
        assert ios.has_ios_thumb_support(cursor) is True

    def test_build_ios_query_thumb_clause(self):
        from wab_archiver import ios_handler as ios

        query_no_thumb = ios.build_ios_query(None, None, False, False, True, True, True)
        assert "ZXMPPTHUMBPATH" not in query_no_thumb

        query_with_thumb = ios.build_ios_query(None, None, False, True, True, True, True)
        assert "ZXMPPTHUMBPATH" in query_with_thumb
        assert "AS thumb_path" in query_with_thumb
        assert "mi.ZXMPPTHUMBPATH IS NOT NULL" in query_with_thumb

    def test_run_forward_mode_ios_conditional_thumbnail_extraction(self, tmp_path, logger):
        from wab_archiver.main import parse_args, run_forward_mode

        db_path = tmp_path / "ChatStorage.sqlite"
        conn = make_ios_db(db_path)
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME, ZCONTACTABID, ZLASTMESSAGEDATE)
            VALUES (1, '123456@s.whatsapp.net', NULL, 'Alice', 1, 1000.0);
            -- Row 1: media exists, thumb exists
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH)
            VALUES (1, 'media1.jpg', NULL, NULL, 'thumb1.thumb');
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID)
            VALUES (10, 1000.0, 1, 0, 1, 1, NULL, 'Alice', 'STANZA1');

            -- Row 2: media missing, thumb exists
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH)
            VALUES (2, 'missing.jpg', NULL, NULL, 'thumb2.thumb');
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID)
            VALUES (11, 1001.0, 1, 0, 1, 2, NULL, 'Alice', 'STANZA2');
        """)
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        msg_dir = wa_root / "Message"
        msg_dir.mkdir()
        (msg_dir / "media1.jpg").write_bytes(b"JPEG_MEDIA_1")
        (msg_dir / "thumb1.thumb").write_bytes(b"JPEG_THUMB_1")
        (msg_dir / "thumb2.thumb").write_bytes(b"JPEG_THUMB_2")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch(
            "sys.argv",
            [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(db_path),
                "--wa-root",
                str(wa_root),
                "--output",
                str(output_dir),
            ],
        ):
            args = parse_args()
        run_forward_mode(args, logger)

        # Verified: media1 was copied to Contacts
        media1_files = list(output_dir.rglob("*media1*"))
        assert len(media1_files) == 1
        assert media1_files[0].read_bytes() == b"JPEG_MEDIA_1"

        # Verified: thumb1 was NOT extracted because media1 exists!
        thumb_dir = output_dir / "Whatsapp Databases" / ".thumbnails"
        assert not (thumb_dir / "thumb1.thumb").exists()

        # Verified: thumb2 WAS extracted because media2 is missing!
        assert (thumb_dir / "thumb2.thumb").exists()
        assert (thumb_dir / "thumb2.thumb").read_bytes() == b"JPEG_THUMB_2"

        # Check archive_copies table
        db_out = sqlite3.connect(str(output_dir / ".wa_media_archiver.db"))
        copies = db_out.execute(
            "SELECT original_path, archive_path FROM archive_copies ORDER BY original_path"
        ).fetchall()
        db_out.close()

        paths = {c[0]: c[1] for c in copies}
        # thumb1 must not be in archive_copies
        assert "Message/thumb1.thumb" not in paths
        # thumb2 must be in archive_copies pointing to Whatsapp Databases/.thumbnails/thumb2.thumb
        assert "Message/thumb2.thumb" in paths
        assert paths["Message/thumb2.thumb"] == "Whatsapp Databases/.thumbnails/thumb2.thumb"

    def test_run_forward_mode_ios_dry_run_does_not_extract_thumbnails(self, tmp_path, logger):
        from wab_archiver.main import parse_args, run_forward_mode

        db_path = tmp_path / "ChatStorage.sqlite"
        conn = make_ios_db(db_path)
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME, ZCONTACTABID, ZLASTMESSAGEDATE)
            VALUES (1, '123456@s.whatsapp.net', NULL, 'Alice', 1, 1000.0);
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH)
            VALUES (1, 'missing.jpg', NULL, NULL, 'thumb.thumb');
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID)
            VALUES (10, 1000.0, 1, 0, 1, 1, NULL, 'Alice', 'STANZA1');
        """)
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        msg_dir = wa_root / "Message"
        msg_dir.mkdir()
        (msg_dir / "thumb.thumb").write_bytes(b"JPEG_THUMB")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch(
            "sys.argv",
            [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(db_path),
                "--wa-root",
                str(wa_root),
                "--output",
                str(output_dir),
                "--dry-run",
            ],
        ):
            args = parse_args()
        run_forward_mode(args, logger)

        thumb_dir = output_dir / "Whatsapp Databases" / ".thumbnails"
        assert not thumb_dir.exists()


class TestIosMissingMediaReport:
    def test_ios_missing_media_report_format_and_exclusion_of_links(self, tmp_path, logger):
        import csv

        from wab_archiver.main import parse_args, run_forward_mode

        db_path = tmp_path / "ChatStorage.sqlite"
        conn = make_ios_db(db_path)
        conn.executescript("""
            -- Chat 1: 1-to-1 with Alice
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME, ZCONTACTABID, ZLASTMESSAGEDATE)
            VALUES (1, '390000000001@s.whatsapp.net', NULL, NULL, 1, 1000.0);
            -- Chat 2: Group 'Vacation Group'
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME, ZCONTACTABID, ZLASTMESSAGEDATE)
            VALUES (2, '120363000000001@g.us', 1, 'Vacation Group', NULL, 1000.0);

            -- Message 1: Photo on disk missing (ZMEDIALOCALPATH set but file missing from root)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH, ZVCARDNAME, ZVCARDSTRING)
            VALUES (1, 'Media/390000000001@s.whatsapp.net/1/photo1.jpg', 'https://mmg.whatsapp.net/photo1', NULL, NULL, NULL, 'image/jpeg');
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID, ZTEXT)
            VALUES (101, 1000.0, 1, 0, 1, 1, NULL, 'Alice', 'STANZA101', NULL);

            -- Message 2: Photo purged from device (ZMEDIALOCALPATH is NULL, thumb exists)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH, ZVCARDNAME, ZVCARDSTRING)
            VALUES (2, NULL, 'https://mmg.whatsapp.net/photo2', 'Sunset caption', 'Media/120363000000001@g.us/2/sunset_uuid.thumb', NULL, 'image/jpeg');
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID, ZTEXT)
            VALUES (102, 1001.0, 1, 1, 2, 2, NULL, 'Me', 'STANZA102', NULL);

            -- Message 3: Document (ZMESSAGETYPE = 8, ZMEDIALOCALPATH is NULL, title and text set)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH, ZVCARDNAME, ZVCARDSTRING)
            VALUES (3, NULL, 'https://mmg.whatsapp.net/doc3', 'Invoice caption', NULL, NULL, 'application/pdf');
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID, ZTEXT)
            VALUES (103, 1002.0, 8, 0, 1, 3, NULL, 'Alice', 'STANZA103', 'Invoice_2026.pdf');

            -- Message 4: Link preview (ZMESSAGETYPE = 7, should NOT be in missing report)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH, ZVCARDNAME, ZVCARDSTRING)
            VALUES (4, NULL, 'https://example.com/article', 'News Headline', 'Media/thumb_link.thumb', NULL, NULL);
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID, ZTEXT)
            VALUES (104, 1003.0, 7, 0, 1, 4, NULL, 'Alice', 'STANZA104', 'Check this article: https://example.com/article');

            -- Message 5: Location (ZMESSAGETYPE = 5, should NOT be in missing report)
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE, ZXMPPTHUMBPATH, ZVCARDNAME, ZVCARDSTRING)
            VALUES (5, NULL, NULL, NULL, 'Media/thumb_loc.thumb', NULL, NULL);
            INSERT INTO ZWAMESSAGE (Z_PK, ZMESSAGEDATE, ZMESSAGETYPE, ZISFROMME, ZCHATSESSION, ZMEDIAITEM, ZGROUPMEMBER, ZPUSHNAME, ZSTANZAID, ZTEXT)
            VALUES (105, 1004.0, 5, 0, 1, 5, NULL, 'Alice', 'STANZA105', NULL);
        """)
        conn.close()

        wa_root = tmp_path / "AppDomainGroup"
        wa_root.mkdir()
        msg_dir = wa_root / "Message"
        msg_dir.mkdir()
        thumb_file = msg_dir / "Media" / "120363000000001@g.us" / "2" / "sunset_uuid.thumb"
        thumb_file.parent.mkdir(parents=True, exist_ok=True)
        thumb_file.write_bytes(b"SUNSET_THUMB_BYTES")

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        with patch(
            "sys.argv",
            [
                "wab-archiver",
                "archive",
                "--msgstore",
                str(db_path),
                "--wa-root",
                str(wa_root),
                "--output",
                str(output_dir),
            ],
        ):
            args = parse_args()
        run_forward_mode(args, logger)

        report_csv = output_dir / "missing_media_report.csv"
        assert report_csv.exists()

        with open(report_csv, encoding="utf-8") as f:
            reader = list(csv.DictReader(f))

        # Exactly 3 missing media messages (Messages 101, 102, 103); links (104) and location (105) excluded!
        msg_ids = [r["message_id"] for r in reader]
        assert sorted(msg_ids) == ["101", "102", "103"]

        # Check Row 1 (Message 101: missing photo)
        r1 = next(r for r in reader if r["message_id"] == "101")
        assert r1["original_filename"] == "photo1.jpg"
        assert r1["mime_type"] == "image/jpeg"
        assert r1["direction"] == "Received"
        # file_path should be destination archive path, NOT internal sandbox path 'Message/Media/...'
        assert "Contacts" in r1["file_path"]
        assert r1["file_path"].endswith("photo1.jpg")
        assert not r1["file_path"].startswith("Message/")
        assert r1["message_url"] == "https://mmg.whatsapp.net/photo1"

        # Check Row 2 (Message 102: purged group photo with thumb)
        r2 = next(r for r in reader if r["message_id"] == "102")
        assert r2["original_filename"] == "sunset_uuid.jpg"
        assert r2["mime_type"] == "image/jpeg"
        assert r2["direction"] == "Sent"
        assert "Groups" in r2["file_path"]
        assert "Vacation Group" in r2["file_path"]
        assert r2["file_path"].endswith("sunset_uuid_Me.jpg")
        assert r2["message_url"] == "https://mmg.whatsapp.net/photo2"

        # Check Row 3 (Message 103: document)
        r3 = next(r for r in reader if r["message_id"] == "103")
        assert r3["original_filename"] == "Invoice_2026.pdf"
        assert r3["media_name"] == "Invoice_2026.pdf"
        assert r3["mime_type"] == "application/pdf"
        assert "Contacts" in r3["file_path"]
        assert r3["file_path"].endswith("Invoice_2026.pdf")
        assert r3["message_url"] == "https://mmg.whatsapp.net/doc3"


class TestPrepareInputIos:
    def test_prepare_input_ios_encrypted_missing_password_raises(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=str(tmp_path / "backup"),
            ios_password=None,
            ios_contacts=None,
            business=False,
            output=str(tmp_path),
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        with patch("wab_archiver.backup_reader.detect_encrypted", return_value=True):
            with pytest.raises(SystemExit):
                wa._prepare_input(args, None, logger)

    def test_prepare_input_ios_encrypted_success(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=str(tmp_path / "backup"),
            ios_password="secret_password",
            ios_contacts=str(tmp_path / "ContactsV2.sqlite"),
            business=False,
            output=str(tmp_path),
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        fake_db = str(tmp_path / "ChatStorage.sqlite")
        fake_contacts = str(tmp_path / "ContactsV2.sqlite")

        def fake_resolver(fp):
            return f"/extracted/{fp}"

        with (
            patch("wab_archiver.backup_reader.detect_encrypted", return_value=True),
            patch(
                "wab_archiver.backup_reader.extract_encrypted",
                return_value=(fake_db, fake_contacts, fake_resolver),
            ) as mock_ext,
        ):
            platform, resolver, ios_contacts, contacts = wa._prepare_input(args, None, logger)

        assert platform == "ios"
        assert args.msgstore == fake_db
        assert ios_contacts == fake_contacts
        assert resolver("photo.jpg") == "/extracted/photo.jpg"
        mock_ext.assert_called_once_with(
            args.ios_backup,
            "secret_password",
            str(tmp_path),
            fake_contacts,
            False,
            logger,
        )

    def test_prepare_input_ios_plaintext_success(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=str(tmp_path / "backup"),
            ios_password=None,
            ios_contacts=None,
            business=False,
            output=str(tmp_path),
            from_adb=False,
            contacts=None,
            msgstore="dummy",
        )
        fake_manifest = {"Message/photo.jpg": "/extracted/photo.jpg"}
        fake_db = str(tmp_path / "ChatStorage.sqlite")

        with (
            patch("wab_archiver.backup_reader.detect_encrypted", return_value=False),
            patch(
                "wab_archiver.backup_reader.extract_plaintext",
                return_value=(fake_manifest, fake_db, None),
            ) as mock_ext,
        ):
            platform, resolver, ios_contacts, contacts = wa._prepare_input(args, None, logger)

        assert platform == "ios"
        assert args.msgstore == fake_db
        assert ios_contacts is None
        assert resolver("Message/photo.jpg") == "/extracted/photo.jpg"
        assert resolver("Message/missing.jpg") is None
        mock_ext.assert_called_once_with(
            args.ios_backup,
            str(tmp_path),
            None,
            False,
            logger,
        )

    def test_prepare_input_ios_preserve_deleted_baseline_first_run(self, tmp_path, logger):
        args = argparse.Namespace(
            ios_backup=str(tmp_path / "backup"),
            ios_password=None,
            ios_contacts=None,
            business=False,
            output=str(tmp_path),
            from_adb=False,
            contacts=None,
            msgstore="dummy",
            preserve_deleted=True,
        )
        fake_manifest = {"Message/photo.jpg": "/extracted/photo.jpg"}
        fake_db = str(tmp_path / "ChatStorage.sqlite")

        with (
            patch("wab_archiver.backup_reader.detect_encrypted", return_value=False),
            patch(
                "wab_archiver.backup_reader.extract_plaintext",
                return_value=(fake_manifest, fake_db, None),
            ),
        ):
            platform, resolver, ios_contacts, contacts = wa._prepare_input(args, None, logger)

        assert platform == "ios"
        assert args.msgstore == fake_db
        assert getattr(args, "_preservation_stats", None) is not None
        assert args._preservation_stats.messages == 0
        assert args._preservation_stats.chats == 0

    def test_prepare_input_ios_preserve_deleted_incremental_success(self, tmp_path, logger):
        from wab_archiver.preservation import PreservationResult

        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir(parents=True, exist_ok=True)
        existing_db = db_dir / "ChatStorage.sqlite"
        existing_db.write_bytes(b"EXISTING_IOS_DB")

        args = argparse.Namespace(
            ios_backup=str(tmp_path / "backup"),
            ios_password=None,
            ios_contacts=None,
            business=False,
            output=str(tmp_path),
            from_adb=False,
            contacts=None,
            msgstore="dummy",
            preserve_deleted=True,
        )
        fake_manifest = {"Message/photo.jpg": "/extracted/photo.jpg"}
        extracted_db = str(tmp_path / "incoming_ChatStorage.sqlite")

        with (
            patch("wab_archiver.backup_reader.detect_encrypted", return_value=False),
            patch(
                "wab_archiver.backup_reader.extract_plaintext",
                return_value=(fake_manifest, extracted_db, None),
            ),
            patch(
                "wab_archiver.preservation.preserve_deleted_messages",
                return_value=PreservationResult(messages=7, chats=3),
            ) as mock_preserve,
        ):
            platform, resolver, ios_contacts, contacts = wa._prepare_input(args, None, logger)

        assert platform == "ios"
        assert args._preservation_stats.messages == 7
        assert args._preservation_stats.chats == 3
        mock_preserve.assert_called_once()
        call_args = mock_preserve.call_args[0]
        assert call_args[0].endswith("ChatStorage.sqlite.old")
        assert call_args[1] == extracted_db
        assert call_args[4] == "ios"
        # Verify ChatStorage.sqlite.old was cleaned up
        assert not (db_dir / "ChatStorage.sqlite.old").exists()

    def test_prepare_input_ios_preserve_deleted_error_rollback(self, tmp_path, logger):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir(parents=True, exist_ok=True)
        existing_db = db_dir / "ChatStorage.sqlite"
        existing_db.write_bytes(b"INITIAL_IOS_DB")

        args = argparse.Namespace(
            ios_backup=str(tmp_path / "backup"),
            ios_password=None,
            ios_contacts=None,
            business=False,
            output=str(tmp_path),
            from_adb=False,
            contacts=None,
            msgstore="dummy",
            preserve_deleted=True,
        )
        fake_manifest = {"Message/photo.jpg": "/extracted/photo.jpg"}
        extracted_db = str(tmp_path / "incoming_ChatStorage.sqlite")

        with (
            patch("wab_archiver.backup_reader.detect_encrypted", return_value=False),
            patch(
                "wab_archiver.backup_reader.extract_plaintext",
                return_value=(fake_manifest, extracted_db, None),
            ),
            patch(
                "wab_archiver.preservation.preserve_deleted_messages",
                side_effect=RuntimeError("Preservation diff failed"),
            ),
        ):
            with pytest.raises(RuntimeError, match="Preservation diff failed"):
                wa._prepare_input(args, None, logger)

        # Verify rollback restored existing_db
        assert existing_db.exists()
        assert existing_db.read_bytes() == b"INITIAL_IOS_DB"
        assert not (db_dir / "ChatStorage.sqlite.old").exists()
