"""
Tests for wa_chat_viewer.py

Run with:
    pytest tests/test_wa_chat_viewer.py -v

Requires:
    pip install pytest flask
"""

import os
import sqlite3
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "wa_chat_viewer", os.path.join(_ROOT, "wa_chat_viewer.py")
)
viewer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(viewer)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_archive_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE contacts (
            _id INTEGER PRIMARY KEY,
            folder TEXT UNIQUE,
            number TEXT,
            display_name TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE groups (
            _id INTEGER PRIMARY KEY,
            folder TEXT UNIQUE,
            chat_row_id INTEGER,
            subject TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE archive_copies (
            id INTEGER PRIMARY KEY,
            original_path TEXT UNIQUE,
            archive_path TEXT
        )
    """)
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def make_cache_db(path: Path):
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
# Tests: _chat_display_name
# ---------------------------------------------------------------------------

class TestChatDisplayName:
    def test_contact_with_display_name(self, tmp_path):
        archive_db = make_archive_db(tmp_path / ".wa_media_archiver.db")
        cur = archive_db.cursor()
        cur.execute(
            "INSERT INTO contacts (folder, number, display_name) VALUES (?, ?, ?)",
            ("123456789", "123456789", "Alice")
        )
        archive_db.commit()
        name = viewer._chat_display_name("123456789", "contact", cur)
        assert name == "Alice"

    def test_contact_fallback_to_folder(self, tmp_path):
        archive_db = make_archive_db(tmp_path / ".wa_media_archiver.db")
        cur = archive_db.cursor()
        cur.execute(
            "INSERT INTO contacts (folder, number, display_name) VALUES (?, ?, ?)",
            ("987654321", "987654321", "")
        )
        archive_db.commit()
        name = viewer._chat_display_name("987654321", "contact", cur)
        assert name == "987654321"

    def test_group_with_subject(self, tmp_path):
        archive_db = make_archive_db(tmp_path / ".wa_media_archiver.db")
        cur = archive_db.cursor()
        cur.execute(
            "INSERT INTO groups (folder, chat_row_id, subject) VALUES (?, ?, ?)",
            ("Family", 1, "Family Group")
        )
        archive_db.commit()
        name = viewer._chat_display_name("1", "group", cur)
        assert name == "Family Group"

    def test_group_fallback_to_id(self, tmp_path):
        archive_db = make_archive_db(tmp_path / ".wa_media_archiver.db")
        cur = archive_db.cursor()
        name = viewer._chat_display_name("99", "group", cur)
        assert name == "99"


# ---------------------------------------------------------------------------
# Tests: Cache DB schema
# ---------------------------------------------------------------------------

class TestCacheSchema:
    def test_schema_creates_tables(self, tmp_path):
        cache_path = tmp_path / ".wa_chat_viewer_cache.db"
        conn = sqlite3.connect(str(cache_path))
        conn.executescript(viewer.CACHE_SCHEMA)

        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "messages" in table_names
        assert "sync_meta" in table_names

        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        index_names = [i[0] for i in indexes]
        assert "idx_messages_chat_ts" in index_names

        virtuals = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5%'"
        ).fetchall()
        assert ("messages_fts",) in virtuals


# ---------------------------------------------------------------------------
# Tests: _build_cache media-only mode
# ---------------------------------------------------------------------------

class TestBuildCacheMediaOnly:
    def test_build_media_only(self, tmp_path):
        archive_db = make_archive_db(tmp_path / ".wa_media_archiver.db")

        archive_db.execute(
            "INSERT INTO contacts (folder, number, display_name) VALUES (?, ?, ?)",
            ("Contacts", "555", "Bob")
        )
        archive_db.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
            ("Media/WhatsApp Images/IMG-001.jpg", "555/IMG-001.jpg")
        )
        archive_db.commit()

        # create a real file so mtime is available
        media_file = tmp_path / "555" / "IMG-001.jpg"
        media_file.parent.mkdir(parents=True)
        media_file.write_text("fake image content")

        cache_path = tmp_path / ".wa_chat_viewer_cache.db"
        cache_conn = make_cache_db(cache_path)

        viewer._build_cache(archive_db, cache_conn, tmp_path, rescan=False)

        rows = cache_conn.execute("SELECT * FROM messages").fetchall()
        assert len(rows) == 1
        assert rows[0]["chat_id"] == "555"
        assert rows[0]["chat_type"] == "contact"
        assert rows[0]["media_type"] == "image"
        assert rows[0]["archive_path"] == "555/IMG-001.jpg"


# ---------------------------------------------------------------------------
# Tests: Flask routes
# ---------------------------------------------------------------------------

class TestFlaskRoutes:
    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        archive_db = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_db.execute(
            "INSERT INTO contacts (folder, number, display_name) VALUES (?, ?, ?)",
            ("111", "111", "Test Contact")
        )
        archive_db.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
            ("Media/IMG.jpg", "Contacts/IMG.jpg")
        )
        archive_db.commit()
        archive_db.close()

        cache_path = tmp_path / ".wa_chat_viewer_cache.db"
        cache_conn = make_cache_db(cache_path)
        cache_conn.execute(
            "INSERT INTO messages (chat_id, chat_type, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("111", "contact", 1700000000000, "111", 0, "Contacts/IMG.jpg", "image", "IMG.jpg", "Hello")
        )
        cache_conn.execute(
            "INSERT INTO messages_fts (rowid, chat_id, sender, text_body, media_name, archive_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "111", "111", "Hello", "IMG.jpg", "Contacts/IMG.jpg")
        )
        cache_conn.commit()
        cache_conn.close()

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
        assert data[0]["id"] == "111"
        assert data[0]["display_name"] == "Test Contact"

    def test_api_messages_returns_messages(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/messages?chat_id=111&chat_type=contact")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["text_body"] == "Hello"

    def test_api_messages_cursor_before(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/messages?chat_id=111&chat_type=contact&before=1800000000000&limit=10")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_api_search_finds_message(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/search?q=Hello")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["text_body"] == "Hello"

    def test_api_search_empty_query(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/api/search?q=")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_media_404_for_missing_file(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/media/Contacts/IMG.jpg")
        assert resp.status_code == 404

    def test_media_403_for_path_traversal(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get("/media/../wa_media_archiver.py")
        assert resp.status_code == 403
