"""
Tests for shared.schema_validator
"""

import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from shared import schema_validator
from wab_viewer import main as viewer


def create_mock_android_db(
    missing_table: str | None, missing_column: tuple[str, str] | None
) -> sqlite3.Connection:
    """Create in-memory SQLite DB with Android WhatsApp schema."""
    conn = sqlite3.connect(":memory:")
    schema = {
        "message": [
            "_id INTEGER PRIMARY KEY",
            "chat_row_id INTEGER",
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
        "chat": ["_id INTEGER PRIMARY KEY", "subject TEXT", "jid_row_id INTEGER"],
        "jid": ["_id INTEGER PRIMARY KEY", "user TEXT"],
        "jid_map": ["lid_row_id INTEGER", "jid_row_id INTEGER"],
    }
    for tbl, cols in schema.items():
        if tbl == missing_table:
            continue
        col_defs = cols
        if missing_column is not None and missing_column[0] == tbl:
            col_defs = [c for c in cols if not c.startswith(missing_column[1])]
        conn.execute(f"CREATE TABLE {tbl} ({', '.join(col_defs)})")
    conn.commit()
    return conn


def create_mock_ios_db(
    missing_table: str | None, missing_column: tuple[str, str] | None
) -> sqlite3.Connection:
    """Create in-memory SQLite DB with iOS WhatsApp schema."""
    conn = sqlite3.connect(":memory:")
    schema = {
        "ZWAMESSAGE": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZMESSAGEDATE REAL",
            "ZISFROMME INTEGER",
            "ZCHATSESSION INTEGER",
            "ZMEDIAITEM INTEGER",
            "ZGROUPMEMBER INTEGER",
            "ZPUSHNAME TEXT",
        ],
        "ZWACHATSESSION": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZCONTACTJID TEXT",
            "ZGROUPINFO INTEGER",
            "ZPARTNERNAME TEXT",
            "ZCONTACTABID INTEGER",
            "ZLASTMESSAGEDATE REAL",
        ],
        "ZWAMEDIAITEM": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZMEDIALOCALPATH TEXT",
            "ZMEDIAURL TEXT",
            "ZTITLE TEXT",
        ],
        "ZWAGROUPMEMBER": [
            "Z_PK INTEGER PRIMARY KEY",
            "ZMEMBERJID TEXT",
        ],
    }
    for tbl, cols in schema.items():
        if tbl == missing_table:
            continue
        col_defs = cols
        if missing_column is not None and missing_column[0] == tbl:
            col_defs = [c for c in cols if not c.startswith(missing_column[1])]
        conn.execute(f"CREATE TABLE {tbl} ({', '.join(col_defs)})")
    conn.commit()
    return conn


def test_valid_android_schema():
    conn = create_mock_android_db(None, None)
    res = schema_validator.check_wa_schema(conn.cursor(), "android", "1.0.0")
    assert res.is_valid is True
    assert res.missing_tables == []
    assert res.missing_columns == {}
    assert res.issue_url == ""
    assert res.banner == ""


def test_missing_android_table():
    conn = create_mock_android_db("message", None)
    res = schema_validator.check_wa_schema(conn.cursor(), "android", "1.0.0")
    assert res.is_valid is False
    assert "message" in res.missing_tables
    assert "The Android WhatsApp database does not match" in res.banner
    assert "WhatsApp Android schema validation failed" in res.summary
    assert "pip install --upgrade whatsapp-backup-tools" in res.banner
    assert "https://github.com/auanasgheps/whatsapp-backup-tools/issues/new" in res.banner
    assert res.issue_url.startswith(
        "https://github.com/auanasgheps/whatsapp-backup-tools/issues/new"
    )


def test_missing_android_column():
    conn = create_mock_android_db(None, ("message", "timestamp"))
    res = schema_validator.check_wa_schema(conn.cursor(), "android", "1.0.0")
    assert res.is_valid is False
    assert "message" in res.missing_columns
    assert "timestamp" in res.missing_columns["message"]
    assert "timestamp" in res.banner
    assert "The Android WhatsApp database does not match" in res.banner
    assert "WhatsApp Android schema validation failed" in res.summary
    assert "pip install --upgrade whatsapp-backup-tools" in res.banner


