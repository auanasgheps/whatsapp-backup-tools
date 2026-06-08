"""
Tests for wa_media_archiver.py

Run with:
    pytest tests/ -v

Requires:
    pip install pytest
"""

import logging
import os
import plistlib
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the script as a module.
# We skip the if __name__ == '__main__' block because we import, not run it.
# ---------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import importlib.util

spec = importlib.util.spec_from_file_location(
    "wa_media_archiver",
    os.path.join(_ROOT, "wa_media_archiver.py"),
)
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)

import importlib.util as _ilu

_ios_spec = _ilu.spec_from_file_location(
    "ios_handler", os.path.join(_ROOT, "ios_handler.py")
)
ios = _ilu.module_from_spec(_ios_spec)
_ios_spec.loader.exec_module(ios)

_br_spec = _ilu.spec_from_file_location(
    "backup_reader", os.path.join(_ROOT, "backup_reader.py")
)
br = _ilu.module_from_spec(_br_spec)
_br_spec.loader.exec_module(br)

_android_spec = _ilu.spec_from_file_location(
    "android_handler", os.path.join(_ROOT, "android_handler.py")
)
android_handler = _ilu.module_from_spec(_android_spec)
_android_spec.loader.exec_module(android_handler)

_adb_spec = _ilu.spec_from_file_location(
    "adb_extractor", os.path.join(_ROOT, "adb_extractor.py")
)
adb = _ilu.module_from_spec(_adb_spec)
_adb_spec.loader.exec_module(adb)


# ---------------------------------------------------------------------------
# Shared fixture: a silent logger so test output stays clean
# ---------------------------------------------------------------------------

@pytest.fixture
def logger():
    log = logging.getLogger("test")
    log.setLevel(logging.CRITICAL)  # suppress all output during tests
    return log


# ===========================================================================
# Pure helper functions
# ===========================================================================

class TestSanitizeFilename:
    def test_safe_name_unchanged(self):
        assert wa.sanitize_filename("Family Chat") == "Family Chat"

    def test_forbidden_chars_replaced(self):
        # All of \  /  :  *  ?  "  <  >  | are replaced with _
        assert wa.sanitize_filename('bad:name?') == "bad_name_"
        assert wa.sanitize_filename('a/b\\c') == "a_b_c"
        assert wa.sanitize_filename('<pipe>|test') == "_pipe__test"

    def test_strips_leading_trailing_spaces(self):
        assert wa.sanitize_filename("  padded  ") == "padded"

    def test_empty_string(self):
        assert wa.sanitize_filename("") == ""


class TestEscapeLike:
    def test_plain_string_unchanged(self):
        assert wa.escape_like("hello") == "hello"

    def test_percent_escaped(self):
        assert wa.escape_like("50% off") == r"50\% off"

    def test_underscore_escaped(self):
        assert wa.escape_like("file_name") == r"file\_name"

    def test_backslash_escaped_first(self):
        # The backslash must be escaped before % and _ so it is not double-escaped
        assert wa.escape_like("a\\b") == "a\\\\b"

    def test_combined(self):
        assert wa.escape_like("100%_done") == r"100\%\_done"


class TestFormatPhone:
    def test_prepends_double_zero(self):
        assert wa.format_phone("391234567890") == "00391234567890"

    def test_empty_string_returns_empty(self):
        assert wa.format_phone("") == ""


class TestBuildContactFolderName:
    def test_known_contact(self):
        result = wa.build_contact_folder_name("Alice", "391234567890")
        assert result == "Alice (00391234567890)"

    def test_unknown_contact_uses_unknown_prefix(self):
        # display_name is None
        result = wa.build_contact_folder_name(None, "391234567890")
        assert result == "Unknown (00391234567890)"

    def test_display_name_equals_number_treated_as_unknown(self):
        # When WhatsApp has no name, it stores the number as the display name
        result = wa.build_contact_folder_name("391234567890", "391234567890")
        assert result == "Unknown (00391234567890)"

    def test_special_chars_in_name_are_sanitized(self):
        result = wa.build_contact_folder_name("Alice/Bob", "391234567890")
        assert "/" not in result


class TestGetYear:
    def test_known_timestamp(self):
        # 2024-01-15 00:00:00 UTC in milliseconds: 1705276800000
        # Year should be 2024 regardless of timezone (UTC+0 or later)
        # Use a timestamp that is unambiguously in 2024
        ts_ms = 1705276800000  # 2024-01-15 00:00:00 UTC
        year = wa.get_year(ts_ms)
        assert year == "2024"


class TestAppendSenderToFilename:
    def test_normal_case(self):
        result = wa.append_sender_to_filename("IMG-20260512-WA0003.jpg", "JohnDoe")
        assert result == "IMG-20260512-WA0003_JohnDoe.jpg"

    def test_no_extension(self):
        result = wa.append_sender_to_filename("noext", "Me")
        assert result == "noext_Me"

    def test_sender_with_special_chars_is_sanitized(self):
        result = wa.append_sender_to_filename("file.jpg", "Alice/Bob")
        assert "/" not in result


class TestUniqueGroupName:
    def test_unused_name_returned_as_is(self):
        assert wa._unique_group_name("Family", set()) == "Family"
        assert wa._unique_group_name("Family", {"Other"}) == "Family"

    def test_collision_gets_counter_suffix(self):
        assert wa._unique_group_name("Family", {"Family"}) == "Family (2)"

    def test_multiple_collisions(self):
        existing = {"Family", "Family (2)", "Family (3)"}
        assert wa._unique_group_name("Family", existing) == "Family (4)"


# ===========================================================================
# Query builder
# ===========================================================================

class TestBuildQuery:
    def test_documents_included(self):
        query = android_handler.build_query(None, None)
        assert "Media/WhatsApp Documents/%" in query

    def test_all_media_types_present(self):
        query = android_handler.build_query(None, None)
        for path in [
            "Media/WhatsApp Images/%",
            "Media/WhatsApp Video/%",
            "Media/WhatsApp Audio/%",
            "Media/WhatsApp Voice Notes/%",
            "Media/WhatsApp Video Notes/%",
            "Media/WhatsApp Animated Gifs/%",
            "Media/WhatsApp Documents/%",
        ]:
            assert path in query, f"Missing from query: {path}"

    def test_limit_clause_included(self):
        query = android_handler.build_query(limit=100, since_ms=None)
        assert "LIMIT 50" in query  # N//2 per block

    def test_no_limit_when_none(self):
        query = android_handler.build_query(limit=None, since_ms=None)
        assert "LIMIT" not in query

    def test_since_clause_included(self):
        query = android_handler.build_query(limit=None, since_ms=1700000000000)
        assert "1700000000000" in query

    def test_both_union_blocks_have_documents(self):
        query = android_handler.build_query(None, None)
        # 2 occurrences in WHERE clauses + 2 in CASE WHEN guards = 4
        assert query.count("Media/WhatsApp Documents/%") == 4

    def test_limit_applies_to_combined_result(self):
        # LIMIT N//2 must appear twice (once per block), not on the outer wrapper
        query = android_handler.build_query(limit=50, since_ms=None)
        assert query.count("LIMIT 25") == 2
        assert not query.strip().endswith("LIMIT 50")

    def test_group_subjects_query_has_no_date_filter(self):
        query = android_handler.build_group_subjects_query()
        assert 'timestamp >=' not in query


# ===========================================================================
# Schema validation (needs an in-memory SQLite DB)
# ===========================================================================

