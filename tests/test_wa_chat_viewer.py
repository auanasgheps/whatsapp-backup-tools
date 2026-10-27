"""
Tests for wab_viewer
"""

import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from tests.conftest import (
    make_android_db,
    make_archive_db,
    seed_android_db,
)
from wab_viewer import main as viewer


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
        assert viewer._media_type_from_path("foo/stickers/abc.webp") == "sticker"
        assert (
            viewer._media_type_from_path("Groups/Chat/2026/STK-20260101-WA0001.webp") == "sticker"
        )
        assert viewer._media_type_from_path("Groups/Chat/2026/STK-20260101-WA0001.was") == "sticker"

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

    def test_android_detected_in_whatsapp_databases(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        (db_dir / "msgstore.db").touch()
        typ, path = viewer._detect_source_db(tmp_path)
        assert typ == "android"
        assert path == str(db_dir / "msgstore.db")

    def test_ios_detected_in_whatsapp_databases(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        (db_dir / "ChatStorage.sqlite").touch()
        typ, path = viewer._detect_source_db(tmp_path)
        assert typ == "ios"
        assert path == str(db_dir / "ChatStorage.sqlite")


# ---------------------------------------------------------------------------
# Tests: cache schema
# ---------------------------------------------------------------------------


class TestCacheSchema:
    def test_schema_creates_tables(self, tmp_path):
        cache_path = tmp_path / ".wa_viewer.db"
        conn = sqlite3.connect(str(cache_path))
        conn.executescript(viewer.CACHE_SCHEMA)

        tables = {
            t[0]
            for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "message_index" in tables
        assert "sync_meta" in tables
        assert "indexed_chats" in tables
        assert "user_preferences" in tables

        indexes = {
            i[0]
            for i in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert "idx_midx_chat_ts" in indexes

        virtuals = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5%'"
            ).fetchall()
        ]
        assert "message_index_fts" in virtuals
        conn.close()

    def test_schema_no_contentless_delete(self):
        assert "contentless_delete" not in viewer.CACHE_SCHEMA

    def test_clear_fts_index_resets_fts_table(self, tmp_path):
        cache_conn = viewer._open_cache_db(tmp_path)
        cache_conn.execute(
            "INSERT INTO message_index (rowid, chat_id, chat_type, timestamp_ms) VALUES (1, 'c1', 'contact', 1000)"
        )
        cache_conn.execute(
            "INSERT INTO message_index_fts (rowid, text_body) VALUES (1, 'Hello world')"
        )
        cache_conn.execute(
            "INSERT INTO indexed_chats (chat_id, chat_type) VALUES ('c1', 'contact')"
        )
        cache_conn.commit()

        viewer._clear_fts_index(cache_conn)

        assert cache_conn.execute("SELECT COUNT(*) FROM message_index").fetchone()[0] == 0
        assert cache_conn.execute("SELECT COUNT(*) FROM indexed_chats").fetchone()[0] == 0
        fts_count = cache_conn.execute("SELECT COUNT(*) FROM message_index_fts").fetchone()[0]
        assert fts_count == 0

        cache_conn.execute(
            "INSERT INTO message_index_fts (rowid, text_body) VALUES (2, 'New text message')"
        )
        cache_conn.commit()
        match_row = cache_conn.execute(
            "SELECT rowid FROM message_index_fts WHERE message_index_fts MATCH 'New'"
        ).fetchone()
        assert match_row is not None and match_row[0] == 2
        cache_conn.close()

    def test_open_cache_db_recovers_from_incompatible_fts(self, tmp_path, monkeypatch):
        conn = viewer._open_cache_db(tmp_path)
        conn.close()

        first_call = True
        real_connect = sqlite3.connect

        def mock_connect(*args, **kwargs):
            nonlocal first_call
            real_conn = real_connect(*args, **kwargs)
            if first_call:
                first_call = False

                class FailingConn:
                    def __getattr__(self, name):
                        return getattr(real_conn, name)

                    def executescript(self, script):
                        raise sqlite3.OperationalError('unrecognized option: "contentless_delete"')

                return FailingConn()
            return real_conn

        monkeypatch.setattr(viewer.sqlite3, "connect", mock_connect)
        recovered_conn = viewer._open_cache_db(tmp_path)
        assert recovered_conn is not None
        assert not first_call
        recovered_conn.close()


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
        resp = client.get(
            "/api/messages?chat_id=123456789&chat_type=contact&before=1800000000000&limit=10"
        )
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
            if (
                client.get("/api/chat-index-status?chat_id=123456789&chat_type=contact").get_json()[
                    "status"
                ]
                == "done"
            ):
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

    def test_media_range_request_invalid_returns_416(self, app_and_tmp):
        client, tmp = app_and_tmp
        media_file = tmp / "test_video.mp4"
        media_file.write_bytes(b"0123456789ABCDEF")  # 16 bytes

        resp_bad_unit = client.get("/media/test_video.mp4", headers={"Range": "items=0-9"})
        assert resp_bad_unit.status_code == 416
        resp_bad_vals = client.get("/media/test_video.mp4", headers={"Range": "bytes=abc-xyz"})
        assert resp_bad_vals.status_code == 416

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
            resp = client.post("/api/preferences", json={"key": "theme", "value": "light"})
            assert resp.get_json()["ok"] is True
            data = client.get("/api/preferences").get_json()
        assert data["theme"] == "light"

    def test_post_persists_across_restarts(self, tmp_path):
        app1 = self._make_app(tmp_path)
        with app1.test_client() as client:
            client.post("/api/preferences", json={"key": "date_format", "value": "MM/DD/YYYY"})

        app2 = viewer.create_app(tmp_path, rescan=False)
        app2.config["TESTING"] = True
        with app2.test_client() as client:
            data = client.get("/api/preferences").get_json()
        assert data["date_format"] == "MM/DD/YYYY"

    def test_post_invalid_key_returns_400(self, tmp_path):
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/preferences", json={"key": "unknown_key", "value": "x"})
        assert resp.status_code == 400

    def test_post_invalid_value_returns_400(self, tmp_path):
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/preferences", json={"key": "theme", "value": "neon"})
        assert resp.status_code == 400
        assert "invalid value for theme" in resp.get_json()["error"]

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
                if (
                    client.get(
                        "/api/chat-index-status?chat_id=123456789&chat_type=contact"
                    ).get_json()["status"]
                    == "done"
                ):
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
            count_before = (
                sqlite3.connect(str(tmp_path / ".wa_viewer.db"))
                .execute("SELECT COUNT(*) FROM indexed_chats")
                .fetchone()[0]
            )
            assert count_before == 0

            client.get("/api/messages?chat_id=123456789&chat_type=contact")

            # Background thread — poll until done (max 5 s)
            import time

            deadline = time.time() + 5
            count_after = 0
            while time.time() < deadline:
                count_after = (
                    sqlite3.connect(str(tmp_path / ".wa_viewer.db"))
                    .execute("SELECT COUNT(*) FROM indexed_chats")
                    .fetchone()[0]
                )
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
                if (
                    client.get(
                        "/api/chat-index-status?chat_id=123456789&chat_type=contact"
                    ).get_json()["status"]
                    == "done"
                ):
                    break
                time.sleep(0.05)

            # Exactly one entry in indexed_chats — not doubled
            count = (
                sqlite3.connect(str(tmp_path / ".wa_viewer.db"))
                .execute("SELECT COUNT(*) FROM indexed_chats")
                .fetchone()[0]
            )
            assert count == 1

            # Exactly one indexing run worth of rows in message_index
            rows = (
                sqlite3.connect(str(tmp_path / ".wa_viewer.db"))
                .execute("SELECT COUNT(*) FROM message_index")
                .fetchone()[0]
            )
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
            archive_conn.execute(
                f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')"
            )
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
            archive_conn.execute(
                f"INSERT INTO files (original_path, md5) VALUES ('{orig}', x'deadbeef')"
            )
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

    def test_media_types_parameter_filtering(self, tmp_path):
        """/api/media supports types parameter for server-side filtering and combined streams."""
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
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (52, 10, 0, 1, 1700000052000, NULL, 6)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (52, NULL, 'contract.pdf')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            links = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=link"
            ).get_json()
            assert len(links) == 1
            assert links[0]["media_type"] == "link"

            images = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image"
            ).get_json()
            assert len(images) == 1
            assert images[0]["media_type"] == "image"

            mixed = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=image,link"
            ).get_json()
            assert len(mixed) == 2
            assert [r["media_type"] for r in mixed] == ["link", "image"]

            all_items = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=all"
            ).get_json()
            assert len(all_items) == 3
            assert {r["media_type"] for r in all_items} == {"image", "link", "document"}

            paged = client.get(
                f"/api/media?chat_id=123456789&chat_type=contact&types=all&before={mixed[0]['timestamp_ms']}"
            ).get_json()
            assert all(r["timestamp_ms"] < mixed[0]["timestamp_ms"] for r in paged)

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
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '987654321')")
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

    def test_resolve_and_cache_reactions(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_add_on_reaction (
                message_add_on_row_id INTEGER PRIMARY KEY,
                reaction TEXT
            );
        """)
        wa_conn.execute("INSERT INTO message_add_on (_id, parent_message_row_id) VALUES (10, 100)")
        wa_conn.execute(
            "INSERT INTO message_add_on_reaction (message_add_on_row_id, reaction) VALUES (10, '👍')"
        )
        wa_conn.commit()

        rows = [{"chat_id": "123", "msg_id": 100}]
        viewer._resolve_and_cache_reactions("android", wa_conn, arc_conn, rows)
        assert rows[0]["reactions"] == "👍"

        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE msg_id = 100"
        ).fetchone()
        assert cached is not None
        assert cached[0] == "👍"

        # Edge cases
        viewer._resolve_and_cache_reactions("ios", wa_conn, arc_conn, rows)
        viewer._resolve_and_cache_reactions("android", wa_conn, arc_conn, [])
        viewer._resolve_and_cache_reactions("android", wa_conn, arc_conn, [{"chat_id": "123"}])

        wa_conn.close()
        arc_conn.close()

    def test_recent_messages_self_healing_reactions(self, tmp_path):
        """When recent_messages has null reactions and reactions_cache is empty, fallback to wa_conn self-heals."""
        app = self._setup(tmp_path)
        app.config["TESTING"] = True
        archive_path = tmp_path / ".wa_media_archiver.db"

        # Prime recent_messages with null reactions and ensure reactions_cache is wiped
        arc_conn = sqlite3.connect(str(archive_path))
        arc_conn.execute("DELETE FROM reactions_cache")
        arc_conn.execute("DELETE FROM recent_messages")
        arc_conn.execute("""
            INSERT INTO recent_messages (
                chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, media_type, text_body, reactions
            ) VALUES ('123456789', 'contact', 1, 1700000000000, '+123456789', 0, 'text', 'Hello from cached', NULL)
        """)
        arc_conn.commit()
        arc_conn.close()

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            data = resp.get_json()

        assert len(data) == 1
        msg = data[0]
        assert msg["msg_id"] == 1
        # Must be self-healed from wa_conn (message 1 has two thumbs up in _setup)
        assert msg["reactions"] == "👍,👍"

        # Verify write-through persistence to reactions_cache and recent_messages
        arc_conn = sqlite3.connect(str(archive_path))
        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE msg_id = 1"
        ).fetchone()
        assert cached is not None
        assert cached[0] == "👍,👍"

        recent = arc_conn.execute(
            "SELECT reactions FROM recent_messages WHERE msg_id = 1"
        ).fetchone()
        assert recent is not None
        assert recent[0] == "👍,👍"
        arc_conn.close()


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
                _encode_varint((1 << 3) | 0)
                + _encode_varint(m["delivered_delta"])
                + _encode_varint((2 << 3) | 0)
                + _encode_varint(1)  # code (ignored)
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
            _encode_varint((1 << 3) | 0)
            + _encode_varint(m["delivered_delta"])
            + _encode_varint((2 << 3) | 0)
            + _encode_varint(3)  # code (ignored)
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
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 10, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == (base_ts + 10) * 1000
        assert members[0]["read_ts"] is None

    def test_group_delivered_and_read(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 20, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000  # delta 0 is a real delivery
        assert members[0]["read_ts"] == (base_ts + 20) * 1000

    def test_group_read_at_send_delta_zero(self):
        # read_delta == 0 means read at send time — must not collapse to None.
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 0, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] == base_ts * 1000

    def test_group_field4_is_ignored(self):
        # A large field-4 value (read receipts disabled) must not gate delivered/read.
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {
                    "lid": "1234567890123",
                    "delivered_delta": 17,
                    "read_delta": None,
                    "noise4": 1089263,
                },
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["delivered_ts"] == (base_ts + 17) * 1000
        assert members[0]["read_ts"] is None

    def test_group_lid_trailing_f_stripped(self):
        # Odd-length LID is padded with an 'f' nibble; parser must strip it.
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["jid"] == "1234567890123"

    def test_group_multiple_members(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 5, "noise4": None},
                {"lid": "2222222222222", "delivered_delta": 15, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 2
        assert members[0]["read_ts"] == (base_ts + 5) * 1000
        assert members[1]["read_ts"] is None
        assert members[1]["delivered_ts"] == (base_ts + 15) * 1000

    def test_group_regression_verified_deltas(self):
        # Mirrors two real ground-truth messages (synthetic LIDs, real validated deltas):
        #   read 5926s / 4708s / 1462s after send; a member with read receipts off (no field 5).
        base_ts = 1768325919
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 5926, "noise4": 2},
                {
                    "lid": "2222222222222",
                    "delivered_delta": 17,
                    "read_delta": None,
                    "noise4": 1089263,
                },
                {"lid": "3333333333333", "delivered_delta": 0, "read_delta": 0, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert members[0]["read_ts"] == (base_ts + 5926) * 1000
        assert members[1]["read_ts"] is None  # read receipts disabled
        assert members[1]["delivered_ts"] == (base_ts + 17) * 1000
        assert members[2]["read_ts"] == base_ts * 1000  # read at send

    # --- 1-to-1 mode (is_group=False): collapse to a single member ---

    def test_1to1_delivered_and_read(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": 120, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] == (base_ts + 120) * 1000

    def test_1to1_delivered_only(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1234567890123", "delivered_delta": 0, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000
        assert members[0]["read_ts"] is None

    def test_1to1_multi_device_collapses_to_earliest(self):
        base_ts = 1700000000
        blob = _make_receipt_blob(
            base_ts,
            [
                {"lid": "1111111111111", "delivered_delta": 0, "read_delta": 253, "noise4": None},
                {"lid": "2222222222222", "delivered_delta": 26, "read_delta": None, "noise4": None},
            ],
        )
        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == base_ts * 1000  # min delta = 0
        assert members[0]["read_ts"] == (base_ts + 253) * 1000

    # --- compact format: no base_ts, delivered from field 9, read blank ---

    def test_compact_group_delivered_only(self):
        msg_ts_s = 1784037821
        blob = _make_compact_receipt_blob(
            [
                {"lid": "1111111111111", "delivered_delta": 0},
                {"lid": "2222222222222", "delivered_delta": 60},
            ]
        )
        members = viewer._parse_ios_receipt_blob(
            blob, _NoOpConn(), is_group=True, msg_ts_s=msg_ts_s
        )
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
        entry += _encode_varint((8 << 3) | 1) + b"\x00" * 8  # unknown field, wire=1 (fixed64)
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
            entry += ld(2, f"{e['phone']}@s.whatsapp.net".encode())
        elif e.get("lid"):
            entry += ld(2, f"{e['lid']}@lid".encode())
        entry += ld(3, e["emoji"])
        if e.get("from_me"):
            entry += ld(5, b"\x0a\x07" + b"\x81\x39\x34\x00\x00\x00\x00")
        field7 += ld(1, entry)
    top_tag = (7 << 3) | 2
    return _encode_varint(top_tag) + _encode_varint(len(field7)) + field7


class TestIOSReactions:
    def test_parse_heart_reaction(self):
        blob = _make_zreceipt_blob_with_reactions([("334142384436", "❤️".encode())])
        result = viewer._extract_ios_reactions(blob)
        assert len(result) == 1
        sender, emoji = result[0]
        assert sender == "334142384436"
        assert emoji == "❤️"

    def test_parse_multiple_reactions(self):
        blob = _make_zreceipt_blob_with_reactions(
            [
                ("334142384436", "❤️".encode()),
                ("334135434342", "😂".encode()),
            ]
        )
        result = viewer._extract_ios_reactions(blob)
        assert len(result) == 2
        assert result[0][1] == "❤️"
        assert result[1][1] == "😂"

    def test_parse_various_emoji(self):
        blobs = []
        for emoji in ["😂", "😮", "😭", "👏"]:
            blobs.append(
                _make_zreceipt_blob_with_reactions([("334142384436", emoji.encode("utf-8"))])
            )
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
        blob = _make_zreceipt_blob_with_reactions(
            [
                ("334142384436", "❤️".encode() + "👏".encode()),
            ]
        )
        result = viewer._extract_ios_reactions(blob)
        # Both emoji returned; VS16 is a presentation modifier, not a standalone emoji
        assert len(result) == 2
        emojis = sorted(r[1] for r in result)
        assert emojis == sorted(["❤️", "👏"])

    def test_many_reactions_large_field7(self):
        """Field 7 >= 128 bytes needs a multi-byte varint length; all reactors survive.

        The old single-byte length read truncated field 7 and dropped reactors.
        """
        reactors = [(f"33414238{i:04d}", "❤️".encode()) for i in range(11)]
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

        laugh = "😂".encode()
        heart = "❤️".encode()
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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE)"
        )
        # ZWAGROUPMEMBER must exist (even if unused for contact chats)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)"
        )

        def encode_field7_reactor(phone_hex_str, emoji_bytes):
            phone_data = bytes.fromhex(phone_hex_str)
            entry = bytes([0x0A, len(phone_data)]) + phone_data
            entry += bytes([0x1A, len(emoji_bytes)]) + emoji_bytes
            field7 = bytes([0x0A, len(entry)]) + entry
            return bytes([0x3A, len(field7)]) + field7  # field 7, wire 2

        # Message from "me" (ZISFROMME=1) to identify my JID — no ZMESSAGEINFO needed
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, '49123456789@s.whatsapp.net')"
        )
        # A message with reactions (ZISFROMME=0 = received)
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (encode_field7_reactor("34393132343536373839", "😂".encode()),),
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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ZWAMESSAGEINFO_ZMESSAGE_INDEX ON ZWAMESSAGEINFO (ZMESSAGE)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT)"
        )

        def encode_field7_reactor(phone_hex_str, emoji_bytes):
            phone_data = bytes.fromhex(phone_hex_str)
            entry = bytes([0x0A, len(phone_data)]) + phone_data
            entry += bytes([0x1A, len(emoji_bytes)]) + emoji_bytes
            field7 = bytes([0x0A, len(entry)]) + entry
            return bytes([0x3A, len(field7)]) + field7

        # Sent message with NULL ZFROMJID (system message) — "me" detection must not crash
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZFROMJID) "
            "VALUES (1, 10, 1, NULL, 1000, NULL)"
        )
        # A message with reactions — reactor phone differs from "me" (which has NULL JID → my_phone=None)
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)",
            (encode_field7_reactor("34393132343536373839", "😂".encode()),),
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
        dynamic_ids = {"img-lightbox"}
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
        fn_start = script.index("async function _loadGalleryPage(")
        # find the end: next top-level 'async function' or plain 'function' at col 2
        import re as _re

        next_fn = _re.search(r"\n  (async )?function ", script[fn_start + 1 :])
        fn_body = script[fn_start : fn_start + 1 + (next_fn.start() if next_fn else len(script))]

        assert "lightboxItems.push" not in fn_body, (
            "_loadGalleryPage must not push to lightboxItems — renderGalleryItem handles that"
        )

    def test_msg_media_aspect_ratio_preservation_css(self):
        """msg-media img/video must preserve aspect ratio with width: auto and object-fit: contain,
        preventing horizontal stretching for tall/vertical photos with captions."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")

        rule_match = re.search(r"\.msg-media\s+img[^{]*\{([^}]+)\}", css)
        assert rule_match, ".msg-media img rule missing in app.css"
        rule_body = rule_match.group(1)
        assert "width: auto" in rule_body, (
            "Media must use width: auto to avoid forced horizontal stretch"
        )
        assert "object-fit: contain" in rule_body, (
            "Media must use object-fit: contain to preserve aspect ratio"
        )
        assert "max-height:" in rule_body, "Media must specify max-height constraint"

        wrap_match = re.search(r"(?<!-)\.msg-media\s*\{([^}]+)\}", css)
        assert wrap_match, ".msg-media rule missing in app.css"
        wrap_body = wrap_match.group(1)
        assert "max-width: 320px" in wrap_body, "Media container must clamp max-width to 320px"
        assert "width: fit-content" in wrap_body, (
            "Media container must use width: fit-content to prevent empty wasted space"
        )

    def test_lightbox_wheel_zoom_implemented(self):
        """app.js must provide lightbox wheel-to-zoom and pan functionality."""
        app_js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        script = app_js_path.read_text(encoding="utf-8")

        assert "_setupLightboxZoom" in script, "_setupLightboxZoom helper must be present in app.js"
        assert "lb.addEventListener('wheel'" in script, "Lightbox wheel listener must be registered"
        assert "scale" in script and "panX" in script, "Zoom scale and pan state must be tracked"
        assert "lb-zoom-badge" in script, "Zoom reset indicator badge must be supported"


