"""
Tests for wab_viewer
"""

import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from wab_viewer import main as viewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_archive_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS contacts (
            number       TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS groups (
            chat_row_id  TEXT PRIMARY KEY,
            folder       TEXT NOT NULL,
            subject      TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS files (
            original_path TEXT PRIMARY KEY,
            md5           BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_copies (
            original_path TEXT NOT NULL REFERENCES files(original_path),
            archive_path  TEXT NOT NULL,
            PRIMARY KEY (original_path, archive_path)
        );
        CREATE TABLE IF NOT EXISTS recent_messages (
            chat_id        TEXT NOT NULL,
            chat_type      TEXT NOT NULL,
            msg_id         INTEGER NOT NULL,
            timestamp_ms   INTEGER NOT NULL,
            sender         TEXT NOT NULL,
            from_me        INTEGER NOT NULL,
            archive_path   TEXT,
            media_type     TEXT NOT NULL DEFAULT 'text',
            media_name     TEXT,
            text_body      TEXT NOT NULL,
            quoted_text    TEXT,
            quoted_sender  TEXT,
            quoted_ts      INTEGER,
            reactions      TEXT,
            PRIMARY KEY (chat_id, chat_type, msg_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recent_chat_ts
            ON recent_messages(chat_id, chat_type, timestamp_ms DESC);
        CREATE TABLE IF NOT EXISTS reactions_cache (
            chat_id    TEXT NOT NULL,
            msg_id     INTEGER NOT NULL,
            reactions  TEXT NOT NULL,
            PRIMARY KEY (chat_id, msg_id)
        );
    """)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def make_android_db(path: Path) -> sqlite3.Connection:
    """Minimal msgstore.db with the tables the viewer joins against."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE jid (
            _id   INTEGER PRIMARY KEY,
            user  TEXT
        );
        CREATE TABLE chat (
            _id                     INTEGER PRIMARY KEY,
            jid_row_id              INTEGER,
            subject                 TEXT,
            hidden                  INTEGER DEFAULT 0,
            sort_timestamp          INTEGER,
            display_message_row_id  INTEGER
        );
        CREATE TABLE message (
            _id          INTEGER PRIMARY KEY,
            chat_row_id  INTEGER NOT NULL,
            from_me      INTEGER NOT NULL DEFAULT 0,
            sender_jid_row_id INTEGER,
            timestamp    INTEGER,
            text_data    TEXT,
            message_type INTEGER DEFAULT 0
        );
        CREATE TABLE message_media (
            _id             INTEGER PRIMARY KEY,
            message_row_id  INTEGER,
            file_path       TEXT,
            media_name      TEXT
        );
        CREATE TABLE message_quoted (
            message_row_id  INTEGER PRIMARY KEY,
            text_data       TEXT,
            from_me         INTEGER DEFAULT 0,
            sender_jid_row_id INTEGER,
            timestamp       INTEGER DEFAULT 0
        );
        CREATE TABLE jid_map (
            lid_row_id  INTEGER,
            jid_row_id  INTEGER
        );
    """)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def seed_android_db(wa_conn, archive_conn):
    """Insert one contact chat with one text message."""
    wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '123456789')")
    wa_conn.execute(
        "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
        "VALUES (10, 1, NULL, 0, 1700000000000, 1)"
    )
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (1, 10, 0, 1, 1700000000000, 'Hello world', 0)"
    )
    wa_conn.commit()

    archive_conn.execute(
        "INSERT INTO contacts (number, folder, display_name) VALUES ('123456789', 'Alice (00123456789)', 'Alice')"
    )
    archive_conn.commit()


def make_cache_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(viewer.CACHE_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Tests: _media_type_from_path
# ---------------------------------------------------------------------------

class TestMediaTypeFromPath:
    def test_image_jpg(self):
        assert viewer._media_type_from_path("foo/bar.jpg") == "image"

    def test_image_png(self):
        assert viewer._media_type_from_path("foo/IMG.png") == "image"

    def test_video_mp4(self):
        assert viewer._media_type_from_path("foo/video.mp4") == "video"

    def test_audio_mp3(self):
        assert viewer._media_type_from_path("foo/voice.mp3") == "audio"

    def test_gif(self):
        assert viewer._media_type_from_path("foo/anim.gif") == "gif"

    def test_sticker(self):
        assert viewer._media_type_from_path("foo/Sticker/abc.webp") == "sticker"

    def test_document(self):
        assert viewer._media_type_from_path("foo/document.pdf") == "document"

    def test_none_returns_text(self):
        assert viewer._media_type_from_path(None) == "text"

    def test_empty_string_returns_document(self):
        # No extension → falls through to "document"
        assert viewer._media_type_from_path("") == "document"

    def test_no_extension_returns_document(self):
        # No recognized extension → falls through to "document"
        assert viewer._media_type_from_path("foo/bar") == "document"

    def test_uppercase_extension_normalized(self):
        assert viewer._media_type_from_path("foo/bar.MP4") == "video"

    def test_path_with_multiple_dots(self):
        # Last extension wins
        assert viewer._media_type_from_path("foo/file.name.tar.gz") == "document"
        assert viewer._media_type_from_path("foo/file.name.jpg") == "image"

    def test_mixed_case_extension(self):
        assert viewer._media_type_from_path("photo.JpG") == "image"


# ---------------------------------------------------------------------------
# Tests: _detect_source_db
# ---------------------------------------------------------------------------

class TestDetectSourceDb:
    def test_android_detected(self, tmp_path):
        (tmp_path / "msgstore.db").touch()
        typ, path = viewer._detect_source_db(tmp_path)
        assert typ == "android"
        assert path == str(tmp_path / "msgstore.db")

    def test_ios_detected(self, tmp_path):
        (tmp_path / "ChatStorage.sqlite").touch()
        typ, path = viewer._detect_source_db(tmp_path)
        assert typ == "ios"
        assert path == str(tmp_path / "ChatStorage.sqlite")

    def test_android_wins_when_both_present(self, tmp_path):
        (tmp_path / "msgstore.db").touch()
        (tmp_path / "ChatStorage.sqlite").touch()
        typ, path = viewer._detect_source_db(tmp_path)
        assert typ == "android"

    def test_none_when_no_source_db(self, tmp_path):
        typ, path = viewer._detect_source_db(tmp_path)
        assert typ is None
        assert path is None


# ---------------------------------------------------------------------------
# Tests: cache schema
# ---------------------------------------------------------------------------

class TestCacheSchema:
    def test_schema_creates_tables(self, tmp_path):
        cache_path = tmp_path / ".wa_viewer.db"
        conn = sqlite3.connect(str(cache_path))
        conn.executescript(viewer.CACHE_SCHEMA)

        tables = {t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "message_index" in tables
        assert "sync_meta" in tables
        assert "indexed_chats" in tables
        assert "user_preferences" in tables

        indexes = {i[0] for i in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        assert "idx_midx_chat_ts" in indexes

        virtuals = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5%'"
        ).fetchall()]
        assert "message_index_fts" in virtuals


# ---------------------------------------------------------------------------
# Tests: freshness check
# ---------------------------------------------------------------------------

class TestFreshnessCheck:
    def test_changed_when_no_stamp(self, tmp_path):
        db_path = tmp_path / "msgstore.db"
        db_path.write_text("x")
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        assert viewer._source_changed(cache_conn, str(db_path)) is True

    def test_unchanged_after_stamp_saved(self, tmp_path):
        db_path = tmp_path / "msgstore.db"
        db_path.write_text("x")
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._save_source_stamp(cache_conn, str(db_path))
        assert viewer._source_changed(cache_conn, str(db_path)) is False

    def test_changed_after_file_grows(self, tmp_path):
        db_path = tmp_path / "msgstore.db"
        db_path.write_text("x")
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._save_source_stamp(cache_conn, str(db_path))
        db_path.write_text("xxxx")  # size changed
        assert viewer._source_changed(cache_conn, str(db_path)) is True


# ---------------------------------------------------------------------------
# Tests: FTS index build (Android)
# ---------------------------------------------------------------------------

class TestFtsBuildAndroid:
    def test_fts_index_populated(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._build_fts_chat(cache_conn, "android", str(wa_path), "123456789", "contact")

        rows = cache_conn.execute("SELECT * FROM message_index").fetchall()
        assert len(rows) == 1
        assert rows[0]["chat_type"] == "contact"

    def test_fts_search_works(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._build_fts_chat(cache_conn, "android", str(wa_path), "123456789", "contact")

        results = cache_conn.execute("""
            SELECT mi.rowid, mi.chat_id FROM message_index mi
            JOIN message_index_fts fts ON fts.rowid = mi.rowid
            WHERE message_index_fts MATCH 'Hello'
        """).fetchall()
        assert len(results) == 1
        assert results[0]["chat_id"] == "123456789"

    def test_system_messages_excluded(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        # system message: no text, no media (message_type=0)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (99, 10, 0, 1700000001000, NULL, 0)"
        )
        # system event: non-zero message_type but no media file (e.g. encryption notice, business account change)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (100, 10, 0, 1700000002000, NULL, 12)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._build_fts_chat(cache_conn, "android", str(wa_path), "123456789", "contact")

        rows = cache_conn.execute("SELECT * FROM message_index").fetchall()
        assert len(rows) == 1  # both system events excluded


# ---------------------------------------------------------------------------
# Tests: Flask routes
# ---------------------------------------------------------------------------

class TestFlaskRoutes:
    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_index_returns_html(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"<!DOCTYPE html>" in resp.data

    def test_api_chats_returns_list(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/chats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "123456789"
        assert data[0]["display_name"] == "Alice"
        assert data[0]["last_msg_preview"] == "Hello world"
        assert data[0]["last_msg_type"] == "text"
        assert "msg_count" not in data[0]

    def test_api_chats_display_message_row_id_resolves_preview(self, tmp_path):
        """display_message_row_id points to the specific message shown as preview.
        A system event inserted after the real message must not change the preview
        or the sort order — sort_timestamp and display_message_row_id are managed
        by WhatsApp and already reflect this."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)  # real message id=1 at ts=1700000000000
        # system event inserted after; display_message_row_id still points to message 1
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (99, 10, 0, 1800000000000, NULL, 12)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chats").get_json()
        assert len(data) == 1
        # preview comes from display_message_row_id=1 ('Hello world'), not the system event
        assert data[0]["last_msg_preview"] == "Hello world"
        # sort uses sort_timestamp=1700000000000 set by seed_android_db
        assert data[0]["newest_ts"] == 1700000000000

    def test_api_messages_returns_messages(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["text_body"] == "Hello world"

    def test_api_messages_cursor_before(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/messages?chat_id=123456789&chat_type=contact&before=1800000000000&limit=10")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_api_messages_at(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/messages/at?chat_id=123456789&chat_type=contact&ts=1700000000000")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_api_messages_at_missing_ts(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/messages/at?chat_id=123456789&chat_type=contact")
        assert resp.status_code == 400

    def test_api_search_finds_message(self, app_and_tmp):
        import time
        client, tmp = app_and_tmp
        # Open the chat; wait for background indexing to complete
        client.get("/api/messages?chat_id=123456789&chat_type=contact")
        deadline = time.time() + 5
        while time.time() < deadline:
            if client.get("/api/chat-index-status?chat_id=123456789&chat_type=contact"
                          ).get_json()["status"] == "done":
                break
            time.sleep(0.05)
        resp = client.get("/api/search?q=Hello&chat_id=123456789&chat_type=contact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "results" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["text_body"] == "Hello world"

    def test_api_search_empty_query(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/search?q=")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["results"] == []

    def test_api_search_bad_fts_query(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/search?q=AND")
        assert resp.status_code == 400

    def test_media_404_for_missing_file(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/media/Contacts/IMG.jpg")
        assert resp.status_code == 404

    def test_media_403_for_path_traversal(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/media/../wa_media_archiver.py")
        assert resp.status_code == 403

    def test_media_range_request_returns_slice(self, app_and_tmp):
        client, tmp = app_and_tmp
        # Write a known file into the archive root
        media_file = tmp / "test_video.mp4"
        media_file.write_bytes(b"0123456789ABCDEF")  # 16 bytes

        resp = client.get("/media/test_video.mp4", headers={"Range": "bytes=4-9"})
        assert resp.status_code == 206
        assert resp.data == b"456789"
        assert resp.headers["Content-Length"] == "6"
        assert resp.headers["Content-Range"] == "bytes 4-9/16"

    def test_media_range_request_does_not_load_full_file(self, app_and_tmp):
        client, tmp = app_and_tmp
        media_file = tmp / "test_video.mp4"
        media_file.write_bytes(b"0123456789ABCDEF")  # 16 bytes

        resp = client.get("/media/test_video.mp4", headers={"Range": "bytes=0-3"})
        assert resp.status_code == 206
        assert len(resp.data) == 4  # must not return full 16 bytes

    def test_group_message_sender_resolved_to_name(self, tmp_path):
        """Inbound group message sender should be resolved to the contact display name."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Add a group chat: jid 2 (group JID), chat 20 with subject
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (20, 2, 'Test Group')")
        # Group member JID
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (3, '987654321')")
        # Inbound message from group member jid 3
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (10, 20, 0, 3, 1700000000001, 'Hi group', 0)"
        )
        wa_conn.commit()
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('987654321', 'Bob (00987654321)', 'Bob')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=20&chat_type=group").get_json()

        assert len(data) == 1
        assert data[0]["sender"] == "Bob"

    def test_1on1_received_sender_not_raw_number(self, tmp_path):
        """Inbound 1-on-1 message: sender must be the contact name (or +number),
        never the raw phone number without a + prefix."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        # seed_android_db creates jid 1 (user='123456789'), chat 10, message 1 (sender_jid_row_id=1)
        # arch.contacts has number='123456789', display_name='Alice'
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()

        assert len(data) == 1
        sender = data[0]["sender"]
        # Resolved to display name; raw number without + is never acceptable
        assert sender != "123456789", "sender must not be raw phone number without + prefix"
        # With arch.contacts entry present, should resolve to the display name
        assert sender == "Alice"

    def test_second_start_skips_fts_rebuild(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()

        # first start — no chats indexed yet
        app1 = viewer.create_app(tmp_path, rescan=False)
        with app1.test_client() as client:
            client.get("/api/messages?chat_id=123456789&chat_type=contact")

        # Wait for the background daemon to finish indexing (macOS can be slower)
        import time
        deadline = time.time() + 5
        while time.time() < deadline:
            cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
            count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            cache_conn.close()
            if count == 1:
                break
            time.sleep(0.05)

        # second start with same file — indexed_chats should persist
        app2 = viewer.create_app(tmp_path, rescan=False)
        with app2.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
            count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            cache_conn.close()
            assert count == 1


# ---------------------------------------------------------------------------
# Tests: user preferences
# ---------------------------------------------------------------------------

class TestPreferences:
    def _make_app(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        return app

    def test_get_returns_defaults(self, tmp_path):
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            data = client.get("/api/preferences").get_json()
        assert data["theme"] == "dark"
        assert data["date_format"] == "DD/MM/YYYY"
        assert data["font_size"] == "medium"

    def test_post_persists_value(self, tmp_path):
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/preferences",
                               json={"key": "theme", "value": "light"})
            assert resp.get_json()["ok"] is True
            data = client.get("/api/preferences").get_json()
        assert data["theme"] == "light"

    def test_post_persists_across_restarts(self, tmp_path):
        app1 = self._make_app(tmp_path)
        with app1.test_client() as client:
            client.post("/api/preferences",
                        json={"key": "date_format", "value": "MM/DD/YYYY"})

        app2 = viewer.create_app(tmp_path, rescan=False)
        app2.config["TESTING"] = True
        with app2.test_client() as client:
            data = client.get("/api/preferences").get_json()
        assert data["date_format"] == "MM/DD/YYYY"

    def test_post_invalid_key_returns_400(self, tmp_path):
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/preferences",
                               json={"key": "unknown_key", "value": "x"})
        assert resp.status_code == 400

    def test_cache_db_filename(self, tmp_path):
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            client.get("/api/chats")
        assert (tmp_path / ".wa_viewer.db").exists()
        assert not (tmp_path / ".wa_chat_viewer_cache.db").exists()


# ---------------------------------------------------------------------------
# Tests: lazy per-chat indexing
# ---------------------------------------------------------------------------

class TestLazyIndexing:
    def _setup(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()
        return wa_path

    def test_initial_state_has_no_indexed_chats(self, tmp_path):
        self._setup(tmp_path)
        app = viewer.create_app(tmp_path, rescan=False)
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
        assert count == 0

    def test_ensure_chat_indexed_builds_fts(self, tmp_path):
        wa_path = self._setup(tmp_path)
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")

        row = cache_conn.execute(
            "SELECT 1 FROM indexed_chats WHERE chat_id = ? AND chat_type = ?",
            ("123456789", "contact"),
        ).fetchone()
        assert row is not None

        results = cache_conn.execute("""
            SELECT mi.rowid FROM message_index mi
            JOIN message_index_fts fts ON fts.rowid = mi.rowid
            WHERE message_index_fts MATCH 'Hello'
        """).fetchall()
        assert len(results) == 1

    def test_ensure_chat_indexed_is_idempotent(self, tmp_path):
        wa_path = self._setup(tmp_path)
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")
        viewer._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")

        count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
        assert count == 1
        rows = cache_conn.execute("SELECT COUNT(*) FROM message_index").fetchone()[0]
        assert rows == 1

    def test_ensure_chat_indexed_with_system_event(self, tmp_path):
        """_ensure_chat_indexed must not crash when the chat contains a system
        event (non-zero message_type, no message_media row).  Regression for
        IndexError: No item with that key on 'media_file'."""
        wa_path = self._setup(tmp_path)
        # add a system event to the existing chat (type=12, no media row)
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (50, 10, 0, 1700000009000, NULL, 12)"
        )
        wa_conn.commit()
        wa_conn.close()

        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        # must not raise
        viewer._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")
        rows = cache_conn.execute("SELECT COUNT(*) FROM message_index").fetchone()[0]
        assert rows == 1  # system event excluded, only the text message indexed

    def test_source_change_clears_indexed_chats(self, tmp_path):
        import time
        wa_path = self._setup(tmp_path)
        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            client.get("/api/messages?chat_id=123456789&chat_type=contact")
            # Wait for background thread to finish before simulating source change
            deadline = time.time() + 5
            while time.time() < deadline:
                if client.get("/api/chat-index-status?chat_id=123456789&chat_type=contact"
                              ).get_json()["status"] == "done":
                    break
                time.sleep(0.05)

        # simulate source DB change by growing the file
        wa_path.write_bytes(wa_path.read_bytes() + b"\x00" * 100)

        app2 = viewer.create_app(tmp_path, rescan=False)
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        count = cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
        assert count == 0

    def test_messages_route_triggers_indexing(self, tmp_path):
        self._setup(tmp_path)
        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            count_before = sqlite3.connect(
                str(tmp_path / ".wa_viewer.db")
            ).execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            assert count_before == 0

            client.get("/api/messages?chat_id=123456789&chat_type=contact")

            # Background thread — poll until done (max 5 s)
            import time
            deadline = time.time() + 5
            count_after = 0
            while time.time() < deadline:
                count_after = sqlite3.connect(
                    str(tmp_path / ".wa_viewer.db")
                ).execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
                if count_after == 1:
                    break
                time.sleep(0.05)
            assert count_after == 1

    def test_concurrent_messages_requests_do_not_double_index(self, tmp_path):
        import time
        self._setup(tmp_path)
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # Fire two initial requests before the first indexing completes
            client.get("/api/messages?chat_id=123456789&chat_type=contact")
            client.get("/api/messages?chat_id=123456789&chat_type=contact")

            deadline = time.time() + 5
            while time.time() < deadline:
                if client.get(
                    "/api/chat-index-status?chat_id=123456789&chat_type=contact"
                ).get_json()["status"] == "done":
                    break
                time.sleep(0.05)

            # Exactly one entry in indexed_chats — not doubled
            count = sqlite3.connect(
                str(tmp_path / ".wa_viewer.db")
            ).execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0]
            assert count == 1

            # Exactly one indexing run worth of rows in message_index
            rows = sqlite3.connect(
                str(tmp_path / ".wa_viewer.db")
            ).execute("SELECT COUNT(*) FROM message_index").fetchone()[0]
            assert rows == 1  # matches the single seeded message

    def test_chat_index_status_idle_then_done(self, tmp_path):
        self._setup(tmp_path)
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            r = client.get("/api/chat-index-status?chat_id=123456789&chat_type=contact")
            assert r.get_json()["status"] == "idle"

            client.get("/api/messages?chat_id=123456789&chat_type=contact")

            import time
            deadline = time.time() + 5
            status = "indexing"
            while time.time() < deadline and status != "done":
                status = client.get(
                    "/api/chat-index-status?chat_id=123456789&chat_type=contact"
                ).get_json()["status"]
                time.sleep(0.05)
            assert status == "done"


# ---------------------------------------------------------------------------
# Helper: seed a media message with an archive_copies entry
# ---------------------------------------------------------------------------

def seed_android_db_with_media(wa_conn, archive_conn, tmp_path):
    """Add one archived image message to the seeded chat (jid 1, chat 10)."""
    wa_conn.execute(
        "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
        "VALUES (2, 10, 1, NULL, 1700000001000, NULL, 3)"
    )
    wa_conn.execute(
        "INSERT INTO message_media (message_row_id, file_path, media_name) "
        "VALUES (2, 'Media/Images/photo.jpg', 'photo.jpg')"
    )
    wa_conn.commit()

    media_file = tmp_path / "Alice (00123456789)" / "photo.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

    archive_path = "Alice (00123456789)/photo.jpg"
    archive_conn.execute(
        "INSERT INTO files (original_path, md5) VALUES ('Media/Images/photo.jpg', x'deadbeef')"
    )
    archive_conn.execute(
        "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/photo.jpg', ?)",
        (archive_path,),
    )
    archive_conn.commit()
    return archive_path


# ---------------------------------------------------------------------------
# Tests: /api/media
# ---------------------------------------------------------------------------

class TestApiMedia:
    @pytest.fixture
    def app_with_media(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_returns_only_archived_media(self, app_with_media):
        """Text message excluded; image media row returned (archive_path set)."""
        client, _ = app_with_media
        resp = client.get("/api/media?chat_id=123456789&chat_type=contact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["media_type"] == "image"

    def test_returns_correct_fields(self, app_with_media):
        """Response includes archive_path, media_type, timestamp_ms."""
        client, _ = app_with_media
        data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        item = data[0]
        assert "archive_path" in item
        assert "timestamp_ms" in item
        assert item["timestamp_ms"] == 1700000001000

    def test_excludes_unarchived_media(self, tmp_path):
        """/api/media only returns items with an archive_copies entry."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) "
            "VALUES (3, 10, 0, 1700000002000, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (3, 'Media/Images/missing.jpg', 'missing.jpg')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 0  # unarchived item excluded from gallery

    def test_ordered_newest_first(self, app_with_media):
        """Results are sorted descending by timestamp."""
        client, _ = app_with_media
        data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        timestamps = [r["timestamp_ms"] for r in data]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_pagination_first_page_limited(self, tmp_path):
        """First page respects GALLERY_PAGE_SIZE — seed 101 media messages, expect 100 returned."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        for i in range(101):
            msg_id = 100 + i
            orig = f"Media/Images/photo{i}.jpg"
            arch = f"Alice (00123456789)/photo{i}.jpg"
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
                f"VALUES ({msg_id}, 10, 1, {1700000010000 + i * 1000}, NULL, 3)"
            )
            wa_conn.execute(
                f"INSERT INTO message_media (message_row_id, file_path, media_name) VALUES ({msg_id}, '{orig}', 'p.jpg')"
            )
            archive_conn.execute(f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')")
            archive_conn.execute(
                f"INSERT INTO archive_copies (original_path, archive_path) VALUES ('{orig}', '{arch}')"
            )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 100

    def test_pagination_before_cursor(self, tmp_path):
        """before cursor returns the next page, not overlapping with the first."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        for i in range(101):
            msg_id = 100 + i
            orig = f"Media/Images/photo{i}.jpg"
            arch = f"Alice (00123456789)/photo{i}.jpg"
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
                f"VALUES ({msg_id}, 10, 1, {1700000010000 + i * 1000}, NULL, 3)"
            )
            wa_conn.execute(
                f"INSERT INTO message_media (message_row_id, file_path, media_name) VALUES ({msg_id}, '{orig}', 'p.jpg')"
            )
            archive_conn.execute(f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')")
            archive_conn.execute(
                f"INSERT INTO archive_copies (original_path, archive_path) VALUES ('{orig}', '{arch}')"
            )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            page1 = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
            oldest_ts = page1[-1]["timestamp_ms"]
            page2 = client.get(
                f"/api/media?chat_id=123456789&chat_type=contact&before={oldest_ts}"
            ).get_json()

        assert len(page1) == 100
        assert len(page2) == 1  # the 101st item
        # no overlap
        page1_ts = {r["timestamp_ms"] for r in page1}
        assert all(r["timestamp_ms"] not in page1_ts for r in page2)

    def test_media_count_returns_totals(self, app_with_media):
        """Count endpoint returns total and per-type breakdown."""
        client, _ = app_with_media
        data = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
        assert data["total"] == 1
        assert data["archived"] == 1
        assert "image" in data["by_type"]
        assert data["by_type"]["image"]["count"] == 1
        assert data["by_type"]["image"]["missing"] == 0

    def test_media_count_missing(self, tmp_path):
        """Count endpoint counts unarchived items as missing."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        # media message with no archive_copies entry
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (5, 10, 1, 1700000005000, NULL, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (5, 'Media/Images/lost.jpg', 'lost.jpg')"
        )
        archive_conn.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Media/Images/lost.jpg', x'deadbeef')"
        )
        # deliberately no archive_copies row
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
        assert data["total"] == 1
        assert data["archived"] == 0
        assert data["by_type"]["image"]["missing"] == 1

    def test_media_count_includes_links(self, tmp_path):
        """/api/media/count includes link-text messages and merges them into total."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        # add a link message (type=0, text contains https URL)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (50, 10, 0, 1700000050000, 'check this out https://example.com/page', NULL)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/media/count?chat_id=123456789&chat_type=contact").get_json()
        assert data["total"] == 2  # 1 image + 1 link
        assert data["archived"] == 1  # only the image is archived
        assert data["by_type"]["link"]["count"] == 1
        assert data["by_type"]["link"]["missing"] == 0  # links have no archive concept
        assert data["by_type"]["image"]["count"] == 1

    def test_media_links_endpoint(self, tmp_path):
        """/api/media/links returns link rows; /api/media never returns them."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (51, 10, 1, 1700000051000, 'see http://example.org/thing', NULL)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            links = client.get("/api/media/links?chat_id=123456789&chat_type=contact").get_json()
            media = client.get("/api/media?chat_id=123456789&chat_type=contact").get_json()
        # links endpoint returns the link row
        assert len(links) == 1
        assert links[0]["media_type"] == "link"
        assert "http://example.org/thing" in links[0]["text_body"]
        # /api/media must NOT contain any link rows — regression guard
        assert all(r["media_type"] != "link" for r in media)
        # /api/media still returns the image
        assert any(r["media_type"] == "image" for r in media)

    def test_media_documents_endpoint_returns_undownloaded_docs(self, tmp_path):
        """Documents with no file_path but a media_name are returned by /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        # Undownloaded document: message_type=6 (document), file_path=NULL, media_name set
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (5, 10, 0, 1, 1700000005000, NULL, 6)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (5, NULL, 'Annual Report 2024.pdf')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 1
        assert docs[0]["media_name"] == "Annual Report 2024.pdf"

    def test_media_documents_excludes_downloaded_media(self, tmp_path):
        """Regular downloaded media (file_path set) must not appear in /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 0  # the seeded media has file_path set

    def test_media_documents_excludes_text_messages(self, tmp_path):
        """Text messages (message_type=0) must not appear in /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)  # message 1 is type=0 text
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 0


class TestChatFilter:
    """Tests for _android_chat_filter and _ios_chat_filter."""

    def test_android_contact_filter_uses_jid_user(self):
        pred, params = viewer._android_chat_filter("391234567890", "contact")
        assert "j_chat.user" in pred
        assert params == ["391234567890"]

    def test_android_group_filter_uses_chat_row_id(self):
        pred, params = viewer._android_chat_filter("42", "group")
        assert "m.chat_row_id = CAST" in pred
        assert params == ["42"]

    def test_ios_filter_uses_zchatsession(self):
        pred, params = viewer._ios_chat_filter("99")
        assert "m.ZCHATSESSION = CAST" in pred
        assert params == ["99"]


class TestAndroidReactions:
    """Tests for message reaction emoji from message_add_on tables."""

    def _setup(self, tmp_path):
        """Set up Android DB with message_add_on tables."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        # Create reaction tables
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id                   INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                from_me               INTEGER DEFAULT 0,
                sender_jid_row_id     INTEGER
            );
            CREATE TABLE message_add_on_reaction (
                _id                   INTEGER PRIMARY KEY,
                message_add_on_row_id INTEGER,
                reaction              TEXT,
                sender_timestamp      INTEGER
            );
        """)
        # Seed contact chat (chat 10, jid 1)
        seed_android_db(wa_conn, archive_conn)
        # Add a second message for edge case tests
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (2, 10, 1, NULL, 1700000001000, 'Another message', 0)"
        )
        # Add reaction to message 1: two thumbs up
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (1, 1, 1, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (1, '👍', 1700000000100)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (2, 1, 0, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (2, '👍', 1700000000200)"
        )
        # Add reaction to message 2: one heart
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (3, 2, 0, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (3, '❤️', 1700000000300)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        return viewer.create_app(tmp_path, rescan=False)

    def test_reactions_column_present(self, tmp_path):
        """Response includes a reactions key."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 2
        assert "reactions" in data[0]

    def test_reactions_aggregated_from_multiple(self, tmp_path):
        """Two 👍 reactions on the same message are aggregated."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        # Message 1 has two 👍 reactions
        msg1 = next(m for m in data if m["msg_id"] == 1)
        assert msg1["reactions"] == "👍,👍"

    def test_reactions_different_emoji(self, tmp_path):
        """Different emoji on the same message appear as comma-separated values."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        msg2 = next(m for m in data if m["msg_id"] == 2)
        assert msg2["reactions"] == "❤️"

    def test_no_reactions_returns_null(self, tmp_path):
        """Message in a chat without reactions has null reactions column."""
        # Use the existing setup (has reaction tables) but query a different chat
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        wa_path = tmp_path / "msgstore.db"
        # Add a second chat with a message (no reactions)
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.execute(
            "INSERT INTO jid (_id, user) VALUES (2, '987654321')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (20, 2, NULL, 0, 1700000002000, 10)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (10, 20, 0, 2, 1700000002000, 'No reactions here', 0)"
        )
        wa_conn.commit()
        wa_conn.close()
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=987654321&chat_type=contact").get_json()
        assert len(data) == 1
        assert data[0]["reactions"] is None

    def test_reactions_null_without_tables(self, tmp_path):
        """When message_add_on tables don't exist, reactions column is NULL."""
        # Start with plain make_android_db (no message_add_on tables)
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/messages?chat_id=123456789&chat_type=contact").get_json()
        assert len(data) == 1
        assert data[0]["reactions"] is None


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    result = []
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            result.append(b | 0x80)
        else:
            result.append(b)
            break
    return bytes(result)


def _lid_field1_bytes(lid_digits: str) -> bytes:
    """Encode a LID as field-1 bytes: a prefix byte + hex-nibble digits.

    The parser reads value[1:].hex() and strips a single trailing 'f', so an
    odd-length LID is padded with an 'f' nibble here. Synthetic values only.
    """
    h = lid_digits if len(lid_digits) % 2 == 0 else lid_digits + "f"
    return b"\x8c" + bytes.fromhex(h)


def _make_receipt_blob(base_ts: int, members: list) -> bytes:
    """Build a rich-format ZRECEIPTINFO blob (top field 3 = base_ts).

    members: list of dicts, each with ALL keys present (None to omit the field):
      - lid: synthetic LID digit string (e.g. "1234567890123")
      - delivered_delta: seconds offset for the field-10 delivered event, or None
      - read_delta: seconds offset for the field-5 read time, or None
      - noise4: value for field 4 (ignored by the parser), or None
    """
    blob = _encode_varint((3 << 3) | 0) + _encode_varint(base_ts)
    for m in members:
        lid_bytes = _lid_field1_bytes(m["lid"])
        entry = _encode_varint((1 << 3) | 2) + _encode_varint(len(lid_bytes)) + lid_bytes
        if m["noise4"] is not None:
            entry += _encode_varint((4 << 3) | 0) + _encode_varint(m["noise4"])
        if m["read_delta"] is not None:
            entry += _encode_varint((5 << 3) | 0) + _encode_varint(m["read_delta"])
        if m["delivered_delta"] is not None:
            event = (
                _encode_varint((1 << 3) | 0) + _encode_varint(m["delivered_delta"]) +
                _encode_varint((2 << 3) | 0) + _encode_varint(1)  # code (ignored)
            )
            entry += _encode_varint((10 << 3) | 2) + _encode_varint(len(event)) + event
        blob += _encode_varint((2 << 3) | 2) + _encode_varint(len(entry)) + entry
    return blob


def _make_compact_receipt_blob(members: list) -> bytes:
    """Build a compact-format ZRECEIPTINFO blob (NO top field 3, NO field 5).

    Delivered events live in field 9; read is never stored.
    members: list of dicts with keys: lid, delivered_delta (seconds).
    """
    blob = b""
    for m in members:
        lid_bytes = _lid_field1_bytes(m["lid"])
        entry = _encode_varint((1 << 3) | 2) + _encode_varint(len(lid_bytes)) + lid_bytes
        event = (
            _encode_varint((1 << 3) | 0) + _encode_varint(m["delivered_delta"]) +
            _encode_varint((2 << 3) | 0) + _encode_varint(3)  # code (ignored)
        )
        entry += _encode_varint((9 << 3) | 2) + _encode_varint(len(event)) + event
        blob += _encode_varint((2 << 3) | 2) + _encode_varint(len(entry)) + entry
    return blob


class _NoOpConn:
    """Minimal connection stub — no contacts to look up."""
    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        return None


class TestIosReceiptBlobParser:
    # Validated model: base_ts = top field 3; delivered = base + min(field-10 deltas)
    # in seconds (0 valid); read = base + field 5 (seconds) if present; field 4 ignored.

    def test_group_delivered_only(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 10, "read_delta": None, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == (base_ts + 10) * 1000
        assert members[0]["read_ts"] is None

    def test_group_delivered_and_read(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 20, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000  # delta 0 is a real delivery
        assert members[0]["read_ts"] == (base_ts + 20) * 1000

    def test_group_read_at_send_delta_zero(self):
        # read_delta == 0 means read at send time — must not collapse to None.
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 0, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] == base_ts * 1000

    def test_group_field4_is_ignored(self):
        # A large field-4 value (read receipts disabled) must not gate delivered/read.
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 17, "read_delta": None, "noise4": 1089263},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["delivered_ts"] == (base_ts + 17) * 1000
        assert members[0]["read_ts"] is None

    def test_group_lid_trailing_f_stripped(self):
        # Odd-length LID is padded with an 'f' nibble; parser must strip it.
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 0, "read_delta": None, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["jid"] == "1234567890123"

    def test_group_multiple_members(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 5, "noise4": None},
            {"lid": "2222222222222", "delivered_delta": 15, "read_delta": None, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 2
        assert members[0]["read_ts"] == (base_ts + 5) * 1000
        assert members[1]["read_ts"] is None
        assert members[1]["delivered_ts"] == (base_ts + 15) * 1000

    def test_group_regression_verified_deltas(self):
        # Mirrors two real ground-truth messages (synthetic LIDs, real validated deltas):
        #   read 5926s / 4708s / 1462s after send; a member with read receipts off (no field 5).
        base_ts = 1768325919
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 5926, "noise4": 2},
            {"lid": "2222222222222", "delivered_delta": 17, "read_delta": None, "noise4": 1089263},
            {"lid": "3333333333333", "delivered_delta": 0, "read_delta": 0, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["read_ts"] == (base_ts + 5926) * 1000
        assert members[1]["read_ts"] is None  # read receipts disabled
        assert members[1]["delivered_ts"] == (base_ts + 17) * 1000
        assert members[2]["read_ts"] == base_ts * 1000  # read at send

    # --- 1-to-1 mode (is_group=False): collapse to a single member ---

    def test_1to1_delivered_and_read(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 120, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] == (base_ts + 120) * 1000

    def test_1to1_delivered_only(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1234567890123", "delivered_delta": 0, "read_delta": None, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] is None

    def test_1to1_multi_device_collapses_to_earliest(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(base_ts, [
            {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 253, "noise4": None},
            {"lid": "2222222222222", "delivered_delta": 26, "read_delta": None, "noise4": None},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000  # min delta = 0
        assert members[0]["read_ts"] == (base_ts + 253) * 1000

    # --- compact format: no base_ts, delivered from field 9, read blank ---

    def test_compact_group_delivered_only(self):
        msg_ts_s = 1784037821
        blob = _make_compact_receipt_blob([
            {"lid": "1111111111111", "delivered_delta": 0},
            {"lid": "2222222222222", "delivered_delta": 60},
        ])
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True, msg_ts_s=msg_ts_s)
        assert len(members) == 2
        assert members[0]["delivered_ts"] == msg_ts_s * 1000
        assert members[0]["read_ts"] is None
        assert members[1]["delivered_ts"] == (msg_ts_s + 60) * 1000
        assert members[1]["read_ts"] is None

    def test_empty_blob_returns_empty_list(self):
        members = viewer._parse_ios_receipt_blob(b"", _NoOpConn())
        assert members == []

    def test_unknown_fixed64_wire_type_is_skipped(self):
        """A fixed64 field (wire=1) in an entry should be skipped, not abort parsing."""
        base_ts = 1700000000
        lid_bytes = _lid_field1_bytes("1234567890123")
        entry = _encode_varint((1 << 3) | 2) + _encode_varint(len(lid_bytes)) + lid_bytes
        entry += _encode_varint((8 << 3) | 1) + b'\x00' * 8  # unknown field, wire=1 (fixed64)
        entry += _encode_varint((5 << 3) | 0) + _encode_varint(10)  # read delta=10
        event = _encode_varint((1 << 3) | 0) + _encode_varint(0)
        entry += _encode_varint((10 << 3) | 2) + _encode_varint(len(event)) + event

        blob = _encode_varint((3 << 3) | 0) + _encode_varint(base_ts)
        blob += _encode_varint((2 << 3) | 2) + _encode_varint(len(entry)) + entry

        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["read_ts"] == (base_ts + 10) * 1000
        assert members[0]["delivered_ts"] == base_ts * 1000


class TestIosReadWithoutTimestamp:
    """iOS keeps the aggregate read flag (ZMESSAGESTATUS 8) for recent messages but
    no longer stores per-recipient read timestamps; the route surfaces that state."""

    def test_read_status_without_timestamp_is_flagged(self):
        assert viewer._ios_read_without_timestamp(8, None) is True

    def test_read_status_with_timestamp_is_not_flagged(self):
        # A stored timestamp already conveys the read state.
        assert viewer._ios_read_without_timestamp(8, 1700000000000) is False

    def test_delivered_status_is_not_flagged(self):
        # Status 6 = delivered, not read.
        assert viewer._ios_read_without_timestamp(6, None) is False

    def test_sent_status_is_not_flagged(self):
        assert viewer._ios_read_without_timestamp(1, None) is False


# ---------------------------------------------------------------------------
# Tests: iOS reactions — ZRECEIPTINFO protobuf extraction
# ---------------------------------------------------------------------------

def _make_zreceipt_blob_with_reactions(reactors: list) -> bytes:
    """
    Build a ZRECEIPTINFO blob containing reaction entries.
    reactors: list of (phone_hex_str, emoji_utf8_bytes) tuples

    Length prefixes are varint-encoded, so blobs with field 7 >= 128 bytes
    (many reactors) are represented correctly.
    """
    def encode_len_delimited(field, wire, data):
        tag = (field << 3) | wire
        return _encode_varint(tag) + _encode_varint(len(data)) + data

    def make_reactor_entry(phone_hex_str, emoji_bytes):
        # sub-field 1: sender phone as raw bytes (not hex string ASCII)
        phone_data = bytes.fromhex(phone_hex_str)
        # sub-field 3: emoji bytes
        emoji_data = emoji_bytes
        entry = encode_len_delimited(1, 2, phone_data)
        entry += encode_len_delimited(3, 2, emoji_data)
        return entry

    field7_data = b""
    for phone_hex, emoji_bytes in reactors:
        entry_data = make_reactor_entry(phone_hex, emoji_bytes)
        # entry tag 0x0a = field 1, wire 2
        field7_data += _encode_varint(0x0A) + _encode_varint(len(entry_data)) + entry_data

    # Top-level: field 7, wire 2
    top_tag = (7 << 3) | 2
    return _encode_varint(top_tag) + _encode_varint(len(field7_data)) + field7_data


def _make_ios_reaction_blob(entries: list) -> bytes:
    """Build a ZRECEIPTINFO reaction group (field 7) from realistic reactor entries.

    Each entry is a dict:
      - "token": str    opaque per-reaction id stored as ASCII in sub-field 1
      - "emoji": bytes  reaction emoji (sub-field 3)
      - "phone": str|None  when set, stored as '<phone>@s.whatsapp.net' in sub-field 2
      - "lid": str|None    when set, stored as '<decimal>@lid' in sub-field 2 (mutually
                           exclusive with phone; recent group reactions use this form)
      - "from_me": bool    (legacy) when True, appends a delivery-receipt block (sub-field
                           5, prefixed 0x0a 0x07); present in real data but no longer used
                           as the from_me signal (no identity at all = own reaction)
    """
    def ld(field, data):
        tag = (field << 3) | 2
        return _encode_varint(tag) + _encode_varint(len(data)) + data

    field7 = b""
    for e in entries:
        entry = ld(1, e["token"].encode("utf-8"))
        if e.get("phone"):
            entry += ld(2, f"{e['phone']}@s.whatsapp.net".encode("utf-8"))
        elif e.get("lid"):
            entry += ld(2, f"{e['lid']}@lid".encode("utf-8"))
        entry += ld(3, e["emoji"])
        if e.get("from_me"):
            entry += ld(5, b"\x0a\x07" + b"\x81\x39\x34\x00\x00\x00\x00")
        field7 += ld(1, entry)
    top_tag = (7 << 3) | 2
    return _encode_varint(top_tag) + _encode_varint(len(field7)) + field7


class TestIOSReactions:

    def test_parse_heart_reaction(self):
        blob = _make_zreceipt_blob_with_reactions([("334142384436", "❤️".encode("utf-8"))])
        result = viewer._extract_ios_reactions(blob)
        assert len(result) == 1
        sender, emoji = result[0]
        assert sender == "334142384436"
        assert emoji == "❤️"

    def test_parse_multiple_reactions(self):
        blob = _make_zreceipt_blob_with_reactions([
            ("334142384436", "❤️".encode("utf-8")),
            ("334135434342", "😂".encode("utf-8")),
        ])
        result = viewer._extract_ios_reactions(blob)
        assert len(result) == 2
        assert result[0][1] == "❤️"
        assert result[1][1] == "😂"

    def test_parse_various_emoji(self):
        blobs = []
        for emoji in ["😂", "😮", "😭", "👏"]:
            blobs.append(_make_zreceipt_blob_with_reactions([("334142384436", emoji.encode("utf-8"))]))
        for i, emoji in enumerate(["😂", "😮", "😭", "👏"]):
            result = viewer._extract_ios_reactions(blobs[i])
            assert len(result) == 1, f"Failed for {emoji}"
            assert result[0][1] == emoji, f"Expected {emoji}, got {result[0][1]}"

    def test_parse_empty_blob(self):
        result = viewer._extract_ios_reactions(b"")
        assert result == []

    def test_parse_no_field7(self):
        # Blob with a different field (e.g. field 1)
        blob = bytes([(1 << 3) | 2, 2, 0x41, 0x42])
        result = viewer._extract_ios_reactions(blob)
        assert result == []

    def test_vs16_is_skipped_between_emoji(self):
        # VS16 (efb88f) appears BETWEEN two real emoji — it should not be a separate reaction
        blob = _make_zreceipt_blob_with_reactions([
            ("334142384436", "❤️".encode("utf-8") + "👏".encode("utf-8")),
        ])
        result = viewer._extract_ios_reactions(blob)
        # Both emoji returned; VS16 is a presentation modifier, not a standalone emoji
        assert len(result) == 2
        emojis = sorted(r[1] for r in result)
        assert emojis == sorted(["❤️", "👏"])

    def test_many_reactions_large_field7(self):
        """Field 7 >= 128 bytes needs a multi-byte varint length; all reactors survive.

        The old single-byte length read truncated field 7 and dropped reactors.
        """
        reactors = [(f"33414238{i:04d}", "❤️".encode("utf-8")) for i in range(11)]
        blob = _make_zreceipt_blob_with_reactions(reactors)
        assert len(blob) > 128  # forces a multi-byte varint length on field 7
        result = viewer._extract_ios_reactions(blob)
        assert len(result) == 11
        assert all(emoji == "❤️" for _, emoji in result)

    def test_emoji_in_subfield_2(self):
        """Some reactor entries store the emoji in sub-field 2 (tag 0x12), not 3.

        Combined with a field 7 that needs a 2-byte varint length, the old parser's
        single-byte length read dropped the trailing byte and discarded this entry.
        """
        # Two entries with emoji in sub-field 3, plus a final entry with emoji in
        # sub-field 2. Pad the blob past 128 bytes so field 7 uses a 2-byte varint.
        def entry(field, emoji):
            sub1 = _encode_varint(0x0A) + _encode_varint(30) + (b"\x11" * 30)
            sub_emoji = _encode_varint((field << 3) | 2) + _encode_varint(len(emoji)) + emoji
            body = sub1 + sub_emoji
            return _encode_varint(0x0A) + _encode_varint(len(body)) + body

        laugh = "😂".encode("utf-8")
        heart = "❤️".encode("utf-8")
        field7 = entry(3, heart) + entry(3, laugh) + entry(3, laugh) + entry(2, laugh)
        blob = _encode_varint((7 << 3) | 2) + _encode_varint(len(field7)) + field7
        assert len(field7) > 128  # forces a 2-byte varint length on field 7
        result = viewer._extract_ios_reactions(blob)
        counts = Counter(emoji for _, emoji in result)
        assert counts == Counter({"😂": 3, "❤️": 1})

    def test_protobuf_skips_deprecated_wire_types(self):
        # Build a blob with a deprecated wire type (6) embedded — should not crash
        # Wire type 6 = deprecated start_group; we'll embed it inside field 7
        entry_data = bytes([0x41, 0x42])  # field 8, wire 1 = deprecated
        inner = bytes([0x0A, len(entry_data) + 1]) + bytes([6]) + entry_data
        blob = bytes([(7 << 3) | 2, len(inner)]) + inner
        result = viewer._extract_ios_reactions(blob)
        assert result == []

    def test_ios_reactions_me_detection_contact_chat(self, tmp_path):
        """In 1-to-1 chats ZGROUPMEMBER is NULL — _ios_reactions must fall back to ZFROMJID."""
        # Build minimal in-memory WA DB with the needed schema
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB)")
        conn.execute("CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE)")
        # ZWAGROUPMEMBER must exist (even if unused for contact chats)
        conn.execute("CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)")

        def encode_field7_reactor(phone_hex_str, emoji_bytes):
            phone_data = bytes.fromhex(phone_hex_str)
            entry = bytes([0x0a, len(phone_data)]) + phone_data
            entry += bytes([0x1a, len(emoji_bytes)]) + emoji_bytes
            field7 = bytes([0x0a, len(entry)]) + entry
            return bytes([0x3a, len(field7)]) + field7  # field 7, wire 2

        # Message from "me" (ZISFROMME=1) to identify my JID — no ZMESSAGEINFO needed
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '49123456789@s.whatsapp.net')"
        )
        # A message with reactions (ZISFROMME=0 = received)
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (encode_field7_reactor("34393132343536373839", "😂".encode("utf-8")),)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, '49876543210@s.whatsapp.net')"
        )
        conn.commit()

        result = viewer._ios_reactions(conn, "10", [2])
        assert 2 in result
        reactors = result[2]
        assert len(reactors) == 1
        sender, emoji, from_me = reactors[0]
        assert emoji == "😂"
        # Sender phone 34393132343536373839 ≠ my phone 49123456789 → from_me=0
        assert from_me == 0

    def test_ios_reactions_me_detection_null_jid(self, tmp_path):
        """System messages have NULL ZFROMJID — _ios_reactions must not crash."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB)")
        conn.execute("CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE)")
        conn.execute("CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)")

        def encode_field7_reactor(phone_hex_str, emoji_bytes):
            phone_data = bytes.fromhex(phone_hex_str)
            entry = bytes([0x0a, len(phone_data)]) + phone_data
            entry += bytes([0x1a, len(emoji_bytes)]) + emoji_bytes
            field7 = bytes([0x0a, len(entry)]) + entry
            return bytes([0x3a, len(field7)]) + field7

        # Sent message with NULL ZFROMJID (system message) — "me" detection must not crash
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, NULL)"
        )
        # A message with reactions — reactor phone differs from "me" (which has NULL JID → my_phone=None)
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (encode_field7_reactor("34393132343536373839", "😂".encode("utf-8")),)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, '49876543210@s.whatsapp.net')"
        )
        conn.commit()

        # Must not raise AttributeError: 'NoneType' object has no attribute 'find'
        result = viewer._ios_reactions(conn, "10", [2])
        assert 2 in result
        sender, emoji, from_me = result[2][0]
        assert emoji == "😂"
        assert from_me == 0  # my_phone=None, reactor phone ≠ None → not from me

# ---------------------------------------------------------------------------
# Tests: HTML template integrity
# ---------------------------------------------------------------------------

class TestHtmlTemplate:
    def test_all_getElementById_targets_exist_before_script(self):
        """Every getElementById('id') in app.js must refer to an element defined
        in the HTML before the external script tag.  A missing or late-placed
        element causes a TypeError that silently kills the entire IIFE."""
        template = viewer.HTML_TEMPLATE

        script_start = template.index("<script>")
        html_before_script = template[:script_start]

        app_js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        script_body = app_js_path.read_text(encoding="utf-8")

        ids_in_html = set(re.findall(r'\bid=["\']([^"\']+)["\']', html_before_script))
        ids_accessed = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", script_body))

        # ids created dynamically at runtime (not in static HTML) are expected
        dynamic_ids = {'img-lightbox'}
        missing = ids_accessed - ids_in_html - dynamic_ids
        assert not missing, (
            f"getElementById called for IDs not present in HTML before <script>: {sorted(missing)}"
        )

    def test_load_gallery_page_does_not_push_lightbox_items(self):
        """renderGalleryItem owns lightboxItems.push — _loadGalleryPage must not do it
        too or every item ends up double-counted, breaking lightbox indices."""
        app_js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        script = app_js_path.read_text(encoding="utf-8")

        # isolate the _loadGalleryPage function body
        fn_start = script.index('async function _loadGalleryPage(')
        # find the end: next top-level 'async function' or plain 'function' at col 2
        import re as _re
        next_fn = _re.search(r'\n  (async )?function ', script[fn_start + 1:])
        fn_body = script[fn_start: fn_start + 1 + (next_fn.start() if next_fn else len(script))]

        assert 'lightboxItems.push' not in fn_body, (
            "_loadGalleryPage must not push to lightboxItems — renderGalleryItem handles that"
        )


# ---------------------------------------------------------------------------
# Tests: /api/chat-info and /api/chat-info/media-size
# ---------------------------------------------------------------------------

def _make_android_app(tmp_path, seed_fn=None):
    wa_path = tmp_path / "msgstore.db"
    archive_path = tmp_path / ".wa_media_archiver.db"
    wa_conn = make_android_db(wa_path)
    archive_conn = make_archive_db(archive_path)
    seed_android_db(wa_conn, archive_conn)
    if seed_fn:
        seed_fn(wa_conn, archive_conn, tmp_path)
    wa_conn.close()
    archive_conn.close()
    app = viewer.create_app(tmp_path, rescan=False)
    app.config["TESTING"] = True
    return app


class TestChatInfo:
    def test_contact_returns_expected_fields(self, tmp_path):
        app = _make_android_app(tmp_path)
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=123456789&chat_type=contact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["number"] == "123456789"
        assert "first_ts" in data
        assert "last_ts" in data
        assert data["sent"] is not None
        assert data["received"] is not None
        assert data["total"] is not None
        assert "members" not in data
        assert "top_senders" not in data

    def test_contact_sent_received_counts(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        # Add a sent message to the same chat
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (2, 10, 1, NULL, 1700000001000, 'Reply', 0)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=123456789&chat_type=contact").get_json()
        assert data["sent"] == 1
        assert data["received"] == 1
        assert data["total"] == 2

    def test_group_returns_members_and_top_senders(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Set up a group chat
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)")
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (3, '987654321')")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (10, 20, 0, 3, 1700000000001, 'Hi group', 0)"
        )
        wa_conn.commit()
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('987654321', 'Bob (00987654321)', 'Bob')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('20', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=20&chat_type=group").get_json()

        assert "members" in data
        assert "top_senders" in data
        assert data["total"] == 1
        assert data["number"] is None
        # top senders should list Bob
        assert any(s["name"] == "Bob" for s in data["top_senders"])

    def test_group_no_participants_table_falls_back_to_senders(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)")
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (3, '987654321')")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (10, 20, 0, 3, 1700000000001, 'Hi group', 0)"
        )
        wa_conn.commit()
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('987654321', 'Bob (00987654321)', 'Bob')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('20', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()
        # Note: group_participants table is NOT created in make_android_db, so the fallback path is exercised

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=20&chat_type=group").get_json()

        assert "members" in data
        assert isinstance(data["members"], list)

    def test_first_ts_comes_from_cache(self, tmp_path):
        """first_ts / last_ts are derived from message_index (cache), not live DB."""
        app = _make_android_app(tmp_path)
        # Seed the cache by opening messages (triggers background indexing)
        import time
        with app.test_client() as client:
            client.get("/api/messages?chat_id=123456789&chat_type=contact")
            deadline = time.time() + 5
            while time.time() < deadline:
                if client.get(
                    "/api/chat-index-status?chat_id=123456789&chat_type=contact"
                ).get_json()["status"] == "done":
                    break
                time.sleep(0.05)
            data = client.get("/api/chat-info?chat_id=123456789&chat_type=contact").get_json()
        assert data["first_ts"] == 1700000000000
        assert data["last_ts"] == 1700000000000

    def test_media_only_mode_returns_null_counts(self, tmp_path):
        """Without a WA source DB, counts are null and first_ts comes from cache."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('123456789', 'Alice', 'Alice')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=123456789&chat_type=contact").get_json()
        assert data["sent"] is None
        assert data["received"] is None
        assert data["total"] is None

    def test_media_size_returns_bytes(self, tmp_path):
        """media-size sums file sizes for the chat folder."""
        def seed_media(wa_conn, archive_conn, tmp_path):
            seed_android_db_with_media(wa_conn, archive_conn, tmp_path)
            # Reorganise the archive copy to use the proper Contacts/ prefix
            archive_conn.execute("DELETE FROM archive_copies")
            archive_path = "Contacts/Alice (00123456789)/2023/Received/photo.jpg"
            media_dir = tmp_path / "Contacts" / "Alice (00123456789)" / "2023" / "Received"
            media_dir.mkdir(parents=True, exist_ok=True)
            (media_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff" * 100)  # ~300 bytes
            archive_conn.execute(
                "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/photo.jpg', ?)",
                (archive_path,),
            )
            archive_conn.commit()

        app = _make_android_app(tmp_path, seed_fn=seed_media)
        with app.test_client() as client:
            data = client.get(
                "/api/chat-info/media-size?chat_id=123456789&chat_type=contact"
            ).get_json()
        assert "bytes" in data
        assert data["bytes"] > 0

    def test_media_size_zero_when_no_files(self, tmp_path):
        app = _make_android_app(tmp_path)
        with app.test_client() as client:
            data = client.get(
                "/api/chat-info/media-size?chat_id=123456789&chat_type=contact"
            ).get_json()
        assert data["bytes"] == 0


# ---------------------------------------------------------------------------
# recent_messages cache
# ---------------------------------------------------------------------------

def _seed_android_db_large(wa_conn):
    """Insert 10 chats × 50 messages each for performance and routing tests.

    Timestamps are Unix epoch milliseconds (like WhatsApp's message.timestamp column).
    For chat_id=1 the range is 1700000000000..170000049000.
    """
    for chat_id in range(1, 11):
        jid_row_id = chat_id
        wa_conn.execute(
            "INSERT OR IGNORE INTO jid (_id, user) VALUES (?, ?)",
            (jid_row_id, str(chat_id * 111111111))
        )
        wa_conn.execute(
            "INSERT OR IGNORE INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (?, ?, NULL, 0, ?, ?)",
            (chat_id, jid_row_id, 1700000000000 + chat_id * 1000, chat_id * 50)
        )
        for i in range(50):
            msg_id = chat_id * 100 + i
            ts = 1700000000000 + (chat_id - 1) * 50 * 1000 + i * 1000
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (msg_id, chat_id, i % 2, jid_row_id, ts, f"msg {i}")
            )
    wa_conn.commit()


class TestWaDbIndex:
    def test_index_created_on_first_create_app(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        _seed_android_db_large(wa_conn)
        wa_conn.close()

        make_archive_db(archive_path)
        # create_app opens the WA DB and creates the index
        viewer.create_app(tmp_path, rescan=False)

        conn = sqlite3.connect(str(wa_path))
        try:
            idxs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )]
        finally:
            conn.close()
        assert "idx_message_chat_ts" in idxs

    def test_index_not_recreated_on_second_create_app(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        _seed_android_db_large(wa_conn)
        wa_conn.close()
        make_archive_db(archive_path)

        viewer.create_app(tmp_path, rescan=False)
        viewer.create_app(tmp_path, rescan=False)

        conn = sqlite3.connect(str(wa_path))
        try:
            idxs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_message_chat_ts'"
            )]
        finally:
            conn.close()
        assert len(idxs) == 1  # one index, not duplicated


class TestRecentMessagesSchema:
    def test_recent_messages_table_created(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = make_archive_db(archive_path)
        conn.close()

        conn = sqlite3.connect(str(archive_path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(recent_messages)")}
        finally:
            conn.close()
        expected = {
            "chat_id", "chat_type", "msg_id", "timestamp_ms", "sender", "from_me",
            "archive_path", "media_type", "media_name", "text_body",
            "quoted_text", "quoted_sender", "quoted_ts"
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_recent_messages_pk_replace(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = make_archive_db(archive_path)

        conn.execute("""
            INSERT INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
            VALUES ('1', 'contact', 1, 1000, 'Alice', 0, NULL, 'text', '', 'Hello')
        """)
        conn.commit()

        # Same PK: REPLACE, not duplicate
        conn.execute("""
            INSERT OR REPLACE INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
            VALUES ('1', 'contact', 1, 2000, 'Bob', 1, NULL, 'text', '', 'Updated')
        """)
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM recent_messages WHERE chat_id='1' AND msg_id=1"
        ).fetchone()[0]
        assert count == 1
        sender = conn.execute(
            "SELECT sender FROM recent_messages WHERE chat_id='1' AND msg_id=1"
        ).fetchone()[0]
        assert sender == "Bob"  # replaced, not duplicated

    def test_recent_messages_index_exists(self, tmp_path):
        archive_path = tmp_path / ".wa_media_archiver.db"
        make_archive_db(archive_path)

        conn = sqlite3.connect(str(archive_path))
        try:
            idxs = [(r[0], r[1]) for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='recent_messages'"
            )]
        finally:
            conn.close()
        assert any("timestamp_ms" in (sql or "") for _, sql in idxs)


class TestRecentMessagesRouting:
    def test_initial_load_served_from_recent_messages(self, tmp_path):
        """When recent_messages has rows, /api/messages returns them directly (no WA DB)."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        # Pre-populate recent_messages with known sender names (distinct from WA DB)
        for i in range(10):
            archive_conn.execute("""
                INSERT OR REPLACE INTO recent_messages
                (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
                VALUES (?, 'contact', ?, ?, 'FROM_CACHE', 0, NULL, 'text', '', ?)
            """, ("111111111", i + 1, 1700000000000 + i, f"cached msg {i}"))
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=111111111&chat_type=contact&limit=50")
        data = resp.get_json()

        assert resp.status_code == 200
        assert len(data) == 10
        # All messages have sender='FROM_CACHE' — proves they came from recent_messages, not WA DB
        assert all(r["sender"] == "FROM_CACHE" for r in data)
        # WA DB sender would be '+111111111', so this check is definitive

    def test_fallback_and_backfill(self, tmp_path):
        """When recent_messages is empty, /api/messages falls back to WA DB and backfills."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=111111111&chat_type=contact&limit=50")
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data) == 50  # all 50 messages for chat_row_id=1

        # Wait for the daemon backfill thread to finish
        time.sleep(0.3)

        # recent_messages should now be backfilled
        archive_conn2 = make_archive_db(archive_path)
        count = archive_conn2.execute(
            "SELECT COUNT(*) FROM recent_messages WHERE chat_id='111111111' AND chat_type='contact'"
        ).fetchone()[0]
        archive_conn2.close()
        assert count == 50

    def test_pagination_hits_wa_db(self, tmp_path):
        """Scroll-up (?before=) always hits WA DB regardless of recent_messages content."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        # Pre-populate recent_messages with the 5 OLDEST messages (timestamps 1700000000000..)
        # WA DB will return the 50 NEWEST messages (timestamps 170000045000..) — no overlap
        for i in range(5):
            archive_conn.execute("""
                INSERT OR REPLACE INTO recent_messages
                (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
                VALUES (?, 'contact', ?, ?, 'FROM_CACHE', 0, NULL, 'text', '', ?)
            """, ("111111111", 100 + i, 1700000000000 + i * 1000, f"cached {i}"))
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            # ?before=9999999999999 orders all messages DESC and returns the newest 50.
            # recent_messages has the oldest 5 messages (100..104, ts 1700000000000..170000004000)
            # WA DB returns the newest 50 (100..149, ts 170000045000..170000049000) — no overlap.
            # Sender for WA DB rows is '+111111111', not 'FROM_CACHE'.
            resp = client.get(
                "/api/messages?chat_id=111111111&chat_type=contact&limit=50&before=9999999999999"
            )
        data = resp.get_json()
        assert len(data) > 0, "pagination should hit WA DB and return messages"
        assert all(r["sender"] != "FROM_CACHE" for r in data), \
            "pagination returned cached messages — WA DB was not used"

    def test_recent_messages_response_schema(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        _seed_android_db_large(wa_conn)
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=111111111&chat_type=contact&limit=50")
        data = resp.get_json()
        assert len(data) > 0
        expected_keys = {
            "msg_id", "chat_id", "chat_type", "timestamp_ms", "sender",
            "from_me", "archive_path", "media_type", "media_name", "text_body",
            "quoted_text", "quoted_sender", "quoted_ts", "reactions"
        }
        assert set(data[0].keys()) == expected_keys


# ---------------------------------------------------------------------------
# Tests: /api/reaction_details endpoint
# ---------------------------------------------------------------------------

class TestReactionDetailsEndpoint:
    """Tests for /api/reaction_details/<message_id> — Android and iOS."""

    # ---- Android helpers ----

    def _setup_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id                   INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                from_me               INTEGER DEFAULT 0,
                sender_jid_row_id     INTEGER
            );
            CREATE TABLE message_add_on_reaction (
                _id                   INTEGER PRIMARY KEY,
                message_add_on_row_id INTEGER,
                reaction              TEXT,
                sender_timestamp      INTEGER
            );
        """)
        seed_android_db(wa_conn, archive_conn)
        # from_me reaction on message 1 (no sender_jid_row_id)
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (1, 1, 1, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (1, '👍', 1700000000100)"
        )
        # Alice's reaction on message 1 (sender_jid_row_id=1 → '123456789' → 'Alice')
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, from_me, sender_jid_row_id) "
            "VALUES (2, 1, 0, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp) "
            "VALUES (2, '❤️', 1700000000200)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        return viewer.create_app(tmp_path, rescan=False)

    def test_android_total_and_reactors(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        assert data["available"] is True
        assert data["total"] == 2
        assert len(data["reactors"]) == 2

    def test_android_from_me_flag(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        from_me_reactors = [r for r in data["reactors"] if r["from_me"] == 1]
        assert len(from_me_reactors) == 1
        assert from_me_reactors[0]["emoji"] == "👍"

    def test_android_name_resolved_from_contacts(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        names = {r["name"] for r in data["reactors"]}
        assert "Alice" in names

    def test_android_empty_when_no_reactions(self, tmp_path):
        app = self._setup_android(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/999").get_json()
        assert data["available"] is True
        assert data["total"] == 0
        assert data["reactors"] == []

    def test_android_unavailable_when_no_table(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        assert data["available"] is False

    # ---- iOS helpers ----

    def _setup_ios(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB
            );
            CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX
                ON ZWAMESSAGEINFO (ZMESSAGE);
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT
            );
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID) VALUES (10, 1, '99@g.us')"
        )
        # Message from me to provide my_phone derivation
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '15550001111@s.whatsapp.net')"
        )
        # Two reactors identified by phone JID (sub-field 2); sub-field 1 is an opaque token.
        blob = _make_ios_reaction_blob([
            {"token": "3EB0903616E9C7C36F7B", "phone": "15550003333", "emoji": "👍".encode("utf-8")},
            {"token": "3AB0BA8A4CA7122CA06A", "phone": "15550004444", "emoji": "❤️".encode("utf-8")},
        ])
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (blob,)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, '15550002222@s.whatsapp.net')"
        )
        conn.commit()
        conn.close()
        archive_conn = make_archive_db(archive_path)
        archive_conn.close()
        return viewer.create_app(tmp_path, rescan=False)

    def test_ios_total_and_reactors(self, tmp_path):
        app = self._setup_ios(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["total"] == 2
        assert len(data["reactors"]) == 2

    def test_ios_emoji_preserved(self, tmp_path):
        app = self._setup_ios(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        emojis = {r["emoji"] for r in data["reactors"]}
        assert emojis == {"👍", "❤️"}

    def test_ios_unavailable_when_no_blob(self, tmp_path):
        app = self._setup_ios(tmp_path)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        # Message 1 has no ZMESSAGEINFO row → available: False
        assert data["available"] is False

    def test_ios_unavailable_when_no_table(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT
            );
        """)
        conn.commit()
        conn.close()
        archive_conn = make_archive_db(archive_path)
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/1").get_json()
        assert data["available"] is False

    def _ios_db_with_blob(self, tmp_path, contact_jid, blob):
        """Create a minimal iOS ChatStorage with one reacted message (Z_PK 2)."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB
            );
            CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE);
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT
            );
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID) VALUES (10, NULL, ?)",
            (contact_jid,)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '15550001111@s.whatsapp.net')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)", (blob,)
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (2, 10, 0, 100, 2000, NULL)"
        )
        conn.commit()
        conn.close()

    def test_ios_phone_resolves_to_name(self, tmp_path):
        """Reactor identified by phone JID (sub-field 2) is resolved via arch.contacts."""
        phone = "41234567890"
        blob = _make_ios_reaction_blob([
            {"token": "3EB0903616E9C7C36F7B", "phone": phone, "emoji": "👍".encode("utf-8")},
        ])
        self._ios_db_with_blob(tmp_path, "88@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES (?, 'Contacts', ?)",
            (phone, "Bob")
        )
        archive_conn.commit()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["total"] == 1
        assert data["reactors"][0]["name"] == "Bob"
        assert data["reactors"][0]["emoji"] == "👍"

    def test_ios_no_identity_is_from_me(self, tmp_path):
        """Reactor entry with no phone and no @lid JID is the current user's own reaction."""
        blob = _make_ios_reaction_blob([
            {"token": "3EB0903616E9C7C36F7B", "phone": None, "emoji": "👍".encode("utf-8")},
        ])
        self._ios_db_with_blob(tmp_path, "88@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["reactors"][0]["from_me"] == 1

    def test_ios_lid_resolves_via_pushname(self, tmp_path):
        """Group reactor identified by @lid JID resolves to a name via ZWAPROFILEPUSHNAME."""
        lid = "271936022126772"
        blob = _make_ios_reaction_blob([
            {"token": "3A77C1478A60D1C41588", "lid": lid, "emoji": "❤️".encode("utf-8")},
        ])
        self._ios_db_with_blob(tmp_path, "99@g.us", blob)
        # Insert a push-name row keyed by the @lid JID
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.execute(
            "INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) VALUES (1, ?, 'Elena')",
            (f"{lid}@lid",)
        )
        conn.commit()
        conn.close()
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["reactors"][0]["name"] == "Elena"
        assert data["reactors"][0]["emoji"] == "❤️"
        assert data["reactors"][0]["from_me"] == 0

    def test_ios_lid_unknown_when_no_name_source(self, tmp_path):
        """@lid reactor with no matching entry in any name table resolves to 'Unknown'."""
        blob = _make_ios_reaction_blob([
            {"token": "3A42C9813B3F02D513AA", "lid": "999000111222333", "emoji": "👍".encode("utf-8")},
        ])
        self._ios_db_with_blob(tmp_path, "99@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()
        assert data["available"] is True
        assert data["reactors"][0]["name"] == "Unknown"
        assert data["reactors"][0]["from_me"] == 0