def _make_msgstore(extra_tables=None, missing_col=None):
    """
    Build an in-memory SQLite DB that looks like a minimal WhatsApp msgstore.
    Pass extra_tables=["extra_table"] to add tables beyond the required set.
    Pass missing_col=("table", "col") to drop a required column.
    """
    conn = sqlite3.connect(":memory:")
    schema = {
        "message": [
            "_id INTEGER PRIMARY KEY",
            "timestamp INTEGER",
            "sender_jid_row_id INTEGER",
            "from_me INTEGER",
        ],
        "message_media": [
            "file_path TEXT",
            "mime_type TEXT",
            "chat_row_id INTEGER",
            "message_row_id INTEGER",
            "message_url TEXT",
            "media_name TEXT",
        ],
        "chat": [
            "_id INTEGER PRIMARY KEY",
            "subject TEXT",
            "jid_row_id INTEGER",
        ],
        "jid": [
            "_id INTEGER PRIMARY KEY",
            "user TEXT",
        ],
        "jid_map": [
            "lid_row_id INTEGER",
            "jid_row_id INTEGER",
        ],
    }
    if extra_tables:
        for t in extra_tables:
            schema[t] = ["_id INTEGER PRIMARY KEY"]

    for table, cols in schema.items():
        if missing_col and missing_col[0] == table:
            cols = [c for c in cols if not c.startswith(missing_col[1])]
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")

    conn.commit()
    return conn


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


# ===========================================================================
# Number consolidation map
# ===========================================================================

def _make_number_change_db(pairs):
    """
    Build an in-memory DB with jid and message_system_number_change tables.
    pairs: list of (old_number, new_number) tuples.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT);
        CREATE TABLE message_system_number_change (
            old_jid_row_id INTEGER,
            new_jid_row_id INTEGER
        );
    """)
    jid_id = 1
    jid_map = {}
    for old, new in pairs:
        for num in (old, new):
            if num not in jid_map:
                conn.execute("INSERT INTO jid VALUES (?, ?)", (jid_id, num))
                jid_map[num] = jid_id
                jid_id += 1
    for old, new in pairs:
        conn.execute(
            "INSERT INTO message_system_number_change VALUES (?, ?)",
            (jid_map[old], jid_map[new]),
        )
    conn.commit()
    return conn


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


# ===========================================================================
# Android: load_contacts
# ===========================================================================

class TestLoadAndroidContacts:
    def test_parses_display_name_and_number(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text("Row: display_name=Alice, data1=391234567890\n", encoding='utf-8')
        result = android_handler.load_contacts(str(f), logger)
        assert result == {"391234567890": "Alice"}

    def test_multiple_contacts(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: display_name=Alice, data1=111\n"
            "Row: display_name=Bob, data1=222\n",
            encoding='utf-8',
        )
        result = android_handler.load_contacts(str(f), logger)
        assert result == {"111": "Alice", "222": "Bob"}

    def test_email_addresses_excluded(self, tmp_path, logger):
        # data1 values containing @ are skipped by the regex
        f = tmp_path / "contacts.txt"
        f.write_text(
            "Row: display_name=Alice, data1=alice@example.com\n"
            "Row: display_name=Bob, data1=222\n",
            encoding='utf-8',
        )
        result = android_handler.load_contacts(str(f), logger)
        assert "alice@example.com" not in result
        assert result.get("222") == "Bob"

    def test_empty_file_returns_empty(self, tmp_path, logger):
        f = tmp_path / "contacts.txt"
        f.write_text("", encoding='utf-8')
        result = android_handler.load_contacts(str(f), logger)
        assert result == {}

    def test_missing_file_returns_empty(self, tmp_path, logger):
        result = android_handler.load_contacts(str(tmp_path / "nonexistent.txt"), logger)
        assert result == {}

    def test_none_path_returns_empty(self, logger):
        result = android_handler.load_contacts(None, logger)
        assert result == {}

    def test_empty_contacts_emits_warning(self, tmp_path, caplog):
        import logging as _logging
        f = tmp_path / "contacts.txt"
        f.write_text("", encoding='utf-8')
        log = _logging.getLogger("warn_test")
        log.setLevel(_logging.WARNING)
        with caplog.at_level(_logging.WARNING, logger="warn_test"):
            android_handler.load_contacts(str(f), log)
        assert any("no WhatsApp contacts" in r.message for r in caplog.records)


# ===========================================================================
# iOS: validate_ios_schema
# ===========================================================================

def _make_ios_msgstore(missing_table=None, missing_col=None):
    """
    Build an in-memory SQLite DB mimicking ChatStorage.sqlite.
    Pass missing_table='ZWAMESSAGE' to omit a table.
    Pass missing_col=('ZWAMESSAGE', 'ZMESSAGEDATE') to drop a column.
    """
    conn = sqlite3.connect(":memory:")
    schema = {
        'ZWAMESSAGE': [
            'Z_PK INTEGER PRIMARY KEY',
            'ZMESSAGEDATE REAL',
            'ZISFROMME INTEGER',
            'ZCHATSESSION INTEGER',
            'ZMEDIAITEM INTEGER',
            'ZGROUPMEMBER INTEGER',
            'ZPUSHNAME TEXT',
        ],
        'ZWACHATSESSION': [
            'Z_PK INTEGER PRIMARY KEY',
            'ZCONTACTJID TEXT',
            'ZGROUPINFO INTEGER',
            'ZPARTNERNAME TEXT',
            'ZCONTACTABID INTEGER',
            'ZLASTMESSAGEDATE REAL',
        ],
        'ZWAMEDIAITEM': [
            'Z_PK INTEGER PRIMARY KEY',
            'ZMEDIALOCALPATH TEXT',
            'ZMEDIAURL TEXT',
            'ZTITLE TEXT',
        ],
        'ZWAGROUPMEMBER': [
            'Z_PK INTEGER PRIMARY KEY',
            'ZMEMBERJID TEXT',
        ],
    }
    for table, cols in schema.items():
        if table == missing_table:
            continue
        if missing_col and missing_col[0] == table:
            cols = [c for c in cols if not c.startswith(missing_col[1])]
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
    conn.commit()
    return conn


class TestValidateIosSchema:
    def test_valid_schema_passes(self, logger):
        conn = _make_ios_msgstore()
        ios.validate_ios_schema(conn.cursor(), logger)  # should not raise

    def test_missing_zwamessage_aborts(self, logger):
        conn = _make_ios_msgstore(missing_table='ZWAMESSAGE')
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_missing_zwachatsession_aborts(self, logger):
        conn = _make_ios_msgstore(missing_table='ZWACHATSESSION')
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_missing_zwamediaitem_aborts(self, logger):
        conn = _make_ios_msgstore(missing_table='ZWAMEDIAITEM')
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_missing_column_aborts(self, logger):
        conn = _make_ios_msgstore(missing_col=('ZWAMESSAGE', 'ZMESSAGEDATE'))
        with pytest.raises(SystemExit):
            ios.validate_ios_schema(conn.cursor(), logger)

    def test_extra_tables_are_allowed(self, logger):
        conn = _make_ios_msgstore()
        conn.execute("CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        ios.validate_ios_schema(conn.cursor(), logger)  # should not raise


# ===========================================================================
# iOS: validate_ios_wa_root
# ===========================================================================

class TestValidateIosWaRoot:
    def test_valid_root_with_message_dir_passes(self, tmp_path, logger):
        (tmp_path / 'Message').mkdir()
        ios.validate_ios_wa_root(str(tmp_path), logger)  # should not raise

    def test_missing_message_dir_aborts(self, tmp_path, logger):
        # tmp_path exists but has no Message/ subdirectory
        with pytest.raises(SystemExit):
            ios.validate_ios_wa_root(str(tmp_path), logger)

    def test_nonexistent_path_aborts(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            ios.validate_ios_wa_root(str(tmp_path / 'nonexistent'), logger)


# ===========================================================================
# iOS: build_ios_query structural checks
# ===========================================================================

class TestBuildIosQuery:
    def test_zpartnername_not_null_in_group_block(self):
        query = ios.build_ios_query(None, None)
        assert 'ZPARTNERNAME IS NOT NULL' in query

    def test_media_name_restricted_to_documents_paths(self):
        query = ios.build_ios_query(None, None)
        # ZTITLE is wrapped in a CASE guarded by the Documents path check
        assert "LIKE '%Documents%'" in query
        assert 'ZTITLE' in query

    def test_group_detection_uses_zgroupinfo_not_null(self):
        query = ios.build_ios_query(None, None)
        assert 'ZGROUPINFO IS NOT NULL' in query

    def test_one_to_one_detection_uses_zgroupinfo_null(self):
        query = ios.build_ios_query(None, None)
        assert 'ZGROUPINFO IS NULL' in query

    def test_union_all_present(self):
        query = ios.build_ios_query(None, None)
        assert 'UNION ALL' in query

    def test_limit_clause_included(self):
        query = ios.build_ios_query(limit=50, since_ms=None)
        assert 'LIMIT 25' in query  # N//2 per block

    def test_no_limit_when_none(self):
        query = ios.build_ios_query(limit=None, since_ms=None)
        assert 'LIMIT' not in query

    def test_since_clause_included(self):
        since_ms = 1704067200000
        query = ios.build_ios_query(None, since_ms)
        apple_since = (since_ms / 1000.0) - ios.APPLE_EPOCH_OFFSET
        # The float value appears literally in the query string
        assert str(apple_since) in query

    def test_no_since_no_date_filter(self):
        query = ios.build_ios_query(None, None)
        assert 'ZMESSAGEDATE >=' not in query

    def test_group_sender_jid_without_at_uses_full_jid(self):
        # ZMEMBERJID with no '@' returns the full JID as sender.
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, NULL, 1, 'Group A');
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAGROUPMEMBER  VALUES (1, 'nojid');
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, 1, NULL);
        """)
        rows = conn.execute(ios.build_ios_query(None, None)).fetchall()
        assert len(rows) == 1
        sender = rows[0][6]  # sender column
        assert sender == 'nojid'

    def test_1to1_sender_jid_without_at_uses_full_jid(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, 'nojid', NULL, NULL);
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, NULL, NULL);
        """)
        rows = conn.execute(ios.build_ios_query(None, None)).fetchall()
        assert len(rows) == 1
        sender = rows[0][6]  # sender column
        assert sender == 'nojid'

    def test_group_sender_normal_jid_strips_at_suffix(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, NULL, 1, 'Group A');
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAGROUPMEMBER  VALUES (1, '447700900123@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, 1, NULL);
        """)
        rows = conn.execute(ios.build_ios_query(None, None)).fetchall()
        assert rows[0][6] == '447700900123'

    def test_1to1_sender_normal_jid_strips_at_suffix(self):
        conn = _make_ios_msgstore()
        conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '447700900456@s.whatsapp.net', NULL, NULL);
            INSERT INTO ZWAMEDIAITEM    VALUES (1, 'Message/img.jpg', NULL, NULL);
            INSERT INTO ZWAMESSAGE      VALUES (1, 1000.0, 0, 1, 1, NULL, NULL);
        """)
        rows = conn.execute(ios.build_ios_query(None, None)).fetchall()
        assert rows[0][6] == '447700900456'