class TestReactionsFrontendLayout:
    def test_reactions_css_corner_alignment(self):
        """Reactions must be anchored on the receiver's corner:
        sent (from-me) -> right, recv (from-them) -> left."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")

        sent_match = re.search(r"\.msg-row\.sent\s+\.msg-reactions\s*\{([^}]+)\}", css)
        assert sent_match, ".msg-row.sent .msg-reactions rule missing in app.css"
        assert "right:" in sent_match.group(1), ".msg-row.sent .msg-reactions must align right"

        recv_match = re.search(r"\.msg-row\.recv\s+\.msg-reactions\s*\{([^}]+)\}", css)
        assert recv_match, ".msg-row.recv .msg-reactions rule missing in app.css"
        assert "left:" in recv_match.group(1), ".msg-row.recv .msg-reactions must align left"

    def test_reactions_css_no_split_badge(self):
        """app.css must not contain opposite-corner split rules (rx-badge-2)."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        assert "rx-badge-2" not in css, "Obsolete rx-badge-2 rule still found in app.css"

    def test_reactions_js_no_split_logic(self):
        """app.js must not split reactions into rx-badge-2."""
        js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        js = js_path.read_text(encoding="utf-8")
        assert "rx-badge-2" not in js, "Obsolete rx-badge-2 logic still found in app.js"

    def test_reactions_js_marks_has_reactions(self):
        """app.js must add 'has-reactions' class to message rows with reactions."""
        js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        js = js_path.read_text(encoding="utf-8")
        assert "has-reactions" in js

    def test_gallery_month_headers_synced_on_open_and_load(self):
        """openMediaGallery and _loadGalleryPage must synchronize month headers."""
        js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        js = js_path.read_text(encoding="utf-8")
        assert "_syncMonthHeaders();\n  }\n\n  async function _loadGalleryPage" in js
        assert (
            "_appendToArchiveView(items, activeArchiveDir());\n      }\n      _syncMonthHeaders();"
            in js
        )

    def test_gallery_month_headers_chronological_insertion(self):
        """Gallery grid items and month headers must be inserted in strictly descending
        chronological order without stale global month state."""
        js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        js = js_path.read_text(encoding="utf-8")
        assert "function _insertGalleryGridItem(grid, msg)" in js
        assert "galleryLastMonthKey" not in js, (
            "Stale galleryLastMonthKey state must not exist in app.js"
        )
        assert "monthHdr.dataset.month = String(mk);" in js
        assert "cell.dataset.ts = String(msg.timestamp_ms" in js


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
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)"
        )
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
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)"
        )
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
                if (
                    client.get(
                        "/api/chat-index-status?chat_id=123456789&chat_type=contact"
                    ).get_json()["status"]
                    == "done"
                ):
                    break
                time.sleep(0.05)
            data = client.get("/api/chat-info?chat_id=123456789&chat_type=contact").get_json()
        assert data["first_ts"] == 1700000000000
        assert data["last_ts"] == 1700000000000

    def test_create_app_raises_when_no_wa_db(self, tmp_path):
        """Without a WA source DB, create_app refuses to start by raising FileNotFoundError."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        with pytest.raises(FileNotFoundError, match="WhatsApp database not found"):
            viewer.create_app(tmp_path, rescan=False)

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

    def test_android_group_chat_info_with_description(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)"
        )
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (3, '987654321')")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (10, 20, 0, 3, 1700000000001, 'Hi group', 0)"
        )
        wa_conn.execute(
            "CREATE TABLE message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER)"
        )
        wa_conn.execute("INSERT INTO message_system VALUES (11, 27)")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (11, 20, 0, 3, 1700000000002, 'Synthesized group description text', 7)"
        )
        wa_conn.commit()
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

        assert data["description"] == "Synthesized group description text"

    def test_android_group_chat_info_without_description(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)"
        )
        wa_conn.commit()
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

        assert data["description"] is None

    def test_android_group_chat_info_cleared_description(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (20, 2, 'Test Group', 0, 1700000000001, 10)"
        )
        wa_conn.execute(
            "CREATE TABLE message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER)"
        )
        wa_conn.execute("INSERT INTO message_system VALUES (11, 27)")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (11, 20, 0, 3, 1700000000002, 'Old description', 7)"
        )
        # More recent description-clear message
        wa_conn.execute("INSERT INTO message_system VALUES (12, 27)")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (12, 20, 0, 3, 1700000000005, '', 7)"
        )
        wa_conn.commit()
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

        assert data["description"] is None

    def test_contact_chat_info_has_no_description(self, tmp_path):
        app = _make_android_app(tmp_path)
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=123456789&chat_type=contact").get_json()
        assert "description" not in data

    def test_ios_group_chat_info_with_description(self, tmp_path):
        import base64

        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        desc_text = "Synthesized iOS Group Description"
        desc_bytes = desc_text.encode("utf-8")
        sub_msg = bytes([0x1A, len(desc_bytes)]) + desc_bytes
        main_msg = bytes([0x0A, len(sub_msg)]) + sub_msg
        proto_pic = "+" + base64.b64encode(main_msg).decode("ascii")

        conn = sqlite3.connect(str(wa_path))
        conn.executescript(f"""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT, ZPICTUREID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '15550001111@s.whatsapp.net', '{proto_pic}');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test iOS Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '15550001111@s.whatsapp.net', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Alice', 'Hi', '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test iOS Group', 'Test iOS Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=10&chat_type=group").get_json()

        assert data["description"] == desc_text

    def test_ios_group_chat_info_without_description(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT, ZPICTUREID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '15550001111@s.whatsapp.net', NULL);
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test iOS Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '15550001111@s.whatsapp.net', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Alice', 'Hi', '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test iOS Group', 'Test iOS Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=10&chat_type=group").get_json()

        assert data["description"] is None

    def test_protobuf_extract_ios_group_description_edge_cases(self):
        from wab_viewer.main import _extract_ios_group_description

        assert _extract_ios_group_description(None) is None
        assert _extract_ios_group_description("") is None
        assert _extract_ios_group_description("not-base64!#$@") is None
        assert _extract_ios_group_description("+") is None


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
            (jid_row_id, str(chat_id * 111111111)),
        )
        wa_conn.execute(
            "INSERT OR IGNORE INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (?, ?, NULL, 0, ?, ?)",
            (chat_id, jid_row_id, 1700000000000 + chat_id * 1000, chat_id * 50),
        )
        for i in range(50):
            msg_id = chat_id * 100 + i
            ts = 1700000000000 + (chat_id - 1) * 50 * 1000 + i * 1000
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (msg_id, chat_id, i % 2, jid_row_id, ts, f"msg {i}"),
            )
    wa_conn.commit()


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
            "chat_id",
            "chat_type",
            "msg_id",
            "timestamp_ms",
            "sender",
            "from_me",
            "archive_path",
            "media_type",
            "media_name",
            "text_body",
            "quoted_text",
            "quoted_sender",
            "quoted_ts",
            "is_edited",
            "edited_ts",
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
            idxs = [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='recent_messages'"
                )
            ]
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
            archive_conn.execute(
                """
                INSERT OR REPLACE INTO recent_messages
                (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
                VALUES (?, 'contact', ?, ?, 'FROM_CACHE', 0, NULL, 'text', '', ?)
            """,
                ("111111111", i + 1, 1700000000000 + i, f"cached msg {i}"),
            )
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
            archive_conn.execute(
                """
                INSERT OR REPLACE INTO recent_messages
                (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body)
                VALUES (?, 'contact', ?, ?, 'FROM_CACHE', 0, NULL, 'text', '', ?)
            """,
                ("111111111", 100 + i, 1700000000000 + i * 1000, f"cached {i}"),
            )
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
        assert all(r["sender"] != "FROM_CACHE" for r in data), (
            "pagination returned cached messages — WA DB was not used"
        )

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
            "msg_id",
            "chat_id",
            "chat_type",
            "timestamp_ms",
            "sender",
            "from_me",
            "archive_path",
            "media_type",
            "media_name",
            "text_body",
            "quoted_text",
            "quoted_sender",
            "quoted_ts",
            "quoted_msg_id",
            "quoted_has_thumb",
            "reactions",
            "is_edited",
            "edited_ts",
            "is_view_once",
            "stanza_id",
            "media_width",
            "media_height",
            "media_size",
            "media_duration",
            "aspect_ratio",
            "has_thumbnail",
            "link_title",
            "link_desc",
            "link_url",
            "link_preview_type",
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
        blob = _make_ios_reaction_blob(
            [
                {"token": "3EB0903616E9C7C36F7B", "phone": "15550003333", "emoji": "👍".encode()},
                {"token": "3AB0BA8A4CA7122CA06A", "phone": "15550004444", "emoji": "❤️".encode()},
            ]
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (100, 2, ?)", (blob,)
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
            (contact_jid,),
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
        blob = _make_ios_reaction_blob(
            [
                {"token": "3EB0903616E9C7C36F7B", "phone": phone, "emoji": "👍".encode()},
            ]
        )
        self._ios_db_with_blob(tmp_path, "88@g.us", blob)
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES (?, 'Contacts', ?)",
            (phone, "Bob"),
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
        blob = _make_ios_reaction_blob(
            [
                {"token": "3EB0903616E9C7C36F7B", "phone": None, "emoji": "👍".encode()},
            ]
        )
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
        blob = _make_ios_reaction_blob(
            [
                {"token": "3A77C1478A60D1C41588", "lid": lid, "emoji": "❤️".encode()},
            ]
        )
        self._ios_db_with_blob(tmp_path, "99@g.us", blob)
        # Insert a push-name row keyed by the @lid JID
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.execute(
            "INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) VALUES (1, ?, 'Elena')",
            (f"{lid}@lid",),
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
        blob = _make_ios_reaction_blob(
            [
                {"token": "3A42C9813B3F02D513AA", "lid": "999000111222333", "emoji": "👍".encode()},
            ]
        )
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


# ===========================================================================
# iOS MessagingInfraDatabase & LID.sqlite Tests
# ===========================================================================


class TestIosReceiptDeviceParser:
    def test_1to1_receipt_device_timestamps_converted_to_ms(self):
        class _MockConn:
            def execute(self, sql, params=None):
                class _Row:
                    def fetchone(self):
                        return None

                return _Row()

        infra_rows = [
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "device_id": 0,
                "delivered_timestamp": 1700000010,
                "read_timestamp": 1700000050,
                "played_timestamp": 1700000080,
            },
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "device_id": 1,
                "delivered_timestamp": 1700000015,
                "read_timestamp": None,
                "played_timestamp": None,
            },
        ]

        members = viewer._parse_ios_receipt_device_rows(infra_rows, _MockConn(), is_group=False)
        assert len(members) == 1
        entry = members[0]
        assert entry["delivered_ts"] == 1700000010 * 1000
        assert entry["read_ts"] == 1700000050 * 1000
        assert entry["played_ts"] == 1700000080 * 1000
        assert entry["read_known"] is True

    def test_group_receipt_device_resolves_names(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE _ios_contacts (jid TEXT, full_name TEXT)")
        conn.execute("INSERT INTO _ios_contacts VALUES ('15550002222@s.whatsapp.net', 'Alice')")
        conn.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")
        conn.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES ('15550003333@s.whatsapp.net', 'Bob')")

        infra_rows = [
            {
                "user_jid": "15550002222@s.whatsapp.net",
                "device_id": 0,
                "delivered_timestamp": 1700000010,
                "read_timestamp": 1700000050,
                "played_timestamp": None,
            },
            {
                "user_jid": "15550003333@s.whatsapp.net",
                "device_id": 0,
                "delivered_timestamp": 1700000012,
                "read_timestamp": None,
                "played_timestamp": None,
            },
        ]

        members = viewer._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
        assert len(members) == 2
        m_map = {m["jid"]: m for m in members}
        assert m_map["15550002222"]["name"] == "Alice"
        assert m_map["15550002222"]["read_ts"] == 1700000050 * 1000
        assert m_map["15550003333"]["name"] == "Bob"
        assert m_map["15550003333"]["read_ts"] is None


class TestIosLidDatabaseResolution:
    def test_lid_db_resolves_unknown_lid_to_phone(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        lid_path = tmp_path / "LID.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # 1. ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEINFO INTEGER, ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            INSERT INTO ZWACHATSESSION VALUES (10, 1, '99@g.us', 'Test Group');
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 1, NULL, 1000, '15550001111@s.whatsapp.net', NULL);
        """)

        # Reaction from @lid user not in ContactsV2
        lid_val = "999888777666555"
        blob = _make_ios_reaction_blob(
            [
                {"token": "3AB0BA8A4CA7122CA06A", "lid": lid_val, "emoji": "🎉".encode()},
            ]
        )
        conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (100, 2, ?)", (blob,))
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (2, 10, 0, 100, 2000, '15550002222@s.whatsapp.net', NULL)"
        )
        conn.commit()
        conn.close()

        # 2. LID.sqlite mapping lid_val to phone number
        lid_conn = sqlite3.connect(str(lid_path))
        lid_conn.executescript(f"""
            CREATE TABLE ZWAZACCOUNT (
                Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR
            );
            INSERT INTO ZWAZACCOUNT (Z_PK, ZIDENTIFIER, ZPHONENUMBER)
            VALUES (1, '{lid_val}@lid', '15553334444');
        """)
        lid_conn.commit()
        lid_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/reaction_details/2").get_json()

        assert data["available"] is True
        assert data["reactors"][0]["name"] == "+15553334444"


class TestIosMessageReceiptsApi:
    def test_message_receipts_from_messaging_infra_db(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        infra_path = tmp_path / "MessagingInfraDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE INTEGER, ZSTANZAID TEXT, ZMESSAGESTATUS INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);

            INSERT INTO ZWACHATSESSION VALUES (1, NULL, '15550002222@s.whatsapp.net', NULL);
            INSERT INTO ZWAMESSAGE VALUES (10, 1, 1, 1000, 'STANZA_123', 8);
        """)
        conn.commit()
        conn.close()

        # MessagingInfraDatabase
        infra_conn = sqlite3.connect(str(infra_path))
        infra_conn.executescript("""
            CREATE TABLE receipt_device (
                _id INTEGER PRIMARY KEY, stanza_id TEXT, chat_jid TEXT, user_jid TEXT,
                device_id INTEGER, send_timestamp LONG_INT, delivered_timestamp LONG_INT,
                read_timestamp LONG_INT, played_timestamp LONG_INT, device_version INTEGER
            );
            INSERT INTO receipt_device VALUES (
                1, 'STANZA_123', '15550002222@s.whatsapp.net', '15550002222@s.whatsapp.net',
                0, 1700000000, 1700000005, 1700000045, 1700000060, 1
            );
        """)
        infra_conn.commit()
        infra_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/10")
            data = resp.get_json()

        assert data["available"] is True
        assert data["delivered_ts"] == 1700000005 * 1000
        assert data["read_ts"] == 1700000045 * 1000
        assert data["played_ts"] == 1700000060 * 1000
        assert data["read_known"] is True

    def test_message_receipts_from_messaging_infra_db_in_whatsapp_databases(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        wa_path = db_dir / "ChatStorage.sqlite"
        infra_path = db_dir / "MessagingInfraDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE INTEGER, ZSTANZAID TEXT, ZMESSAGESTATUS INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);

            INSERT INTO ZWACHATSESSION VALUES (1, NULL, '15550002222@s.whatsapp.net', NULL);
            INSERT INTO ZWAMESSAGE VALUES (10, 1, 1, 1000, 'STANZA_123', 8);
        """)
        conn.commit()
        conn.close()

        # MessagingInfraDatabase
        infra_conn = sqlite3.connect(str(infra_path))
        infra_conn.executescript("""
            CREATE TABLE receipt_device (
                _id INTEGER PRIMARY KEY, stanza_id TEXT, chat_jid TEXT, user_jid TEXT,
                device_id INTEGER, send_timestamp LONG_INT, delivered_timestamp LONG_INT,
                read_timestamp LONG_INT, played_timestamp LONG_INT, device_version INTEGER
            );
            INSERT INTO receipt_device VALUES (
                1, 'STANZA_123', '15550002222@s.whatsapp.net', '15550002222@s.whatsapp.net',
                0, 1700000000, 1700000005, 1700000045, 1700000060, 1
            );
        """)
        infra_conn.commit()
        infra_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/10")
            data = resp.get_json()

        assert data["available"] is True
        assert data["delivered_ts"] == 1700000005 * 1000
        assert data["read_ts"] == 1700000045 * 1000
        assert data["played_ts"] == 1700000060 * 1000
        assert data["read_known"] is True


class TestIosHdDeduplication:
    def test_ios_hd_dedup_filters_lq_parent_when_ext_db_present(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            -- Parent message (LQ)
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/lq.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'STANZA_LQ', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            -- Child message (HD)
            INSERT INTO ZWAMEDIAITEM VALUES (21, 'Media/hd.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'STANZA_HD', 1, 21, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        # ExtChatDatabase with HD association (type 10)
        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
            INSERT INTO message_parent_association VALUES ('STANZA_HD', 'STANZA_LQ', 10, 1, 'group1@g.us');
        """)
        ext_conn.commit()
        ext_conn.close()

        # Archive DB: only the HD file was archived
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/hd.jpg', 'photos/hd.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            messages = resp.get_json()

        # Only the HD message should be returned, LQ parent filtered out
        assert len(messages) == 1
        assert messages[0]["msg_id"] == 101
        assert messages[0]["archive_path"] == "photos/hd.jpg"

    def test_ios_hd_dedup_in_subfolder(self, tmp_path):
        wa_dir = tmp_path / "Whatsapp Databases"
        wa_dir.mkdir()
        wa_path = wa_dir / "ChatStorage.sqlite"
        ext_path = wa_dir / "ExtChatDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/lq.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'STANZA_LQ', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            INSERT INTO ZWAMEDIAITEM VALUES (21, 'Media/hd.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'STANZA_HD', 1, 21, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
            INSERT INTO message_parent_association VALUES ('STANZA_HD', 'STANZA_LQ', 10, 1, 'group1@g.us');
        """)
        ext_conn.commit()
        ext_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/hd.jpg', 'photos/hd.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            messages = resp.get_json()

        assert len(messages) == 1
        assert messages[0]["msg_id"] == 101

    def test_ios_hd_dedup_keeps_both_when_ext_db_absent(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/lq.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'STANZA_LQ', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            INSERT INTO ZWAMEDIAITEM VALUES (21, 'Media/hd.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'STANZA_HD', 1, 21, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            messages = resp.get_json()

        # Both returned when ExtChatDatabase is absent
        assert len(messages) == 2

    def test_ios_fts_indexing_excludes_lq_parent(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        cache_path = tmp_path / ".wa_viewer.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZTEXT TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/lq.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 1000.0, 'STANZA_LQ', 1, 20, 'Photo 1');

            INSERT INTO ZWAMEDIAITEM VALUES (21, 'Media/hd.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 1001.0, 'STANZA_HD', 1, 21, 'Photo 1 HD');
        """)
        conn.commit()
        conn.close()

        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
            INSERT INTO message_parent_association VALUES ('STANZA_HD', 'STANZA_LQ', 10, 1, 'group1@g.us');
        """)
        ext_conn.commit()
        ext_conn.close()

        cache_conn = make_cache_db(cache_path)
        viewer._build_fts_chat(cache_conn, "ios", str(wa_path), "10", "group", None)

        indexed = cache_conn.execute("SELECT rowid FROM message_index").fetchall()
        rowids = [r["rowid"] for r in indexed]
        assert 101 in rowids
        assert 100 not in rowids
        cache_conn.close()

    def test_ios_hd_dedup_album_multiple_photos(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
        """)
        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
        """)
        archive_conn = make_archive_db(archive_path)

        for i in range(5):
            lq_pk = 100 + i
            hd_pk = 200 + i
            lq_mi = 10 + i
            hd_mi = 30 + i
            lq_stanza = f"LQ_STANZA_{i}"
            hd_stanza = f"HD_STANZA_{i}"
            lq_file = f"Media/lq_{i}.jpg"
            hd_file = f"Media/hd_{i}.jpg"

            conn.execute("INSERT INTO ZWAMEDIAITEM VALUES (?, ?, NULL)", (lq_mi, lq_file))
            conn.execute(
                "INSERT INTO ZWAMESSAGE VALUES (?, 10, 0, ?, ?, 1, ?, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net')",
                (lq_pk, 1000.0 + i, lq_stanza, lq_mi),
            )

            conn.execute("INSERT INTO ZWAMEDIAITEM VALUES (?, ?, NULL)", (hd_mi, hd_file))
            conn.execute(
                "INSERT INTO ZWAMESSAGE VALUES (?, 10, 0, ?, ?, 1, ?, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net')",
                (hd_pk, 1000.0 + i + 0.1, hd_stanza, hd_mi),
            )

            ext_conn.execute(
                "INSERT INTO message_parent_association VALUES (?, ?, 10, ?, 'group1@g.us')",
                (hd_stanza, lq_stanza, i),
            )

            archive_conn.execute(
                "INSERT INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
                (f"Message/{hd_file}", f"photos/{hd_file}"),
            )

        conn.commit()
        conn.close()
        ext_conn.commit()
        ext_conn.close()
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            messages = resp.get_json()

        # Should return exactly 5 HD messages, none of the 5 LQ preview rows
        assert len(messages) == 5
        for m in messages:
            assert m["msg_id"] >= 200
            assert m["archive_path"] is not None

    def test_ios_hd_dedup_gallery_counts_and_missing(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            -- Parent message (LQ) - NOT in archive_copies
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/lq.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'STANZA_LQ', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            -- Child message (HD) - in archive_copies
            INSERT INTO ZWAMEDIAITEM VALUES (21, 'Media/hd.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'STANZA_HD', 1, 21, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            -- Independent genuinely missing message - NOT in archive_copies
            INSERT INTO ZWAMEDIAITEM VALUES (22, 'Media/missing.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (102, 10, 0, 1002.0, 'STANZA_MISSING', 1, 22, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT, parent_stanza_id TEXT, type INTEGER, sort INTEGER, chat_jid TEXT
            );
            INSERT INTO message_parent_association VALUES ('STANZA_HD', 'STANZA_LQ', 10, 1, 'group1@g.us');
        """)
        ext_conn.commit()
        ext_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/hd.jpg', 'photos/hd.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            count_resp = client.get("/api/media/count?chat_id=10&chat_type=group")
            counts = count_resp.get_json()

            gallery_resp = client.get("/api/media?chat_id=10&chat_type=group")
            gallery = gallery_resp.get_json()

        # Deduplication check:
        # - Total is 2 (1 HD photo + 1 genuine missing photo, LQ parent excluded)
        # - Archived is 1 (the HD photo)
        # - Missing is 1 (the genuine missing photo, NOT inflated by the LQ duplicate)
        assert counts["total"] == 2
        assert counts["archived"] == 1
        assert counts["by_type"]["image"]["count"] == 2
        assert counts["by_type"]["image"]["missing"] == 1

        # Gallery grid only returns the archived HD photo
        assert len(gallery) == 1
        assert gallery[0]["msg_id"] == 101
        assert gallery[0]["archive_path"] == "photos/hd.jpg"

        # Gallery with types filter also deduplicates and only returns the HD photo
        gallery_types = client.get("/api/media?chat_id=10&chat_type=group&types=image").get_json()
        assert len(gallery_types) == 1
        assert gallery_types[0]["msg_id"] == 101