def test_valid_ios_schema():
    conn = create_mock_ios_db(None, None)
    res = schema_validator.check_wa_schema(conn.cursor(), "ios", "1.0.0")
    assert res.is_valid is True
    assert res.missing_tables == []
    assert res.missing_columns == {}


def test_missing_ios_table():
    conn = create_mock_ios_db("ZWAMESSAGE", None)
    res = schema_validator.check_wa_schema(conn.cursor(), "ios", "1.0.0")
    assert res.is_valid is False
    assert "ZWAMESSAGE" in res.missing_tables
    assert "The iOS WhatsApp database does not match" in res.banner
    assert "WhatsApp iOS schema validation failed" in res.summary
    assert "pip install --upgrade whatsapp-backup-tools" in res.banner


def test_missing_ios_column():
    conn = create_mock_ios_db(None, ("ZWAMESSAGE", "ZMESSAGEDATE"))
    res = schema_validator.check_wa_schema(conn.cursor(), "ios", "1.0.0")
    assert res.is_valid is False
    assert "ZWAMESSAGE" in res.missing_columns
    assert "ZMESSAGEDATE" in res.missing_columns["ZWAMESSAGE"]
    assert "ZMESSAGEDATE" in res.banner


def test_unknown_platform_raises():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="Unknown platform 'macos'"):
        schema_validator.validate_archiver_schema(conn.cursor(), "macos", "1.0.0")
    with pytest.raises(ValueError, match="Unknown platform 'macos'"):
        schema_validator.validate_viewer_schema(conn.cursor(), "macos", "1.0.0")


def test_validate_viewer_schema_android():
    conn = create_mock_android_db(None, None)
    res = schema_validator.validate_viewer_schema(conn.cursor(), "android", "1.0.0")
    assert res.is_valid is True

    bad_conn = create_mock_android_db("chat", None)
    bad_res = schema_validator.validate_viewer_schema(bad_conn.cursor(), "android", "1.0.0")
    assert bad_res.is_valid is False
    assert "chat" in bad_res.missing_tables


def test_validate_viewer_schema_ios():
    conn = create_mock_ios_db(None, None)
    res = schema_validator.validate_viewer_schema(conn.cursor(), "ios", "1.0.0")
    assert res.is_valid is True

    bad_conn = create_mock_ios_db("ZWAMESSAGE", None)
    bad_res = schema_validator.validate_viewer_schema(bad_conn.cursor(), "ios", "1.0.0")
    assert bad_res.is_valid is False
    assert "ZWAMESSAGE" in bad_res.missing_tables


def test_github_issue_url_generation():
    url = schema_validator.build_github_issue_url(
        "android",
        ["message_media"],
        {"message": ["sender_jid_row_id"]},
        "PRAGMA user_version=5; SQLite=3.45.0",
        "0.70",
    )
    assert url.startswith("https://github.com/auanasgheps/whatsapp-backup-tools/issues/new?")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    assert "title" in qs
    assert "Android" in qs["title"][0]
    assert "body" in qs
    body = qs["body"][0]
    assert "0.70" in body
    assert "message_media" in body
    assert "sender_jid_row_id" in body
    assert "PRAGMA user_version=5" in body


def test_fingerprint_extraction_android():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 42")
    conn.execute("CREATE TABLE props (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO props VALUES ('version', '2.24.1.5')")
    conn.commit()

    fp = schema_validator.extract_database_fingerprint(conn.cursor(), "android")
    assert "PRAGMA user_version=42" in fp
    assert "props.version=2.24.1.5" in fp


def test_fingerprint_extraction_ios():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 17")
    conn.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER, Z_PLIST BLOB)")
    conn.execute("INSERT INTO Z_METADATA VALUES (3, NULL)")
    conn.commit()

    fp = schema_validator.extract_database_fingerprint(conn.cursor(), "ios")
    assert "PRAGMA user_version=17" in fp
    assert "Z_METADATA.Z_VERSION=3" in fp