# ===========================================================================
# iOS: build_ios_group_subjects_query structural checks
# ===========================================================================

class TestBuildIosGroupSubjectsQuery:
    def test_only_groups_returned(self):
        query = ios.build_ios_group_subjects_query()
        assert 'ZGROUPINFO IS NOT NULL' in query

    def test_zpartnername_not_null_filter_present(self):
        query = ios.build_ios_group_subjects_query()
        assert 'ZPARTNERNAME IS NOT NULL' in query

    def test_no_date_filter(self):
        query = ios.build_ios_group_subjects_query()
        assert 'ZMESSAGEDATE >=' not in query


# ===========================================================================
# Platform detection
# ===========================================================================

class TestDetectDbPlatform:
    def test_ios_db_detected(self, tmp_path):
        db_path = str(tmp_path / 'ios.db')
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert wa.detect_db_platform(db_path) == 'ios'

    def test_android_db_detected(self, tmp_path):
        db_path = str(tmp_path / 'android.db')
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE message (_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert wa.detect_db_platform(db_path) == 'android'


# ===========================================================================
# Backup reader: detect_encrypted
# ===========================================================================

class TestDetectEncrypted:
    def test_unencrypted_manifest_plist_returns_false(self, tmp_path, logger):
        with open(tmp_path / 'Manifest.plist', 'wb') as f:
            plistlib.dump({'IsEncrypted': False}, f)
        assert br.detect_encrypted(str(tmp_path), logger) is False

    def test_encrypted_manifest_plist_returns_true(self, tmp_path, logger):
        with open(tmp_path / 'Manifest.plist', 'wb') as f:
            plistlib.dump({'IsEncrypted': True}, f)
        assert br.detect_encrypted(str(tmp_path), logger) is True

    def test_info_plist_without_flag_falls_back_to_manifest(self, tmp_path, logger):
        # Info.plist exists but has no IsEncrypted key → fall back to Manifest.plist
        with open(tmp_path / 'Info.plist', 'wb') as f:
            plistlib.dump({'DeviceName': 'iPhone'}, f)
        with open(tmp_path / 'Manifest.plist', 'wb') as f:
            plistlib.dump({'IsEncrypted': True}, f)
        assert br.detect_encrypted(str(tmp_path), logger) is True

    def test_missing_plist_files_exits(self, tmp_path, logger):
        # No Info.plist or Manifest.plist → SystemExit
        with pytest.raises(SystemExit):
            br.detect_encrypted(str(tmp_path), logger)

    def test_nonexistent_directory_exits(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            br.detect_encrypted(str(tmp_path / 'nonexistent'), logger)


# ===========================================================================
# Backup reader: extract_to_temp
# ===========================================================================

class TestExtractToTemp:
    def test_copies_file_to_temp_and_returns_path(self, tmp_path, logger):
        hash_dir = tmp_path / 'ab'
        hash_dir.mkdir()
        fake_file = hash_dir / ('ab' + 'c' * 38)
        fake_file.write_bytes(b'sqlite data')
        manifest_map = {'ChatStorage.sqlite': str(fake_file)}

        temp_path = br.extract_to_temp(manifest_map, 'ChatStorage.sqlite', logger)
        try:
            assert os.path.isfile(temp_path)
            with open(temp_path, 'rb') as f:
                assert f.read() == b'sqlite data'
        finally:
            os.unlink(temp_path)

    def test_preserves_file_extension(self, tmp_path, logger):
        src = tmp_path / 'srcfile'
        src.write_bytes(b'data')
        manifest_map = {'ContactsV2.sqlite': str(src)}

        temp_path = br.extract_to_temp(manifest_map, 'ContactsV2.sqlite', logger)
        try:
            assert temp_path.endswith('.sqlite')
        finally:
            os.unlink(temp_path)

    def test_file_not_in_manifest_exits(self, logger):
        with pytest.raises(SystemExit):
            br.extract_to_temp({}, 'ChatStorage.sqlite', logger)

    def test_hash_file_missing_from_disk_exits(self, tmp_path, logger):
        manifest_map = {'ChatStorage.sqlite': str(tmp_path / 'nonexistent_hash')}
        with pytest.raises(SystemExit):
            br.extract_to_temp(manifest_map, 'ChatStorage.sqlite', logger)


# ===========================================================================
# Backup reader: extract_plaintext
# ===========================================================================

class TestExtractPlaintext:
    def _make_backup(self, tmp_path):
        """Minimal fake backup: Manifest.db + hash files for ChatStorage and a media file."""
        import sqlite3 as _sq
        tmp_path.mkdir(parents=True, exist_ok=True)
        manifest_db = tmp_path / 'Manifest.db'
        conn = _sq.connect(str(manifest_db))
        conn.execute("""
            CREATE TABLE Files (
                fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER
            )
        """)
        # ChatStorage.sqlite
        chat_id = 'aa' + 'b' * 38
        conn.execute("INSERT INTO Files VALUES (?,?,?,1)",
                     (chat_id, 'AppDomainGroup-group.net.whatsapp.WhatsApp.shared',
                      'ChatStorage.sqlite'))
        # Media file
        media_id = 'cc' + 'd' * 38
        conn.execute("INSERT INTO Files VALUES (?,?,?,1)",
                     (media_id, 'AppDomainGroup-group.net.whatsapp.WhatsApp.shared',
                      'Message/Media/WhatsApp Images/IMG001.jpg'))
        conn.commit()
        conn.close()
        # Create hash directories and files
        for fid, content in [(chat_id, b'SQLITE'), (media_id, b'JPEGDATA')]:
            d = tmp_path / fid[:2]
            d.mkdir(exist_ok=True)
            (d / fid).write_bytes(content)
        return tmp_path

    def test_returns_manifest_map_msgstore_path(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / 'backup')
        output_dir = tmp_path / 'output'
        manifest_map, msgstore_path, contacts_path = br.extract_plaintext(
            str(backup_dir), str(output_dir), None, False, logger
        )
        assert 'ChatStorage.sqlite' in manifest_map
        assert 'Message/Media/WhatsApp Images/IMG001.jpg' in manifest_map
        assert os.path.isfile(msgstore_path)
        assert msgstore_path == str(output_dir / 'ChatStorage.sqlite')

    def test_contacts_none_when_not_in_backup(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / 'backup')
        output_dir = tmp_path / 'output'
        _, _, contacts_path = br.extract_plaintext(
            str(backup_dir), str(output_dir), None, False, logger
        )
        assert contacts_path is None

    def test_contacts_override_respected(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / 'backup')
        output_dir = tmp_path / 'output'
        fake_contacts = str(tmp_path / 'contacts.sqlite')
        _, _, contacts_path = br.extract_plaintext(
            str(backup_dir), str(output_dir), fake_contacts, False, logger
        )
        assert contacts_path == fake_contacts

    def test_overwrite_warning_logged(self, tmp_path, logger):
        backup_dir = self._make_backup(tmp_path / 'backup')
        output_dir = tmp_path / 'output'
        output_dir.mkdir()
        # Pre-create the DB file so overwrite warning fires
        (output_dir / 'ChatStorage.sqlite').write_bytes(b'OLD')
        import unittest.mock as mock
        with mock.patch.object(logger, 'warning') as mock_warn:
            br.extract_plaintext(str(backup_dir), str(output_dir), None, False, logger)
        warning_msgs = ' '.join(str(c) for c in mock_warn.call_args_list)
        assert 'Overwriting' in warning_msgs or 'overwriting' in warning_msgs.lower()


# ===========================================================================
# Backup reader: extract_encrypted
# ===========================================================================

class TestExtractEncrypted:
    def _make_mock_backup(self, tmp_path):
        """Return a mock EncryptedBackup that extracts fake files."""
        import unittest.mock as mock

        def fake_extract_file(*, relative_path, domain_like, output_filename):
            if relative_path == 'ChatStorage.sqlite':
                with open(output_filename, 'wb') as f:
                    f.write(b'SQLITE')
            elif relative_path == 'ContactsV2.sqlite':
                with open(output_filename, 'wb') as f:
                    f.write(b'CONTACTS')
            elif relative_path == 'Message/Media/WhatsApp Images/IMG001.jpg':
                with open(output_filename, 'wb') as f:
                    f.write(b'MEDIAFILE')
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

        fake_mod = types.ModuleType('iphone_backup_decrypt')
        fake_mod.EncryptedBackup = mock.MagicMock(return_value=mock_backup)
        fake_mod.DomainLike = mock.MagicMock()
        return mock.patch.dict(sys.modules, {'iphone_backup_decrypt': fake_mod})

    def test_returns_resolver_and_msgstore(self, tmp_path, logger):
        mock_backup = self._make_mock_backup(tmp_path)
        output_dir = tmp_path / 'output'

        with self._patch_library(mock_backup):
            msgstore_path, contacts_path, resolver = br.extract_encrypted(
                str(tmp_path / 'backup'), 'secret', str(output_dir),
                None, False, logger,
            )

        assert os.path.isfile(msgstore_path)
        assert msgstore_path == str(output_dir / 'ChatStorage.sqlite')
        assert contacts_path is not None
        # Resolver decrypts on demand
        media_path = resolver('Message/Media/WhatsApp Images/IMG001.jpg')
        assert media_path is not None
        assert os.path.isfile(media_path)
        # Second call returns the cached path without re-decrypting
        mock_backup.extract_file.reset_mock()
        media_path2 = resolver('Message/Media/WhatsApp Images/IMG001.jpg')
        assert media_path2 == media_path
        mock_backup.extract_file.assert_not_called()

    def test_resolver_returns_none_for_missing_file(self, tmp_path, logger):
        mock_backup = self._make_mock_backup(tmp_path)
        output_dir = tmp_path / 'output'

        with self._patch_library(mock_backup):
            _, _, resolver = br.extract_encrypted(
                str(tmp_path / 'backup'), 'secret', str(output_dir),
                None, False, logger,
            )

        result = resolver('Message/Media/nonexistent.jpg')
        assert result is None

    def test_wrong_password_exits(self, tmp_path, logger):
        import unittest.mock as mock
        mock_backup = mock.MagicMock()
        mock_backup.test_decryption.side_effect = ValueError("incorrect passphrase")
        output_dir = tmp_path / 'output'

        with self._patch_library(mock_backup):
            with pytest.raises(SystemExit):
                br.extract_encrypted(
                    str(tmp_path / 'backup'), 'wrongpw', str(output_dir),
                    None, False, logger,
                )

    def test_missing_library_exits(self, tmp_path, logger):
        import sys
        import unittest.mock as mock
        output_dir = tmp_path / 'output'

        with mock.patch.dict(sys.modules, {'iphone_backup_decrypt': None}):
            with pytest.raises(SystemExit):
                br.extract_encrypted(
                    str(tmp_path / 'backup'), 'secret', str(output_dir),
                    None, False, logger,
                )

    def test_contacts_override_skips_extraction(self, tmp_path, logger):
        mock_backup = self._make_mock_backup(tmp_path)
        output_dir = tmp_path / 'output'
        fake_contacts = str(tmp_path / 'my_contacts.sqlite')

        with self._patch_library(mock_backup):
            _, contacts_path, _ = br.extract_encrypted(
                str(tmp_path / 'backup'), 'secret', str(output_dir),
                fake_contacts, False, logger,
            )

        assert contacts_path == fake_contacts
        # extract_file should only have been called for ChatStorage, not ContactsV2
        calls = [c.kwargs.get('relative_path') for c in mock_backup.extract_file.call_args_list]
        assert 'ContactsV2.sqlite' not in calls

class TestResolveGroupFolder:
    def test_new_group_assigned_sanitized_name(self):
        index = {}
        folder = wa.resolve_group_folder('42', 'Family Chat', index)
        assert folder == 'Family Chat'
        assert index['42']['folder'] == 'Family Chat'

    def test_existing_group_returns_stable_name(self):
        # Even if subject has changed, the stable folder from the index is returned.
        index = {'42': {'folder': 'OldName', 'subject': 'OldName'}}
        folder = wa.resolve_group_folder('42', 'NewName', index)
        assert folder == 'OldName'

    def test_none_subject_gets_unknown_fallback(self):
        index = {}
        folder = wa.resolve_group_folder('99', None, index)
        assert 'Unknown Group' in folder
        assert '99' in folder

    def test_name_collision_with_existing_group_disambiguated(self):
        index = {'10': {'folder': 'Friends', 'subject': 'Friends'}}
        folder = wa.resolve_group_folder('20', 'Friends', index)
        assert folder == 'Friends (2)'

    def test_special_chars_in_subject_sanitized(self):
        index = {}
        folder = wa.resolve_group_folder('1', 'Chat: Family/Work', index)
        assert '/' not in folder
        assert ':' not in folder


# ===========================================================================
# Archive DB: record_file_archived and check_db_health
# ===========================================================================

class TestRecordFileArchived:
    def test_creates_file_and_copy_records(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = b'\x01' * 16
        wa.record_file_archived(cursor, 'orig.jpg', md5,
                                'Contacts/Alice (00111)/2024/Received/orig.jpg')
        conn.commit()
        row = conn.execute(
            "SELECT md5 FROM files WHERE original_path = 'orig.jpg'"
        ).fetchone()
        assert row[0] == md5
        copy = conn.execute(
            "SELECT archive_path FROM archive_copies WHERE original_path = 'orig.jpg'"
        ).fetchone()
        assert 'Contacts/Alice (00111)/2024/Received/orig.jpg' == copy[0]
        conn.close()

    def test_duplicate_archive_path_silently_ignored(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = b'\x01' * 16
        wa.record_file_archived(cursor, 'orig.jpg', md5, 'Contacts/path.jpg')
        wa.record_file_archived(cursor, 'orig.jpg', md5, 'Contacts/path.jpg')
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_copies WHERE original_path = 'orig.jpg'"
        ).fetchone()[0]
        assert count == 1
        conn.close()

    def test_multiple_archive_paths_for_same_original(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = b'\x01' * 16
        wa.record_file_archived(cursor, 'orig.jpg', md5, 'path1.jpg')
        wa.record_file_archived(cursor, 'orig.jpg', md5, 'path2.jpg')
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_copies WHERE original_path = 'orig.jpg'"
        ).fetchone()[0]
        assert count == 2
        conn.close()


class TestCheckDbHealth:
    def test_healthy_db_passes(self, tmp_path, logger):
        conn = wa.open_archive_db(str(tmp_path))
        wa.check_db_health(conn, logger)  # should not raise
        conn.close()


# ===========================================================================
# CSV reports
# ===========================================================================

class TestWriteMissingReport:
    def test_no_rows_does_not_create_file(self, tmp_path, logger):
        path = str(tmp_path / 'missing.csv')
        wa.write_missing_report(path, [], logger)
        assert not os.path.exists(path)

    def test_rows_written_to_csv_with_correct_fields(self, tmp_path, logger):
        rows = [{
            'message_id': 1,
            'timestamp_human': '2024-01-15 00:00:00',
            'original_filename': 'img.jpg',
            'mime_type': 'image/jpeg',
            'sender': 'Alice',
            'chat_name': 'Family',
            'direction': 'Received',
            'file_path': 'Media/WhatsApp Images/img.jpg',
            'message_url': 'https://mmg.whatsapp.net/v/example',
            'media_name': '',
        }]
        path = str(tmp_path / 'missing.csv')
        wa.write_missing_report(path, rows, logger)
        assert os.path.isfile(path)
        with open(path, encoding='utf-8') as f:
            content = f.read()
        assert 'message_id' in content
        assert 'img.jpg' in content
        assert 'Alice' in content


class TestWriteDuplicateReport:
    def test_no_duplicates_does_not_create_file(self, tmp_path, logger):
        conn = wa.open_archive_db(str(tmp_path))
        path = str(tmp_path / 'dups.csv')
        wa.write_duplicate_report(path, conn, logger)
        conn.close()
        assert not os.path.exists(path)

    def test_duplicates_written_to_csv(self, tmp_path, logger):
        import hashlib
        conn = wa.open_archive_db(str(tmp_path))
        cursor = conn.cursor()
        md5 = hashlib.md5(b'data').digest()
        cursor.execute("INSERT INTO files VALUES (?, ?)", ('orig.jpg', md5))
        cursor.execute("INSERT INTO archive_copies VALUES (?, ?)",
                       ('orig.jpg', 'Contacts/Alice (00111)/2024/Received/orig.jpg'))
        cursor.execute("INSERT INTO archive_copies VALUES (?, ?)",
                       ('orig.jpg', 'Groups/Family/2024/orig_Alice.jpg'))
        conn.commit()
        path = str(tmp_path / 'dups.csv')
        wa.write_duplicate_report(path, conn, logger)
        conn.close()
        assert os.path.isfile(path)
        with open(path, encoding='utf-8') as f:
            content = f.read()
        assert 'md5_hex' in content
        assert 'file_count' in content


# ===========================================================================
# Contact/group rename sync
# ===========================================================================

class TestSyncFolderNames:
    def test_unchanged_name_leaves_folder_intact(self, tmp_path, logger):
        contacts_root = tmp_path / 'Contacts'
        contacts_root.mkdir()
        (contacts_root / 'Alice (00111)').mkdir()
        folder_index = {'111': ('Alice (00111)', 'Alice')}
        contacts = {'111': 'Alice'}
        result = wa.sync_folder_names(
            contacts, {}, str(tmp_path), folder_index, logger
        )
        assert result['111'][0] == 'Alice (00111)'
        assert (contacts_root / 'Alice (00111)').exists()

    def test_changed_name_renames_folder_on_disk(self, tmp_path, logger):
        contacts_root = tmp_path / 'Contacts'
        contacts_root.mkdir()
        (contacts_root / 'OldName (00111)').mkdir()
        folder_index = {'111': ('OldName (00111)', 'OldName')}
        contacts = {'111': 'NewName'}
        result = wa.sync_folder_names(
            contacts, {}, str(tmp_path), folder_index, logger
        )
        assert result['111'][0] == 'NewName (00111)'
        assert not (contacts_root / 'OldName (00111)').exists()
        assert (contacts_root / 'NewName (00111)').exists()

    def test_rename_skipped_when_target_exists_index_updated(self, tmp_path, logger):
        contacts_root = tmp_path / 'Contacts'
        contacts_root.mkdir()
        (contacts_root / 'OldName (00111)').mkdir()
        (contacts_root / 'NewName (00111)').mkdir()  # target already exists
        folder_index = {'111': ('OldName (00111)', 'OldName')}
        contacts = {'111': 'NewName'}
        result = wa.sync_folder_names(
            contacts, {}, str(tmp_path), folder_index, logger
        )
        # Index is updated to the new name even though the on-disk rename was skipped,
        # so future runs don't re-detect the mismatch and re-fire the warning.
        assert result['111'][0] == 'NewName (00111)'

    def test_name_changed_no_folder_on_disk_index_updated(self, tmp_path, logger):
        # Name changed but folder has never been created; index should update anyway
        folder_index = {'111': ('OldName (00111)', 'OldName')}
        contacts = {'111': 'NewName'}
        result = wa.sync_folder_names(
            contacts, {}, str(tmp_path), folder_index, logger
        )
        assert result['111'][0] == 'NewName (00111)'

    def test_new_contact_registered_in_index(self, tmp_path, logger):
        result = wa.sync_folder_names(
            {'111': 'Alice'}, {}, str(tmp_path), {}, logger
        )
        assert '111' in result

    def test_number_map_resolves_old_to_canonical(self, tmp_path, logger):
        # Contact is known by old number '111'; canonical is '222'
        contacts = {'111': 'Alice'}
        number_map = {'111': '222'}
        result = wa.sync_folder_names(
            contacts, number_map, str(tmp_path), {}, logger
        )
        # Registered under canonical '222', folder uses canonical number
        assert '222' in result
        assert result['222'][0] == 'Alice (00222)'


class TestSyncGroupNames:
    def test_unchanged_subject_leaves_folder_intact(self, tmp_path, logger):
        groups_root = tmp_path / 'Groups'
        groups_root.mkdir()
        (groups_root / 'Family').mkdir()
        group_index = {'42': {'folder': 'Family', 'subject': 'Family'}}
        result = wa.sync_group_names(
            {'42': 'Family'}, str(tmp_path), group_index, logger
        )
        assert result['42']['folder'] == 'Family'
        assert (groups_root / 'Family').exists()

    def test_changed_subject_renames_folder_on_disk(self, tmp_path, logger):
        groups_root = tmp_path / 'Groups'
        groups_root.mkdir()
        (groups_root / 'OldName').mkdir()
        group_index = {'42': {'folder': 'OldName', 'subject': 'OldName'}}
        result = wa.sync_group_names(
            {'42': 'NewName'}, str(tmp_path), group_index, logger
        )
        assert result['42']['folder'] == 'NewName'
        assert not (groups_root / 'OldName').exists()
        assert (groups_root / 'NewName').exists()

    def test_rename_skipped_when_target_exists_index_updated(self, tmp_path, logger):
        groups_root = tmp_path / 'Groups'
        groups_root.mkdir()
        (groups_root / 'OldName').mkdir()
        (groups_root / 'NewName').mkdir()  # target already exists
        group_index = {'42': {'folder': 'OldName', 'subject': 'OldName'}}
        result = wa.sync_group_names(
            {'42': 'NewName'}, str(tmp_path), group_index, logger
        )
        # Index is updated even when the on-disk rename is skipped,
        # so future runs don't re-detect the mismatch and repeat the warning.
        assert result['42']['folder'] == 'NewName'

    def test_new_group_not_in_index_is_skipped(self, tmp_path, logger):
        # New groups are not yet in group_index; sync_group_names skips them
        result = wa.sync_group_names(
            {'99': 'Brand New Group'}, str(tmp_path), {}, logger
        )
        assert '99' not in result


# ===========================================================================
# Core processing: process_rows
# ===========================================================================

class TestProcessRows:
    """
    Uses a fixed timestamp of 1705276800000 (2024-01-15 00:00:00 UTC).
    All destination path assertions use year='2024'.
    """

    _TS = 1705276800000  # 2024-01-15 00:00:00 UTC

    def _row(self, msg_id=1, timestamp=None, file_path='img.jpg',
             mime_type=None, chat_row_id='42', chat_subject='Family',
             sender='111', key_from_me=0, message_url=None, media_name=None):
        return (msg_id, timestamp if timestamp is not None else self._TS,
                file_path, mime_type, chat_row_id, chat_subject,
                sender, key_from_me, message_url, media_name)

    def _setup_src(self, tmp_path, name='img.jpg', content=b'image data'):
        src_dir = tmp_path / 'src'
        src_dir.mkdir(exist_ok=True)
        (src_dir / name).write_bytes(content)
        return str(src_dir)

    def _resolver(self, src_dir):
        return lambda fp: os.path.join(src_dir, fp)

    # --- Routing ---

    def test_group_chat_routed_to_groups_folder(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(chat_subject='Family', sender='111', key_from_me=0)]
        stats, _, _, missing = wa.process_rows(
            rows, 1, {'111': 'Alice'}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert stats['copied'] == 1
        assert stats['missing'] == 0
        assert os.path.isfile(
            os.path.join(out, 'Groups', 'Family', '2024', 'img_Alice.jpg')
        )

    def test_group_own_message_tagged_me(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        # key_from_me=1, sender=None — own group message
        rows = [self._row(chat_subject='Family', sender=None, key_from_me=1)]
        stats, _, _, _ = wa.process_rows(
            rows, 1, {}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert stats['copied'] == 1
        assert os.path.isfile(
            os.path.join(out, 'Groups', 'Family', '2024', 'img_Me.jpg')
        )

    def test_one_to_one_received_routed_to_received_folder(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        # chat_subject=None → 1-to-1 chat
        rows = [self._row(chat_subject=None, sender='111', key_from_me=0)]
        stats, _, _, _ = wa.process_rows(
            rows, 1, {'111': 'Alice'}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert stats['copied'] == 1
        assert os.path.isfile(
            os.path.join(out, 'Contacts', 'Alice (00111)', '2024', 'Received', 'img.jpg')
        )

    def test_one_to_one_sent_routed_to_sent_folder(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender='111', key_from_me=1)]
        wa.process_rows(
            rows, 1, {'111': 'Alice'}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert os.path.isfile(
            os.path.join(out, 'Contacts', 'Alice (00111)', '2024', 'Sent', 'img.jpg')
        )

    def test_unknown_sender_falls_back_to_phone_number(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender='999', key_from_me=0)]
        wa.process_rows(
            rows, 1, {}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert os.path.isfile(
            os.path.join(out, 'Contacts', 'Unknown (00999)', '2024', 'Received', 'img.jpg')
        )

    def test_number_map_consolidates_old_number_to_canonical(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender='111', key_from_me=0)]
        # 111 is the old number; 222 is canonical; contacts only knows 222
        wa.process_rows(
            rows, 1, {'222': 'Alice'}, {'111': '222'}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert os.path.isfile(
            os.path.join(out, 'Contacts', 'Alice (00222)', '2024', 'Received', 'img.jpg')
        )

    # --- Missing / invalid rows ---

    def test_missing_source_file_added_to_missing_rows(self, tmp_path, logger):
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(file_path='nonexistent.jpg')]
        stats, _, _, missing = wa.process_rows(
            rows, 1, {}, {}, {}, {},
            lambda fp: str(tmp_path / 'nowhere' / fp),
            out, logger, dry_run=False,
        )
        assert stats['missing'] == 1
        assert stats['copied'] == 0
        assert len(missing) == 1

    def test_null_file_path_added_to_missing_rows(self, tmp_path, logger):
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(file_path=None)]
        stats, _, _, missing = wa.process_rows(
            rows, 1, {}, {}, {}, {},
            lambda fp: fp,
            out, logger, dry_run=False,
        )
        assert stats['missing'] == 1
        assert len(missing) == 1

    def test_null_timestamp_added_to_missing_rows(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        row = (1, None, 'img.jpg', None, '42', 'Family', '111', 0, None, None)
        stats, _, _, missing = wa.process_rows(
            [row], 1, {}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert stats['missing'] == 1
        assert stats['copied'] == 0
        assert len(missing) == 1

    # --- Dry run ---

    def test_dry_run_increments_copied_but_writes_no_file(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(chat_subject='Family', sender='111')]
        stats, _, _, _ = wa.process_rows(
            rows, 1, {'111': 'Alice'}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=True,
        )
        assert stats['copied'] == 1
        assert not os.path.isfile(
            os.path.join(out, 'Groups', 'Family', '2024', 'img_Alice.jpg')
        )

    # --- Re-run / deduplication ---

    def test_identical_file_skipped_on_rerun(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path, content=b'same data')
        out = str(tmp_path / 'out')
        # Pre-place identical file at the expected destination
        dest_dir = os.path.join(out, 'Groups', 'Family', '2024')
        os.makedirs(dest_dir)
        with open(os.path.join(dest_dir, 'img_Alice.jpg'), 'wb') as f:
            f.write(b'same data')
        rows = [self._row(chat_subject='Family', sender='111')]
        stats, _, _, _ = wa.process_rows(
            rows, 1, {'111': 'Alice'}, {}, {}, {},
            self._resolver(src_dir), out, logger, dry_run=False,
        )
        assert stats['skipped'] == 1
        assert stats['copied'] == 0

    # --- Archive DB integration ---

    def test_archive_db_updated_on_new_copy(self, tmp_path, logger):
        src_dir = self._setup_src(tmp_path)
        out = str(tmp_path / 'out')
        os.makedirs(out)
        rows = [self._row(chat_subject=None, sender='111', key_from_me=0,
                          file_path='img.jpg')]
        archive_conn = wa.open_archive_db(out)
        try:
            wa.process_rows(
                rows, 1, {'111': 'Alice'}, {}, {}, {},
                self._resolver(src_dir), out, logger, dry_run=False,
                conn=archive_conn,
            )
            archive_conn.commit()
            count = archive_conn.execute(
                "SELECT COUNT(*) FROM archive_copies WHERE original_path = 'img.jpg'"
            ).fetchone()[0]
            assert count == 1
        finally:
            archive_conn.close()


# ===========================================================================
# Filesystem: resolve_unique_dest
# ===========================================================================

class TestResolveUniqueDest:
    def test_new_path_returned_as_is(self, tmp_path, logger):
        dest = str(tmp_path / "image.jpg")
        src = str(tmp_path / "src.jpg")
        tmp_path.joinpath("src.jpg").write_bytes(b"data")
        assert wa.resolve_unique_dest(dest, src, logger) == dest

    def test_identical_file_returns_none(self, tmp_path, logger):
        data = b"same content"
        src = tmp_path / "src.jpg"
        dest = tmp_path / "image.jpg"
        src.write_bytes(data)
        dest.write_bytes(data)
        result = wa.resolve_unique_dest(str(dest), str(src), logger)
        assert result is None

    def test_collision_gets_numbered_suffix(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        dest = tmp_path / "image.jpg"
        src.write_bytes(b"new content")
        dest.write_bytes(b"old content")
        result = wa.resolve_unique_dest(str(dest), str(src), logger)
        assert result == str(tmp_path / "image_1.jpg")

    def test_multiple_collisions_increment_counter(self, tmp_path, logger):
        src = tmp_path / "src.jpg"
        dest = tmp_path / "image.jpg"
        src.write_bytes(b"new content")
        dest.write_bytes(b"old content")
        (tmp_path / "image_1.jpg").write_bytes(b"another collision")
        result = wa.resolve_unique_dest(str(dest), str(src), logger)
        assert result == str(tmp_path / "image_2.jpg")


# ===========================================================================
# Filesystem: validate_wa_root
# ===========================================================================

class TestValidateWaRoot:
    def test_valid_root_passes(self, tmp_path, logger):
        media = tmp_path / "Media"
        media.mkdir()
        (media / "WhatsApp Images").mkdir()
        android_handler.validate_wa_root(str(tmp_path), logger)  # should not raise

    def test_nonexistent_path_aborts(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root(str(tmp_path / "does_not_exist"), logger)

    def test_media_folder_passed_directly_aborts(self, tmp_path, logger):
        media = tmp_path / "Media"
        media.mkdir()
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root(str(media), logger)

    def test_subfolder_of_media_passed_aborts(self, tmp_path, logger):
        images = tmp_path / "WhatsApp Images"
        images.mkdir()
        with pytest.raises(SystemExit):
            android_handler.validate_wa_root(str(images), logger)

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
        android_handler.validate_wa_root(str(tmp_path), caplog_logger)  # must not raise SystemExit
        assert logging.WARNING in handler_called


# ===========================================================================
# Archive DB: contact and group persistence round-trip
# ===========================================================================

class TestContactPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        index = {"391234567890": ("Alice (00391234567890)", "Alice")}
        wa.save_contacts_to_db(conn, index)
        conn.commit()
        loaded = wa.load_contacts_from_db(conn)
        conn.close()
        assert loaded == index

    def test_update_existing_contact(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        wa.save_contacts_to_db(conn, {"111": ("Old Name (00111)", "Old Name")})
        wa.save_contacts_to_db(conn, {"111": ("New Name (00111)", "New Name")})
        conn.commit()
        loaded = wa.load_contacts_from_db(conn)
        conn.close()
        assert loaded["111"] == ("New Name (00111)", "New Name")


class TestGroupPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        index = {"42": {"folder": "Family Chat", "subject": "Family Chat"}}
        wa.save_groups_to_db(conn, index)
        conn.commit()
        loaded = wa.load_groups_from_db(conn)
        conn.close()
        assert loaded == index


# ===========================================================================
# iOS: timestamp conversion
# ===========================================================================

class TestIosTimestampConversion:
    def test_apple_epoch_offset_value(self):
        assert ios.APPLE_EPOCH_OFFSET == 978307200

    def test_query_contains_epoch_conversion(self):
        query = ios.build_ios_query(None, None)
        assert "978307200" in query
        assert "1000" in query

    def test_since_converted_to_apple_epoch(self):
        # Unix epoch ms for 2024-01-01 00:00:00 UTC = 1704067200000
        since_ms = 1704067200000
        query = ios.build_ios_query(None, since_ms)
        expected_apple = (since_ms / 1000.0) - ios.APPLE_EPOCH_OFFSET
        assert str(int(expected_apple)) in query or f"{expected_apple:.1f}" in query


# ===========================================================================
# iOS: build_manifest_map
# ===========================================================================

def _make_manifest_db(tmp_path, rows):
    """
    Create a synthetic Manifest.db with the given rows.
    rows: list of (fileID, relativePath) tuples.
    """
    db_path = tmp_path / "Manifest.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE Files (
            fileID       TEXT PRIMARY KEY,
            domain       TEXT,
            relativePath TEXT
        )
    """)
    for file_id, relative_path in rows:
        conn.execute(
            "INSERT INTO Files VALUES (?, ?, ?)",
            (file_id, br._WA_DOMAIN, relative_path)
        )
    conn.commit()
    conn.close()
    return tmp_path


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


# ===========================================================================
# Backup reader: build_manifest_map domain filtering (WhatsApp Business)
# ===========================================================================

def _make_manifest_db_multi_domain(tmp_path, rows):
    """
    Create a Manifest.db with rows as (fileID, domain, relativePath) tuples.
    Allows testing domain-based filtering directly.
    """
    db_path = tmp_path / "Manifest.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE Files (
            fileID       TEXT PRIMARY KEY,
            domain       TEXT,
            relativePath TEXT
        )
    """)
    for file_id, domain, relative_path in rows:
        conn.execute("INSERT INTO Files VALUES (?, ?, ?)", (file_id, domain, relative_path))
    conn.commit()
    conn.close()


class TestBuildManifestMapDomain:
    def test_default_domain_excludes_business_files(self, tmp_path, logger):
        _make_manifest_db_multi_domain(tmp_path, [
            ("aa" + "1" * 38, br._WA_DOMAIN,          "ChatStorage.sqlite"),
            ("bb" + "2" * 38, br._WA_BUSINESS_DOMAIN, "ChatStorage.sqlite"),
        ])
        result = br.build_manifest_map(str(tmp_path), logger)
        assert len(result) == 1
        assert list(result.values())[0].endswith("aa" + "1" * 38)

    def test_business_domain_excludes_regular_files(self, tmp_path, logger):
        _make_manifest_db_multi_domain(tmp_path, [
            ("aa" + "1" * 38, br._WA_DOMAIN,          "ChatStorage.sqlite"),
            ("bb" + "2" * 38, br._WA_BUSINESS_DOMAIN, "ChatStorage.sqlite"),
        ])
        result = br.build_manifest_map(str(tmp_path), logger, domain=br._WA_BUSINESS_DOMAIN)
        assert len(result) == 1
        assert list(result.values())[0].endswith("bb" + "2" * 38)

    def test_business_domain_constant_value(self):
        assert br._WA_BUSINESS_DOMAIN == \
            'AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared'


# ===========================================================================
# iOS: load_ios_contacts
# ===========================================================================

def _make_contacts_db(tmp_path, rows):
    """
    Create a synthetic ContactsV2.sqlite.
    rows: list of (jid, full_name) tuples.
    """
    db_path = tmp_path / "ContactsV2.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE ZWAADDRESSBOOKCONTACT (
            ZWHATSAPPID TEXT,
            ZFULLNAME   TEXT
        )
    """)
    for jid, name in rows:
        conn.execute("INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (?, ?)", (jid, name))
    conn.commit()
    conn.close()
    return str(db_path)


class TestLoadIosContacts:
    def test_parses_jid_to_number(self, tmp_path, logger):
        path = _make_contacts_db(tmp_path, [
            ("393357214425@s.whatsapp.net", "Alice")
        ])
        result = ios.load_ios_contacts(path, logger)
        assert result == {"393357214425": "Alice"}

    def test_multiple_contacts(self, tmp_path, logger):
        path = _make_contacts_db(tmp_path, [
            ("111@s.whatsapp.net", "Alice"),
            ("222@s.whatsapp.net", "Bob"),
        ])
        result = ios.load_ios_contacts(path, logger)
        assert result == {"111": "Alice", "222": "Bob"}

    def test_non_whatsapp_jids_excluded(self, tmp_path, logger):
        path = _make_contacts_db(tmp_path, [
            ("111@s.whatsapp.net", "Alice"),
            ("222@other.net", "Bob"),
        ])
        result = ios.load_ios_contacts(path, logger)
        assert "222" not in result
        assert "111" in result

    def test_null_name_excluded(self, tmp_path, logger):
        path = _make_contacts_db(tmp_path, [
            ("111@s.whatsapp.net", None),
            ("222@s.whatsapp.net", "Bob"),
        ])
        result = ios.load_ios_contacts(path, logger)
        assert "111" not in result
        assert result["222"] == "Bob"

    def test_missing_file_returns_empty(self, tmp_path, logger):
        result = ios.load_ios_contacts(str(tmp_path / "nonexistent.sqlite"), logger)
        assert result == {}


# ===========================================================================
# iOS: build_ios_number_map
# ===========================================================================

def _make_ios_sessions_db(pairs):
    """
    Build an in-memory ZWACHATSESSION DB with ZCONTACTABID grouping.
    pairs: list of (old_jid, new_jid, shared_abid) tuples.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ZWACHATSESSION (
            Z_PK              INTEGER PRIMARY KEY,
            ZCONTACTJID       TEXT,
            ZGROUPINFO        INTEGER,
            ZCONTACTABID      INTEGER,
            ZLASTMESSAGEDATE  REAL
        )
    """)
    pk = 1
    ts = 1000.0
    for old_jid, new_jid, abid in pairs:
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTABID, ZLASTMESSAGEDATE) VALUES (?, ?, ?, ?)",
            (pk, old_jid, abid, ts)
        )
        pk += 1
        ts += 100.0
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTABID, ZLASTMESSAGEDATE) VALUES (?, ?, ?, ?)",
            (pk, new_jid, abid, ts)
        )
        pk += 1
        ts += 100.0
    conn.commit()
    return conn


class TestBuildIosNumberMap:
    def test_single_number_change(self, logger):
        conn = _make_ios_sessions_db([
            ("111@s.whatsapp.net", "222@s.whatsapp.net", 42)
        ])
        result = ios.build_ios_number_map(conn.cursor(), logger)
        assert result == {"111": "222"}

    def test_chain_resolved(self, logger):
        # A -> B -> C: use separate abids per pair, share via timestamps
        conn = _make_ios_sessions_db([
            ("111@s.whatsapp.net", "222@s.whatsapp.net", 1),
            ("222@s.whatsapp.net", "333@s.whatsapp.net", 2),
        ])
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


# ===========================================================================
# adb_extractor
# ===========================================================================

class TestCheckAdb:
    def test_found(self, logger):
        with patch("adb_extractor.shutil.which", return_value="/usr/bin/adb"):
            assert adb.check_adb(logger) is True

    def test_not_found(self, logger):
        with patch("adb_extractor.shutil.which", return_value=None):
            assert adb.check_adb(logger) is False


class TestCheckDeviceConnected:
    def _make_result(self, stdout):
        r = MagicMock()
        r.stdout = stdout.encode()
        return r

    def test_device_connected(self, logger):
        output = "List of devices attached\nemulator-5554\tdevice\n"
        with patch("adb_extractor.subprocess.run", return_value=self._make_result(output)):
            assert adb.check_device_connected(logger) is True

    def test_no_devices(self, logger):
        output = "List of devices attached\n"
        with patch("adb_extractor.subprocess.run", return_value=self._make_result(output)):
            assert adb.check_device_connected(logger) is False

    def test_unauthorized_not_counted(self, logger):
        output = "List of devices attached\n98abc123\tunauthorized\n"
        with patch("adb_extractor.subprocess.run", return_value=self._make_result(output)):
            assert adb.check_device_connected(logger) is False

    def test_adb_failure(self, logger):
        with patch("adb_extractor.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, 'adb', stderr=b"error")):
            assert adb.check_device_connected(logger) is False


class TestPullMsgstore:
    def test_regular_path(self, logger, tmp_path):
        with patch("adb_extractor.subprocess.run") as mock_run:
            result = adb.pull_msgstore(str(tmp_path), business=False, logger=logger)
            call_args = mock_run.call_args[0][0]
            assert 'com.whatsapp/WhatsApp' in call_args[2]
            assert 'com.whatsapp.w4b' not in call_args[2]
        assert result == str(tmp_path / 'msgstore.db.crypt15') or \
               result == os.path.join(str(tmp_path), 'msgstore.db.crypt15')

    def test_business_path(self, logger, tmp_path):
        with patch("adb_extractor.subprocess.run") as mock_run:
            adb.pull_msgstore(str(tmp_path), business=True, logger=logger)
            call_args = mock_run.call_args[0][0]
            assert 'com.whatsapp.w4b' in call_args[2]

    def test_failure_raises(self, logger, tmp_path):
        with patch("adb_extractor.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, 'adb', stderr=b"fail")):
            with pytest.raises(subprocess.CalledProcessError):
                adb.pull_msgstore(str(tmp_path), logger=logger)


class TestPullContacts:
    def test_filters_whatsapp_lines(self, logger, tmp_path):
        raw = (
            "Row: 0 display_name=Alice, data1=391234567890@s.whatsapp.net\n"
            "Row: 1 display_name=Bob, data1=bob@gmail.com\n"
            "Row: 2 display_name=Carol, data1=390987654321@s.whatsapp.net\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = raw.encode()
        with patch("adb_extractor.subprocess.run", return_value=mock_result):
            dest = adb.pull_contacts(str(tmp_path), logger=logger)

        content = open(dest, encoding='utf-8').read()
        assert '@s.whatsapp.net' in content
        assert 'bob@gmail.com' not in content
        assert content.count('@s.whatsapp.net') == 2

    def test_failure_raises(self, logger, tmp_path):
        with patch("adb_extractor.subprocess.run",
                   side_effect=subprocess.CalledProcessError(1, 'adb', stderr=b"fail")):
            with pytest.raises(subprocess.CalledProcessError):
                adb.pull_contacts(str(tmp_path), logger=logger)


# ---------------------------------------------------------------------------
# TestCheckDependencies
# ---------------------------------------------------------------------------

class TestCheckDependencies:
    def _args(self, **kwargs):
        """Return a minimal namespace; only set the fields under test."""
        defaults = dict(mode=None, e2e_key=None, ios_password=None)
        defaults.update(kwargs)
        import argparse
        return argparse.Namespace(**defaults)

    def test_no_issues_passes_silently(self, logger):
        args = self._args()
        wa.check_dependencies(args, logger)  # must not raise

    def test_adb_missing_raises(self, logger):
        args = self._args(mode='adb')
        with patch("wa_media_archiver.shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)

    def test_adb_present_passes(self, logger):
        args = self._args(mode='adb')
        with patch("wa_media_archiver.shutil.which", return_value="/usr/bin/adb"):
            wa.check_dependencies(args, logger)  # must not raise

    def test_wa_crypt_tools_missing_raises(self, logger):
        args = self._args(e2e_key='key.bin')
        with patch("importlib.util.find_spec", return_value=None):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)

    def test_iphone_backup_decrypt_missing_raises(self, logger):
        args = self._args(ios_password='secret')
        with patch("importlib.util.find_spec", return_value=None):
            with pytest.raises(SystemExit):
                wa.check_dependencies(args, logger)

    def test_multiple_missing_reported_together(self, logger, caplog):
        args = self._args(mode='adb', e2e_key='key.bin')
        # Capture what logger.error receives by inspecting the call
        errors = []
        logger.error = lambda msg, *a, **kw: errors.append(msg)
        with patch("wa_media_archiver.shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=None), \
             pytest.raises(SystemExit):
            wa.check_dependencies(args, logger)
        assert len(errors) == 1, "Expected a single combined error message"
        assert "adb" in errors[0]
        assert "wa-crypt-tools" in errors[0]