class TestGroupMembersLidResolution:
    def test_ios_group_members_lid_resolved_via_contactsv2(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        contacts_path = tmp_path / "ContactsV2.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage with active group members and creator
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103624826949001@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103624826949001@lid', 1);
            INSERT INTO ZWAGROUPMEMBER VALUES (2, 10, '15550002222@s.whatsapp.net', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Alice', 'Hello', '103624826949001@lid');
        """)
        conn.commit()
        conn.close()

        # ContactsV2 mapping LID -> phone & name
        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY, ZLID VARCHAR, ZWHATSAPPID VARCHAR, ZFULLNAME VARCHAR
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '103624826949001@lid', '15550001111@s.whatsapp.net', 'Alice Tester');
        """)
        c_conn.commit()
        c_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550001111', 'Alice', 'Alice Tester')"
        )
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550002222', 'Bob', 'Bob Standard')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert data["creator_number"] == "15550001111"
        assert data["creator_name"] == "Alice Tester"
        assert len(data["members"]) == 2

        alice_m = next(m for m in data["members"] if m["name"] == "Alice Tester")
        assert alice_m["number"] == "15550001111"

        bob_m = next(m for m in data["members"] if m["name"] == "Bob Standard")
        assert bob_m["number"] == "15550002222"

    def test_ios_group_members_lid_resolved_via_lid_db(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        lid_path = tmp_path / "LID.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103624826949002@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103624826949002@lid', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Charlie', 'Hi', '103624826949002@lid');
        """)
        conn.commit()
        conn.close()

        lid_conn = sqlite3.connect(str(lid_path))
        lid_conn.executescript("""
            CREATE TABLE ZWAZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR);
            INSERT INTO ZWAZACCOUNT VALUES (1, '103624826949002@lid', '15550003333');
        """)
        lid_conn.commit()
        lid_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550003333', 'Charlie', 'Charlie Tester')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert data["creator_number"] == "15550003333"
        assert data["creator_name"] == "Charlie Tester"
        assert len(data["members"]) == 1
        assert data["members"][0]["name"] == "Charlie Tester"
        assert data["members"][0]["number"] == "15550003333"

    def test_ios_group_members_unresolved_lid_never_leaks_numeric_lid(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103624826949003@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103624826949003@lid', 1);
            INSERT INTO ZWAGROUPMEMBER VALUES (2, 10, '103624826949004@lid', 1);
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '103624826949003@lid', 'Mystery Member');
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Push1', 'Hi', '103624826949003@lid');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        # Creator number should be None / empty because LID cannot be resolved to a phone number
        assert data["creator_number"] is None
        assert data["creator_name"] == "Mystery Member"

        # Members: neither must have numeric LID in 'number', and unmapped LID without pushname must be 'Unknown'
        members = data["members"]
        assert len(members) == 2
        m1 = next(m for m in members if m["name"] == "Mystery Member")
        assert m1["number"] == ""

        m2 = next(m for m in members if m["name"] == "Unknown")
        assert m2["number"] == ""

    def test_ios_group_members_filters_inactive_members(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '15550001111@s.whatsapp.net');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '15550001111@s.whatsapp.net', 1);
            INSERT INTO ZWAGROUPMEMBER VALUES (2, 10, '15550002222@s.whatsapp.net', 0);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'User1', 'Hi', '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550001111', 'Active User', 'Active User')"
        )
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550002222', 'Left User', 'Left User')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert len(data["members"]) == 1
        assert data["members"][0]["name"] == "Active User"
        assert data["members"][0]["number"] == "15550001111"

    def test_android_group_members_lid_resolved_and_unresolved(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id        INTEGER PRIMARY KEY,
                user       TEXT,
                server     TEXT,
                raw_string TEXT
            );
            CREATE TABLE chat (
                _id                     INTEGER PRIMARY KEY,
                jid_row_id              INTEGER,
                subject                 TEXT,
                hidden                  INTEGER DEFAULT 0,
                sort_timestamp          INTEGER,
                display_message_row_id  INTEGER,
                created_timestamp       INTEGER
            );
            CREATE TABLE message (
                _id               INTEGER PRIMARY KEY,
                chat_row_id       INTEGER NOT NULL,
                from_me           INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp         INTEGER,
                text_data         TEXT,
                message_type      INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            CREATE TABLE group_participants (
                _id   INTEGER PRIMARY KEY,
                gjid  TEXT,
                jid   TEXT,
                admin INTEGER,
                pending INTEGER
            );

            INSERT INTO jid VALUES (1, 'group1', 'g.us', 'group1@g.us');
            INSERT INTO jid VALUES (2, '103624826949005', 'lid', '103624826949005@lid');
            INSERT INTO jid VALUES (3, '15550005555', 's.whatsapp.net', '15550005555@s.whatsapp.net');
            INSERT INTO jid VALUES (4, '103624826949006', 'lid', '103624826949006@lid');

            INSERT INTO jid_map VALUES (2, 3);

            INSERT INTO chat VALUES (10, 1, 'Android Group', 0, 1700000000000, 1, 1700000000000);
            INSERT INTO message VALUES (1, 10, 0, 2, 1700000000000, NULL, 7);

            INSERT INTO group_participants VALUES (1, 'group1@g.us', '103624826949005@lid', 0, 0);
            INSERT INTO group_participants VALUES (2, 'group1@g.us', '103624826949006@lid', 0, 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550005555', 'Dave', 'Dave Android')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Android Group', 'Android Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert data["creator_number"] == "15550005555"
        assert data["creator_name"] == "Dave Android"

        members = data["members"]
        assert len(members) == 2

        mapped_m = next(m for m in members if m["name"] == "Dave Android")
        assert mapped_m["number"] == "15550005555"

        unmapped_m = next(m for m in members if m["name"] == "Unknown")
        assert unmapped_m["number"] == ""

    def test_ios_group_members_unsaved_contact_shows_friendly_push_name(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        contacts_path = tmp_path / "ContactsV2.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        # ChatStorage with member whose LID is mapped to phone, but who has no address book name
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103624826949007@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103624826949007@lid', 1);
            -- Push name stored under phone JID in ZWAPROFILEPUSHNAME
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550009999@s.whatsapp.net', 'Friendly Dave');
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, NULL, 'Hi', '103624826949007@lid');
        """)
        conn.commit()
        conn.close()

        # ContactsV2 has the LID-to-phone mapping but ZFULLNAME is NULL (unsaved contact)
        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY, ZLID VARCHAR, ZWHATSAPPID VARCHAR, ZFULLNAME VARCHAR
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '103624826949007@lid', '15550009999@s.whatsapp.net', NULL);
        """)
        c_conn.commit()
        c_conn.close()

        # Not saved in arch.contacts
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

            msgs = client.get("/api/messages?chat_id=10&chat_type=group").get_json()

        # Creator resolves friendly name from ZWAPROFILEPUSHNAME
        assert data["creator_name"] == "Friendly Dave"
        assert data["creator_number"] == "15550009999"

        # Member list resolves friendly name + real phone number
        assert len(data["members"]) == 1
        assert data["members"][0]["name"] == "Friendly Dave"
        assert data["members"][0]["number"] == "15550009999"

        # Message sender bubble shows friendly name
        assert len(msgs) == 1
        assert msgs[0]["sender"] == "Friendly Dave"

    def test_ios_group_members_friendly_name_from_archived_pushname_map(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        lid_path = tmp_path / "LID.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '15550008888@s.whatsapp.net');
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZSTANZAID TEXT, ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103624826949008@lid', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, NULL, 'Hi', '103624826949008@lid');
        """)
        conn.commit()
        conn.close()

        lid_conn = sqlite3.connect(str(lid_path))
        lid_conn.executescript("""
            CREATE TABLE ZWAZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR);
            INSERT INTO ZWAZACCOUNT VALUES (1, '103624826949008@lid', '15550008888');
        """)
        lid_conn.commit()
        lid_conn.close()

        # In arch.contacts, build_ios_pushname_map stored the push name keyed by the LID string
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('103624826949008', 'Dave Folder', 'Archived Friendly Dave')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert len(data["members"]) == 1
        assert data["members"][0]["name"] == "Archived Friendly Dave"
        assert data["members"][0]["number"] == "15550008888"

    def test_android_group_members_resolution_modern(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id        INTEGER PRIMARY KEY,
                user       TEXT,
                server     TEXT,
                raw_string TEXT
            );
            CREATE TABLE chat (
                _id                     INTEGER PRIMARY KEY,
                jid_row_id              INTEGER,
                subject                 TEXT,
                hidden                  INTEGER DEFAULT 0,
                sort_timestamp          INTEGER,
                display_message_row_id  INTEGER,
                created_timestamp       INTEGER
            );
            CREATE TABLE message (
                _id               INTEGER PRIMARY KEY,
                chat_row_id       INTEGER NOT NULL,
                from_me           INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp         INTEGER,
                text_data         TEXT,
                message_type      INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            CREATE TABLE group_participant_user (
                _id               INTEGER PRIMARY KEY,
                group_jid_row_id  INTEGER,
                user_jid_row_id   INTEGER,
                rank              INTEGER,
                pending           INTEGER
            );
            CREATE TABLE lid_display_name (
                lid_row_id   INTEGER PRIMARY KEY,
                display_name TEXT,
                username     TEXT
            );

            -- JIDs
            INSERT INTO jid VALUES (1, 'group1', 'g.us', 'group1@g.us');
            INSERT INTO jid VALUES (2, 'lid_me', 'lid', 'lid_me');
            INSERT INTO jid VALUES (3, '103624826949001', 'lid', '103624826949001@lid');
            INSERT INTO jid VALUES (4, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO jid VALUES (5, '103624826949002', 'lid', '103624826949002@lid');
            INSERT INTO jid VALUES (6, '15550002222', 's.whatsapp.net', '15550002222@s.whatsapp.net');
            INSERT INTO jid VALUES (7, '103624826949003', 'lid', '103624826949003@lid');
            INSERT INTO jid VALUES (8, '15550003333', 's.whatsapp.net', '15550003333@s.whatsapp.net');
            INSERT INTO jid VALUES (9, '103624826949004', 'lid', '103624826949004@lid');
            INSERT INTO jid VALUES (10, '103624826949005', 'lid', '103624826949005@lid');

            -- JID map (LID -> phone)
            INSERT INTO jid_map VALUES (3, 4);
            INSERT INTO jid_map VALUES (5, 6);
            INSERT INTO jid_map VALUES (7, 8);

            -- Masked phone strings in lid_display_name
            INSERT INTO lid_display_name VALUES (9, '+1555••••004', NULL);

            -- Group chat and members
            INSERT INTO chat VALUES (10, 1, 'Android Modern Group', 0, 1700000000000, 1, 1700000000000);
            -- Creator message (type 7) sent by user 5 (mapped to 15550002222)
            INSERT INTO message VALUES (1, 10, 0, 5, 1700000000000, NULL, 7);

            -- Group members in group_participant_user
            INSERT INTO group_participant_user VALUES (1, 1, 2, 1, 0); -- lid_me
            INSERT INTO group_participant_user VALUES (2, 1, 3, 0, 0); -- saved contact Alice
            INSERT INTO group_participant_user VALUES (3, 1, 5, 0, 0); -- unsaved mapped LID (15550002222)
            INSERT INTO group_participant_user VALUES (4, 1, 7, 0, 0); -- unsaved mapped LID (15550003333)
            INSERT INTO group_participant_user VALUES (5, 1, 9, 0, 0); -- unmapped LID with lid_display_name
            INSERT INTO group_participant_user VALUES (6, 1, 10, 0, 0); -- unmapped LID without lid_display_name
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550001111', 'Alice', 'Alice Saved')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Android Modern Group', 'Android Modern Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert data["creator_number"] == "15550002222"
        assert data["creator_name"] == "+15550002222"

        members = data["members"]
        assert len(members) == 6

        # lid_me resolves to 'You'
        m_me = next(m for m in members if m["name"] == "You")
        assert m_me["number"] == ""

        # Saved contact takes priority
        m_alice = next(m for m in members if m["name"] == "Alice Saved")
        assert m_alice["number"] == "15550001111"

        # Unsaved mapped contact resolves to +<number>
        m_bob = next(m for m in members if m["name"] == "+15550002222")
        assert m_bob["number"] == "15550002222"

        m_num = next(m for m in members if m["name"] == "+15550003333")
        assert m_num["number"] == "15550003333"

        # Unmapped LID with lid_display_name falls back to masked string
        m_masked = next(m for m in members if m["name"] == "+1555••••004")
        assert m_masked["number"] == ""

        # Unmapped LID without lid_display_name resolves to Unknown
        m_unmapped = next(m for m in members if m["name"] == "Unknown")
        assert m_unmapped["number"] == ""

    def test_android_message_sender_and_reactions_show_real_phone(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id        INTEGER PRIMARY KEY,
                user       TEXT,
                server     TEXT,
                raw_string TEXT
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
                _id               INTEGER PRIMARY KEY,
                chat_row_id       INTEGER NOT NULL,
                from_me           INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp         INTEGER,
                text_data         TEXT,
                message_type      INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_add_on (
                _id                    INTEGER PRIMARY KEY,
                parent_message_row_id  INTEGER,
                sender_jid_row_id      INTEGER,
                from_me                INTEGER DEFAULT 0
            );
            CREATE TABLE message_add_on_reaction (
                _id                   INTEGER PRIMARY KEY,
                message_add_on_row_id INTEGER,
                reaction              TEXT
            );
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            CREATE TABLE lid_display_name (
                lid_row_id   INTEGER PRIMARY KEY,
                display_name TEXT,
                username     TEXT
            );

            INSERT INTO jid VALUES (1, '103624826949010', 'lid', '103624826949010@lid');
            INSERT INTO jid VALUES (2, '15550004444', 's.whatsapp.net', '15550004444@s.whatsapp.net');
            INSERT INTO jid VALUES (3, 'group1', 'g.us', 'group1@g.us');

            INSERT INTO jid_map VALUES (1, 2);
            INSERT INTO lid_display_name VALUES (1, '+1555••••444', NULL);

            INSERT INTO chat VALUES (10, 3, 'Test Group', 0, 1700000000000, 1);
            -- Message sent by user 1 (LID mapped to 15550004444)
            INSERT INTO message VALUES (1, 10, 0, 1, 1700000000000, 'Hello from Charlie', 0);
            -- Reaction to message 1 by user 1
            INSERT INTO message_add_on VALUES (1, 1, 1, 0);
            INSERT INTO message_add_on_reaction VALUES (1, 1, '👍');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["sender"] == "+15550004444"

            # Reaction details
            r_resp = client.get("/api/reaction_details/1")
            r_data = r_resp.get_json()
            assert r_data["available"] is True
            assert r_data["reactors"][0]["name"] == "+15550004444"
            assert r_data["reactors"][0]["emoji"] == "👍"

    def test_android_1on1_reaction_details_shows_contact_name(self, tmp_path):
        """1-on-1 contact reactions with sender_jid_row_id = -1 resolve contact name."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id        INTEGER PRIMARY KEY,
                user       TEXT,
                server     TEXT,
                raw_string TEXT
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
                _id               INTEGER PRIMARY KEY,
                chat_row_id       INTEGER NOT NULL,
                from_me           INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp         INTEGER,
                text_data         TEXT,
                message_type      INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_add_on (
                _id                    INTEGER PRIMARY KEY,
                chat_row_id            INTEGER,
                parent_message_row_id  INTEGER,
                sender_jid_row_id      INTEGER,
                from_me                INTEGER DEFAULT 0
            );
            CREATE TABLE message_add_on_reaction (
                _id                   INTEGER PRIMARY KEY,
                message_add_on_row_id INTEGER,
                reaction              TEXT
            );

            INSERT INTO jid VALUES (1, '15550007777', 's.whatsapp.net', '15550007777@s.whatsapp.net');
            INSERT INTO chat VALUES (11, 1, NULL, 0, 1700000000000, 2);
            -- Message 2: outgoing message in 1-on-1 chat
            INSERT INTO message VALUES (2, 11, 1, 0, 1700000000000, 'Hey Diana', 0);
            -- Message 3: message without reactions to test negative caching
            INSERT INTO message VALUES (3, 11, 0, 1, 1700000001000, 'No reactions here', 0);
            -- Incoming reaction from contact in 1-on-1 chat
            INSERT INTO message_add_on VALUES (20, 11, 2, -1, 0);
            INSERT INTO message_add_on_reaction VALUES (1, 20, '❤️');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550007777', 'Diana', 'Diana Contact')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Test reaction details popup resolves 1-on-1 contact display name
            r_resp = client.get("/api/reaction_details/2")
            r_data = r_resp.get_json()
            assert r_data["available"] is True
            assert r_data["total"] == 1
            assert r_data["reactors"][0]["name"] == "Diana Contact"
            assert r_data["reactors"][0]["emoji"] == "❤️"
            assert r_data["reactors"][0]["from_me"] == 0

            # 2. Test negative caching in /api/messages for message 3 (0 reactions)
            m_resp = client.get("/api/messages?chat_id=11&chat_type=contact")
            msgs = m_resp.get_json()
            assert len(msgs) == 2

        # Verify negative caching in reactions_cache for message 3
        arc_conn = sqlite3.connect(str(archive_path))
        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE msg_id = 3"
        ).fetchone()
        assert cached is not None
        assert cached[0] == ""
        arc_conn.close()


class TestServiceMessages:
    """Tests for WhatsApp service messages and group events (iOS & Android)."""

    def test_ios_service_events_formatting_and_preview(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        contacts_path = tmp_path / "ContactsV2.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT, ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZMESSAGETYPE INTEGER, ZGROUPEVENTTYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'iOS Test Group', 0);

            -- 1. Group created with JSON payload containing subject and LID author
            INSERT INTO ZWAMESSAGE VALUES (
                1, 10, 0, 700000000.0, 6, 12, NULL, NULL, NULL, NULL,
                '{"subject": "Alpha Project", "author": "103624826949010@lid"}',
                '103624826949010@lid'
            );
            -- 2. Participant added (by me)
            INSERT INTO ZWAMESSAGE VALUES (
                2, 10, 1, 700000010.0, 6, 2, NULL, NULL, NULL, NULL,
                '15550002222@s.whatsapp.net', NULL
            );
            -- 3. Subject changed (by me)
            INSERT INTO ZWAMESSAGE VALUES (
                3, 10, 1, 700000020.0, 6, 1, NULL, NULL, NULL, NULL,
                'Beta Project', NULL
            );
            -- 4. Group icon changed (by Bob)
            INSERT INTO ZWAMESSAGE VALUES (
                4, 10, 0, 700000030.0, 6, 3, NULL, NULL, NULL, NULL,
                NULL, '15550002222@s.whatsapp.net'
            );
            -- 5. Disappearing messages set
            INSERT INTO ZWAMESSAGE VALUES (
                5, 10, 0, 700000040.0, 6, 26, NULL, NULL, NULL, NULL,
                '7776000', '15550002222@s.whatsapp.net'
            );
            -- 6. Disappearing messages turned off
            INSERT INTO ZWAMESSAGE VALUES (
                6, 10, 0, 700000050.0, 6, 26, NULL, NULL, NULL, NULL,
                '0', '15550002222@s.whatsapp.net'
            );
            -- 7. Admin promoted
            INSERT INTO ZWAMESSAGE VALUES (
                7, 10, 0, 700000060.0, 6, 9, NULL, NULL, NULL, NULL,
                '15550002222@s.whatsapp.net', NULL
            );
            -- 8. Admin demoted
            INSERT INTO ZWAMESSAGE VALUES (
                8, 10, 0, 700000070.0, 6, 5, NULL, NULL, NULL, NULL,
                '15550002222@s.whatsapp.net', NULL
            );
            -- 9. Participant removed (by me)
            INSERT INTO ZWAMESSAGE VALUES (
                9, 10, 1, 700000080.0, 6, 7, NULL, NULL, NULL, NULL,
                '103624826949010@lid', NULL
            );
            -- 10. Regular chat text message (PK 30)
            INSERT INTO ZWAMESSAGE VALUES (
                30, 10, 0, 700000090.0, 0, 0, NULL, NULL, NULL, NULL,
                'Hello team', '15550002222@s.whatsapp.net'
            );
            -- 20. Participant added by WhatsApp official system sender (0@s.whatsapp.net, PK 20)
            INSERT INTO ZWAMESSAGE VALUES (
                20, 10, 0, 700000085.0, 6, 2, NULL, NULL, NULL, NULL,
                '15550002222@s.whatsapp.net', '0@s.whatsapp.net'
            );
        """)
        conn.commit()
        conn.close()

        # Setup ContactsV2 with LID mapping for user 1 (Alice)
        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY, ZLID VARCHAR, ZWHATSAPPID VARCHAR,
                ZFULLNAME VARCHAR, ZPHONENUMBER VARCHAR
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (
                1, '103624826949010@lid', '15550001111@s.whatsapp.net',
                'Alice Smith', '15550001111'
            );
        """)
        c_conn.commit()
        c_conn.close()

        # Setup archive db with contact for Bob Jones
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550002222', 'Bob', 'Bob Jones')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'iOS Test Group', 'iOS Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            # 1. Fetch messages
            resp = client.get("/api/messages?chat_id=10&chat_type=group&limit=50")
            msgs = resp.get_json()
            assert len(msgs) == 11

            by_id = {m["msg_id"]: m for m in msgs}

            # Msg 1: Group created
            assert by_id[1]["media_type"] == "service"
            assert by_id[1]["text_body"] == 'Alice Smith created group "Alpha Project"'
            assert "@lid" not in by_id[1]["text_body"]

            # Msg 2: Participant added
            assert by_id[2]["media_type"] == "service"
            assert by_id[2]["text_body"] == "You added Bob Jones"

            # Msg 3: Subject changed
            assert by_id[3]["media_type"] == "service"
            assert by_id[3]["text_body"] == 'You changed the subject to "Beta Project"'

            # Msg 4: Icon changed
            assert by_id[4]["media_type"] == "service"
            assert by_id[4]["text_body"] == "Bob Jones changed this group's icon"

            # Msg 5: Disappearing messages on
            assert by_id[5]["media_type"] == "service"
            assert by_id[5]["text_body"] == "Disappearing messages set to 90 days"

            # Msg 6: Disappearing messages off
            assert by_id[6]["media_type"] == "service"
            assert by_id[6]["text_body"] == "Disappearing messages turned off"

            # Msg 7: Admin promoted
            assert by_id[7]["media_type"] == "service"
            assert by_id[7]["text_body"] == "Bob Jones is now an admin"

            # Msg 8: Admin demoted
            assert by_id[8]["media_type"] == "service"
            assert by_id[8]["text_body"] == "Bob Jones is no longer an admin"

            # Msg 9: Participant removed
            assert by_id[9]["media_type"] == "service"
            assert by_id[9]["text_body"] == "You removed Alice Smith"
            assert "@lid" not in by_id[9]["text_body"]

            # Msg 20: WhatsApp system sender
            assert by_id[20]["media_type"] == "service"
            assert by_id[20]["text_body"] == "WhatsApp added Bob Jones"

            # Msg 30: Regular message
            assert by_id[30]["media_type"] == "text"
            assert by_id[30]["text_body"] == "Hello team"

            # 2. Chat preview in /api/chats
            c_resp = client.get("/api/chats")
            chats = c_resp.get_json()
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "text"
            assert chats[0]["last_msg_preview"] == "Hello team"

            # Add a newer service message and verify /api/chats reflects service preview
            conn = sqlite3.connect(str(wa_path))
            conn.execute("""
                INSERT INTO ZWAMESSAGE VALUES (
                    40, 10, 1, 700000100.0, 6, 1, NULL, NULL, NULL, NULL,
                    'Gamma Project', NULL
                )
            """)
            conn.commit()
            conn.close()

            c_resp2 = client.get("/api/chats")
            chats2 = c_resp2.get_json()
            assert chats2[0]["last_msg_type"] == "service"
            assert chats2[0]["last_msg_preview"] == 'You changed the subject to "Gamma Project"'

            # 3. /api/chat-info excludes service messages from total/sent/received
            info_resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            info = info_resp.get_json()
            assert info["total"] == 1
            assert info["sent"] == 0
            assert info["received"] == 1

    def test_ios_service_events_actor_target_and_ordering(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        contacts_path = tmp_path / "ContactsV2.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT, ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMEMBERJID TEXT, ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER,
                ZMESSAGEDATE REAL, ZMESSAGETYPE INTEGER, ZGROUPEVENTTYPE INTEGER,
                ZMEDIAITEM INTEGER, ZGROUPMEMBER INTEGER, ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT, ZTEXT TEXT, ZFROMJID TEXT, ZTOJID TEXT, ZSORT INTEGER
            );

            INSERT INTO ZWACHATSESSION VALUES (20, 1, 'awesomegroup@g.us', 'Awesome Group', 0);
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 20, '15550002222@s.whatsapp.net', 1);
            INSERT INTO ZWAGROUPMEMBER VALUES (2, 20, '15550003333@s.whatsapp.net', 1);

            -- 1. Group created (ZSORT = 2, PK = 101, TS = 700000000.0)
            INSERT INTO ZWAMESSAGE VALUES (
                101, 20, 0, 700000000.0, 6, 12, NULL, NULL, NULL, NULL,
                '{"subject": "Awesome Group", "author": "15550001111@s.whatsapp.net"}',
                'awesomegroup@g.us', '15550009999@s.whatsapp.net', 2
            );
            -- 2. Participant added: Alice added Bob (ZSORT = 3, PK = 100, TS = 700000000.0)
            -- Note: PK is lower than group created to test sort ordering by ZSORT
            INSERT INTO ZWAMESSAGE VALUES (
                100, 20, 0, 700000000.0, 6, 2, NULL, 1, NULL, NULL,
                '15550001111@s.whatsapp.net', 'awesomegroup@g.us', '15550009999@s.whatsapp.net', 3
            );
            -- 3. Participant added: Alice added you (member_jid is NULL -> target is you)
            INSERT INTO ZWAMESSAGE VALUES (
                102, 20, 0, 700000010.0, 6, 2, NULL, NULL, NULL, NULL,
                '15550001111@s.whatsapp.net', 'awesomegroup@g.us', '15550009999@s.whatsapp.net', 4
            );
            -- 4. Participant removed: Alice removed you (member_jid is NULL -> target is you)
            INSERT INTO ZWAMESSAGE VALUES (
                103, 20, 0, 700000020.0, 6, 7, NULL, NULL, NULL, NULL,
                '15550001111@s.whatsapp.net', 'awesomegroup@g.us', '15550009999@s.whatsapp.net', 5
            );
            -- 5. Participant left: Bob left
            INSERT INTO ZWAMESSAGE VALUES (
                104, 20, 0, 700000030.0, 6, 4, NULL, 1, NULL, NULL,
                NULL, 'awesomegroup@g.us', '15550009999@s.whatsapp.net', 6
            );
            -- 6. Joined via invite link: Bob joined
            INSERT INTO ZWAMESSAGE VALUES (
                105, 20, 0, 700000040.0, 6, 15, NULL, 1, NULL, NULL,
                NULL, 'awesomegroup@g.us', '15550009999@s.whatsapp.net', 7
            );
            -- 7. Multiple participants added: Alice added Bob and Charlie
            INSERT INTO ZWAMESSAGE VALUES (
                106, 20, 0, 700000050.0, 6, 50, NULL, NULL, NULL, NULL,
                '15550001111@s.whatsapp.net; 15550002222@s.whatsapp.net, 15550003333@s.whatsapp.net',
                'awesomegroup@g.us', '15550009999@s.whatsapp.net', 8
            );
        """)
        conn.commit()
        conn.close()

        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY, ZLID VARCHAR, ZWHATSAPPID VARCHAR,
                ZFULLNAME VARCHAR, ZPHONENUMBER VARCHAR
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (
                1, '103624826949010@lid', '15550001111@s.whatsapp.net',
                'Alice Smith', '15550001111'
            );
        """)
        c_conn.commit()
        c_conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550002222', 'Bob', 'Bob Jones')"
        )
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550003333', 'Charlie', 'Charlie Brown')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('20', 'Awesome Group', 'Awesome Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=20&chat_type=group&limit=50")
            msgs = resp.get_json()
            assert len(msgs) == 7

            by_id = {m["msg_id"]: m for m in msgs}

            # 1. Group created
            assert by_id[101]["media_type"] == "service"
            assert by_id[101]["text_body"] == 'Alice Smith created group "Awesome Group"'

            # 2. Alice added Bob Jones (actor in ZTEXT, target in member_jid)
            assert by_id[100]["media_type"] == "service"
            assert by_id[100]["text_body"] == "Alice Smith added Bob Jones"
            assert "Awesome Group" not in by_id[100]["sender"]

            # 3. Alice added you (target is you when member_jid is NULL)
            assert by_id[102]["media_type"] == "service"
            assert by_id[102]["text_body"] == "Alice Smith added you"

            # 4. Alice removed you
            assert by_id[103]["media_type"] == "service"
            assert by_id[103]["text_body"] == "Alice Smith removed you"

            # 5. Bob Jones left (event 4)
            assert by_id[104]["media_type"] == "service"
            assert by_id[104]["text_body"] == "Bob Jones left"

            # 6. Joined via invite link (event 15)
            assert by_id[105]["media_type"] == "service"
            assert by_id[105]["text_body"] == "Bob Jones joined using this group's invite link"

            # 7. Multiple participants added (event 50)
            assert by_id[106]["media_type"] == "service"
            assert by_id[106]["text_body"] == "Alice Smith added Bob Jones and Charlie Brown"

            # Verify ordering at same timestamp:
            # In descending order (newest first): msgs returned by /api/messages
            # For same timestamp (700000000.0), sort_id 3 (Bob added) comes before sort_id 2 (Group created)
            # When reversed (chronological order), Group Created (sort_id 2) comes BEFORE Participant Added (sort_id 3)
            same_ts_msgs = [
                m
                for m in reversed(msgs)
                if m["timestamp_ms"] == int((700000000.0 + 978307200) * 1000)
            ]
            assert len(same_ts_msgs) == 2
            assert same_ts_msgs[0]["msg_id"] == 101  # Group created (ZSORT = 2)
            assert same_ts_msgs[1]["msg_id"] == 100  # Bob added (ZSORT = 3, despite PK 100 < 101)

    def test_android_service_events_formatting_and_preview(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id        INTEGER PRIMARY KEY,
                user       TEXT,
                server     TEXT,
                raw_string TEXT
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
                _id               INTEGER PRIMARY KEY,
                chat_row_id       INTEGER NOT NULL,
                from_me           INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp         INTEGER,
                text_data         TEXT,
                message_type      INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT
            );
            CREATE TABLE message_quoted (
                _id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT,
                from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER
            );
            CREATE TABLE message_system (
                _id            INTEGER PRIMARY KEY,
                message_row_id INTEGER NOT NULL,
                action_type    INTEGER NOT NULL
            );
            CREATE TABLE message_system_chat_participant (
                _id              INTEGER PRIMARY KEY,
                message_row_id   INTEGER NOT NULL,
                user_jid_row_id  INTEGER NOT NULL
            );
            CREATE TABLE jid_map (
                lid_row_id INTEGER,
                jid_row_id INTEGER
            );
            CREATE TABLE lid_display_name (
                lid_row_id   INTEGER PRIMARY KEY,
                display_name TEXT,
                username     TEXT
            );

            INSERT INTO jid VALUES (1, '103624826949010', 'lid', '103624826949010@lid');
            INSERT INTO jid VALUES (2, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO jid VALUES (3, '15550002222', 's.whatsapp.net', '15550002222@s.whatsapp.net');
            INSERT INTO jid VALUES (4, 'group1', 'g.us', 'group1@g.us');
            INSERT INTO jid VALUES (5, '103624826949099', 'lid', '103624826949099@lid');
            INSERT INTO jid VALUES (6, 'me', 'lid', 'lid_me');

            INSERT INTO jid_map VALUES (1, 2);

            INSERT INTO chat VALUES (10, 4, 'Android Group', 0, 1700000000000, 3);

            -- 1. Action 11: Group created
            INSERT INTO message VALUES (1, 10, 0, 1, 1700000000000, 'Android Group', 7);
            INSERT INTO message_system VALUES (1, 1, 11);

            -- 2. Action 12: Participant added (by me)
            INSERT INTO message VALUES (2, 10, 1, 0, 1700000001000, NULL, 7);
            INSERT INTO message_system VALUES (2, 2, 12);
            INSERT INTO message_system_chat_participant VALUES (1, 2, 3);

            -- 3. Action 1: Subject changed (by me)
            INSERT INTO message VALUES (3, 10, 1, 0, 1700000002000, 'Renamed Android Group', 7);
            INSERT INTO message_system VALUES (3, 3, 1);

            -- 4. Action 6: Group icon changed (by Bob)
            INSERT INTO message VALUES (4, 10, 0, 3, 1700000003000, NULL, 7);
            INSERT INTO message_system VALUES (4, 4, 6);

            -- 5. Action 27: Group description changed (by me)
            INSERT INTO message VALUES (5, 10, 1, 0, 1700000004000, NULL, 7);
            INSERT INTO message_system VALUES (5, 5, 27);

            -- 6. Action 15: Admin promoted
            INSERT INTO message VALUES (6, 10, 0, 1, 1700000005000, NULL, 7);
            INSERT INTO message_system VALUES (6, 6, 15);
            INSERT INTO message_system_chat_participant VALUES (2, 6, 3);

            -- 7. Action 20: Admin demoted
            INSERT INTO message VALUES (7, 10, 0, 1, 1700000006000, NULL, 7);
            INSERT INTO message_system VALUES (7, 7, 20);
            INSERT INTO message_system_chat_participant VALUES (3, 7, 3);

            -- 8. Action 79: Joined via invite link
            INSERT INTO message VALUES (8, 10, 0, 0, 1700000007000, NULL, 7);
            INSERT INTO message_system VALUES (8, 8, 79);
            INSERT INTO message_system_chat_participant VALUES (4, 8, 3);

            -- 9. Action 14: Participant removed (by me)
            INSERT INTO message VALUES (9, 10, 1, 0, 1700000008000, NULL, 7);
            INSERT INTO message_system VALUES (9, 9, 14);
            INSERT INTO message_system_chat_participant VALUES (5, 9, 3);

            -- 10. Action 13: Participant left
            INSERT INTO message VALUES (10, 10, 0, 0, 1700000009000, NULL, 7);
            INSERT INTO message_system VALUES (10, 10, 13);
            INSERT INTO message_system_chat_participant VALUES (6, 10, 3);

            -- 11. Action 58: Announcement mode on
            INSERT INTO message VALUES (11, 10, 0, 1, 1700000010000, 'true', 7);
            INSERT INTO message_system VALUES (11, 11, 58);

            -- 12. Action 58: Announcement mode off
            INSERT INTO message VALUES (12, 10, 0, 1, 1700000011000, 'false', 7);
            INSERT INTO message_system VALUES (12, 12, 58);

            -- 13. Action 12: Unmapped LID participant added (must not leak raw LID)
            INSERT INTO message VALUES (13, 10, 0, 1, 1700000012000, NULL, 7);
            INSERT INTO message_system VALUES (13, 13, 12);
            INSERT INTO message_system_chat_participant VALUES (7, 13, 5);

            -- 14. Action 67: Noisy encryption notice (must be filtered out)
            INSERT INTO message VALUES (14, 10, 0, 0, 1700000013000, NULL, 7);
            INSERT INTO message_system VALUES (14, 14, 67);

            -- 15. Action 18: Noisy device change (must be filtered out)
            INSERT INTO message VALUES (15, 10, 0, 0, 1700000014000, NULL, 7);
            INSERT INTO message_system VALUES (15, 15, 18);

            -- 16. Regular chat message
            INSERT INTO message VALUES (16, 10, 0, 3, 1700000015000, 'All systems go', 0);

            -- 17. Action 12: Alice added You (lid_me participant)
            INSERT INTO message VALUES (17, 10, 0, 1, 1700000016000, NULL, 7);
            INSERT INTO message_system VALUES (17, 17, 12);
            INSERT INTO message_system_chat_participant VALUES (8, 17, 6);

            -- 18. Action 13: Bob left (no participant row, sender fallback)
            INSERT INTO message VALUES (18, 10, 0, 3, 1700000017000, NULL, 7);
            INSERT INTO message_system VALUES (18, 18, 13);

            -- 19. Action 6: Group icon changed by Bob with from_me = 1 (Android DB quirk)
            INSERT INTO message VALUES (19, 10, 1, 3, 1700000018000, NULL, 7);
            INSERT INTO message_system VALUES (19, 19, 6);

            -- 20. Action 14: Bob removed You with from_me = 1
            INSERT INTO message VALUES (20, 10, 1, 3, 1700000019000, NULL, 7);
            INSERT INTO message_system VALUES (20, 20, 14);
            INSERT INTO message_system_chat_participant VALUES (9, 20, 6);

            -- 21. Action 4: Bob joined on his own (from_me = 1, sender = Bob)
            INSERT INTO message VALUES (21, 10, 1, 3, 1700000020000, NULL, 7);
            INSERT INTO message_system VALUES (21, 21, 4);

            -- 22. Action 5: You left (from_me = 1, sender = 0)
            INSERT INTO message VALUES (22, 10, 1, 0, 1700000021000, NULL, 7);
            INSERT INTO message_system VALUES (22, 22, 5);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550001111', 'Alice', 'Alice Smith')"
        )
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550002222', 'Bob', 'Bob Jones')"
        )
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Android Group', 'Android Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group&limit=50")
            msgs = resp.get_json()

            # Actions 67 and 18 are E2E encryption notices and are displayed
            msg_ids = [m["msg_id"] for m in msgs]
            assert 14 in msg_ids
            assert 15 in msg_ids
            assert len(msgs) == 22  # 21 service messages + 1 text message

            by_id = {m["msg_id"]: m for m in msgs}

            # 1. Group created
            assert by_id[1]["media_type"] == "service"
            assert by_id[1]["text_body"] == 'Alice Smith created group "Android Group"'

            # 2. Participant added
            assert by_id[2]["media_type"] == "service"
            assert by_id[2]["text_body"] == "You added Bob Jones"

            # 3. Subject changed
            assert by_id[3]["media_type"] == "service"
            assert by_id[3]["text_body"] == 'You changed the subject to "Renamed Android Group"'

            # 4. Icon changed
            assert by_id[4]["media_type"] == "service"
            assert by_id[4]["text_body"] == "Bob Jones changed this group's icon"

            # 5. Description changed
            assert by_id[5]["media_type"] == "service"
            assert by_id[5]["text_body"] == "You changed the group description"

            # 6. Admin promoted
            assert by_id[6]["media_type"] == "service"
            assert by_id[6]["text_body"] == "Bob Jones is now an admin"

            # 7. Admin demoted
            assert by_id[7]["media_type"] == "service"
            assert by_id[7]["text_body"] == "Bob Jones is no longer an admin"

            # 8. Joined via invite link
            assert by_id[8]["media_type"] == "service"
            assert by_id[8]["text_body"] == "Bob Jones joined using this group's invite link"

            # 9. Participant removed
            assert by_id[9]["media_type"] == "service"
            assert by_id[9]["text_body"] == "You removed Bob Jones"

            # 10. Participant left
            assert by_id[10]["media_type"] == "service"
            assert by_id[10]["text_body"] == "Bob Jones left"

            # 11. Announcement mode on
            assert by_id[11]["media_type"] == "service"
            assert by_id[11]["text_body"] == "Only admins can send messages in this group"

            # 12. Announcement mode off
            assert by_id[12]["media_type"] == "service"
            assert by_id[12]["text_body"] == "All participants can send messages in this group"

            # 13. Unmapped LID participant
            assert by_id[13]["media_type"] == "service"
            assert by_id[13]["text_body"] == "Alice Smith added Unknown"
            assert "103624826949099" not in by_id[13]["text_body"]
            assert "@lid" not in by_id[13]["text_body"]

            # 14. E2E encryption notice (action 67)
            assert by_id[14]["media_type"] == "service"
            assert "end-to-end encrypted" in by_id[14]["text_body"]

            # 15. E2E encryption notice (action 18)
            assert by_id[15]["media_type"] == "service"
            assert "end-to-end encrypted" in by_id[15]["text_body"]

            # 16. Regular message
            assert by_id[16]["media_type"] == "text"
            assert by_id[16]["text_body"] == "All systems go"

            # 17. Alice added You
            assert by_id[17]["media_type"] == "service"
            assert by_id[17]["text_body"] == "Alice Smith added You"

            # 18. Bob left (sender fallback)
            assert by_id[18]["media_type"] == "service"
            assert by_id[18]["text_body"] == "Bob Jones left"

            # 19. Icon changed by Bob with from_me = 1 (Android DB quirk)
            assert by_id[19]["media_type"] == "service"
            assert by_id[19]["text_body"] == "Bob Jones changed this group's icon"

            # 20. Bob removed You with from_me = 1
            assert by_id[20]["media_type"] == "service"
            assert by_id[20]["text_body"] == "Bob Jones removed you"

            # 21. Bob joined on his own (from_me = 1, sender = Bob)
            assert by_id[21]["media_type"] == "service"
            assert by_id[21]["text_body"] == "Bob Jones joined"

            # 22. You left (from_me = 1, sender = 0)
            assert by_id[22]["media_type"] == "service"
            assert by_id[22]["text_body"] == "You left"

            # /api/chats preview
            c_resp = client.get("/api/chats")
            chats = c_resp.get_json()
            assert len(chats) == 1
            # display_message_row_id in chat was 3 (subject changed)
            assert chats[0]["last_msg_type"] == "service"
            assert (
                chats[0]["last_msg_preview"] == 'You changed the subject to "Renamed Android Group"'
            )

            # /api/chat-info stats
            info_resp = client.get("/api/chat-info?chat_id=10&chat_type=group")
            info = info_resp.get_json()
            assert info["total"] == 1
            assert info["sent"] == 0
            assert info["received"] == 1

    def test_service_messages_at_endpoint(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_system (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL, action_type INTEGER NOT NULL);
            CREATE TABLE message_system_chat_participant (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL, user_jid_row_id INTEGER NOT NULL);

            INSERT INTO jid VALUES (1, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO jid VALUES (2, 'group1', 'g.us', 'group1@g.us');

            INSERT INTO chat VALUES (10, 2, 'Test Group', 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 10, 1, 0, 1700000001000, 'Welcome to the team', 0);
            INSERT INTO message VALUES (2, 10, 1, 0, 1700000002000, 'Renamed Team', 7);
            INSERT INTO message_system VALUES (1, 2, 1);
            INSERT INTO message VALUES (3, 10, 0, 1, 1700000003000, 'Thanks everyone', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get(
                "/api/messages/at?chat_id=10&chat_type=group&ts=1700000002000&limit=10"
            )
            msgs = resp.get_json()
            assert len(msgs) == 3
            by_id = {m["msg_id"]: m for m in msgs}
            assert by_id[2]["media_type"] == "service"
            assert by_id[2]["text_body"] == 'You changed the subject to "Renamed Team"'

    def test_android_business_event_filtered_when_no_user_messages(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_system (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL, action_type INTEGER NOT NULL);
            CREATE TABLE message_system_chat_participant (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL, user_jid_row_id INTEGER NOT NULL);

            INSERT INTO jid VALUES (1, '15551111111', 's.whatsapp.net', '15551111111@s.whatsapp.net');
            INSERT INTO jid VALUES (2, '15552222222', 's.whatsapp.net', '15552222222@s.whatsapp.net');
            INSERT INTO jid VALUES (3, '15553333333', 's.whatsapp.net', '15553333333@s.whatsapp.net');

            -- Chat 1: 1:1 contact chat with only business account verified event (action 69) and 0 user messages
            INSERT INTO chat VALUES (1, 1, NULL, 0, 1700000010000, 1);
            INSERT INTO message VALUES (1, 1, 1, 0, 1700000010000, NULL, 7);
            INSERT INTO message_system VALUES (1, 1, 69);

            -- Chat 2: 1:1 contact chat with real user message + business account verified event
            INSERT INTO chat VALUES (2, 2, NULL, 0, 1700000020000, 3);
            INSERT INTO message VALUES (2, 2, 0, 2, 1700000015000, 'Hello from business', 0);
            INSERT INTO message VALUES (3, 2, 1, 0, 1700000020000, NULL, 7);
            INSERT INTO message_system VALUES (2, 3, 69);

            -- Chat 3: 1:1 contact chat with E2E encryption notice (action 67)
            INSERT INTO chat VALUES (3, 3, NULL, 0, 1700000030000, 4);
            INSERT INTO message VALUES (4, 3, 1, 0, 1700000030000, NULL, 7);
            INSERT INTO message_system VALUES (3, 4, 67);
        """)
        conn.commit()
        conn.close()

        archive_conn = sqlite3.connect(str(archive_path))
        archive_conn.executescript("""
            CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
            CREATE TABLE archive_copies (original_path TEXT PRIMARY KEY, archive_path TEXT);
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            chat_ids = [c["id"] for c in chats]
            # Chat 1 (only business state, 0 user messages) must be filtered out
            assert "15551111111" not in chat_ids
            # Chat 2 (has real user message) must be present
            assert "15552222222" in chat_ids
            c2 = next(c for c in chats if c["id"] == "15552222222")
            assert c2["last_msg_preview"] == "Hello from business"
            assert c2["newest_ts"] == 1700000015000
            # Chat 3 (has only E2E banner and 0 user messages) must be excluded from chat list
            assert "15553333333" not in chat_ids

            # Opening Chat 3 directly displays the E2E banner message in conversation history
            m_resp = client.get("/api/messages?chat_id=15553333333&chat_type=contact")
            msgs = m_resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["media_type"] == "service"
            assert "Messages and calls are end-to-end encrypted" in msgs[0]["text_body"]

    def test_android_recents_sorting_ignores_system_messages(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_system (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL, action_type INTEGER NOT NULL);
            CREATE TABLE message_system_chat_participant (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL, user_jid_row_id INTEGER NOT NULL);

            INSERT INTO jid VALUES (1, '15551111111', 's.whatsapp.net', '15551111111@s.whatsapp.net');
            INSERT INTO jid VALUES (2, '15552222222', 's.whatsapp.net', '15552222222@s.whatsapp.net');

            -- Chat 1: Older real message (ts=1000), but has newer encryption notice (ts=3000) bumping sort_timestamp
            INSERT INTO chat VALUES (1, 1, NULL, 0, 3000, 2);
            INSERT INTO message VALUES (1, 1, 0, 1, 1000, 'Message from Chat 1', 0);
            INSERT INTO message VALUES (2, 1, 0, 0, 3000, NULL, 7);
            INSERT INTO message_system VALUES (1, 2, 67);

            -- Chat 2: Newer real message (ts=2000), sort_timestamp=2000
            INSERT INTO chat VALUES (2, 2, NULL, 0, 2000, 3);
            INSERT INTO message VALUES (3, 2, 0, 2, 2000, 'Message from Chat 2', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = sqlite3.connect(str(archive_path))
        archive_conn.executescript("""
            CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
            CREATE TABLE archive_copies (original_path TEXT PRIMARY KEY, archive_path TEXT);
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 2
            # Chat 2 (real msg ts=2000) must rank BEFORE Chat 1 (real msg ts=1000),
            # ignoring Chat 1's system event at ts=3000
            assert chats[0]["id"] == "15552222222"
            assert chats[0]["newest_ts"] == 2000
            assert chats[0]["last_msg_preview"] == "Message from Chat 2"

            assert chats[1]["id"] == "15551111111"
            assert chats[1]["newest_ts"] == 1000
            assert chats[1]["last_msg_preview"] == "Message from Chat 1"

    def test_ios_e2e_event_and_empty_chat_filtering(self, tmp_path):
        ios_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(ios_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0,
                ZGROUPINFO INTEGER,
                ZMESSAGECOUNTER INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER NOT NULL,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZGROUPEVENTTYPE INTEGER DEFAULT 0,
                ZMESSAGEDATE REAL,
                ZISFROMME INTEGER DEFAULT 0,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZTOJID TEXT,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);

            -- Chat 1: 1:1 contact chat with background/empty event (0 user messages)
            INSERT INTO ZWACHATSESSION VALUES (1, '15551111111@s.whatsapp.net', 'Contact One', 0, NULL, 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 1, 10, 0, 700000000.0, 0, NULL, NULL, '15551111111@s.whatsapp.net', NULL, NULL, NULL, NULL);

            -- Chat 2: 1:1 contact chat with user message + Event 2
            INSERT INTO ZWACHATSESSION VALUES (2, '15552222222@s.whatsapp.net', 'Contact Two', 0, NULL, 2);
            INSERT INTO ZWAMESSAGE VALUES (2, 2, 10, 2, 700000000.0, 0, NULL, NULL, '15552222222@s.whatsapp.net', NULL, NULL, NULL, NULL);
            INSERT INTO ZWAMESSAGE VALUES (3, 2, 0, 0, 700000010.0, 0, NULL, 'Hello from iPhone', '15552222222@s.whatsapp.net', NULL, NULL, NULL, NULL);

            -- Chat 3: 1:1 contact chat with only Event 2 (session/E2E init) and 0 user messages
            INSERT INTO ZWACHATSESSION VALUES (3, '15553333333@s.whatsapp.net', 'Contact Three', 0, NULL, 1);
            INSERT INTO ZWAMESSAGE VALUES (4, 3, 10, 2, 700000005.0, 0, NULL, NULL, '15553333333@s.whatsapp.net', NULL, NULL, NULL, NULL);
        """)
        conn.commit()
        conn.close()

        archive_conn = sqlite3.connect(str(archive_path))
        archive_conn.executescript("""
            CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT);
            CREATE TABLE archive_copies (original_path TEXT PRIMARY KEY, archive_path TEXT);
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            chat_ids = [c["id"] for c in chats]
            # Chat 1 (background event, 0 user messages) must be filtered out
            assert "1" not in chat_ids
            # Chat 2 (has real message) must be present
            assert "2" in chat_ids
            c2 = next(c for c in chats if c["id"] == "2")
            # Chat list preview and sorting must only reflect real user interactions, never internal E2E banners
            assert c2["last_msg_preview"] == "Hello from iPhone"
            # Chat 3 (has only E2E banner and 0 user messages) must be excluded from chat list
            assert "3" not in chat_ids

            # Opening Chat 2 returns both the user message and the formatted E2E notice
            m_resp = client.get("/api/messages?chat_id=2&chat_type=contact")
            msgs = m_resp.get_json()
            assert len(msgs) == 2
            by_id = {m["msg_id"]: m for m in msgs}
            assert by_id[3]["text_body"] == "Hello from iPhone"
            assert by_id[2]["media_type"] == "service"
            assert "Messages and calls are end-to-end encrypted" in by_id[2]["text_body"]

            # Opening Chat 3 directly displays the internal E2E banner message in conversation history
            m_resp3 = client.get("/api/messages?chat_id=3&chat_type=contact")
            msgs3 = m_resp3.get_json()
            assert len(msgs3) == 1
            assert msgs3[0]["media_type"] == "service"
            assert "Messages and calls are end-to-end encrypted" in msgs3[0]["text_body"]

    def test_fts_indexing_ignores_raw_lid_or_json(self, tmp_path):
        cache_path = tmp_path / "cache.db"
        cache_conn = sqlite3.connect(str(cache_path))
        cache_conn.executescript("""
            CREATE TABLE message_index (
                rowid        INTEGER PRIMARY KEY,
                chat_id      TEXT NOT NULL,
                chat_type    TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE message_index_fts USING fts5(
                text_body
            );
        """)
        cache_conn.row_factory = sqlite3.Row

        raw_rows = [
            {
                "rowid": 1,
                "chat_id": "1",
                "chat_type": "group",
                "timestamp_ms": 1000,
                "message_type": 6,
                "text_body": "103624826949010@lid",
                "media_file": None,
            },
            {
                "rowid": 2,
                "chat_id": "1",
                "chat_type": "group",
                "timestamp_ms": 2000,
                "message_type": 6,
                "text_body": '{"subject": "Secret", "author": "123"}',
                "media_file": None,
            },
            {
                "rowid": 3,
                "chat_id": "1",
                "chat_type": "group",
                "timestamp_ms": 3000,
                "message_type": 0,
                "text_body": "Important meeting tomorrow",
                "media_file": None,
            },
        ]

        viewer._stream_fts_rows(raw_rows, cache_conn, 2000, None)

        indexed_fts = cache_conn.execute(
            "SELECT rowid, text_body FROM message_index_fts"
        ).fetchall()
        assert len(indexed_fts) == 1
        assert indexed_fts[0]["rowid"] == 3
        assert indexed_fts[0]["text_body"] == "Important meeting tomorrow"
        cache_conn.close()

    def test_resolve_ios_jid_branches(self):
        cmap = {
            "12345@s.whatsapp.net": ("Alice", "12345"),
            "99999@s.whatsapp.net": (None, "99999"),
            "55555@lid": ("Bob", "55555"),
            "77777": ("Charlie", "77777"),
        }
        assert viewer._resolve_ios_jid("", cmap) == "Someone"
        assert viewer._resolve_ios_jid("0", cmap) == "WhatsApp"
        assert viewer._resolve_ios_jid("0@s.whatsapp.net", cmap) == "WhatsApp"
        assert viewer._resolve_ios_jid("12345@s.whatsapp.net", cmap) == "Alice"
        assert viewer._resolve_ios_jid("99999@s.whatsapp.net", cmap) == "+99999"
        assert viewer._resolve_ios_jid("55555@lid", cmap) == "Bob"
        assert viewer._resolve_ios_jid("12345", cmap) == "Alice"
        assert viewer._resolve_ios_jid("77777@s.whatsapp.net", cmap) == "Charlie"
        assert viewer._resolve_ios_jid("77777@lid", cmap) == "Charlie"
        assert viewer._resolve_ios_jid("33333@s.whatsapp.net", cmap) == "+33333"
        assert viewer._resolve_ios_jid("44444@lid", cmap) == "Unknown"
        assert viewer._resolve_ios_jid("1234567890", cmap) == "+1234567890"
        assert viewer._resolve_ios_jid("random_string", cmap) == "random_string"