def test_viewer_schema_incompatible_fallback(tmp_path: Path):
    wa_db_dir = tmp_path / "Whatsapp Databases"
    wa_db_dir.mkdir()
    msgstore_path = wa_db_dir / "msgstore.db"

    # Create an invalid WhatsApp DB (missing required tables)
    bad_conn = sqlite3.connect(str(msgstore_path))
    bad_conn.execute("CREATE TABLE dummy (id INT)")
    bad_conn.commit()
    bad_conn.close()

    # Create minimal archive DB
    arc_conn = sqlite3.connect(str(tmp_path / ".wa_media_archiver.db"))
    arc_conn.execute(
        "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
    )
    arc_conn.execute(
        "CREATE TABLE groups (chat_row_id TEXT PRIMARY KEY, folder TEXT, subject TEXT)"
    )
    arc_conn.execute("CREATE TABLE files (original_path TEXT PRIMARY KEY, md5 BLOB)")
    arc_conn.execute(
        "CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT, PRIMARY KEY (original_path, archive_path))"
    )
    arc_conn.execute(
        "CREATE TABLE recent_messages (chat_id TEXT, chat_type TEXT, msg_id INT, timestamp_ms INT, sender TEXT, from_me INT, archive_path TEXT, media_type TEXT, media_name TEXT, text_body TEXT, quoted_text TEXT, quoted_sender TEXT, quoted_ts INT)"
    )
    arc_conn.commit()
    arc_conn.close()

    app = viewer.create_app(tmp_path, False)
    assert app.config["SCHEMA_ALERT"] is not None
    assert "missing tables" in app.config["SCHEMA_ALERT"]["summary"]

    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "schema-alert-banner" in html
    assert "WhatsApp Database Schema Incompatibility Detected" in html
    assert "Report Issue on GitHub" in html


def test_detect_legacy_android_messages_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (_id INT, key_remote_jid TEXT, data TEXT)")
    conn.execute("CREATE TABLE chat_list (_id INT, key_remote_jid TEXT)")
    conn.commit()

    res = schema_validator.validate_archiver_schema(conn.cursor(), "android", "1.0.0")
    assert res.is_valid is False
    assert res.is_legacy is True
    assert "The Android WhatsApp database appears to be from an older, legacy version" in res.banner
    assert "Legacy WhatsApp Android database detected" in res.summary
    assert "LEGACY OR UNSUPPORTED WHATSAPP DATABASE DETECTED" in res.banner
    assert "Found legacy 'messages' table" in res.banner
    assert (
        "[Feature Request / Legacy Schema] Support for older legacy WhatsApp Android database format"
        in urllib.parse.unquote_plus(res.issue_url)
    )


def test_detect_legacy_android_remote_jid_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE message (_id INT, key_remote_jid TEXT, data TEXT)")
    conn.commit()

    res = schema_validator.validate_archiver_schema(conn.cursor(), "android", "1.0.0")
    assert res.is_valid is False
    assert res.is_legacy is True
    assert "The Android WhatsApp database appears to be from an older, legacy version" in res.banner
    assert "Legacy WhatsApp Android database detected" in res.summary
    assert "key_remote_jid" in res.banner


def test_detect_legacy_ios_zmessage_table():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ZMESSAGE (Z_PK INT, ZTEXT TEXT)")
    conn.commit()

    res = schema_validator.validate_archiver_schema(conn.cursor(), "ios", "1.0.0")
    assert res.is_valid is False
    assert res.is_legacy is True
    assert "The iOS WhatsApp database appears to be from an older, legacy version" in res.banner
    assert "Legacy WhatsApp iOS database detected" in res.summary
    assert "Found legacy 'ZMESSAGE' table" in res.banner
    assert (
        "[Feature Request / Legacy Schema] Support for older legacy WhatsApp iOS database format"
        in urllib.parse.unquote_plus(res.issue_url)
    )


def test_detect_legacy_ios_media_path_in_zwamessage():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ZWAMESSAGE (Z_PK INT, ZMESSAGEDATE REAL, ZISFROMME INT, ZCHATSESSION INT, ZMEDIALOCALPATH TEXT)"
    )
    conn.execute("CREATE TABLE ZWACHATSESSION (Z_PK INT, ZCONTACTJID TEXT)")
    conn.commit()

    res = schema_validator.validate_archiver_schema(conn.cursor(), "ios", "1.0.0")
    assert res.is_valid is False
    assert res.is_legacy is True
    assert "The iOS WhatsApp database appears to be from an older, legacy version" in res.banner
    assert "Legacy WhatsApp iOS database detected" in res.summary
    assert "ZWAMEDIAITEM" in res.banner


