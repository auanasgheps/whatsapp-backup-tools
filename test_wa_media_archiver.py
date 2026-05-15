"""
Tests for wa_media_archiver.py

Run with:
    pytest test_wa_media_archiver.py -v

Requires:
    pip install pytest
"""

import logging
import os
import sqlite3
import sys

import pytest

# ---------------------------------------------------------------------------
# Import the script as a module.
# We skip the if __name__ == '__main__' block because we import, not run it.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.dirname(__file__))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "wa_media_archiver",
    os.path.join(os.path.dirname(__file__), "wa_media_archiver.py"),
)
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)


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
        query = wa.build_query(None, None)
        assert "Media/WhatsApp Documents/%" in query

    def test_all_media_types_present(self):
        query = wa.build_query(None, None)
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
        query = wa.build_query(limit=100, since_ms=None)
        assert "LIMIT 100" in query

    def test_no_limit_when_none(self):
        query = wa.build_query(limit=None, since_ms=None)
        assert "LIMIT" not in query

    def test_since_clause_included(self):
        query = wa.build_query(limit=None, since_ms=1700000000000)
        assert "1700000000000" in query

    def test_both_union_blocks_have_documents(self):
        query = wa.build_query(None, None)
        assert query.count("Media/WhatsApp Documents/%") == 2


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
        wa.validate_schema(cur, logger)  # should not raise

    def test_missing_table_aborts(self, logger):
        conn = sqlite3.connect(":memory:")  # completely empty DB
        cur = conn.cursor()
        with pytest.raises(SystemExit):
            wa.validate_schema(cur, logger)

    def test_missing_column_aborts(self, logger):
        conn = _make_msgstore(missing_col=("message", "timestamp"))
        cur = conn.cursor()
        with pytest.raises(SystemExit):
            wa.validate_schema(cur, logger)

    def test_extra_tables_are_allowed(self, logger):
        conn = _make_msgstore(extra_tables=["props", "message_thumbnail"])
        cur = conn.cursor()
        wa.validate_schema(cur, logger)  # should not raise


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
        result = wa.build_number_map(conn.cursor(), logger)
        assert result == {}

    def test_single_change(self, logger):
        conn = _make_number_change_db([("111", "222")])
        result = wa.build_number_map(conn.cursor(), logger)
        assert result == {"111": "222"}

    def test_chain_resolved(self, logger):
        # A -> B -> C should resolve A -> C
        conn = _make_number_change_db([("111", "222"), ("222", "333")])
        result = wa.build_number_map(conn.cursor(), logger)
        assert result["111"] == "333"
        assert result["222"] == "333"

    def test_missing_table_returns_empty(self, logger):
        conn = sqlite3.connect(":memory:")  # no tables at all
        result = wa.build_number_map(conn.cursor(), logger)
        assert result == {}


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
        wa.validate_wa_root(str(tmp_path), logger)  # should not raise

    def test_nonexistent_path_aborts(self, tmp_path, logger):
        with pytest.raises(SystemExit):
            wa.validate_wa_root(str(tmp_path / "does_not_exist"), logger)

    def test_media_folder_passed_directly_aborts(self, tmp_path, logger):
        media = tmp_path / "Media"
        media.mkdir()
        with pytest.raises(SystemExit):
            wa.validate_wa_root(str(media), logger)

    def test_subfolder_of_media_passed_aborts(self, tmp_path, logger):
        images = tmp_path / "WhatsApp Images"
        images.mkdir()
        with pytest.raises(SystemExit):
            wa.validate_wa_root(str(images), logger)

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
        wa.validate_wa_root(str(tmp_path), caplog_logger)  # must not raise SystemExit
        assert logging.WARNING in handler_called


# ===========================================================================
# Archive DB: contact and group persistence round-trip
# ===========================================================================

class TestContactPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        index = {"391234567890": "Alice (00391234567890)"}
        wa.save_contacts_to_db(conn, index)
        conn.commit()
        loaded = wa.load_contacts_from_db(conn)
        conn.close()
        assert loaded == index

    def test_update_existing_contact(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        wa.save_contacts_to_db(conn, {"111": "Old Name (00111)"})
        wa.save_contacts_to_db(conn, {"111": "New Name (00111)"})
        conn.commit()
        loaded = wa.load_contacts_from_db(conn)
        conn.close()
        assert loaded["111"] == "New Name (00111)"


class TestGroupPersistence:
    def test_save_and_load_round_trip(self, tmp_path):
        conn = wa.open_archive_db(str(tmp_path))
        index = {"42": {"folder": "Family Chat", "subject": "Family Chat"}}
        wa.save_groups_to_db(conn, index)
        conn.commit()
        loaded = wa.load_groups_from_db(conn)
        conn.close()
        assert loaded == index