class TestFaviconAndStaticAssets:
    def test_favicon_served(self, tmp_path):
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        archive_conn.close()
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp_svg = client.get("/static/favicon.svg")
            assert resp_svg.status_code == 200
            assert "image/svg+xml" in resp_svg.content_type
            assert b"<svg" in resp_svg.data

            resp_css = client.get("/static/app.css")
            assert resp_css.status_code == 200

            resp_js = client.get("/static/app.js")
            assert resp_js.status_code == 200

            resp_ico = client.get("/favicon.ico")
            assert resp_ico.status_code == 200
            assert "image/svg+xml" in resp_ico.content_type
            assert b"<svg" in resp_ico.data

            resp_index = client.get("/")
            assert resp_index.status_code == 200
            assert b'href="/static/favicon.svg"' in resp_index.data
            assert b'id="settings-app-logo"' in resp_index.data
            assert b'id="settings-body"' in resp_index.data


# ===========================================================================
# wab_viewer entrypoint tests
# ===========================================================================


class TestViewerEntrypoint:
    def test_import_main_does_not_execute(self):
        import importlib
        from unittest.mock import patch

        with patch("wab_viewer.main.main") as mock_main:
            import wab_viewer.__main__

            importlib.reload(wab_viewer.__main__)
            mock_main.assert_not_called()

    def test_run_as_directory_help(self):
        import subprocess

        res = subprocess.run(
            [sys.executable, "wab_viewer", "--help"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        assert "Browse archived WhatsApp chats" in res.stdout

    def test_run_as_module_help(self):
        import subprocess

        res = subprocess.run(
            [sys.executable, "-m", "wab_viewer", "--help"],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0

    def test_validate_output_root_checks(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            viewer.validate_output_root(tmp_path / "nonexistent")
        assert exc.value.code == 1

        dummy_file = tmp_path / "dummy.txt"
        dummy_file.write_text("hello", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            viewer.validate_output_root(dummy_file)
        assert exc.value.code == 1

        with pytest.raises(SystemExit) as exc:
            viewer.validate_output_root(tmp_path)
        assert exc.value.code == 1

    def test_refuse_to_start_without_wa_db(self, tmp_path):
        arc_path = tmp_path / ".wa_media_archiver.db"
        arc_conn = make_archive_db(arc_path)
        arc_conn.close()

        # CLI entrypoint validation fails
        with pytest.raises(SystemExit) as exc:
            viewer.validate_output_root(tmp_path)
        assert exc.value.code == 1

        # Application factory fails
        with pytest.raises(FileNotFoundError, match="WhatsApp database not found"):
            viewer.create_app(tmp_path, rescan=False)

        (tmp_path / ".wa_media_archiver.db").touch()
        with pytest.raises(SystemExit) as exc_no_wa:
            viewer.validate_output_root(tmp_path)
        assert exc_no_wa.value.code == 1

        (tmp_path / "msgstore.db").touch()
        viewer.validate_output_root(tmp_path)

    def test_resolve_browser_url(self):
        assert viewer.resolve_browser_url("0.0.0.0", 5000) == "http://127.0.0.1:5000"
        assert viewer.resolve_browser_url("::", 8080) == "http://127.0.0.1:8080"
        assert viewer.resolve_browser_url("192.168.1.50", 5000) == "http://192.168.1.50:5000"
        assert viewer.resolve_browser_url("localhost", 3000) == "http://localhost:3000"
        assert viewer.resolve_browser_url("127.0.0.1", 5000) == "http://127.0.0.1:5000"

    def test_parse_args_browser_options(self, tmp_path):
        from unittest.mock import patch

        archive_dir = str(tmp_path)
        with patch.object(sys, "argv", ["wab-viewer", archive_dir]):
            args = viewer.parse_args()
            assert args.output_root == archive_dir
            assert args.host == "127.0.0.1"
            assert args.port == 5000
            assert args.no_browser is False
            assert args.verbose is False

        with patch.object(
            sys,
            "argv",
            [
                "wab-viewer",
                archive_dir,
                "--host",
                "192.168.1.50",
                "--port",
                "8080",
                "--no-browser",
                "--verbose",
            ],
        ):
            args = viewer.parse_args()
            assert args.host == "192.168.1.50"
            assert args.port == 8080
            assert args.no_browser is True
            assert args.verbose is True

        with patch.object(sys, "argv", ["wab-viewer", archive_dir, "-v"]):
            args = viewer.parse_args()
            assert args.verbose is True

    def test_main_browser_launch(self, tmp_path):
        from unittest.mock import MagicMock, patch

        archive_dir = str(tmp_path)
        mock_app = MagicMock()

        # 1. Host 192.168.1.50 opens http://192.168.1.50:5000
        with (
            patch.object(sys, "argv", ["wab-viewer", archive_dir, "--host", "192.168.1.50"]),
            patch("wab_viewer.main.validate_output_root"),
            patch("wab_viewer.main.create_app", return_value=mock_app),
            patch("wab_viewer.main.webbrowser.open") as mock_open,
        ):
            viewer.main()
            mock_open.assert_called_once_with("http://192.168.1.50:5000")
            mock_app.run.assert_called_once_with(
                host="192.168.1.50", port=5000, debug=False, threaded=True
            )

        mock_app.reset_mock()

        # 2. Host 0.0.0.0 opens http://127.0.0.1:5000
        with (
            patch.object(sys, "argv", ["wab-viewer", archive_dir, "--host", "0.0.0.0"]),
            patch("wab_viewer.main.validate_output_root"),
            patch("wab_viewer.main.create_app", return_value=mock_app),
            patch("wab_viewer.main.webbrowser.open") as mock_open,
        ):
            viewer.main()
            mock_open.assert_called_once_with("http://127.0.0.1:5000")
            mock_app.run.assert_called_once_with(
                host="0.0.0.0", port=5000, debug=False, threaded=True
            )

        mock_app.reset_mock()

        # 3. With --no-browser, webbrowser.open is not called
        with (
            patch.object(sys, "argv", ["wab-viewer", archive_dir, "--no-browser"]),
            patch("wab_viewer.main.validate_output_root"),
            patch("wab_viewer.main.create_app", return_value=mock_app),
            patch("wab_viewer.main.webbrowser.open") as mock_open,
        ):
            viewer.main()
            mock_open.assert_not_called()
            mock_app.run.assert_called_once_with(
                host="127.0.0.1", port=5000, debug=False, threaded=True
            )

    def test_main_logging_level(self, tmp_path):
        import logging
        from unittest.mock import MagicMock, patch

        archive_dir = str(tmp_path)
        mock_app = MagicMock()

        # Default log level should be ERROR
        with (
            patch.object(sys, "argv", ["wab-viewer", archive_dir]),
            patch("wab_viewer.main.validate_output_root"),
            patch("wab_viewer.main.create_app", return_value=mock_app),
            patch("wab_viewer.main.webbrowser.open"),
        ):
            viewer.main()
            assert logging.getLogger("werkzeug").level == logging.ERROR

        # With --verbose, log level should be INFO
        with (
            patch.object(sys, "argv", ["wab-viewer", archive_dir, "--verbose"]),
            patch("wab_viewer.main.validate_output_root"),
            patch("wab_viewer.main.create_app", return_value=mock_app),
            patch("wab_viewer.main.webbrowser.open"),
        ):
            viewer.main()
            assert logging.getLogger("werkzeug").level == logging.INFO


class TestDatabaseOptimizationsAndMaintenance:
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
            idxs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")]
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
            idxs = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_message_chat_ts'"
                )
            ]
        finally:
            conn.close()
        assert len(idxs) == 1  # one index, not duplicated

    def test_cache_db_pragmas_and_covering_index(self, tmp_path):
        conn = viewer._open_cache_db(tmp_path)
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == 1
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        assert auto_vacuum == 2  # INCREMENTAL
        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_midx_chat_type_ts" in indexes
        conn.close()

    def test_run_db_maintenance(self, tmp_path):
        cache_conn = viewer._open_cache_db(tmp_path)
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        viewer._run_db_maintenance(cache_conn, arc_conn)
        cache_conn.close()
        arc_conn.close()

    def test_recent_messages_reactions_persisted(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        rows = [
            {
                "chat_id": "12345",
                "chat_type": "contact",
                "msg_id": 100,
                "timestamp_ms": 1000,
                "sender": "Alice",
                "from_me": 0,
                "archive_path": "path.jpg",
                "media_type": "image",
                "media_name": "pic.jpg",
                "text_body": "hello",
                "quoted_text": None,
                "quoted_sender": None,
                "quoted_ts": None,
                "reactions": "❤️",
            }
        ]
        viewer._backfill_recent_messages(arc_conn, rows)
        saved = arc_conn.execute(
            "SELECT reactions FROM recent_messages WHERE msg_id = 100"
        ).fetchone()[0]
        assert saved == "❤️"
        arc_conn.close()

    def test_api_index_optimize_endpoint(self, tmp_path):
        make_archive_db(tmp_path / ".wa_media_archiver.db")
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.post("/api/index/optimize")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert "size_before" in data
            assert "size_after" in data
            assert "reclaimed" in data

    def test_api_index_optimize_conflict_when_indexing(self, tmp_path):
        make_archive_db(tmp_path / ".wa_media_archiver.db")
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            with app._indexing_lock:
                app._bulk_index_state["running"] = True
            try:
                resp = client.post("/api/index/optimize")
                assert resp.status_code == 409
                assert resp.get_json()["error"] == "indexing in progress"
            finally:
                with app._indexing_lock:
                    app._bulk_index_state["running"] = False

    def test_api_index_clear_conflict_when_indexing(self, tmp_path):
        make_archive_db(tmp_path / ".wa_media_archiver.db")
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            with app._indexing_lock:
                app._bulk_index_state["running"] = True
            try:
                resp = client.post("/api/index/clear")
                assert resp.status_code == 409
                assert resp.get_json()["error"] == "indexing in progress"
            finally:
                with app._indexing_lock:
                    app._bulk_index_state["running"] = False

    def test_api_index_all_endpoint_validation(self, tmp_path):

        make_archive_db(tmp_path / ".wa_media_archiver.db")
        # Incompatible schema causes source_type to be None while DB file exists
        sqlite3.connect(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.post("/api/index/all")
            assert resp.status_code == 400
            assert "no source database" in resp.get_json()["error"]

        wa_path = tmp_path / "msgstore.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.close()

        app2 = viewer.create_app(tmp_path, rescan=False)
        with app2.test_client() as client2:
            with app2._indexing_lock:
                app2._bulk_index_state["running"] = True
            try:
                resp2 = client2.post("/api/index/all")
                assert resp2.status_code == 200
                assert resp2.get_json()["error"] == "already running"
            finally:
                with app2._indexing_lock:
                    app2._bulk_index_state["running"] = False

    def test_bg_index_all_execution_android(self, tmp_path):
        arc_path = tmp_path / ".wa_media_archiver.db"
        arc_conn = make_archive_db(arc_path)
        wa_path = tmp_path / "msgstore.db"
        wa_conn = make_android_db(wa_path)
        seed_android_db(wa_conn, arc_conn)
        wa_conn.close()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.post("/api/index/all")
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True
            for _ in range(50):
                with app._indexing_lock:
                    if not app._bulk_index_state["running"]:
                        break
                time.sleep(0.05)
            with app._indexing_lock:
                assert app._bulk_index_state["running"] is False
                assert app._bulk_index_state["total_msgs"] >= 1

    def test_bg_index_all_execution_ios(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550000001', 'Alice', 'Alice')"
        )
        arc_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        arc_conn.commit()
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (10, 1, '120363000000001@g.us', 'Test Group', 0);
            INSERT INTO ZWACHATSESSION VALUES (20, NULL, '15550000001@s.whatsapp.net', 'Alice', 0);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, NULL, NULL, 'Alice', 'Group message text', '15550000001@s.whatsapp.net');
            INSERT INTO ZWAMESSAGE VALUES (2, 20, 1, 700000002.0, 'STANZA2', 0, NULL, NULL, NULL, NULL, 'Contact message text', NULL);
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.post("/api/index/all")
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True

            for _ in range(50):
                with app._indexing_lock:
                    if not app._bulk_index_state["running"]:
                        break
                time.sleep(0.05)

            with app._indexing_lock:
                assert app._bulk_index_state["running"] is False
                assert app._bulk_index_state["total_msgs"] == 2


class TestMessageReceipts:
    def test_receipts_table_missing(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        seed_android_db(wa_conn, arc_conn)
        wa_conn.close()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/1")
            assert resp.status_code == 200
            assert resp.get_json() == {"available": False}

    def test_receipts_empty_and_populated(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        seed_android_db(wa_conn, arc_conn)
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS receipt_user (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                receipt_user_jid_row_id INTEGER,
                receipt_timestamp INTEGER,
                read_timestamp INTEGER,
                played_timestamp INTEGER
            );
        """)
        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/message_receipts/1")
            assert resp.status_code == 200
            assert resp.get_json() == {"available": True, "members": []}

            wa_conn.execute(
                "INSERT INTO jid (_id, user, raw_string) VALUES (2, '987654321', '987654321@s.whatsapp.net')"
            )
            wa_conn.execute(
                "INSERT INTO receipt_user (message_row_id, receipt_user_jid_row_id, receipt_timestamp, read_timestamp, played_timestamp) "
                "VALUES (1, 2, 1700000005000, 1700000010000, NULL)"
            )
            wa_conn.commit()

            resp = client.get("/api/message_receipts/1")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["available"] is True
            assert len(data["members"]) == 1
            member = data["members"][0]
            assert member["jid"] == "987654321@s.whatsapp.net"
            assert member["name"] == "+987654321"
            assert member["delivered_ts"] == 1700000005000
            assert member["read_ts"] == 1700000010000
            assert data["delivered_ts"] == 1700000005000
            assert data["read_ts"] == 1700000010000
        wa_conn.close()
        arc_conn.close()

    def test_ios_receipts_filtered_by_group_members(self, tmp_path):
        from unittest.mock import patch

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGESTATUS INTEGER,
                ZSTANZAID TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZRECEIPTINFO BLOB
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
        """)
        conn.execute("INSERT INTO ZWAGROUPINFO (Z_PK) VALUES (1)")
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, 'group1@g.us', 1, 'Group 1')"
        )
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZMEMBERJID, ZCONTACTNAME) VALUES (10, 'member1@s.whatsapp.net', 'Member One')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZMESSAGESTATUS, ZSTANZAID, ZGROUPMEMBER) VALUES (100, 1, 1, 1, 700000000.0, 3, 'stanza1', 10)"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (1, 100, X'0102')"
        )
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            with patch("wab_viewer.main._parse_ios_receipt_blob") as mock_parse:
                mock_parse.return_value = [
                    {"jid": "member1", "delivered_ts": 1000, "read_ts": 2000},
                    {"jid": "stranger", "delivered_ts": 1000, "read_ts": 2000},
                ]
                resp = client.get("/api/message_receipts/100")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data["available"] is True
                assert len(data["members"]) == 1
                assert data["members"][0]["jid"] == "member1"

    def test_ios_group_receipts_includes_silent_members_and_lid_bridging(self, tmp_path):
        from unittest.mock import patch

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGESTATUS INTEGER,
                ZSTANZAID TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER
            );
            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZRECEIPTINFO BLOB
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
        """)
        conn.execute("INSERT INTO ZWAGROUPINFO (Z_PK) VALUES (1)")
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, 'group1@g.us', 1, 'Group 1')"
        )
        # member1 (sender), silent_reader (never sent a message, phone JID), lid_member (LID in ZRECEIPTINFO, mapped in _ios_contacts)
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (10, 1, 'member1@s.whatsapp.net', 'Member One')"
        )
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (11, 1, 'silent_reader@s.whatsapp.net', 'Silent Reader')"
        )
        conn.execute(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME) VALUES (12, 1, '44112233@s.whatsapp.net', 'LID Member')"
        )
        # Only member1 has a row in ZWAMESSAGE; silent_reader and 44112233 have NO rows in ZWAMESSAGE
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEINFO, ZMESSAGEDATE, ZMESSAGESTATUS, ZSTANZAID, ZGROUPMEMBER) VALUES (100, 1, 1, 1, 700000000.0, 3, 'stanza1', 10)"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (1, 100, X'0102')"
        )
        conn.commit()
        conn.close()

        # Create LID.sqlite so _init_ios_contacts automatically bridges 999888777@lid <-> 44112233
        lid_conn = sqlite3.connect(str(tmp_path / "LID.sqlite"))
        lid_conn.executescript("""
            CREATE TABLE ZWAZACCOUNT (
                Z_PK INTEGER PRIMARY KEY,
                ZIDENTIFIER TEXT,
                ZPHONENUMBER TEXT
            );
            INSERT INTO ZWAZACCOUNT VALUES (1, '999888777@lid', '44112233');
        """)
        lid_conn.commit()
        lid_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            with patch("wab_viewer.main._parse_ios_receipt_blob") as mock_parse:
                mock_parse.return_value = [
                    {"jid": "member1", "delivered_ts": 1000, "read_ts": 2000},
                    {"jid": "silent_reader", "delivered_ts": 1100, "read_ts": 2100},
                    {"jid": "999888777", "delivered_ts": 1200, "read_ts": 2200},
                    {"jid": "outsider", "delivered_ts": 1300, "read_ts": 2300},
                ]
                resp = client.get("/api/message_receipts/100")
                assert resp.status_code == 200
                data = resp.get_json()
                assert data["available"] is True
                returned_jids = [m["jid"] for m in data["members"]]
                assert "member1" in returned_jids
                assert "silent_reader" in returned_jids
                assert "999888777" in returned_jids
                assert "outsider" not in returned_jids


class TestIosChatInfoAndMedia:
    def test_ios_media_endpoints_with_blob_metadata(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Message/media1.jpg', 'archived/media1.jpg')"
        )
        arc_conn.commit()
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZHIDDEN INTEGER DEFAULT 0,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMETADATA BLOB,
                ZTITLE TEXT,
                ZMEDIAURL TEXT,
                ZFILESIZE INTEGER,
                ZMOVIEDURATION INTEGER,
                ZASPECTRATIO REAL
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMESSAGETYPE INTEGER DEFAULT 1,
                ZMESSAGEDATE REAL,
                ZISFROMME INTEGER DEFAULT 1,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            );
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, 'contact1@s.whatsapp.net', 0, NULL, 'Contact')"
        )
        # mi.ZMETADATA is a raw binary BLOB
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 'media1.jpg', X'08011204deadbeef', 'Photo', NULL, 1024, 0, 1.0)"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGETYPE, ZMESSAGEDATE, ZISFROMME, ZMEDIAITEM) VALUES (1, 1, 1, 700000000.0, 1, 1)"
        )
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp_media = client.get("/api/media?chat_id=1&chat_type=contact")
            assert resp_media.status_code == 200
            items = resp_media.get_json()
            assert len(items) == 1
            assert "_raw_metadata" not in items[0]
            assert items[0]["msg_id"] == 1

            resp_docs = client.get("/api/media/documents?chat_id=1&chat_type=contact")
            assert resp_docs.status_code == 200

    def test_ios_contact_summary_endpoint(self, tmp_path):
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44123456', 'Alice', 'Alice')"
        )
        arc_conn.commit()
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZFROMJID TEXT,
                ZSTANZAID TEXT,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Partner')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) VALUES (1, 1, 1, 700000000.0, 0, 'Hi')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) VALUES (2, 1, 0, 700000010.0, 0, 'Hey')"
        )
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["display_name"] == "Alice"
            assert data["number"] == "44123456"
            assert data["total"] == 2
            assert data["sent"] == 1
            assert data["received"] == 1


class TestProtobufParserEdgeCases:
    def test_read_varint(self):
        val, pos = viewer._read_varint(b"\x96\x01", 0)
        assert val == 150
        assert pos == 2

    def test_extract_ios_group_description_invalid(self):
        assert viewer._extract_ios_group_description("") is None
        assert viewer._extract_ios_group_description("not-valid-base64!!!") is None

    def test_extract_ios_group_description_wire_types_and_subfields(self):
        import base64

        sub = bytes([32, 5, 41]) + b"12345678" + bytes([53]) + b"1234" + bytes([26, 4]) + b"Desc"
        top = bytes([16, 2, 25]) + b"abcdefgh" + bytes([37]) + b"abcd" + bytes([10, len(sub)]) + sub
        b64 = "+" + base64.b64encode(top).decode("ascii")
        desc = viewer._extract_ios_group_description(b64)
        assert desc == "Desc"


class TestProtobufWireTypesInReceiptBlob:
    def test_parse_ios_receipt_blob_wire1_wire5_fallback(self):
        from tests.test_wa_chat_viewer import _encode_varint, _lid_field1_bytes, _NoOpConn

        base_ts = 1700000000
        lid_bytes = _lid_field1_bytes("1555000123456")

        # Member entry with wire 1 (fixed64) and wire 5 (fixed32)
        entry = _encode_varint((1 << 3) | 2) + _encode_varint(len(lid_bytes)) + lid_bytes
        # Add wire 1 (tag: field 7, wire 1) + 8 dummy bytes
        entry += _encode_varint((7 << 3) | 1) + b"\x01\x02\x03\x04\x05\x06\x07\x08"
        # Add wire 5 (tag: field 8, wire 5) + 4 dummy bytes
        entry += _encode_varint((8 << 3) | 5) + b"\x01\x02\x03\x04"

        # Event delta (field 9: fallback delta) containing wire 1, 2, 5
        event_inner = _encode_varint((1 << 3) | 0) + _encode_varint(45)  # delta = 45s
        event_inner += (
            _encode_varint((2 << 3) | 2) + _encode_varint(3) + b"foo"
        )  # wire 2 inside event
        event_inner += (
            _encode_varint((3 << 3) | 1) + b"\x11\x22\x33\x44\x55\x66\x77\x88"
        )  # wire 1 inside event
        event_inner += _encode_varint((4 << 3) | 5) + b"\xaa\xbb\xcc\xdd"  # wire 5 inside event
        entry += _encode_varint((9 << 3) | 2) + _encode_varint(len(event_inner)) + event_inner

        # Top-level blob with wire 1 and wire 5
        blob = _encode_varint((3 << 3) | 0) + _encode_varint(base_ts)
        blob += _encode_varint((11 << 3) | 1) + b"\x00" * 8  # top-level wire 1
        blob += _encode_varint((12 << 3) | 5) + b"\x00" * 4  # top-level wire 5
        blob += _encode_varint((2 << 3) | 2) + _encode_varint(len(entry)) + entry

        members = viewer._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
        assert len(members) == 1
        assert members[0]["delivered_ts"] == (base_ts + 45) * 1000


class TestChatInfoMediaSizeAndStatus:
    def test_chat_info_media_size_contact_and_group(self, tmp_path):
        from tests.test_wa_chat_viewer import make_android_db, make_archive_db

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('1001', 'Alice', 'Alice')"
        )
        arc_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('2001', 'Friends', 'Friends')"
        )
        arc_conn.execute("INSERT INTO files (original_path, md5) VALUES ('p1.jpg', X'0102')")
        arc_conn.execute("INSERT INTO files (original_path, md5) VALUES ('p2.jpg', X'0304')")
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('p1.jpg', 'Contacts/Alice/pic.jpg')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('p2.jpg', 'Groups/Friends/pic.jpg')"
        )
        arc_conn.commit()
        arc_conn.close()

        make_android_db(tmp_path / "msgstore.db").close()

        c_file = tmp_path / "Contacts" / "Alice" / "pic.jpg"
        c_file.parent.mkdir(parents=True, exist_ok=True)
        c_file.write_bytes(b"12345")  # 5 bytes

        g_file = tmp_path / "Groups" / "Friends" / "pic.jpg"
        g_file.parent.mkdir(parents=True, exist_ok=True)
        g_file.write_bytes(b"12345678")  # 8 bytes

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp_c = client.get("/api/chat-info/media-size?chat_id=1001&chat_type=contact")
            assert resp_c.status_code == 200
            assert resp_c.get_json()["bytes"] == 5

            resp_g = client.get("/api/chat-info/media-size?chat_id=2001&chat_type=group")
            assert resp_g.status_code == 200
            assert resp_g.get_json()["bytes"] == 8

            resp_missing = client.get("/api/chat-info/media-size?chat_id=9999&chat_type=contact")
            assert resp_missing.status_code == 200
            assert resp_missing.get_json()["bytes"] == 0

            # Index status endpoints
            idx_resp = client.get("/api/index-status")
            assert idx_resp.status_code == 200
            assert "indexed" in idx_resp.get_json()

            chat_idx_resp = client.get("/api/chat-index-status?chat_id=1001&chat_type=contact")
            assert chat_idx_resp.status_code == 200
            assert "status" in chat_idx_resp.get_json()

    def test_chat_info_media_size_ios_contact(self, tmp_path):
        from tests.test_wa_chat_viewer import make_archive_db

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44123456', 'Alice', 'Alice')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Message/Media/1.jpg', 'Contacts/Alice/2026/Sent/1.jpg')"
        )
        arc_conn.commit()
        arc_conn.close()

        conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZGROUPINFO INTEGER, ZPARTNERNAME TEXT, ZHIDDEN INTEGER DEFAULT 0);
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE REAL, ZMESSAGETYPE INTEGER, ZTEXT TEXT, ZMEDIAITEM INTEGER, ZFROMJID TEXT, ZSTANZAID TEXT, ZPARENTMESSAGE INTEGER, ZPUSHNAME TEXT, ZGROUPMEMBER INTEGER);
        """)
        conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Partner')"
        )
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE) VALUES (1, 'Media/1.jpg', '1.jpg')"
        )
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM) VALUES (1, 1, 1, 700000000.0, 1, '', 1)"
        )
        conn.commit()
        conn.close()

        f = tmp_path / "Contacts" / "Alice" / "2026" / "Sent" / "1.jpg"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"1234567890")

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/chat-info/media-size?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            assert resp.get_json()["bytes"] == 10

    def test_chat_info_media_size_android_lid_contact(self, tmp_path):
        from tests.test_wa_chat_viewer import make_archive_db

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44123456', 'Bob', 'Bob')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/Images/1.jpg', 'Contacts/Bob/2026/Sent/1.jpg')"
        )
        arc_conn.commit()
        arc_conn.close()

        wa_conn = sqlite3.connect(str(tmp_path / "msgstore.db"))
        wa_conn.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT, sender_jid_row_id INTEGER);
            CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, chat_row_id INTEGER, file_path TEXT, media_name TEXT, mime_type TEXT);
        """)
        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '103624826949008', 'lid', '103624826949008@lid')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, NULL, 0, 1678307200000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type, text_data, sender_jid_row_id) VALUES (1, 1, 1, 1678307200000, 1, '', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, chat_row_id, file_path) VALUES (1, 1, 'Media/Images/1.jpg')"
        )
        wa_conn.commit()
        wa_conn.close()

        f = tmp_path / "Contacts" / "Bob" / "2026" / "Sent" / "1.jpg"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"1234567890")

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/chat-info/media-size?chat_id=103624826949008&chat_type=contact")
            assert resp.status_code == 200
            assert resp.get_json()["bytes"] == 10


class TestVcardViewer:
    def test_media_type_from_path_vcf(self):
        assert viewer._media_type_from_path("Contacts/Alice/Bob.vcf") == "vcard"
        assert viewer._media_type_from_path("Contacts/Alice/Bob.VCF") == "vcard"

    def test_android_vcard_messages_endpoint(self, tmp_path):
        from tests.test_wa_chat_viewer import make_archive_db

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44123456', 'Alice', 'Alice')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('vcard:1', 'Contacts/Alice/Bob.vcf')"
        )
        arc_conn.commit()
        arc_conn.close()

        wa_conn = sqlite3.connect(str(tmp_path / "msgstore.db"))
        wa_conn.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT, sender_jid_row_id INTEGER);
            CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, chat_row_id INTEGER, file_path TEXT, media_name TEXT, mime_type TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER, sender_jid_row_id INTEGER);
            CREATE TABLE message_vcard (message_row_id INTEGER PRIMARY KEY, vcard TEXT);
        """)
        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '44123456', 's.whatsapp.net', '44123456@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, NULL, 0, 1678307200000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type, text_data, sender_jid_row_id) VALUES (1, 1, 0, 1678307200000, 4, 'Bob', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_vcard (message_row_id, vcard) VALUES (1, 'BEGIN:VCARD\r\nFN:Bob\r\nEND:VCARD\r\n')"
        )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=44123456&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["media_type"] == "vcard"
            assert msgs[0]["archive_path"] == "Contacts/Alice/Bob.vcf"
            assert msgs[0]["media_name"] == "Bob"

            # Check chat list last_msg_type
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "vcard"

    def test_ios_vcard_messages_endpoint(self, tmp_path):
        from tests.test_wa_chat_viewer import make_archive_db

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44123456', 'Alice', 'Alice')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('vcard:1', 'Contacts/Alice/Doctor Bob.vcf')"
        )
        arc_conn.commit()
        arc_conn.close()

        wa_conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZFROMJID TEXT,
                ZSTANZAID TEXT,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Partner')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZVCARDNAME, ZVCARDSTRING) VALUES (1, NULL, NULL, 'Doctor Bob', 'BEGIN:VCARD\r\nFN:Doctor Bob\r\nEND:VCARD\r\n')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM) VALUES (1, 1, 0, 700000000.0, 4, NULL, 1)"
        )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["media_type"] == "vcard"
            assert msgs[0]["archive_path"] == "Contacts/Alice/Doctor Bob.vcf"
            assert msgs[0]["media_name"] == "Doctor Bob"

            # Check chat list last_msg_type
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "vcard"