def test_viewer_legacy_schema_alert(tmp_path: Path):
    wa_db_dir = tmp_path / "Whatsapp Databases"
    wa_db_dir.mkdir()
    msgstore_path = wa_db_dir / "msgstore.db"

    # Create a legacy Android WhatsApp DB (messages table instead of message)
    legacy_conn = sqlite3.connect(str(msgstore_path))
    legacy_conn.execute("CREATE TABLE messages (_id INT, key_remote_jid TEXT, data TEXT)")
    legacy_conn.commit()
    legacy_conn.close()

    # Create minimal archive DB
    arc_conn = sqlite3.connect(str(tmp_path / ".wa_media_archiver.db"))
    arc_conn.execute(
        "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
    )
    arc_conn.execute(
        "CREATE TABLE groups (chat_row_id TEXT PRIMARY KEY, folder TEXT, subject TEXT)"
    )
    arc_conn.execute("CREATE TABLE files (original_path TEXT PRIMARY KEY, md5 BLOB)")
    arc_conn.execute(
        "CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT, PRIMARY KEY (original_path, archive_path))"
    )
    arc_conn.execute(
        "CREATE TABLE recent_messages (chat_id TEXT, chat_type TEXT, msg_id INT, timestamp_ms INT, sender TEXT, from_me INT, archive_path TEXT, media_type TEXT, media_name TEXT, text_body TEXT, quoted_text TEXT, quoted_sender TEXT, quoted_ts INT)"
    )
    arc_conn.commit()
    arc_conn.close()

    app = viewer.create_app(tmp_path, False)
    assert app.config["SCHEMA_ALERT"] is not None
    assert app.config["SCHEMA_ALERT"]["is_legacy"] is True
    assert "Legacy WhatsApp Android database detected" in app.config["SCHEMA_ALERT"]["summary"]

    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "schema-alert-banner" in html
    assert "Legacy WhatsApp Database Detected" in html
    assert "Request Support on GitHub" in html


def test_viewer_legacy_ios_schema_alert(tmp_path: Path):
    wa_db_dir = tmp_path / "Whatsapp Databases"
    wa_db_dir.mkdir()
    chatstorage_path = wa_db_dir / "ChatStorage.sqlite"

    # Create a legacy iOS WhatsApp DB (ZMESSAGE table instead of ZWAMESSAGE)
    legacy_conn = sqlite3.connect(str(chatstorage_path))
    legacy_conn.execute("CREATE TABLE ZMESSAGE (Z_PK INT, ZTEXT TEXT)")
    legacy_conn.commit()
    legacy_conn.close()

    # Create minimal archive DB
    arc_conn = sqlite3.connect(str(tmp_path / ".wa_media_archiver.db"))
    arc_conn.execute(
        "CREATE TABLE contacts (number TEXT PRIMARY KEY, folder TEXT, display_name TEXT)"
    )
    arc_conn.execute(
        "CREATE TABLE groups (chat_row_id TEXT PRIMARY KEY, folder TEXT, subject TEXT)"
    )
    arc_conn.execute("CREATE TABLE files (original_path TEXT PRIMARY KEY, md5 BLOB)")
    arc_conn.execute(
        "CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT, PRIMARY KEY (original_path, archive_path))"
    )
    arc_conn.execute(
        "CREATE TABLE recent_messages (chat_id TEXT, chat_type TEXT, msg_id INT, timestamp_ms INT, sender TEXT, from_me INT, archive_path TEXT, media_type TEXT, media_name TEXT, text_body TEXT, quoted_text TEXT, quoted_sender TEXT, quoted_ts INT)"
    )
    arc_conn.commit()
    arc_conn.close()

    app = viewer.create_app(tmp_path, False)
    assert app.config["SCHEMA_ALERT"] is not None
    assert app.config["SCHEMA_ALERT"]["is_legacy"] is True
    assert app.config["SCHEMA_ALERT"]["platform"] == "ios"
    assert "Legacy WhatsApp iOS database detected" in app.config["SCHEMA_ALERT"]["summary"]

    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "schema-alert-banner" in html
    assert "Legacy WhatsApp Database Detected" in html
    assert "Legacy WhatsApp iOS database detected" in html