class TestEditedMessages:
    def test_android_messages_with_message_edit_info(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("""
            CREATE TABLE message_edit_info (
                message_row_id INTEGER PRIMARY KEY,
                original_key_id TEXT,
                edited_timestamp INTEGER,
                sender_timestamp INTEGER
            );
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '12345678')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (101, 1, 0, 1700000000000, 'Original text', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (102, 1, 1, 1700000010000, 'Edited text', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp)
            VALUES (102, 'orig-key-102', 1700000025000, 1700000025000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=12345678&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert msgs[101]["is_edited"] == 0
            assert msgs[101]["edited_ts"] is None
            assert msgs[102]["is_edited"] == 1
            assert msgs[102]["edited_ts"] == 1700000025000

    def test_android_messages_fallback_message_add_on(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                timestamp INTEGER,
                message_add_on_type INTEGER
            );
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '12345678')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (201, 1, 0, 1700000000000, 'Regular message', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (202, 1, 1, 1700000010000, 'Add-on edited message', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_add_on (_id, parent_message_row_id, from_me, timestamp, message_add_on_type)
            VALUES (1, 202, 1, 1700000030000, 74)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=12345678&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert msgs[201]["is_edited"] == 0
            assert msgs[201]["edited_ts"] is None
            assert msgs[202]["is_edited"] == 1
            assert msgs[202]["edited_ts"] == 1700000030000

    def test_recent_messages_cache_preserves_edit_info(self, tmp_path):
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute("""
            INSERT INTO recent_messages
            (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body, is_edited, edited_ts)
            VALUES ('12345678', 'contact', 301, 1700000000000, 'Alice', 0, NULL, 'text', '', 'Cached edit', 1, 1700000045000)
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=12345678&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["msg_id"] == 301
            assert msgs[0]["is_edited"] == 1
            assert msgs[0]["edited_ts"] == 1700000045000

    def test_ios_messages_edit_defaults(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = sqlite3.connect(str(wa_path))
        archive_conn = make_archive_db(archive_path)
        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZFROMJID TEXT,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Partner')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) VALUES (1, 1, 0, 700000000.0, 0, 'iOS message')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_edited"] == 0
            assert msgs[0]["edited_ts"] is None

    def test_messages_at_endpoint_includes_edited(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("""
            CREATE TABLE message_edit_info (
                message_row_id INTEGER PRIMARY KEY,
                original_key_id TEXT,
                edited_timestamp INTEGER,
                sender_timestamp INTEGER
            );
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '99999999')")
        wa_conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type)
            VALUES (401, 1, 0, 1700000000000, 'Original message', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp)
            VALUES (401, 'key-401', 1700000005000, 1700000005000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get(
                "/api/messages/at?chat_id=99999999&chat_type=contact&ts=1700000000000"
            )
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_edited"] == 1
            assert msgs[0]["edited_ts"] == 1700000005000

    def test_frontend_assets_include_msg_edited(self):
        js_path = Path(viewer.__file__).parent / "chat_viewer" / "app.js"
        css_path = Path(viewer.__file__).parent / "chat_viewer" / "app.css"
        js_content = js_path.read_text(encoding="utf-8")
        css_content = css_path.read_text(encoding="utf-8")
        assert "msg-edited" in js_content
        assert "msg-edited" in css_content


class TestStickerSupport:
    def test_ios_sticker_classification_and_gallery(self, tmp_path):
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path_db)
        archive_conn.execute("INSERT INTO contacts (number, folder) VALUES ('44123456', 'Alice')")
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Message/Media/123/abc.webp', 'Contacts/Alice/2026/abc.webp')"
        )
        archive_conn.commit()
        archive_conn.close()

        wa_conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZFROMJID TEXT,
                ZSTANZAID TEXT,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Alice')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE) VALUES (1, 'Media/123/abc.webp', NULL)"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM) VALUES (1, 1, 0, 700000000.0, 15, NULL, 1)"
        )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            # 1. /api/messages classifies as sticker
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["media_type"] == "sticker"
            assert msgs[0]["archive_path"] == "Contacts/Alice/2026/abc.webp"

            # 2. /api/chats classifies last_msg_type as sticker
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "sticker"

            # 3. /api/media with types=sticker returns the sticker
            stickers_resp = client.get("/api/media?chat_id=1&chat_type=contact&types=sticker")
            assert stickers_resp.status_code == 200
            stickers = stickers_resp.get_json()
            assert len(stickers) == 1
            assert stickers[0]["media_type"] == "sticker"

            # 4. /api/media with types=image does NOT return the sticker
            images_resp = client.get("/api/media?chat_id=1&chat_type=contact&types=image")
            assert images_resp.status_code == 200
            images = images_resp.get_json()
            assert len(images) == 0

            # 5. /api/media/count counts it as sticker
            count_resp = client.get("/api/media/count?chat_id=1&chat_type=contact")
            assert count_resp.status_code == 200
            counts = count_resp.get_json()
            assert counts["by_type"]["sticker"]["count"] == 1
            assert counts["by_type"]["sticker"]["missing"] == 0

    def test_android_sticker_classification_and_gallery(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (55, 10, 0, 1, 1700000055000, NULL, 20)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (55, 'Media/WhatsApp Stickers/STK-2026.webp', 'STK-2026.webp')"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/WhatsApp Stickers/STK-2026.webp', 'Contacts/Alice/2026/STK-2026.webp')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            # 1. /api/messages classifies as sticker
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            msgs = [m for m in resp.get_json() if m["msg_id"] == 55]
            assert len(msgs) == 1
            assert msgs[0]["media_type"] == "sticker"
            assert msgs[0]["archive_path"] == "Contacts/Alice/2026/STK-2026.webp"

            # 2. /api/media with types=sticker returns the sticker
            stickers_resp = client.get(
                "/api/media?chat_id=123456789&chat_type=contact&types=sticker"
            )
            assert stickers_resp.status_code == 200
            stickers = stickers_resp.get_json()
            assert len(stickers) == 1
            assert stickers[0]["media_type"] == "sticker"

            # 3. /api/media with types=image does NOT return the sticker
            images_resp = client.get("/api/media?chat_id=123456789&chat_type=contact&types=image")
            assert images_resp.status_code == 200
            images = [img for img in images_resp.get_json() if img["msg_id"] == 55]
            assert len(images) == 0

            # 4. /api/media/count counts it as sticker
            count_resp = client.get("/api/media/count?chat_id=123456789&chat_type=contact")
            assert count_resp.status_code == 200
            counts = count_resp.get_json()
            assert counts["by_type"]["sticker"]["count"] == 1
            assert counts["by_type"]["sticker"]["missing"] == 0

    def test_sticker_deselected_from_default_media_view_frontend(self):
        """Stickers, links, and documents must be in DEFAULT_DESELECTED_TYPES in app.js
        so stickers are deselected by default when opening the media gallery."""
        js_path = Path(viewer.__file__).parent / "chat_viewer" / "app.js"
        js = js_path.read_text(encoding="utf-8")
        assert "DEFAULT_DESELECTED_TYPES" in js
        match = re.search(r"DEFAULT_DESELECTED_TYPES\s*=\s*new\s+Set\(\[([^\]]+)\]\)", js)
        assert match, "DEFAULT_DESELECTED_TYPES Set definition missing in app.js"
        types = [t.strip().strip("'\"") for t in match.group(1).split(",")]
        assert "sticker" in types
        assert "link" in types
        assert "document" in types

        # Verify openMediaGallery filters out DEFAULT_DESELECTED_TYPES
        assert "!DEFAULT_DESELECTED_TYPES.has(t)" in js

        # Verify _toggleTypeFilter toggles DEFAULT_DESELECTED_TYPES exclusively
        assert "DEFAULT_DESELECTED_TYPES.has(type)" in js

    def test_android_deleted_messages(self, tmp_path):
        """Android deleted messages (type 15 and 64) are returned as media_type='deleted' with synthesized text."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '123456789')")
        # Chat whose display_message_row_id is message 4 (deleted by admin)
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (10, 1, NULL, 0, 1700000004000, 4)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES "
            "(1, 10, 0, 1, 1700000001000, 'Normal message', 0),"
            "(2, 10, 0, 1, 1700000002000, NULL, 15),"
            "(3, 10, 1, 1, 1700000003000, NULL, 15),"
            "(4, 10, 0, 1, 1700000004000, NULL, 64)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('123456789', 'Alice', 'Alice')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            # 1. /api/messages returns all 4 messages
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert len(msgs) == 4

            # Message 2: Revoked by other
            assert msgs[2]["media_type"] == "deleted"
            assert msgs[2]["text_body"] == "This message was deleted"
            assert msgs[2]["is_view_once"] == 0

            # Message 3: Revoked by me
            assert msgs[3]["media_type"] == "deleted"
            assert msgs[3]["text_body"] == "You deleted this message"
            assert msgs[3]["is_view_once"] == 0

            # Message 4: Revoked by admin
            assert msgs[4]["media_type"] == "deleted"
            assert msgs[4]["text_body"] == "This message was deleted by an admin"
            assert msgs[4]["is_view_once"] == 0

            # 2. Deleted messages do NOT appear in /api/media
            media_resp = client.get("/api/media?chat_id=123456789&chat_type=contact")
            assert media_resp.status_code == 200
            assert len(media_resp.get_json()) == 0

            # 3. Deleted messages are NOT counted in /api/media/count
            count_resp = client.get("/api/media/count?chat_id=123456789&chat_type=contact")
            assert count_resp.status_code == 200
            counts = count_resp.get_json()
            assert "deleted" not in counts.get("by_type", {})
            assert counts.get("total", 0) == 0

            # 4. /api/chats reflects deleted message preview
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = [c for c in chats_resp.get_json() if c["id"] == "123456789"]
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "deleted"
            assert chats[0]["last_msg_preview"] == "This message was deleted by an admin"
            assert chats[0]["is_view_once"] == 0

    def test_android_legacy_mislabeled_deleted_messages_self_healing(self, tmp_path):
        """Historical deltas with message_type=7 and action_type=1 in 1-to-1 chats self-heal to deleted messages."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '123456789')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (10, 1, NULL, 0, 1700000002000, 2)"
        )
        wa_conn.execute(
            "CREATE TABLE IF NOT EXISTS message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER NOT NULL)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES "
            "(1, 10, 0, 0, 1700000001000, 'Hello', 0),"
            "(2, 10, 1, 0, 1700000002000, NULL, 7)"
        )
        wa_conn.execute("INSERT INTO message_system (message_row_id, action_type) VALUES (2, 1)")
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('123456789', 'Alice', 'Alice')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert len(msgs) == 2

            # Message 2 in 1:1 chat must self-heal to deleted, never 'You changed the group subject'
            assert msgs[2]["media_type"] == "deleted"
            assert msgs[2]["text_body"] == "You deleted this message"
            assert "group subject" not in msgs[2]["text_body"]

            # /api/chats reflects deleted preview rather than group subject
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = [c for c in chats_resp.get_json() if c["id"] == "123456789"]
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "deleted"
            assert chats[0]["last_msg_preview"] == "You deleted this message"
            assert "group subject" not in chats[0]["last_msg_preview"]

    def test_android_revoked_messages_table_and_cached_service_healing(self, tmp_path):
        """Messages in message_revoked are returned as deleted, and cached service rows are healed."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '999888777')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (20, 1, NULL, 0, 1700000003000, 3)"
        )
        wa_conn.execute(
            "CREATE TABLE IF NOT EXISTS message_revoked ("
            "message_row_id INTEGER PRIMARY KEY, "
            "revoked_key_id TEXT, "
            "admin_jid_row_id INTEGER, "
            "revoke_timestamp INTEGER)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES "
            "(1, 20, 0, 0, 1700000001000, 'Original text', 0),"
            "(2, 20, 1, 0, 1700000002000, NULL, 15),"
            "(3, 20, 0, 0, 1700000003000, NULL, 7)"
        )
        wa_conn.execute(
            "INSERT INTO message_revoked (message_row_id, revoked_key_id, admin_jid_row_id, revoke_timestamp) VALUES "
            "(2, 'REVOKEKEY2', 0, 1700000002000),"
            "(3, 'REVOKEKEY3', 0, 1700000003000)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('999888777', 'Bob', 'Bob')"
        )
        # Seed stale/empty service rows in recent_messages to verify startup purge & retrieval healing
        archive_conn.execute(
            "INSERT OR REPLACE INTO recent_messages (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, media_type, text_body) VALUES "
            "('999888777', 'contact', 2, 1700000002000, '', 1, 'service', 'You deleted this message'),"
            "('999888777', 'contact', 99, 1700000000500, '', 0, 'service', '')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)

        # Verify startup healed the empty service row (msg_id 99) to deleted instead of purging it
        arch_chk = sqlite3.connect(archive_path)
        r99 = arch_chk.execute(
            "SELECT media_type, text_body FROM recent_messages WHERE msg_id = 99"
        ).fetchone()
        assert r99 is not None, "Message 99 must be preserved in recent_messages"
        assert r99[0] == "deleted"
        assert r99[1] == "This message was deleted"
        arch_chk.close()

        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=999888777&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}

            # Message 2 (from_me=1) in recent_messages / live DB is returned as deleted
            assert msgs[2]["media_type"] == "deleted"
            assert msgs[2]["text_body"] == "You deleted this message"

            # Message 99 (from_me=0) in recent_messages is returned as deleted
            assert msgs[99]["media_type"] == "deleted"
            assert msgs[99]["text_body"] == "This message was deleted"

            # Query with before to bypass recent_messages and exercise live DB _ANDROID_SELECT
            resp_live = client.get(
                "/api/messages?chat_id=999888777&chat_type=contact&before=1700000004000"
            )
            assert resp_live.status_code == 200
            live_msgs = {m["msg_id"]: m for m in resp_live.get_json()}
            assert live_msgs[2]["media_type"] == "deleted"
            assert live_msgs[2]["text_body"] == "You deleted this message"
            assert live_msgs[3]["media_type"] == "deleted"
            assert live_msgs[3]["text_body"] == "This message was deleted"

            # /api/chats reflects deleted preview for message 3
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = [c for c in chats_resp.get_json() if c["id"] == "999888777"]
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "deleted"
            assert chats[0]["last_msg_preview"] == "This message was deleted"

    def test_android_recent_messages_missing_revoked_cache_hole_self_healing(self, tmp_path):
        """When a revoked message was missing from recent_messages, live DB query heals the cache hole."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '555666777')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (30, 1, NULL, 0, 1700000003000, 3)"
        )
        wa_conn.execute(
            "CREATE TABLE IF NOT EXISTS message_revoked ("
            "message_row_id INTEGER PRIMARY KEY, "
            "revoked_key_id TEXT, "
            "admin_jid_row_id INTEGER, "
            "revoke_timestamp INTEGER)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES "
            "(1, 30, 0, 0, 1700000001000, 'First message', 0),"
            "(2, 30, 1, 0, 1700000002000, NULL, 15),"
            "(3, 30, 0, 0, 1700000003000, 'Third message', 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_revoked (message_row_id, revoked_key_id, admin_jid_row_id, revoke_timestamp) VALUES "
            "(2, 'REVOKEKEY_HOLE', 0, 1700000002000)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('555666777', 'Carol', 'Carol')"
        )
        # Pre-seed recent_messages with ONLY message 1 and message 3 (message 2 is a missing hole)
        archive_conn.execute(
            "INSERT OR REPLACE INTO recent_messages (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, media_type, text_body) VALUES "
            "('555666777', 'contact', 1, 1700000001000, '', 0, 'text', 'First message'),"
            "('555666777', 'contact', 3, 1700000003000, '', 0, 'text', 'Third message')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=555666777&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            # Cache hole detected: live query returned message 2 as deleted
            assert 2 in msgs, "Missing revoked message must be restored into chat response"
            assert msgs[2]["media_type"] == "deleted"
            assert msgs[2]["text_body"] == "You deleted this message"

    def test_android_view_once_messages(self, tmp_path):
        """Android view-once messages (types 42, 43, 82) are distinguished with is_view_once=1."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '987654321')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (20, 1, NULL, 0, 1700000003000, 12)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES "
            "(10, 20, 0, 1, 1700000001000, NULL, 42),"  # Purged VO photo
            "(11, 20, 0, 1, 1700000002000, NULL, 43),"  # Preserved VO video
            "(12, 20, 0, 1, 1700000003000, NULL, 82)"  # Purged VO voice note
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) VALUES "
            "(11, 'Media/WhatsApp Video/VID_VO.mp4', 'VID_VO.mp4')"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('987654321', 'Bob', 'Bob')"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/WhatsApp Video/VID_VO.mp4', 'Contacts/Bob/2026/VID_VO.mp4')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=987654321&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert len(msgs) == 3

            # 10: Purged VO photo
            assert msgs[10]["media_type"] == "image"
            assert msgs[10]["is_view_once"] == 1
            assert msgs[10]["archive_path"] is None

            # 11: Preserved VO video
            assert msgs[11]["media_type"] == "video"
            assert msgs[11]["is_view_once"] == 1
            assert msgs[11]["archive_path"] == "Contacts/Bob/2026/VID_VO.mp4"

            # 12: Purged VO voice note
            assert msgs[12]["media_type"] == "audio"
            assert msgs[12]["is_view_once"] == 1
            assert msgs[12]["archive_path"] is None

            # Preserved VO video appears in /api/media
            media_resp = client.get("/api/media?chat_id=987654321&chat_type=contact")
            assert media_resp.status_code == 200
            media_items = media_resp.get_json()
            assert len(media_items) == 1
            assert media_items[0]["msg_id"] == 11
            assert media_items[0]["is_view_once"] == 1

            # /api/chats reflects VO status
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = [c for c in chats_resp.get_json() if c["id"] == "987654321"]
            assert len(chats) == 1
            assert chats[0]["is_view_once"] == 1
            assert chats[0]["last_msg_type"] == "audio"

    def test_android_view_once_media_auxiliary_table(self, tmp_path):
        """Android messages tracked in message_view_once_media table are distinguished with is_view_once=1."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '987654321')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (20, 1, NULL, 0, 1700000003000, 12)"
        )
        wa_conn.executescript("""
            CREATE TABLE message_view_once_media (
                message_row_id INTEGER PRIMARY KEY,
                state INTEGER NOT NULL
            );
            ALTER TABLE message_quoted ADD COLUMN message_type INTEGER;
            ALTER TABLE message_quoted ADD COLUMN key_id TEXT;
            ALTER TABLE message_quoted ADD COLUMN parent_message_chat_row_id INTEGER;
            ALTER TABLE message_quoted ADD COLUMN chat_row_id INTEGER;
        """)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES "
            "(10, 20, 0, 1, 1700000001000, NULL, 1),"
            "(11, 20, 0, 1, 1700000002000, NULL, 42),"
            "(12, 20, 0, 1, 1700000003000, NULL, 2),"
            "(13, 20, 0, 1, 1700000004000, 'Nice photo', 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_view_once_media (message_row_id, state) VALUES (10, 1), (12, 2)"
        )
        wa_conn.execute(
            "INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, key_id, message_type) "
            "VALUES (13, 20, 20, 0, 'KEY_11', 42)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('987654321', 'Bob', 'Bob')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=987654321&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert msgs[10]["is_view_once"] == 1
            assert msgs[10]["media_type"] == "image"
            assert msgs[10]["archive_path"] is None

            assert msgs[11]["is_view_once"] == 1
            assert msgs[12]["is_view_once"] == 1

            # Quoted VO photo maps to '📷 Photo'
            assert msgs[13]["quoted_text"] == "📷 Photo"

            # /api/media/count does not count purged VO messages as missing
            cnt_resp = client.get("/api/media/count?chat_id=987654321&chat_type=contact")
            assert cnt_resp.status_code == 200
            cnt = cnt_resp.get_json()
            assert cnt["by_type"].get("image", {}).get("missing", 0) == 0

            # /api/chats reflects VO status from message_view_once_media
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = [c for c in chats_resp.get_json() if c["id"] == "987654321"]
            assert len(chats) == 1
            assert chats[0]["is_view_once"] == 1

            # /api/messages/at correctly retains is_view_once=1 for live messages
            at_resp = client.get(
                "/api/messages/at?chat_id=987654321&chat_type=contact&ts=1700000001000"
            )
            assert at_resp.status_code == 200
            at_msgs = {m["msg_id"]: m for m in at_resp.get_json()}
            assert at_msgs[10]["is_view_once"] == 1

    def test_ios_deleted_and_view_once_messages(self, tmp_path):
        """iOS deleted (type 14) and view-once (types 38, 39, 53) are handled accurately."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = sqlite3.connect(str(wa_path))
        archive_conn = make_archive_db(archive_path)

        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZGROUPINFO INTEGER,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZMEDIAITEM INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZFROMJID TEXT,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44123456@s.whatsapp.net', NULL, 'Charlie')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE) VALUES "
            "(1, 'vo_video.mp4', 'vo_video.mp4')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM) VALUES "
            "(1, 1, 0, 700000001.0, 14, NULL, NULL),"  # Revoked by sender
            "(2, 1, 1, 700000002.0, 14, NULL, NULL),"  # Revoked by me
            "(3, 1, 0, 700000003.0, 38, NULL, NULL),"  # Purged VO image
            "(4, 1, 0, 700000004.0, 39, NULL, 1),"  # Preserved VO video
            "(5, 1, 0, 700000005.0, 53, NULL, NULL)"  # Purged VO audio
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Message/vo_video.mp4', 'Contacts/Charlie/2026/vo_video.mp4')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = {m["msg_id"]: m for m in resp.get_json()}
            assert len(msgs) == 5

            # Message 1: Revoked
            assert msgs[1]["media_type"] == "deleted"
            assert msgs[1]["text_body"] == "This message was deleted"
            assert msgs[1]["is_view_once"] == 0

            # Message 2: Revoked by me
            assert msgs[2]["media_type"] == "deleted"
            assert msgs[2]["text_body"] == "You deleted this message"
            assert msgs[2]["is_view_once"] == 0

            # Message 3: VO image purged
            assert msgs[3]["media_type"] == "image"
            assert msgs[3]["is_view_once"] == 1
            assert msgs[3]["archive_path"] is None

            # Message 4: VO video preserved
            assert msgs[4]["media_type"] == "video"
            assert msgs[4]["is_view_once"] == 1
            assert msgs[4]["archive_path"] == "Contacts/Charlie/2026/vo_video.mp4"

            # Message 5: VO audio purged
            assert msgs[5]["media_type"] == "audio"
            assert msgs[5]["is_view_once"] == 1
            assert msgs[5]["archive_path"] is None

            # /api/chats selects message 5 as last real message
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = [c for c in chats_resp.get_json() if c["id"] == "1"]
            assert len(chats) == 1
            assert chats[0]["is_view_once"] == 1
            assert chats[0]["last_msg_type"] == "audio"

    def test_recent_messages_persistence_with_view_once(self, tmp_path):
        """recent_messages table stores and returns is_view_once."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        rows = [
            {
                "chat_id": "12345",
                "chat_type": "contact",
                "msg_id": 1,
                "timestamp_ms": 1700000000000,
                "sender": "Alice",
                "from_me": 0,
                "archive_path": None,
                "media_type": "image",
                "media_name": None,
                "text_body": "",
                "is_view_once": 1,
            }
        ]
        viewer._backfill_recent_messages(archive_conn, rows)

        res = archive_conn.execute("SELECT * FROM recent_messages WHERE msg_id = 1").fetchone()
        assert res["is_view_once"] == 1
        assert res["media_type"] == "image"
        archive_conn.close()


# ---------------------------------------------------------------------------
# Tests: Polls (Android msgstore.db & iOS ChatStorage.sqlite)
# ---------------------------------------------------------------------------


class TestPollSupport:
    """Test suite for poll bubbles, hydration, vote details, and FTS search."""

    @staticmethod
    def _make_poll_blob(
        question: str, options: list[str], multiple: bool, votes: list[dict]
    ) -> bytes:
        def encode_varint(n: int) -> bytes:
            res = bytearray()
            while True:
                b = n & 0x7F
                n >>= 7
                if n:
                    res.append(b | 0x80)
                else:
                    res.append(b)
                    break
            return bytes(res)

        def encode_field(tag: int, wire: int, val: bytes) -> bytes:
            header = encode_varint((tag << 3) | wire)
            if wire == 0:
                return header + val
            elif wire == 2:
                return header + encode_varint(len(val)) + val
            raise ValueError(f"Unsupported wire {wire}")

        f8 = bytearray()
        f8.extend(encode_field(2, 2, question.encode("utf-8")))
        for opt in options:
            opt_b = encode_field(1, 2, opt.encode("utf-8"))
            f8.extend(encode_field(3, 2, opt_b))
        mode_val = 0 if multiple else 1
        f8.extend(encode_field(4, 0, encode_varint(mode_val)))
        for v in votes:
            vp = bytearray()
            packed = bytearray()
            for idx in v.get("option_indices", []):
                packed.extend(encode_varint(idx))
            if packed:
                vp.extend(encode_field(1, 2, bytes(packed)))
            if "client_ts" in v:
                vp.extend(encode_field(2, 0, encode_varint(v["client_ts"])))
            if "stanza_id" in v:
                vp.extend(encode_field(3, 2, v["stanza_id"].encode("utf-8")))
            if "voter_jid" in v:
                vp.extend(encode_field(4, 2, v["voter_jid"].encode("utf-8")))
            if "server_ts" in v:
                vp.extend(encode_field(5, 0, encode_varint(v["server_ts"])))
            f8.extend(encode_field(5, 2, bytes(vp)))

        return encode_field(8, 2, bytes(f8))

    def test_parse_ios_poll_blob(self):
        votes = [
            {
                "option_indices": [0],
                "client_ts": 1700000000000,
                "voter_jid": "15551112222@s.whatsapp.net",
            },
            {
                "option_indices": [0, 1],
                "client_ts": 1700000001000,
                "voter_jid": "15553334444@s.whatsapp.net",
            },
        ]
        blob = self._make_poll_blob("Weekend plan?", ["Option A", "Option B"], True, votes)
        parsed = viewer._parse_ios_poll_blob(blob)
        assert parsed is not None
        assert parsed["question"] == "Weekend plan?"
        assert parsed["options"] == ["Option A", "Option B"]
        assert parsed["multiple_answers"] is True
        assert len(parsed["votes"]) == 2
        assert parsed["votes"][0]["voter_jid"] == "15551112222@s.whatsapp.net"
        assert parsed["votes"][1]["option_indices"] == [0, 1]

        # Empty/corrupt blob
        assert viewer._parse_ios_poll_blob(b"") is None
        assert viewer._parse_ios_poll_blob(b"random bytes") is None

    def test_android_poll_messages_and_details(self, tmp_path):
        """Test Android poll hydration in /api/messages and /api/poll_details."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
            CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, message_add_on_type INTEGER, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_add_on_poll_vote_selected_option (message_add_on_row_id INTEGER, message_poll_option_id INTEGER);
        """)

        # Chat
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '15552222222')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Group Chat', 0, 1700000000000, 10)"
        )

        # Poll message
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (10, 1, 0, 2, 1700000000000, 'Lunch spot?', 66)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (10, 0)"
        )  # Multi-choice
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (101, 10, 'Tacos', 2)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (102, 10, 'Pizza', 1)"
        )

        # Votes: Bob (jid 2) voted Tacos. Device user (from_me 1) voted Tacos and Pizza.
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type, from_me, sender_jid_row_id, timestamp) VALUES (201, 10, 67, 0, 2, 1700000001000)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type, from_me, sender_jid_row_id, timestamp) VALUES (202, 10, 67, 1, NULL, 1700000002000)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (201, 101)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (202, 101)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_poll_vote_selected_option (message_add_on_row_id, message_poll_option_id) VALUES (202, 102)"
        )
        wa_conn.commit()

        # Contacts
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15552222222', 'Bob (15552222222)', 'Bob')"
        )
        archive_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # Test /api/messages
        res = client.get("/api/messages?chat_id=1&chat_type=group")
        assert res.status_code == 200
        msgs = res.get_json()
        assert len(msgs) == 1
        poll_msg = msgs[0]
        assert poll_msg["media_type"] == "poll"
        assert poll_msg["poll"]["question"] == "Lunch spot?"
        assert poll_msg["poll"]["multiple_answers"] is True
        assert poll_msg["poll"]["total_votes"] == 3
        assert poll_msg["poll"]["total_voters"] == 2
        opts = poll_msg["poll"]["options"]
        assert len(opts) == 2
        assert opts[0]["name"] == "Tacos"
        assert opts[0]["vote_count"] == 2
        assert opts[0]["user_selected"] is True
        assert opts[1]["name"] == "Pizza"
        assert opts[1]["vote_count"] == 1
        assert opts[1]["user_selected"] is True

        # Test /api/poll_details/<msg_id>
        res_det = client.get("/api/poll_details/10")
        assert res_det.status_code == 200
        det = res_det.get_json()
        assert det["available"] is True
        assert det["question"] == "Lunch spot?"
        assert det["total_voters"] == 2
        opt_map = {o["id"]: o for o in det["options"]}
        assert len(opt_map[101]["voters"]) == 2
        voter_names = [v["name"] for v in opt_map[101]["voters"]]
        assert "Bob" in voter_names
        assert "You" in voter_names
        assert len(opt_map[102]["voters"]) == 1
        assert opt_map[102]["voters"][0]["name"] == "You"

        # Test non-existent poll details
        res_none = client.get("/api/poll_details/9999")
        assert res_none.status_code == 200
        assert res_none.get_json()["available"] is True
        assert len(res_none.get_json()["options"]) == 0

    def test_ios_poll_messages_and_details(self, tmp_path):
        """Test iOS poll hydration in /api/messages and /api/poll_details."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        wa_path = tmp_path / "ChatStorage.sqlite"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZGROUPINFO INTEGER, ZCONTACTJID TEXT, ZPARTNERNAME TEXT, ZHIDDEN INTEGER DEFAULT 0);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEINFO INTEGER,
                ZMESSAGEDATE INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER, ZMESSAGETYPE INTEGER,
                ZTEXT TEXT, ZMEDIAITEM INTEGER, ZPARENTMESSAGE INTEGER, ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZMEDIALOCALPATH TEXT);
            CREATE TABLE ZWAMESSAGEINFO (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
        """)

        # Contacts in ContactsV2
        cv2_path = tmp_path / "ContactsV2.sqlite"
        cv2_conn = sqlite3.connect(str(cv2_path))
        cv2_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID TEXT, ZFULLNAME TEXT);
        """)
        cv2_conn.execute(
            "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZWHATSAPPID, ZFULLNAME) VALUES (1, '15553333333@s.whatsapp.net', 'Charlie')"
        )
        cv2_conn.commit()
        cv2_conn.close()

        # Chat
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID) VALUES (5, 1, '15550009999-12345@g.us')"
        )

        # Synthetic poll protobuf: question "Meeting day?", options ["Monday", "Friday"], single choice
        votes = [
            {
                "option_indices": [0],
                "client_ts": 1700000000000,
                "voter_jid": "15553333333@s.whatsapp.net",
            },
            {"option_indices": [1], "client_ts": 1700000001000, "voter_jid": "You"},
        ]
        blob = self._make_poll_blob("Meeting day?", ["Monday", "Friday"], False, votes)

        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT) "
            "VALUES (50, 5, 0, 1000, 46, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGEINFO (Z_PK, ZMESSAGE, ZRECEIPTINFO) VALUES (1, 50, ?)", (blob,)
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # Test /api/messages
        res = client.get("/api/messages?chat_id=5&chat_type=group")
        assert res.status_code == 200
        msgs = res.get_json()
        assert len(msgs) == 1
        poll_msg = msgs[0]
        assert poll_msg["media_type"] == "poll"
        assert poll_msg["text_body"] == "Meeting day?"
        assert poll_msg["poll"]["question"] == "Meeting day?"
        assert poll_msg["poll"]["multiple_answers"] is False
        assert poll_msg["poll"]["total_votes"] == 2
        assert poll_msg["poll"]["total_voters"] == 2
        opts = poll_msg["poll"]["options"]
        assert len(opts) == 2
        assert opts[0]["name"] == "Monday"
        assert opts[0]["vote_count"] == 1
        assert opts[1]["name"] == "Friday"
        assert opts[1]["vote_count"] == 1
        assert opts[1]["user_selected"] is True

        # Test /api/poll_details/50
        res_det = client.get("/api/poll_details/50")
        assert res_det.status_code == 200
        det = res_det.get_json()
        assert det["available"] is True
        assert det["question"] == "Meeting day?"
        assert det["total_voters"] == 2
        opt_map = {o["id"]: o for o in det["options"]}
        assert len(opt_map[0]["voters"]) == 1
        assert opt_map[0]["voters"][0]["name"] == "Charlie"
        assert len(opt_map[1]["voters"]) == 1
        assert opt_map[1]["voters"][0]["name"] == "You"

    def test_poll_chat_preview(self, tmp_path):
        """Test that polls format properly in /api/chats sidebar previews."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Event Planning', 0, 1700000000000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (1, 1, 0, 1, 1700000000000, 'Picnic or Hike?', 66)"
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        res = client.get("/api/chats")
        assert res.status_code == 200
        chats = res.get_json()
        assert len(chats) == 1
        assert chats[0]["last_msg_type"] == "poll"
        assert chats[0]["last_msg_preview"] == "Picnic or Hike?"

    def test_poll_search_indexing(self, tmp_path):
        """Test FTS search indexing matches poll questions and options."""
        archive_path = tmp_path / ".wa_media_archiver.db"
        make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
        """)
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Game Night', 0, 1700000000000, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (1, 1, 0, 1, 1700000000000, 'Boardgames?', 66)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (1, 1, 'Catan', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (2, 1, 'Carcassonne', 1)"
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        # Trigger indexing
        client.get("/api/messages?chat_id=1&chat_type=group")
        for _ in range(25):
            time.sleep(0.1)
            q_res = client.get("/api/search?q=Boardgames")
            if q_res.status_code == 200 and len(q_res.get_json()["results"]) == 1:
                break

        # Match question
        q_res = client.get("/api/search?q=Boardgames")
        assert q_res.status_code == 200
        assert len(q_res.get_json()["results"]) == 1

        # Match option
        opt_res = client.get("/api/search?q=Carcassonne")
        assert opt_res.status_code == 200
        assert len(opt_res.get_json()["results"]) == 1

    def test_poll_frontend_badge_wording_and_star(self):
        """Verify poll badge uses descriptive wording without action verbs and winner star is defined."""
        js_path = Path(viewer.__file__).parent / "chat_viewer" / "app.js"
        css_path = Path(viewer.__file__).parent / "chat_viewer" / "app.css"
        js_content = js_path.read_text(encoding="utf-8")
        css_content = css_path.read_text(encoding="utf-8")

        # Badge wording must describe without an action verb
        assert "Multiple choice" in js_content
        assert "Single choice" in js_content
        assert "Select one or more" not in js_content
        assert "Select one" not in js_content

        # Star styles and tokens
        assert "--star-color" in css_content
        assert "msg-poll-winner-star" in css_content
        assert "msg-poll-winner-star" in js_content
        assert "msg-poll-details-option-hdr" in css_content
        assert "msg-poll-details-option-hdr" in js_content

    def test_missing_media_thumbnail_and_dimensions(self, tmp_path):
        """Verify media dimensions and thumbnail streaming for Android missing media."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        wa_conn = sqlite3.connect(tmp_path / "msgstore.db")
        wa_conn.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT)")
        wa_conn.execute(
            "CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER)"
        )
        wa_conn.execute("""
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                media_duration INTEGER,
                file_hash TEXT,
                media_name TEXT
            )
        """)
        wa_conn.execute(
            "CREATE TABLE message_thumbnail (message_row_id INTEGER PRIMARY KEY, thumbnail BLOB)"
        )
        wa_conn.execute(
            "CREATE TABLE media_hash_thumbnail (media_hash TEXT PRIMARY KEY, thumbnail BLOB)"
        )
        wa_conn.execute(
            "CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, from_me INTEGER, text_data TEXT, sender_jid_row_id INTEGER, timestamp INTEGER)"
        )

        wa_conn.execute("INSERT INTO jid VALUES (1, '15551234567')")
        wa_conn.execute("INSERT INTO chat VALUES (1, 1, 'Test Chat', 0)")

        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
        # msg 1: has thumbnail in message_thumbnail
        wa_conn.execute("INSERT INTO message VALUES (1, 1, 0, 1, 1700000000000, 'caption 1', 1)")
        wa_conn.execute(
            "INSERT INTO message_media VALUES (1, 'Media/WhatsApp Images/missing1.jpg', 12345, 800, 600, 0, 'hash1', 'missing1.jpg')"
        )
        wa_conn.execute("INSERT INTO message_thumbnail VALUES (1, ?)", (fake_jpeg,))

        # msg 2: has thumbnail in media_hash_thumbnail
        wa_conn.execute("INSERT INTO message VALUES (2, 1, 0, 1, 1700000001000, 'caption 2', 1)")
        wa_conn.execute(
            "INSERT INTO message_media VALUES (2, 'Media/WhatsApp Images/missing2.jpg', 67890, 1920, 1080, 0, 'hash2', 'missing2.jpg')"
        )
        wa_conn.execute("INSERT INTO media_hash_thumbnail VALUES ('hash2', ?)", (fake_jpeg,))

        # msg 3: no thumbnail
        wa_conn.execute("INSERT INTO message VALUES (3, 1, 0, 1, 1700000002000, 'caption 3', 3)")
        wa_conn.execute(
            "INSERT INTO message_media VALUES (3, 'Media/WhatsApp Video/missing3.mp4', 99999, 1280, 720, 45, 'hash3', 'missing3.mp4')"
        )

        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=15551234567&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}

        # Check msg 1
        m1 = msgs[1]
        assert m1["has_thumbnail"] == 1
        assert m1["media_width"] == 800
        assert m1["media_height"] == 600
        assert m1["media_size"] == 12345
        assert m1["aspect_ratio"] == round(800 / 600, 3)

        # Check msg 2
        m2 = msgs[2]
        assert m2["has_thumbnail"] == 1
        assert m2["media_width"] == 1920
        assert m2["media_height"] == 1080
        assert m2["media_size"] == 67890

        # Check msg 3
        m3 = msgs[3]
        assert m3["has_thumbnail"] == 0
        assert m3["media_width"] == 1280
        assert m3["media_height"] == 720
        assert m3["media_duration"] == 45

        # Check thumbnail endpoint
        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/jpeg"
        assert t1.data == fake_jpeg
        assert "public, max-age=86400" in t1.headers.get("Cache-Control", "")

        t2 = client.get("/api/thumbnail/2")
        assert t2.status_code == 200
        assert t2.data == fake_jpeg

        t3 = client.get("/api/thumbnail/3")
        assert t3.status_code == 404

    def test_missing_media_ios_thumbnail_and_dimensions(self, tmp_path):
        """Verify media dimensions and thumbnail streaming for iOS missing media."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        # Create a fake thumbnail file on disk for iOS thumb path
        thumb_dir = tmp_path / "Media" / "15551234567@s.whatsapp.net" / "a" / "b"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_file = thumb_dir / "test.thumb"
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10"
        thumb_file.write_bytes(fake_png)

        wa_conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
        wa_conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZFILESIZE INTEGER,
                ZASPECTRATIO FLOAT,
                ZMOVIEDURATION INTEGER,
                ZXMPPTHUMBPATH TEXT,
                ZTHUMBNAILLOCALPATH TEXT,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE FLOAT,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            )
        """)

        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15551234567@s.whatsapp.net', 'Alice', NULL)"
        )
        # msg 1: image with thumb on disk
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 54321, 1.333, 0, 'Media/15551234567@s.whatsapp.net/a/b/test.thumb', NULL, 'nonexistent.jpg', 'img.jpg')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 700000000.0, 38, 'photo caption', '15551234567@s.whatsapp.net', NULL, NULL, 'Alice')"
        )

        # msg 2: video without thumb
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (2, 98765, 1.778, 30, NULL, NULL, 'nonexistent.mp4', 'vid.mp4')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (2, 1, 2, 0, 700000010.0, 39, 'video caption', '15551234567@s.whatsapp.net', NULL, NULL, 'Alice')"
        )

        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}

        m1 = msgs[1]
        assert m1["has_thumbnail"] == 1
        assert m1["media_size"] == 54321
        assert m1["aspect_ratio"] == 1.333

        m2 = msgs[2]
        assert m2["has_thumbnail"] == 0
        assert m2["media_size"] == 98765
        assert m2["media_duration"] == 30
        assert m2["aspect_ratio"] == 1.778

        # Test iOS thumbnail endpoint
        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/png"
        assert t1.data == fake_png

        t2 = client.get("/api/thumbnail/2")
        assert t2.status_code == 404

    def test_missing_media_quoted_thumbnail_resolution(self, tmp_path):
        """Verify quoted media thumbnails are linked to original missing media messages."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        wa_conn = sqlite3.connect(tmp_path / "msgstore.db")
        wa_conn.execute("CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT)")
        wa_conn.execute(
            "CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER)"
        )
        wa_conn.execute("""
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                key_id TEXT,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT,
                file_size INTEGER,
                width INTEGER,
                height INTEGER,
                media_duration INTEGER,
                file_hash TEXT,
                media_name TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                key_id TEXT,
                parent_message_chat_row_id INTEGER,
                from_me INTEGER,
                text_data TEXT,
                sender_jid_row_id INTEGER,
                timestamp INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE message_quoted_media (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            )
        """)

        wa_conn.execute("INSERT INTO jid VALUES (1, '15551234567')")
        wa_conn.execute("INSERT INTO chat VALUES (1, 1, 'Test Chat', 0)")

        fake_quote_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xfe\x00\x05QUOTE\xff\xd9"
        # msg 10: original photo message with missing file on disk
        wa_conn.execute(
            "INSERT INTO message VALUES (10, 1, 0, 'PHOTO_ORIG_KEY', 1, 1700000010000, 'orig photo', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media VALUES (10, 'Media/WhatsApp Images/missing10.jpg', 45000, 1200, 900, 0, 'hash10', 'missing10.jpg')"
        )

        # msg 11: reply message quoting msg 10
        wa_conn.execute(
            "INSERT INTO message VALUES (11, 1, 1, 'REPLY_KEY_11', 1, 1700000020000, 'nice picture!', 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_quoted VALUES (11, 'PHOTO_ORIG_KEY', 1, 0, 'orig photo', 1, 1700000010000)"
        )
        wa_conn.execute("INSERT INTO message_quoted_media VALUES (11, ?)", (fake_quote_jpeg,))

        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}

        # Original missing message 10 must have has_thumbnail = 1 bridged from quote
        assert msgs[10]["has_thumbnail"] == 1

        # Fetch thumbnail for message 10
        t10 = client.get("/api/thumbnail/10")
        assert t10.status_code == 200
        assert t10.content_type == "image/jpeg"
        assert t10.data == fake_quote_jpeg
        assert "public, max-age=86400, immutable" in t10.headers.get("Cache-Control", "")

    def test_missing_media_ios_zmetadata_fallback_thumbnail(self, tmp_path):
        """Verify iOS missing media falls back to embedded JPEG in ZWAMEDIAITEM.ZMETADATA."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.commit()

        wa_conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
        wa_conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZFILESIZE INTEGER,
                ZASPECTRATIO FLOAT,
                ZMOVIEDURATION INTEGER,
                ZXMPPTHUMBPATH TEXT,
                ZTHUMBNAILLOCALPATH TEXT,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE FLOAT,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            )
        """)

        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (1, '15551234567@s.whatsapp.net', 'Alice', NULL)"
        )
        fake_embedded_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xfe\x00\x04EMB\xff\xd9"
        proto_blob = b"\x08\x01\x12\x20" + b"\x00" * 32 + fake_embedded_jpeg + b"\x18\x02"

        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 30000, 1.0, 0, NULL, NULL, 'missing_ios.jpg', 'img.jpg', ?)",
            (proto_blob,),
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 700000000.0, 1, 'embedded thumb test', '15551234567@s.whatsapp.net', NULL, NULL, 'Alice')"
        )
        wa_conn.commit()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}
        assert msgs[1]["has_thumbnail"] == 1

        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/jpeg"
        assert t1.data == fake_embedded_jpeg

    def test_missing_media_ios_extracted_thumbnails_dir_resolution(self, tmp_path):
        """Verify viewer resolves iOS thumbnails extracted to Whatsapp Databases/.thumbnails/."""
        arch_conn = sqlite3.connect(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute("CREATE TABLE contacts (number TEXT, display_name TEXT)")
        arch_conn.execute(
            "INSERT INTO archive_copies VALUES ('Message/Media/123@s.whatsapp.net/uuid123.thumb', 'Whatsapp Databases/.thumbnails/uuid123.thumb')"
        )
        arch_conn.commit()

        # Place the thumbnail in Whatsapp Databases/.thumbnails/
        thumb_dir = tmp_path / "Whatsapp Databases" / ".thumbnails"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\xff\xd9"
        (thumb_dir / "uuid123.thumb").write_bytes(fake_jpeg)

        wa_conn = sqlite3.connect(tmp_path / "ChatStorage.sqlite")
        wa_conn.execute("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZXMPPTHUMBPATH TEXT,
                ZMEDIALOCALPATH TEXT,
                ZFILESIZE INTEGER,
                ZASPECTRATIO FLOAT,
                ZMOVIEDURATION INTEGER,
                ZTITLE TEXT
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEDIAITEM INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE FLOAT,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZSTANZAID TEXT,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER
            )
        """)
        wa_conn.execute("""
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            )
        """)
        wa_conn.execute("CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT)")

        wa_conn.execute("INSERT INTO ZWACHATSESSION VALUES (1, '123@s.whatsapp.net', 'Bob', NULL)")
        # Missing media item with thumb path
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/123@s.whatsapp.net/uuid123.thumb', 'Media/123@s.whatsapp.net/missing.jpg', 1234, 1.0, 0, NULL)"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 1000.0, 1, NULL, '123@s.whatsapp.net', 'S1', 'Bob', NULL, NULL)"
        )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=True)
        client = app.test_client()

        res = client.get("/api/messages?chat_id=1&chat_type=contact")
        assert res.status_code == 200
        msgs = {m["msg_id"]: m for m in res.get_json()}
        assert msgs[1]["has_thumbnail"] == 1

        t1 = client.get("/api/thumbnail/1")
        assert t1.status_code == 200
        assert t1.content_type == "image/jpeg"
        assert t1.data == fake_jpeg
        assert "immutable" in t1.headers.get("Cache-Control", "")


class TestIncrementViewerMessages:
    """Verify viewer displays messages ingested from incremental backups."""

    def test_increment_messages_display_with_lid_and_media(self, tmp_path):
        """Messages added to msgstore with LID remote_jid and null file_path appear in /api/messages."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)

        # Base contact and chat
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '5550100', '5550100@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (2, '100200300', '100200300@lid')"
        )
        wa_conn.execute("INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 1)")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (1, 1, NULL, 0, 1729000000000, 100)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (100, 1, 0, 1, 1729000000000, 'Base message', 0)"
        )
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('5550100', '+5550100', 'Alice')"
        )
        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        # Step 1: Open viewer and fetch initial messages to populate recent_messages cache
        app1 = viewer.create_app(tmp_path, rescan=False)
        with app1.test_client() as client1:
            resp1 = client1.get("/api/messages?chat_id=5550100&chat_type=contact")
            assert resp1.status_code == 200
            data1 = resp1.get_json()
            assert len(data1) == 1
            assert data1[0]["text_body"] == "Base message"

        # Step 2: Incremental backup replay simulates new messages: text message & media message
        wa_conn2 = sqlite3.connect(str(wa_path))
        wa_conn2.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (101, 1, 0, 2, 1729000050000, 'Increment text via LID', 0)"
        )
        wa_conn2.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (102, 1, 0, 2, 1729000060000, 'Increment photo', 1)"
        )
        wa_conn2.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (102, NULL, 'Increment photo')"
        )
        wa_conn2.execute("UPDATE chat SET sort_timestamp = 1729000060000 WHERE _id = 1")
        wa_conn2.commit()
        wa_conn2.close()

        # Ensure mtime is visibly newer than initial scan
        st = os.stat(str(wa_path))
        os.utime(str(wa_path), (st.st_atime, st.st_mtime + 5))

        # Step 3: Viewer reopened on updated database. recent_messages cache is invalidated and all 3 messages appear.
        app2 = viewer.create_app(tmp_path, rescan=False)
        with app2.test_client() as client2:
            chats_resp = client2.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            assert chats[0]["id"] == "5550100"
            assert chats[0]["newest_ts"] == 1729000060000

            msgs_resp = client2.get("/api/messages?chat_id=5550100&chat_type=contact")
            assert msgs_resp.status_code == 200
            msgs = msgs_resp.get_json()
            assert len(msgs) == 3
            msg_texts = [m["text_body"] for m in msgs]
            assert "Increment photo" in msg_texts
            assert "Increment text via LID" in msg_texts
            assert "Base message" in msg_texts

            # Check media message classification
            photo_msg = next(m for m in msgs if m["msg_id"] == 102)
            assert photo_msg["media_type"] == "image"
            assert photo_msg["sender"] == "Alice"


class TestDeviceSuffixAndCoverageIndex:
    """Verify contact names resolve correctly when JIDs carry :device suffixes,

    and verify messages load using index on chat_row_id without full scans.
    """

    def test_contact_chat_with_device_suffix_displays_and_opens(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        arc_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '393401234567:0', '393401234567:0@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, NULL, 0, 1729000000000, 10)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (10, 1, 0, 1, 1729000000000, 'Hello contact', 0)"
        )
        wa_conn.commit()
        wa_conn.close()

        arc_conn = make_archive_db(arc_path)
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393401234567', '+393401234567', 'Marco')"
        )
        arc_conn.commit()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats = chats_resp.get_json()
            assert len(chats) == 1
            # Canonical ID is stripped of :0 and display_name is matched to contact list
            assert chats[0]["id"] == "393401234567"
            assert chats[0]["display_name"] == "Marco"

            # Opening with clean canonical ID works immediately
            msgs_resp1 = client.get("/api/messages?chat_id=393401234567&chat_type=contact")
            assert msgs_resp1.status_code == 200
            msgs1 = msgs_resp1.get_json()
            assert len(msgs1) == 1
            assert msgs1[0]["sender"] == "Marco"

            # Opening with legacy dirty ID also works immediately without hanging
            msgs_resp2 = client.get("/api/messages?chat_id=393401234567:0&chat_type=contact")
            assert msgs_resp2.status_code == 200
            msgs2 = msgs_resp2.get_json()
            assert len(msgs2) == 1
            assert msgs2[0]["sender"] == "Marco"

    def test_group_chat_scroll_past_device_suffix_retains_contact_name(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        arc_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (100, 'group1', 'group1@g.us')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '393401234567', '393401234567@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (2, '393401234567:0', '393401234567:0@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (10, 100, 'Test Group', 0, 1729000020000, 12)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (11, 10, 0, 1, 1729000010000, 'Older message with clean JID', 0)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (12, 10, 0, 2, 1729000020000, 'Newer message with device suffix', 0)"
        )
        wa_conn.commit()
        wa_conn.close()

        arc_conn = make_archive_db(arc_path)
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393401234567', '+393401234567', 'Marco')"
        )
        arc_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('10', 'Test Group', 'Test Group')"
        )
        arc_conn.commit()
        arc_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=group")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            # Both older and newer messages must resolve to Marco, never lost or replaced by WhatsApp ID
            for m in msgs:
                assert m["sender"] == "Marco"

    def test_android_chat_filter_query_plan_uses_index(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON message (chat_row_id, timestamp)"
        )
        wa_conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _jid_map_resolved (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER)"
        )
        wa_conn.commit()

        pred, params = viewer._android_chat_filter("393401234567", "contact")
        plan = wa_conn.execute(
            f"EXPLAIN QUERY PLAN SELECT m._id FROM message m WHERE {pred} ORDER BY m.timestamp DESC LIMIT 50",
            params,
        ).fetchall()
        wa_conn.close()

        plan_str = " ".join(" ".join(str(val) for val in tuple(row)) for row in plan)
        # Verifies the query plan uses index seek on chat_row_id rather than full SCAN on message
        assert "idx_msg_chat_ts" in plan_str or "SEARCH" in plan_str


class TestLidAndContactResolution:
    def test_api_chats_lid_resolves_contact_name_without_jid_map(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                account_jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '103624838274932', 'lid', '103624838274932@lid');
            INSERT INTO jid VALUES (2, '393401234567', 's.whatsapp.net', '393401234567@s.whatsapp.net');

            -- Chat uses LID as jid_row_id, but account_jid_row_id links to phone JID. jid_map is EMPTY.
            INSERT INTO chat VALUES (10, 1, 2, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 10, 0, 1, 1700000000000, 'Ciao!', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393401234567', 'Marco Folder', 'Marco Rossi')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 1
            assert chats[0]["id"] == "393401234567"
            assert chats[0]["display_name"] == "Marco Rossi"

    def test_api_chats_deduplicates_phone_and_lid_chats(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                account_jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '103624838274932', 'lid', '103624838274932@lid');
            INSERT INTO jid VALUES (2, '393401234567', 's.whatsapp.net', '393401234567@s.whatsapp.net');

            -- Legacy phone chat (Chat 5) and newer LID chat (Chat 10)
            INSERT INTO chat VALUES (5, 2, NULL, NULL, 0, 1600000000000, 1);
            INSERT INTO message VALUES (1, 5, 0, 2, 1600000000000, 'Old message', 0);

            INSERT INTO chat VALUES (10, 1, 2, NULL, 0, 1700000000000, 2);
            INSERT INTO message VALUES (2, 10, 0, 1, 1700000000000, 'New message', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393401234567', 'Marco Folder', 'Marco Rossi')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            # Must deduplicate into exactly 1 chat
            assert len(chats) == 1
            assert chats[0]["id"] == "393401234567"
            assert chats[0]["display_name"] == "Marco Rossi"

    def test_contacts_resolved_bridges_national_and_international(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            -- WhatsApp has international E.164: 393401234567
            INSERT INTO jid VALUES (1, '393401234567', 's.whatsapp.net', '393401234567@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 1, 0, 1, 1700000000000, 'Test', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        # Address book saved contact without international country code: '3401234567'
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('3401234567', 'Marco Folder', 'Marco National')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 1
            # Suffix bridging should match Marco National
            assert chats[0]["display_name"] == "Marco National"

    def test_api_chats_group_resolves_chat_subject_from_msgstore(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '120363028374928192', 'g.us', '120363028374928192@g.us');
            INSERT INTO chat VALUES (10, 1, 'Project Team Alpha', 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 10, 0, 1, 1700000000000, 'Welcome to the team', 0);
        """)
        conn.commit()
        conn.close()

        # Archive DB has NO entry in groups table for this group
        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 1
            chat = chats[0]
            assert chat["type"] == "group"
            assert chat["id"] == "10"
            assert chat["display_name"] == "Project Team Alpha"

    def test_api_chats_unmapped_lid_fallback_no_fake_phone(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            -- Unmapped LID: no entry in jid_map, no account_jid_row_id, no lid_display_name
            INSERT INTO jid VALUES (1, '108394829384729', 'lid', '108394829384729@lid');
            INSERT INTO chat VALUES (5, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 5, 0, 1, 1700000000000, 'Hey there', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 1
            chat = chats[0]
            assert chat["type"] == "contact"
            assert chat["id"] == "108394829384729"
            # Must NOT be formatted as fake phone "+108394829384729"
            assert not chat["display_name"].startswith("+108394829384729")
            assert "Unknown Contact (5)" in chat["display_name"]

            msg_resp = client.get("/api/messages?chat_id=108394829384729&chat_type=contact")
            msgs = msg_resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["chat_id"] == "108394829384729"
            assert msgs[0]["text_body"] == "Hey there"

    def test_api_chats_group_with_null_subject_retains_group_type(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            -- Group without subject
            INSERT INTO jid VALUES (1, '120363999999999999', 'g.us', '120363999999999999@g.us');
            INSERT INTO chat VALUES (20, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 20, 0, 1, 1700000000000, 'Group message', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 1
            chat = chats[0]
            assert chat["type"] == "group"
            assert chat["id"] == "20"

    def test_api_chats_excludes_1to1_shadow_chat_with_null_text(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            -- 1-to-1 contact chat with a phantom message (type = 0, text_data = NULL, no media)
            INSERT INTO jid VALUES (1, '393926739354', 's.whatsapp.net', '393926739354@s.whatsapp.net');
            INSERT INTO chat VALUES (10, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 10, 0, 1, 1700000000000, NULL, 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 0

    def test_api_chats_excludes_1to1_shadow_chat_with_system_messages_only(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 1,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 7
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            -- 1-to-1 contact chat with only system message (type = 7)
            INSERT INTO jid VALUES (1, '393926739354', 's.whatsapp.net', '393926739354@s.whatsapp.net');
            INSERT INTO chat VALUES (10, 1, NULL, 1, 1700000000000, NULL);
            INSERT INTO message VALUES (1, 10, 0, 1, 1700000000000, NULL, 7);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 0

    def test_api_chats_masked_lid_display_name_ignored(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE lid_display_name (lid_row_id INTEGER NOT NULL PRIMARY KEY, display_name TEXT NOT NULL, username TEXT);

            -- Unmapped LID chat with masked phone string in lid_display_name
            INSERT INTO jid VALUES (1, '47386424570094', 'lid', '47386424570094@lid');
            INSERT INTO chat VALUES (3879, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 3879, 0, 1, 1700000000000, 'Valid conversational message', 0);
            INSERT INTO lid_display_name VALUES (1, '+39\u2219\u2219\u2219\u2219\u2219\u2219\u2219\u221995', NULL);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            chats = resp.get_json()
            assert len(chats) == 1
            chat = chats[0]
            assert "\u2219" not in chat["display_name"]
            assert chat["display_name"] == "Unknown Contact (3879)"


class TestAndroidHdAndMediaMatching:
    def test_android_hd_media_dedup_and_clean_sender(self, tmp_path):
        def seed(wa, arch, root):
            wa.execute("INSERT INTO jid (_id, user) VALUES (2, 'group123')")
            wa.execute("INSERT INTO jid (_id, user) VALUES (3, '5551234.0:1')")
            wa.execute(
                "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
                "VALUES (20, 2, 'Family Group', 0, 1700000002000, 102)"
            )
            arch.execute(
                "INSERT INTO contacts (number, folder, display_name) VALUES ('5551234', 'Alice Folder', 'Alice')"
            )

            # Message 101: LQ parent message (no archived copy)
            wa.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (101, 20, 0, 3, 1700000001000, '', 1)"
            )
            wa.execute(
                "INSERT INTO message_media (message_row_id, file_path) VALUES (101, 'Media/WhatsApp Images/lq.jpg')"
            )

            # Message 102: HD child message (archived copy exists)
            wa.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (102, 20, 0, 3, 1700000002000, 'HD Photo', 1)"
            )
            wa.execute(
                "INSERT INTO message_media (message_row_id, file_path) VALUES (102, 'Media/WhatsApp Images/hd.jpg')"
            )

            # Association table linking 101 -> 102 (type 12 for HD photo)
            wa.execute(
                "CREATE TABLE message_association (parent_message_row_id INTEGER, child_message_row_id INTEGER, association_type INTEGER)"
            )
            wa.execute("INSERT INTO message_association VALUES (101, 102, 12)")

            # Archive DB has copy for HD child only
            arch.execute(
                "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Images/hd.jpg', x'1234')"
            )
            arch.execute(
                "INSERT INTO archive_copies (original_path, archive_path) "
                "VALUES ('Media/WhatsApp Images/hd.jpg', 'Family/2026/hd.jpg')"
            )
            wa.commit()
            arch.commit()

        app = _make_android_app(tmp_path, seed_fn=seed)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=20&chat_type=group")
            assert resp.status_code == 200
            data = resp.get_json()
            msg_ids = [m["msg_id"] for m in data]
            # Parent 101 must be deduplicated; only child 102 returned
            assert 101 not in msg_ids
            assert 102 in msg_ids
            child_msg = next(m for m in data if m["msg_id"] == 102)
            assert child_msg["archive_path"] == "Family/2026/hd.jpg"
            # Multi-device/agent suffix .0:1 stripped cleanly and matched to contact display_name
            assert child_msg["sender"] == "Alice"

            # Check /api/media deduplication
            resp_media = client.get("/api/media?chat_id=20&chat_type=group&types=image")
            assert resp_media.status_code == 200
            media_data = resp_media.get_json()
            media_ids = [m["msg_id"] for m in media_data]
            assert 101 not in media_ids
            assert 102 in media_ids

    def test_android_media_path_resilient_matching_and_recent_refresh(self, tmp_path):
        def seed(wa, arch, root):
            # Message 201: file_path has 'WhatsApp/Media/WhatsApp Images/photo1.jpg'
            # archive_copies has 'Media/WhatsApp Images/photo1.jpg'
            wa.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (201, 10, 0, 1, 1700000003000, '', 1)"
            )
            wa.execute(
                "INSERT INTO message_media (message_row_id, file_path) VALUES (201, 'WhatsApp/Media/WhatsApp Images/photo1.jpg')"
            )

            # Message 202: file_path has 'Media/WhatsApp Images/photo2.jpg'
            # archive_copies has Windows backslashes 'WhatsApp\\Media\\WhatsApp Images\\photo2.jpg'
            wa.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
                "VALUES (202, 10, 0, 1, 1700000004000, '', 1)"
            )
            wa.execute(
                "INSERT INTO message_media (message_row_id, file_path) VALUES (202, 'Media/WhatsApp Images/photo2.jpg')"
            )

            arch.execute(
                "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Images/photo1.jpg', x'1111')"
            )
            arch.execute(
                "INSERT INTO archive_copies (original_path, archive_path) "
                "VALUES ('Media/WhatsApp Images/photo1.jpg', 'Contacts/123456789/photo1.jpg')"
            )

            arch.execute(
                "INSERT INTO files (original_path, md5) VALUES (?, x'2222')",
                (r"WhatsApp\Media\WhatsApp Images\photo2.jpg",),
            )
            arch.execute(
                "INSERT INTO archive_copies (original_path, archive_path) VALUES (?, ?)",
                (r"WhatsApp\Media\WhatsApp Images\photo2.jpg", "Contacts/123456789/photo2.jpg"),
            )

            # Pre-seed recent_messages cache with archive_path = None for msg 201 & 202 to test live refresh
            arch.execute(
                "INSERT INTO recent_messages (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body, quoted_text, quoted_sender, quoted_ts) "
                "VALUES ('123456789', 'contact', 201, 1700000003000, '123456789', 0, NULL, 'image', 'photo1.jpg', '', '', '', 0)"
            )
            arch.execute(
                "INSERT INTO recent_messages (chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body, quoted_text, quoted_sender, quoted_ts) "
                "VALUES ('123456789', 'contact', 202, 1700000004000, '123456789', 0, NULL, 'image', 'photo2.jpg', '', '', '', 0)"
            )
            wa.commit()
            arch.commit()

        app = _make_android_app(tmp_path, seed_fn=seed)
        with app.test_client() as client:
            # 1. Test recent_messages cache refresh when archive_path is NULL
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            m201 = next(m for m in data if m["msg_id"] == 201)
            m202 = next(m for m in data if m["msg_id"] == 202)
            # Both should resolve archive_path despite prefix/backslash variations and stale cache
            assert m201["archive_path"] == "Contacts/123456789/photo1.jpg"
            assert m202["archive_path"] == "Contacts/123456789/photo2.jpg"

            # 2. Test live _ANDROID_SELECT query (before=...)
            resp_live = client.get(
                "/api/messages?chat_id=123456789&chat_type=contact&before=1700000005000"
            )
            assert resp_live.status_code == 200
            data_live = resp_live.get_json()
            m201_live = next(m for m in data_live if m["msg_id"] == 201)
            m202_live = next(m for m in data_live if m["msg_id"] == 202)
            assert m201_live["archive_path"] == "Contacts/123456789/photo1.jpg"
            assert m202_live["archive_path"] == "Contacts/123456789/photo2.jpg"

    def test_contacts_resolved_extracts_name_from_folder_when_display_name_null(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            INSERT INTO jid VALUES (1, '393806446839', 's.whatsapp.net', '393806446839@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 1, 0, 1, 1700000000000, 'Ciao', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        # display_name is empty; name is embedded in folder
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393806446839', 'Martina Castorina (00393806446839)', '')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) == 1
            # Clean contact name extracted from folder
            assert chats[0]["display_name"] == "Martina Castorina"

            # Check messages sender inside the chat
            m_resp = client.get("/api/messages?chat_id=393806446839&chat_type=contact")
            assert m_resp.status_code == 200
            msgs = m_resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["sender"] == "Martina Castorina"

    def test_ensure_jid_map_resolved_prevents_inverted_phone_to_lid_mapping(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER,
                account_jid_row_id INTEGER,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL DEFAULT 0,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, sender_jid_row_id INTEGER, text_data TEXT, timestamp INTEGER, from_me INTEGER);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);

            -- LID: id 10, Phone: id 20
            INSERT INTO jid VALUES (10, '187123453968504', 'lid', '187123453968504@lid');
            INSERT INTO jid VALUES (20, '393454408875', 's.whatsapp.net', '393454408875@s.whatsapp.net');

            -- Chat where jid_row_id is the phone (20) and account_jid_row_id is the LID (10)
            INSERT INTO chat VALUES (100, 20, 10, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 100, 0, 20, 1700000000000, 'Test message', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('393454408875', 'Agata Folder', 'Agata')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) == 1
            # Must resolve to phone number and contact display name, NOT the raw LID
            assert chats[0]["id"] == "393454408875"
            assert chats[0]["display_name"] == "Agata"

    def test_ios_1_to_1_chat_resolves_partner_name_over_stale_lid_folder(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZCONTACTIDENTIFIER TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER NOT NULL,
                ZMESSAGEDATE REAL,
                ZTEXT TEXT,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZISFROMME INTEGER DEFAULT 0,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZFROMJID TEXT,
                ZPUSHNAME TEXT,
                ZPARENTMESSAGE INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );

            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZGROUPINFO, ZHIDDEN)
            VALUES (10, '123456789012345@lid', '393001112223@s.whatsapp.net', 'Synthetic Contact', NULL, 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZISFROMME)
            VALUES (1, 10, 1000.0, 'Hello', 0, 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        # Simulate stale archiver folder with raw LID
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('123456789012345', 'Unknown (00123456789012345)', '')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) == 1
            # Never show the LID or the stale 'Unknown (00...)' folder; show the partner name!
            assert chats[0]["display_name"] == "Synthetic Contact"
            assert "123456789012345" not in chats[0]["display_name"]

            # Check /api/chat_details for this contact chat
            info_resp = client.get("/api/chat_info?chat_id=10&chat_type=contact")
            assert info_resp.status_code == 200
            info = info_resp.get_json()
            assert info["display_name"] == "Synthetic Contact"
            assert info["number"] == "393001112223"
            assert "123456789012345" not in info["display_name"]

    def test_ios_1_to_1_chat_resolves_phone_number_when_no_partner_name(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZCONTACTIDENTIFIER TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER NOT NULL,
                ZMESSAGEDATE REAL,
                ZTEXT TEXT,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZISFROMME INTEGER DEFAULT 0,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZFROMJID TEXT,
                ZPUSHNAME TEXT,
                ZPARENTMESSAGE INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );

            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZGROUPINFO, ZHIDDEN)
            VALUES (10, '123456789012345@lid', '393001112223@s.whatsapp.net', '', NULL, 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZISFROMME)
            VALUES (1, 10, 1000.0, 'Hello', 0, 0);
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) == 1
            # Displays the resolved phone number formatted with +
            assert chats[0]["display_name"] == "+393001112223"
            assert "123456789012345" not in chats[0]["display_name"]

    def test_ios_unmapped_lid_falls_back_to_unknown_contact(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZCONTACTIDENTIFIER TEXT,
                ZPARTNERNAME TEXT,
                ZGROUPINFO INTEGER,
                ZHIDDEN INTEGER DEFAULT 0
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER NOT NULL,
                ZMESSAGEDATE REAL,
                ZTEXT TEXT,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZISFROMME INTEGER DEFAULT 0,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZFROMJID TEXT,
                ZPUSHNAME TEXT,
                ZPARENTMESSAGE INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZMEMBERJID TEXT
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );

            -- Completely unmapped LID
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZGROUPINFO, ZHIDDEN)
            VALUES (10, '123456789012345@lid', NULL, NULL, NULL, 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZISFROMME)
            VALUES (1, 10, 1000.0, 'Hello', 0, 0);
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) == 1
            # Must never show +123456789012345; falls back to 'Unknown Contact (10)'
            assert chats[0]["display_name"] == "Unknown Contact (10)"


class TestQuotedMessageResolution:
    def test_android_quoted_message_resolves_quoted_msg_id_and_ts(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY,
                user TEXT NOT NULL,
                server TEXT,
                raw_string TEXT NOT NULL
            );
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY,
                jid_row_id INTEGER NOT NULL,
                subject TEXT,
                hidden INTEGER DEFAULT 0,
                sort_timestamp INTEGER,
                display_message_row_id INTEGER
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER DEFAULT 0
            );
            CREATE TABLE message_media (
                _id INTEGER PRIMARY KEY,
                message_row_id INTEGER,
                file_path TEXT,
                media_name TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                parent_message_chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                sender_jid_row_id INTEGER,
                key_id TEXT NOT NULL,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );

            INSERT INTO jid VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net');
            INSERT INTO chat VALUES (10, 1, NULL, 0, 1700000010000, 2);

            -- Message 1: Original message at ts 1700000000000
            INSERT INTO message VALUES (1, 10, 0, 'KEY_ORIG_1', 1, 1700000000000, 'Original text', 0);

            -- Message 2: Reply quoting Message 1, but with timestamp=0 in message_quoted
            INSERT INTO message VALUES (2, 10, 1, 'KEY_REPLY_2', 1, 1700000010000, 'Reply text', 0);
            INSERT INTO message_quoted VALUES (2, 10, 10, 0, 1, 'KEY_ORIG_1', 0, 0, 'Original text');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 2)
            assert reply_msg["quoted_msg_id"] == 1
            assert reply_msg["quoted_ts"] == 1700000000000
            assert reply_msg["quoted_text"] == "Original text"

    def test_ios_quoted_message_resolves_quoted_msg_id_and_media_label(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Bob');

            INSERT INTO ZWAMEDIAITEM VALUES (1, 'photo.jpg', 'photo.jpg');

            -- Message 1: Photo message without caption
            INSERT INTO ZWAMESSAGE VALUES (10, 1, 0, 700000000.0, 'STANZA_PHOTO', 1, 1, NULL, NULL, 'Bob', NULL, '15559876543@s.whatsapp.net');

            -- Message 2: Quoted reply pointing to Message 1
            INSERT INTO ZWAMESSAGE VALUES (11, 1, 1, 700000010.0, 'STANZA_REPLY', 0, NULL, NULL, 10, 'You', 'Great shot!', '15559876543@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 11)
            assert reply_msg["quoted_msg_id"] == 10
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)
            assert reply_msg["quoted_text"] == "📷 Photo"


# ---------------------------------------------------------------------------
# Tests: Rich Link Previews (Android and iOS)
# ---------------------------------------------------------------------------


class TestRichLinkPreviews:
    """Verify rich link previews decoding, caching, and serving across endpoints."""

    def test_android_rich_link_previews(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_text (
                message_row_id INTEGER PRIMARY KEY,
                description TEXT,
                page_title TEXT,
                url TEXT,
                matched_text TEXT,
                canonical_url TEXT,
                preview_type INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
        """)
        seed_android_db(wa_conn, make_archive_db(archive_path))
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type)
            VALUES (2, 10, 0, 1, 1700000001000, 'Check this https://github.com', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_text (message_row_id, description, page_title, url, matched_text, canonical_url, preview_type)
            VALUES (2, 'GitHub: Let’s build from here', 'GitHub', 'https://github.com', 'https://github.com', 'https://github.com', 1)
        """)
        wa_conn.execute("""
            INSERT INTO message_thumbnail (message_row_id, thumbnail)
            VALUES (2, X'FFD8FFE000104A464946')
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            link_msg = next(m for m in msgs if m["msg_id"] == 2)
            assert link_msg["link_title"] == "GitHub"
            assert link_msg["link_desc"] == "GitHub: Let’s build from here"
            assert link_msg["link_url"] == "https://github.com"
            assert link_msg["link_preview_type"] == 1
            assert bool(link_msg["has_thumbnail"]) is True

            # Also verify /api/media/links endpoint
            resp_links = client.get("/api/media/links?chat_id=10&chat_type=contact")
            assert resp_links.status_code == 200
            links = resp_links.get_json()
            assert len(links) >= 1
            assert any(item["link_title"] == "GitHub" for item in links)

    def test_ios_rich_link_previews(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Bob');
        """)
        # Wire tag 3: (3 << 3) | 2 = 26 (0x1A), len 14, b"Python website"
        desc_bytes = b"Python website"
        metadata_blob = b"\x1a" + bytes([len(desc_bytes)]) + desc_bytes
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA) VALUES (1, NULL, 'Welcome to Python.org', ?)",
            (metadata_blob,),
        )
        # Message type 7 is link preview on iOS!
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (10, 1, 0, 700000000.0, 'STANZA_LINK_7', 7, 1, NULL, NULL, 'Bob', 'https://www.python.org', '15559876543@s.whatsapp.net')
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            link_msg = msgs[0]
            assert link_msg["msg_id"] == 10
            assert link_msg["media_type"] == "link"
            assert link_msg["link_title"] == "Welcome to Python.org"
            assert link_msg["link_desc"] == "Python website"
            assert link_msg["link_url"] == "https://www.python.org"

            # Also check /api/media/links endpoint
            resp_links = client.get("/api/media/links?chat_id=1&chat_type=contact")
            assert resp_links.status_code == 200
            links = resp_links.get_json()
            assert len(links) == 1
            assert links[0]["link_title"] == "Welcome to Python.org"

    def test_recent_messages_caches_link_preview_info(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_text (
                message_row_id INTEGER PRIMARY KEY,
                description TEXT,
                page_title TEXT,
                url TEXT,
                matched_text TEXT,
                canonical_url TEXT,
                preview_type INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
        """)
        seed_android_db(wa_conn, make_archive_db(archive_path))
        wa_conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type)
            VALUES (2, 10, 0, 1, 1700000001000, 'Check this https://github.com', 0)
        """)
        wa_conn.execute("""
            INSERT INTO message_text (message_row_id, description, page_title, url, matched_text, canonical_url, preview_type)
            VALUES (2, 'GitHub Desc', 'GitHub Title', 'https://github.com', 'https://github.com', 'https://github.com', 1)
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=True)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200

        archive_conn = sqlite3.connect(str(archive_path))
        archive_conn.row_factory = sqlite3.Row
        row = None
        for _ in range(20):
            row = archive_conn.execute("SELECT * FROM recent_messages WHERE msg_id = 2").fetchone()
            if row is not None:
                break
            time.sleep(0.1)
        assert row is not None
        assert row["link_title"] == "GitHub Title"
        assert row["link_desc"] == "GitHub Desc"
        assert row["link_url"] == "https://github.com"
        assert row["link_preview_type"] == 1
        archive_conn.close()

    def test_ios_photo_and_document_captions(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Alice');
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL)
            VALUES
                (1, 'Media/photo.jpg', 'Beautiful Sunset', NULL, 'https://mmg.whatsapp.net/mms/photo.jpg'),
                (2, 'Media/reqs.docx', 'Please review this doc', NULL, 'https://mmg.whatsapp.net/mms/reqs.docx');

            -- Photo with caption (ZMESSAGETYPE=1, ZTEXT is NULL, ZTITLE is caption)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (20, 1, 0, 700000000.0, 'STANZA_PHOTO_20', 1, 1, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Document with caption (ZMESSAGETYPE=8, ZTEXT is filename, ZTITLE is caption)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (21, 1, 0, 700000010.0, 'STANZA_DOC_21', 8, 2, NULL, NULL, 'Alice', 'Requirements_2026.docx', '15559876543@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            photo_msg = next(m for m in msgs if m["msg_id"] == 20)
            doc_msg = next(m for m in msgs if m["msg_id"] == 21)

            assert photo_msg["text_body"] == "Beautiful Sunset"
            assert photo_msg["media_type"] == "image"
            assert photo_msg["link_url"] is None

            assert doc_msg["text_body"] == "Please review this doc"
            assert doc_msg["media_name"] == "Requirements_2026.docx"
            assert doc_msg["media_type"] == "document"
            assert doc_msg["link_url"] is None

    def test_ios_quoted_media_reply_no_url_leak(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Alice');
        """)

        def encode_varint(v):
            res = bytearray()
            while v > 0x7F:
                res.append((v & 0x7F) | 0x80)
                v >>= 7
            res.append(v & 0x7F)
            return bytes(res)

        stanza_bytes = b"STANZA_ORIG_100"
        sender_bytes = b"15559876543@s.whatsapp.net"
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

        im_payload = encode_varint((16 << 3) | 2) + encode_varint(len(fake_jpeg)) + fake_jpeg
        t19_sub3 = encode_varint((3 << 3) | 2) + encode_varint(len(im_payload)) + im_payload

        metadata_blob = (
            encode_varint((5 << 3) | 2)
            + encode_varint(len(stanza_bytes))
            + stanza_bytes
            + encode_varint((6 << 3) | 2)
            + encode_varint(len(sender_bytes))
            + sender_bytes
            + encode_varint((19 << 3) | 2)
            + encode_varint(len(t19_sub3))
            + t19_sub3
        )

        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (10, 'Media/moon.jpg', 'Look at moon', NULL, 'https://mmg.whatsapp.net/mms/moon.jpg')"
        )
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (11, NULL, NULL, ?, 'https://mmg.whatsapp.net/mms/dummy_url')",
            (metadata_blob,),
        )

        # Message 100: Photo
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (100, 1, 0, 700000000.0, 'STANZA_ORIG_100', 1, 10, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net')
        """)

        # Message 101: Reply to photo (ZMESSAGETYPE=0, ZTEXT='With your phone?', ZMEDIAITEM=11)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (101, 1, 1, 700000010.0, 'STANZA_REPLY_101', 0, 11, NULL, NULL, 'Me', 'With your phone?', '15551234567@s.whatsapp.net')
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 101)

            # Quoted metadata should be extracted and linked
            assert reply_msg["quoted_msg_id"] == 100
            assert reply_msg["quoted_sender"] == "Alice"
            assert reply_msg["quoted_has_thumb"] == 1
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)

            # WhatsApp MMS CDN URL must NOT leak as link preview!
            assert reply_msg["link_url"] is None
            assert reply_msg["link_title"] is None
            assert reply_msg["has_thumbnail"] == 0

            # Jump to timestamp via /api/messages/at
            ts_ms = reply_msg["quoted_ts"]
            resp_at = client.get(f"/api/messages/at?chat_id=1&ts={ts_ms}")
            assert resp_at.status_code == 200
            at_msgs = resp_at.get_json()
            assert any(m["msg_id"] == 100 for m in at_msgs)

            # Thumbnail fallback recovery for message 100 from reply quote
            resp_thumb = client.get("/api/thumbnail/100")
            assert resp_thumb.status_code == 200
            assert resp_thumb.data == fake_jpeg

    def test_ios_quoted_photo_reply_older_jump_and_missing_media_redirection(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZTITLE TEXT,
                ZMETADATA BLOB,
                ZMEDIAURL TEXT
            );
            CREATE TABLE ZWAGROUPINFO (
                Z_PK INTEGER PRIMARY KEY,
                ZCREATIONDATE REAL,
                ZCREATORJID TEXT,
                ZPICTUREID TEXT
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZISACTIVE INTEGER
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZISFROMME INTEGER,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Alice');
        """)

        def encode_varint(v):
            res = bytearray()
            while v > 0x7F:
                res.append((v & 0x7F) | 0x80)
                v >>= 7
            res.append(v & 0x7F)
            return bytes(res)

        stanza_bytes = b"STANZA_OLDER_PHOTO_50"
        sender_bytes = b"15559876543@s.whatsapp.net"
        caption_bytes = b"Stunning sunset in the mountains"
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

        # Protobuf submessage for image (field 3) containing thumbnail and caption (subfield 3)
        t3_caption = encode_varint((3 << 3) | 2) + encode_varint(len(caption_bytes)) + caption_bytes
        im_payload = (
            encode_varint((16 << 3) | 2) + encode_varint(len(fake_jpeg)) + fake_jpeg + t3_caption
        )
        t19_sub3 = encode_varint((3 << 3) | 2) + encode_varint(len(im_payload)) + im_payload

        reply_metadata = (
            encode_varint((5 << 3) | 2)
            + encode_varint(len(stanza_bytes))
            + stanza_bytes
            + encode_varint((6 << 3) | 2)
            + encode_varint(len(sender_bytes))
            + sender_bytes
            + encode_varint((19 << 3) | 2)
            + encode_varint(len(t19_sub3))
            + t19_sub3
        )

        # Message 50: Photo with caption whose local media file is missing on device (ZMEDIALOCALPATH is NULL)
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (50, NULL, 'Stunning sunset in the mountains', NULL, 'https://mmg.whatsapp.net/mms/photo50.enc')"
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (50, 1, 0, 700000000.0, 'STANZA_OLDER_PHOTO_50', 1, 50, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net')
        """)

        # Add 60 intervening text messages so Message 50 is older than the default 50-message window
        for i in range(1, 61):
            conn.execute(f"""
                INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
                VALUES ({100 + i}, 1, 0, {700000000.0 + i * 10}, 'STANZA_{100 + i}', 0, NULL, NULL, NULL, 'Alice', 'Intervening message {i}', '15559876543@s.whatsapp.net')
            """)

        # Message 200: Recent reply quoting the older photo (Message 50)
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (200, NULL, NULL, ?, 'https://mmg.whatsapp.net/mms/reply.enc')",
            (reply_metadata,),
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (200, 1, 1, 700001000.0, 'STANZA_REPLY_200', 0, 200, NULL, NULL, 'Me', 'Where was this taken?', '15551234567@s.whatsapp.net')
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Fetch recent messages (Message 50 is older and thus not in the initial 50 window)
            resp = client.get("/api/messages?chat_id=1&chat_type=contact&limit=50")
            assert resp.status_code == 200
            recent_msgs = resp.get_json()
            assert len(recent_msgs) == 50
            assert not any(m["msg_id"] == 50 for m in recent_msgs)

            reply_msg = next(m for m in recent_msgs if m["msg_id"] == 200)
            assert reply_msg["quoted_msg_id"] == 50
            assert reply_msg["quoted_has_thumb"] == 1
            assert "Stunning sunset in the mountains" in reply_msg["quoted_text"]
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)

            # 2. Redirect/jump via /api/messages/at at the quoted message timestamp
            target_ts = reply_msg["quoted_ts"]
            resp_at = client.get(f"/api/messages/at?chat_id=1&chat_type=contact&ts={target_ts}")
            assert resp_at.status_code == 200
            at_msgs = resp_at.get_json()

            # Message 50 must NOT be dropped by _BASE_IOS_FILTER despite ZMEDIALOCALPATH being NULL
            target_msg = next((m for m in at_msgs if m["msg_id"] == 50), None)
            assert target_msg is not None
            assert target_msg["media_type"] == "image"
            assert target_msg["text_body"] == "Stunning sunset in the mountains"
            assert target_msg["archive_path"] is None

    def test_android_quoted_photo_reply_thumbnail_and_caption_jump(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                jid_row_id INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0,
                subject TEXT
            );
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                server TEXT NOT NULL,
                raw_string TEXT
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                parent_message_chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                sender_jid_row_id INTEGER,
                key_id TEXT NOT NULL,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_quoted_media (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                file_path TEXT,
                file_size INTEGER,
                media_name TEXT
            );
        """)
        conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO chat (_id, jid_row_id, hidden, subject) VALUES (1, 1, 0, NULL)")

        # Original photo from contact with caption
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (10, 1, 0, 'PHOTO_STANZA_10', 1, 1000000, 1, 'Beautiful lake view')
        """)

        # Generate 55 subsequent messages to push message 10 out of initial 50-message paging window
        for i in range(11, 66):
            conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data) "
                "VALUES (?, 1, 0, ?, 1, ?, 0, ?)",
                (i, f"STANZA_{i}", 1000000 + i * 1000, f"Chat message {i}"),
            )

        # Replying message quoting the photo
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 'STANZA_REPLY_100', NULL, 2000000, 0, 'Where was this lake?')
        """)
        conn.execute("""
            INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, sender_jid_row_id, key_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 0, 1, 'PHOTO_STANZA_10', 1000000, 1, NULL)
        """)
        conn.execute(
            "INSERT INTO message_quoted_media (message_row_id, thumbnail) VALUES (100, ?)",
            (fake_jpeg,),
        )
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. Fetch recent messages
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact&limit=50")
            assert resp.status_code == 200
            recent_msgs = resp.get_json()
            assert not any(m["msg_id"] == 10 for m in recent_msgs)

            reply_msg = next(m for m in recent_msgs if m["msg_id"] == 100)
            assert reply_msg["quoted_msg_id"] == 10
            assert reply_msg["quoted_has_thumb"] == 1
            assert "Beautiful lake view" in reply_msg["quoted_text"]
            assert reply_msg["quoted_ts"] == 1000000

            # 2. Thumbnail endpoint verification
            # Direct hit on replying message holding message_quoted_media
            thumb_reply = client.get("/api/thumbnail/100")
            assert thumb_reply.status_code == 200
            assert thumb_reply.data == fake_jpeg
            # Hit on quoted message bridged via reply
            thumb_orig = client.get("/api/thumbnail/10")
            assert thumb_orig.status_code == 200
            assert thumb_orig.data == fake_jpeg

            # 3. Jump to target timestamp via /api/messages/at
            resp_at = client.get(
                "/api/messages/at?chat_id=15551234567&chat_type=contact&ts=1000000"
            )
            assert resp_at.status_code == 200
            at_msgs = resp_at.get_json()
            target_msg = next((m for m in at_msgs if m["msg_id"] == 10), None)
            assert target_msg is not None
            assert target_msg["media_type"] == "image"
            assert target_msg["text_body"] == "Beautiful lake view"

    def test_android_quoted_photo_reply_thumbnail_on_orig_and_cache_self_heal(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                jid_row_id INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0,
                subject TEXT
            );
            CREATE TABLE jid (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                server TEXT NOT NULL,
                raw_string TEXT
            );
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid_row_id INTEGER,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_quoted (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER NOT NULL,
                parent_message_chat_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                sender_jid_row_id INTEGER,
                key_id TEXT NOT NULL,
                timestamp INTEGER,
                message_type INTEGER,
                text_data TEXT
            );
            CREATE TABLE message_thumbnail (
                message_row_id INTEGER PRIMARY KEY,
                thumbnail BLOB
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                file_path TEXT,
                file_size INTEGER,
                media_name TEXT
            );
        """)
        conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO chat (_id, jid_row_id, hidden, subject) VALUES (1, 1, 0, NULL)")

        # Original photo message with thumbnail in message_thumbnail
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xd9"
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (10, 1, 0, 'PHOTO_STANZA_10', 1, 1000000, 1, 'Mountain Peak')
        """)
        conn.execute(
            "INSERT INTO message_thumbnail (message_row_id, thumbnail) VALUES (10, ?)",
            (fake_jpeg,),
        )

        # Replying text message quoting the photo; message_quoted_media does NOT exist
        conn.execute("""
            INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 'STANZA_REPLY_100', NULL, 2000000, 0, 'Awesome mountain!')
        """)
        conn.execute("""
            INSERT INTO message_quoted (message_row_id, chat_row_id, parent_message_chat_row_id, from_me, sender_jid_row_id, key_id, timestamp, message_type, text_data)
            VALUES (100, 1, 1, 0, 1, 'PHOTO_STANZA_10', 1000000, 1, 'Mountain Peak')
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        # Pre-seed recent_messages with quoted_has_thumb = 0 and 📷 prefix to test self-heal
        archive_conn.execute("""
            INSERT INTO recent_messages (
                chat_id, chat_type, msg_id, timestamp_ms, sender, from_me,
                media_type, text_body, quoted_msg_id, quoted_text,
                quoted_sender, quoted_ts, quoted_has_thumb
            ) VALUES (
                '15551234567', 'contact', 100, 2000000, 'me', 1,
                'text', 'Awesome mountain!', 10, '📷 Mountain Peak',
                'Contact', 1000000, 0
            )
        """)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact&limit=50")
            assert resp.status_code == 200
            msgs = resp.get_json()
            reply = next(m for m in msgs if m["msg_id"] == 100)
            assert reply["quoted_msg_id"] == 10
            assert reply["quoted_has_thumb"] == 1

            # Thumbnail endpoint: both quoted_msg_id and reply_msg_id must resolve
            thumb_quoted = client.get("/api/thumbnail/10")
            assert thumb_quoted.status_code == 200
            assert thumb_quoted.data == fake_jpeg

            thumb_reply = client.get("/api/thumbnail/100")
            assert thumb_reply.status_code == 200
            assert thumb_reply.data == fake_jpeg
