"""
Tests for wab_viewer
"""

import base64
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
    make_ios_db,
    seed_android_db,
)
from wab_viewer import db_cache as viewer_db_cache
from wab_viewer import fts as viewer_fts
from wab_viewer import hydration as viewer_hydration
from wab_viewer import main as viewer
from wab_viewer import preserved as viewer_preserved
from wab_viewer import protobuf as viewer_protobuf
from wab_viewer.chat_viewer.template import HTML_TEMPLATE as VIEWER_HTML_TEMPLATE
from wab_viewer.queries import sql_builder as viewer_sql_builder


def make_cache_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(viewer_db_cache.CACHE_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Tests: _media_type_from_path
# ---------------------------------------------------------------------------


class TestMediaTypeFromPath:
    def test_image_jpg(self):
        assert viewer_hydration._media_type_from_path("foo/bar.jpg") == "image"

    def test_image_png(self):
        assert viewer_hydration._media_type_from_path("foo/IMG.png") == "image"

    def test_video_mp4(self):
        assert viewer_hydration._media_type_from_path("foo/video.mp4") == "video"

    def test_audio_mp3(self):
        assert viewer_hydration._media_type_from_path("foo/voice.mp3") == "audio"

    def test_gif(self):
        assert viewer_hydration._media_type_from_path("foo/anim.gif") == "gif"
        assert (
            viewer_hydration._media_type_from_path(
                "Media/WhatsApp Animated Gifs/VID-20230101-WA0001.mp4"
            )
            == "gif"
        )
        assert viewer_hydration._media_type_from_path("WhatsApp Animated Gifs/GIF-123.mp4") == "gif"
        assert viewer_hydration._media_type_from_path("foo/GIF-123.mp4") == "gif"

    def test_sticker(self):
        assert viewer_hydration._media_type_from_path("foo/Sticker/abc.webp") == "sticker"
        assert viewer_hydration._media_type_from_path("foo/stickers/abc.webp") == "sticker"
        assert (
            viewer_hydration._media_type_from_path("Groups/Chat/2026/STK-20260101-WA0001.webp")
            == "sticker"
        )
        assert (
            viewer_hydration._media_type_from_path("Groups/Chat/2026/STK-20260101-WA0001.was")
            == "sticker"
        )

    def test_document(self):
        assert viewer_hydration._media_type_from_path("foo/document.pdf") == "document"

    def test_none_returns_text(self):
        assert viewer_hydration._media_type_from_path(None) == "text"

    def test_empty_string_returns_document(self):
        # No extension → falls through to "document"
        assert viewer_hydration._media_type_from_path("") == "document"

    def test_no_extension_returns_document(self):
        # No recognized extension → falls through to "document"
        assert viewer_hydration._media_type_from_path("foo/bar") == "document"

    def test_uppercase_extension_normalized(self):
        assert viewer_hydration._media_type_from_path("foo/bar.MP4") == "video"

    def test_path_with_multiple_dots(self):
        # Last extension wins
        assert viewer_hydration._media_type_from_path("foo/file.name.tar.gz") == "document"
        assert viewer_hydration._media_type_from_path("foo/file.name.jpg") == "image"

    def test_mixed_case_extension(self):
        assert viewer_hydration._media_type_from_path("photo.JpG") == "image"


# ---------------------------------------------------------------------------
# Tests: _detect_source_db
# ---------------------------------------------------------------------------


class TestDetectSourceDb:
    def test_android_detected(self, tmp_path):
        (tmp_path / "msgstore.db").touch()
        typ, path = viewer_db_cache._detect_source_db(tmp_path)
        assert typ == "android"
        assert path == str(tmp_path / "msgstore.db")

    def test_ios_detected(self, tmp_path):
        (tmp_path / "ChatStorage.sqlite").touch()
        typ, path = viewer_db_cache._detect_source_db(tmp_path)
        assert typ == "ios"
        assert path == str(tmp_path / "ChatStorage.sqlite")

    def test_android_wins_when_both_present(self, tmp_path):
        (tmp_path / "msgstore.db").touch()
        (tmp_path / "ChatStorage.sqlite").touch()
        typ, path = viewer_db_cache._detect_source_db(tmp_path)
        assert typ == "android"

    def test_none_when_no_source_db(self, tmp_path):
        typ, path = viewer_db_cache._detect_source_db(tmp_path)
        assert typ is None
        assert path is None

    def test_android_detected_in_whatsapp_databases(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        (db_dir / "msgstore.db").touch()
        typ, path = viewer_db_cache._detect_source_db(tmp_path)
        assert typ == "android"
        assert path == str(db_dir / "msgstore.db")

    def test_ios_detected_in_whatsapp_databases(self, tmp_path):
        db_dir = tmp_path / "Whatsapp Databases"
        db_dir.mkdir()
        (db_dir / "ChatStorage.sqlite").touch()
        typ, path = viewer_db_cache._detect_source_db(tmp_path)
        assert typ == "ios"
        assert path == str(db_dir / "ChatStorage.sqlite")


# ---------------------------------------------------------------------------
# Tests: cache schema
# ---------------------------------------------------------------------------


class TestCacheSchema:
    def test_schema_creates_tables(self, tmp_path):
        cache_path = tmp_path / ".wa_viewer.db"
        conn = sqlite3.connect(str(cache_path))
        conn.executescript(viewer_db_cache.CACHE_SCHEMA)

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
        assert "contentless_delete" not in viewer_db_cache.CACHE_SCHEMA

    def test_clear_fts_index_resets_fts_table(self, tmp_path):
        cache_conn = viewer_db_cache._open_cache_db(tmp_path)
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

        viewer_fts._clear_fts_index(cache_conn)

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
        conn = viewer_db_cache._open_cache_db(tmp_path)
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

        monkeypatch.setattr(viewer_db_cache.sqlite3, "connect", mock_connect)
        recovered_conn = viewer_db_cache._open_cache_db(tmp_path)
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
        assert viewer_db_cache._source_changed(cache_conn, str(db_path)) is True

    def test_unchanged_after_stamp_saved(self, tmp_path):
        db_path = tmp_path / "msgstore.db"
        db_path.write_text("x")
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer_db_cache._save_source_stamp(cache_conn, str(db_path))
        assert viewer_db_cache._source_changed(cache_conn, str(db_path)) is False

    def test_changed_after_file_grows(self, tmp_path):
        db_path = tmp_path / "msgstore.db"
        db_path.write_text("x")
        cache_conn = make_cache_db(tmp_path / ".wa_viewer.db")
        viewer_db_cache._save_source_stamp(cache_conn, str(db_path))
        db_path.write_text("xxxx")  # size changed
        assert viewer_db_cache._source_changed(cache_conn, str(db_path)) is True


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
        viewer_fts._build_fts_chat(cache_conn, "android", str(wa_path), "123456789", "contact")

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
        viewer_fts._build_fts_chat(cache_conn, "android", str(wa_path), "123456789", "contact")

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
        viewer_fts._build_fts_chat(cache_conn, "android", str(wa_path), "123456789", "contact")

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

    def test_api_chats_system_stubs_do_not_override_conversational_preview(self, tmp_path):
        """A system stub (e.g. type 12) inserted after the real message must not change
        the preview or the sort order — only genuine conversational messages or whitelisted
        system actions are surfaced."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)  # real message id=1 at ts=1700000000000
        # system event inserted after; must not affect conversational preview or sort
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
        # preview comes from authentic message 1 ('Hello world'), not the system stub
        assert data[0]["last_msg_preview"] == "Hello world"
        assert data[0]["newest_ts"] == 1700000000000

    def test_api_chats_captionless_media_resolves_preview_over_stale_display_message_row_id(
        self, tmp_path
    ):
        """A newer captionless photo must be surfaced as the latest preview even if
        chat.display_message_row_id is stale or pointing to an older text message."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)  # text message id=1 at ts=1700000000000
        # Newer captionless photo at ts=1700050000000
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type) "
            "VALUES (2, 10, 0, 1700050000000, NULL, 1)"
        )
        # Intentionally point display_message_row_id to the older text message 1
        wa_conn.execute("UPDATE chat SET display_message_row_id = 1 WHERE _id = 10")
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chats").get_json()
        assert len(data) == 1
        assert data[0]["last_msg_type"] == "image"
        assert data[0]["last_msg_preview"] == ""
        assert data[0]["newest_ts"] == 1700050000000

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

    def test_api_messages_cursor_before_decimal_string(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get(
            "/api/messages?chat_id=123456789&chat_type=contact&before=1800000000000.5&limit=10"
        )
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_api_messages_cursor_before_invalid_string(self, app_and_tmp):
        client, tmp = app_and_tmp
        resp = client.get(
            "/api/messages?chat_id=123456789&chat_type=contact&before=undefined&limit=10"
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
        viewer_fts._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")

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
        viewer_fts._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")
        viewer_fts._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")

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
        viewer_fts._ensure_chat_indexed(cache_conn, "android", str(wa_path), "123456789", "contact")
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

    def test_ios_media_count_includes_links_without_missing(self, tmp_path):
        """/api/media/count on iOS includes ZMESSAGETYPE=7 links without marking them missing or double-counting."""
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
            -- 1. Archived photo (ZMESSAGETYPE=1)
            INSERT INTO ZWAMEDIAITEM VALUES (20, 'Media/photo.jpg', NULL);
            INSERT INTO ZWAMESSAGE VALUES (100, 10, 0, 1000.0, 'MSG_1', 1, 20, NULL, NULL, 'User1', NULL, '15550001111@s.whatsapp.net');

            -- 2. Link with preview (ZMESSAGETYPE=7)
            INSERT INTO ZWAMEDIAITEM VALUES (21, NULL, 'Example Title');
            INSERT INTO ZWAMESSAGE VALUES (101, 10, 0, 1001.0, 'MSG_2', 7, 21, NULL, NULL, 'User1', 'https://example.com/preview', '15550001111@s.whatsapp.net');

            -- 3. Plain text link (ZMESSAGETYPE=0)
            INSERT INTO ZWAMESSAGE VALUES (102, 10, 0, 1002.0, 'MSG_3', 0, NULL, NULL, NULL, 'User1', 'Check out https://another.com', '15550001111@s.whatsapp.net');
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) "
            "VALUES ('Message/Media/photo.jpg', 'photos/photo.jpg')"
        )
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/media/count?chat_id=10&chat_type=group")
            data = resp.get_json()

        assert data["total"] == 3  # 1 image + 2 links
        assert data["archived"] == 1  # only image is archived
        assert data["by_type"]["image"]["count"] == 1
        assert data["by_type"]["image"]["missing"] == 0
        assert data["by_type"]["link"]["count"] == 2
        assert data["by_type"]["link"]["missing"] == 0  # links cannot be missing

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

    def test_media_documents_excludes_undownloaded_images_and_view_once(self, tmp_path):
        """Undownloaded images (type 1) and view-once media (type 42) must not appear in /api/media/documents."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, message_type) "
            "VALUES (10, 10, 0, 1700000010000, 1), (11, 10, 0, 1700000011000, 42)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name) "
            "VALUES (10, NULL, 'photo.jpg'), (11, NULL, 'vo_photo.jpg')"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            docs = client.get("/api/media/documents?chat_id=123456789&chat_type=contact").get_json()
        assert len(docs) == 0

    def test_gallery_missing_media_card_css_and_warning_badge(self):
        """Gallery missing media cards must use the warning badge, yellow icon, and blurred DB thumbnail."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        app_js_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.js"
        js = app_js_path.read_text(encoding="utf-8")

        assert ".gallery-item.is-missing" in css
        assert ".gallery-missing-badge" in css
        assert ".gallery-missing-icon-warn" in css
        assert "#f59e0b" in css, "Warning yellow color token must be present in app.css"
        assert "buildMissingGalleryCard" in js
        assert "_sanitizeMediaName" in js
        assert "View Once" in js


class TestChatFilter:
    """Tests for _android_chat_filter and _ios_chat_filter."""

    def test_android_contact_filter_uses_jid_user(self):
        pred, params = viewer_sql_builder._android_chat_filter("391234567890", "contact")
        assert "j_chat.user" in pred
        assert params == ["391234567890"]

    def test_android_group_filter_uses_chat_row_id(self):
        pred, params = viewer_sql_builder._android_chat_filter("42", "group")
        assert "m.chat_row_id = CAST" in pred
        assert params == ["42"]

    def test_ios_filter_uses_zchatsession(self):
        pred, params = viewer_sql_builder._ios_chat_filter("99")
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
        viewer_hydration._resolve_and_cache_reactions("android", wa_conn, arc_conn, rows)
        assert rows[0]["reactions"] == "👍"

        cached = arc_conn.execute(
            "SELECT reactions FROM reactions_cache WHERE msg_id = 100"
        ).fetchone()
        assert cached is not None
        assert cached[0] == "👍"

        # Edge cases
        viewer_hydration._resolve_and_cache_reactions("ios", wa_conn, arc_conn, rows)
        viewer_hydration._resolve_and_cache_reactions("android", wa_conn, arc_conn, [])
        viewer_hydration._resolve_and_cache_reactions(
            "android", wa_conn, arc_conn, [{"chat_id": "123"}]
        )

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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
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
        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
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
        members = viewer_hydration._parse_ios_receipt_blob(
            blob, _NoOpConn(), is_group=True, msg_ts_s=msg_ts_s
        )
        assert len(members) == 2
        assert members[0]["delivered_ts"] == msg_ts_s * 1000
        assert members[0]["read_ts"] is None
        assert members[1]["delivered_ts"] == (msg_ts_s + 60) * 1000
        assert members[1]["read_ts"] is None

    def test_empty_blob_returns_empty_list(self):
        members = viewer_hydration._parse_ios_receipt_blob(b"", _NoOpConn())
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

        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=True)
        assert len(members) == 1
        assert members[0]["read_ts"] == (base_ts + 10) * 1000
        assert members[0]["delivered_ts"] == base_ts * 1000


class TestIosReadWithoutTimestamp:
    """iOS keeps the aggregate read flag (ZMESSAGESTATUS 8) for recent messages but
    no longer stores per-recipient read timestamps; the route surfaces that state."""

    def test_read_status_without_timestamp_is_flagged(self):
        assert viewer_hydration._ios_read_without_timestamp(8, None) is True

    def test_read_status_with_timestamp_is_not_flagged(self):
        # A stored timestamp already conveys the read state.
        assert viewer_hydration._ios_read_without_timestamp(8, 1700000000000) is False

    def test_delivered_status_is_not_flagged(self):
        # Status 6 = delivered, not read.
        assert viewer_hydration._ios_read_without_timestamp(6, None) is False

    def test_sent_status_is_not_flagged(self):
        assert viewer_hydration._ios_read_without_timestamp(1, None) is False


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
        result = viewer_protobuf._extract_ios_reactions(blob)
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
        result = viewer_protobuf._extract_ios_reactions(blob)
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
            result = viewer_protobuf._extract_ios_reactions(blobs[i])
            assert len(result) == 1, f"Failed for {emoji}"
            assert result[0][1] == emoji, f"Expected {emoji}, got {result[0][1]}"

    def test_parse_empty_blob(self):
        result = viewer_protobuf._extract_ios_reactions(b"")
        assert result == []

    def test_parse_no_field7(self):
        # Blob with a different field (e.g. field 1)
        blob = bytes([(1 << 3) | 2, 2, 0x41, 0x42])
        result = viewer_protobuf._extract_ios_reactions(blob)
        assert result == []

    def test_vs16_is_skipped_between_emoji(self):
        # VS16 (efb88f) appears BETWEEN two real emoji — it should not be a separate reaction
        blob = _make_zreceipt_blob_with_reactions(
            [
                ("334142384436", "❤️".encode() + "👏".encode()),
            ]
        )
        result = viewer_protobuf._extract_ios_reactions(blob)
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
        result = viewer_protobuf._extract_ios_reactions(blob)
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
        result = viewer_protobuf._extract_ios_reactions(blob)
        counts = Counter(emoji for _, emoji in result)
        assert counts == Counter({"😂": 3, "❤️": 1})

    def test_protobuf_skips_deprecated_wire_types(self):
        # Build a blob with a deprecated wire type (6) embedded — should not crash
        # Wire type 6 = deprecated start_group; we'll embed it inside field 7
        entry_data = bytes([0x41, 0x42])  # field 8, wire 1 = deprecated
        inner = bytes([0x0A, len(entry_data) + 1]) + bytes([6]) + entry_data
        blob = bytes([(7 << 3) | 2, len(inner)]) + inner
        result = viewer_protobuf._extract_ios_reactions(blob)
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

        result = viewer_hydration._ios_reactions(conn, "10", [2])
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
        result = viewer_hydration._ios_reactions(conn, "10", [2])
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
        template = VIEWER_HTML_TEMPLATE

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
        """Orientation-locked media boxes must scale with Display scale tokens, clamp aspect ratios,
        keep inline video/stickers uncropped, and contain captions/quotes within media width."""
        css_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        tpl_path = Path(_ROOT) / "wab_viewer" / "chat_viewer" / "template.py"
        tpl = tpl_path.read_text(encoding="utf-8")

        assert "Display scale" in tpl, "Settings modal must label the scale control 'Display scale'"

        rule_match = re.search(r"\.msg-media\s+img[^{]*\{([^}]+)\}", css)
        assert rule_match, ".msg-media img rule missing in app.css"
        rule_body = rule_match.group(1)
        assert "width: auto" in rule_body, (
            "Fallback media rule must use width: auto to avoid unguarded horizontal stretch"
        )
        assert "object-fit: contain" in rule_body, (
            "Fallback media rule must use object-fit: contain"
        )

        wrap_match = re.search(r"(?<!-)\.msg-media\s*\{([^}]+)\}", css)
        assert wrap_match, ".msg-media rule missing in app.css"
        wrap_body = wrap_match.group(1)
        assert "max-width: var(--media-w-land)" in wrap_body, (
            "Media container must clamp max-width to var(--media-w-land)"
        )
        assert "width: fit-content" in wrap_body, (
            "Media container must use width: fit-content to prevent empty wasted space"
        )

        assert ".msg-media.is-landscape" in css and "width: var(--media-w-land)" in css
        assert ".msg-media.is-portrait" in css and "width: var(--media-w-port)" in css
        assert "object-fit: cover;" in css
        assert "contain: inline-size;" in css, (
            "Captions and quotes in media bubbles must use contain: inline-size"
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
        from wab_viewer.protobuf import _extract_ios_group_description

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
            "latitude",
            "longitude",
            "location_name",
            "location_address",
            "location_url",
            "live_location_share_duration",
            "is_starred",
            "is_pinned",
            "is_video_note",
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
        lid = "145500000000004"
        blob = _make_ios_reaction_blob(
            [
                {"token": "3A000000000000000001", "lid": lid, "emoji": "❤️".encode()},
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

        members = viewer_hydration._parse_ios_receipt_device_rows(
            infra_rows, _MockConn(), is_group=False
        )
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

        members = viewer_hydration._parse_ios_receipt_device_rows(infra_rows, conn, is_group=True)
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
        viewer_fts._build_fts_chat(cache_conn, "ios", str(wa_path), "10", "group", None)

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

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103000000000001@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103000000000001@lid', 1);
            INSERT INTO ZWAGROUPMEMBER VALUES (2, 10, '15550002222@s.whatsapp.net', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Alice', 'Hello', '103000000000001@lid');
        """)
        conn.commit()
        conn.close()

        # ContactsV2 mapping LID -> phone & name
        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY, ZLID VARCHAR, ZWHATSAPPID VARCHAR, ZFULLNAME VARCHAR
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '103000000000001@lid', '15550001111@s.whatsapp.net', 'Alice Tester');
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

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103000000000002@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103000000000002@lid', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Charlie', 'Hi', '103000000000002@lid');
        """)
        conn.commit()
        conn.close()

        lid_conn = sqlite3.connect(str(lid_path))
        lid_conn.executescript("""
            CREATE TABLE ZWAZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR);
            INSERT INTO ZWAZACCOUNT VALUES (1, '103000000000002@lid', '15550003333');
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

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103000000000003@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103000000000003@lid', 1);
            INSERT INTO ZWAGROUPMEMBER VALUES (2, 10, '103000000000004@lid', 1);
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '103000000000003@lid', 'Mystery Member');
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, 'Push1', 'Hi', '103000000000003@lid');
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
            INSERT INTO jid VALUES (2, '103000000000005', 'lid', '103000000000005@lid');
            INSERT INTO jid VALUES (3, '15550005555', 's.whatsapp.net', '15550005555@s.whatsapp.net');
            INSERT INTO jid VALUES (4, '103000000000006', 'lid', '103000000000006@lid');

            INSERT INTO jid_map VALUES (2, 3);

            INSERT INTO chat VALUES (10, 1, 'Android Group', 0, 1700000000000, 1, 1700000000000);
            INSERT INTO message VALUES (1, 10, 0, 2, 1700000000000, NULL, 7);

            INSERT INTO group_participants VALUES (1, 'group1@g.us', '103000000000005@lid', 0, 0);
            INSERT INTO group_participants VALUES (2, 'group1@g.us', '103000000000006@lid', 0, 0);
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

            INSERT INTO ZWAGROUPINFO VALUES (1, 700000000.0, '103000000000007@lid');
            INSERT INTO ZWACHATSESSION VALUES (10, 1, 'group1@g.us', 'Test Group');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103000000000007@lid', 1);
            -- Push name stored under phone JID in ZWAPROFILEPUSHNAME
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '15550009999@s.whatsapp.net', 'Friendly Dave');
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, NULL, 'Hi', '103000000000007@lid');
        """)
        conn.commit()
        conn.close()

        # ContactsV2 has the LID-to-phone mapping but ZFULLNAME is NULL (unsaved contact)
        c_conn = sqlite3.connect(str(contacts_path))
        c_conn.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (
                Z_PK INTEGER PRIMARY KEY, ZLID VARCHAR, ZWHATSAPPID VARCHAR, ZFULLNAME VARCHAR
            );
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '103000000000007@lid', '15550009999@s.whatsapp.net', NULL);
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
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 10, '103000000000008@lid', 1);
            INSERT INTO ZWAMESSAGE VALUES (1, 10, 0, 700000001.0, 'STANZA1', 0, NULL, 1, NULL, NULL, 'Hi', '103000000000008@lid');
        """)
        conn.commit()
        conn.close()

        lid_conn = sqlite3.connect(str(lid_path))
        lid_conn.executescript("""
            CREATE TABLE ZWAZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, ZPHONENUMBER VARCHAR);
            INSERT INTO ZWAZACCOUNT VALUES (1, '103000000000008@lid', '15550008888');
        """)
        lid_conn.commit()
        lid_conn.close()

        # In arch.contacts, build_ios_pushname_map stored the push name keyed by the LID string
        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('103000000000008', 'Dave Folder', 'Archived Friendly Dave')"
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
            INSERT INTO jid VALUES (3, '103000000000001', 'lid', '103000000000001@lid');
            INSERT INTO jid VALUES (4, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO jid VALUES (5, '103000000000002', 'lid', '103000000000002@lid');
            INSERT INTO jid VALUES (6, '15550002222', 's.whatsapp.net', '15550002222@s.whatsapp.net');
            INSERT INTO jid VALUES (7, '103000000000003', 'lid', '103000000000003@lid');
            INSERT INTO jid VALUES (8, '15550003333', 's.whatsapp.net', '15550003333@s.whatsapp.net');
            INSERT INTO jid VALUES (9, '103000000000004', 'lid', '103000000000004@lid');
            INSERT INTO jid VALUES (10, '103000000000005', 'lid', '103000000000005@lid');

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

            INSERT INTO jid VALUES (1, '103000000000010', 'lid', '103000000000010@lid');
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
                '{"subject": "Alpha Project", "author": "103000000000010@lid"}',
                '103000000000010@lid'
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
                '103000000000010@lid', NULL
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
                1, '103000000000010@lid', '15550001111@s.whatsapp.net',
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

            -- 1. Group created (ZSORT = 2, PK = 100, TS = 700000000.0)
            INSERT INTO ZWAMESSAGE VALUES (
                100, 20, 0, 700000000.0, 6, 12, NULL, NULL, NULL, NULL,
                '{"subject": "Awesome Group", "author": "15550001111@s.whatsapp.net"}',
                'awesomegroup@g.us', '15550009999@s.whatsapp.net', 2
            );
            -- 2. Participant added: Alice added Bob (ZSORT = 3, PK = 101, TS = 700000000.0)
            INSERT INTO ZWAMESSAGE VALUES (
                101, 20, 0, 700000000.0, 6, 2, NULL, 1, NULL, NULL,
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
                1, '103000000000010@lid', '15550001111@s.whatsapp.net',
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
            assert by_id[100]["media_type"] == "service"
            assert by_id[100]["text_body"] == 'Alice Smith created group "Awesome Group"'

            # 2. Alice added Bob Jones (actor in ZTEXT, target in member_jid)
            assert by_id[101]["media_type"] == "service"
            assert by_id[101]["text_body"] == "Alice Smith added Bob Jones"
            assert "Awesome Group" not in by_id[101]["sender"]

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
            # When reversed (chronological order), Group Created (PK 100) comes BEFORE Participant Added (PK 101)
            same_ts_msgs = [
                m
                for m in reversed(msgs)
                if m["timestamp_ms"] == int((700000000.0 + 978307200) * 1000)
            ]
            assert len(same_ts_msgs) == 2
            assert same_ts_msgs[0]["msg_id"] == 100  # Group created (PK = 100)
            assert same_ts_msgs[1]["msg_id"] == 101  # Bob added (PK = 101)

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

            INSERT INTO jid VALUES (1, '103000000000010', 'lid', '103000000000010@lid');
            INSERT INTO jid VALUES (2, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net');
            INSERT INTO jid VALUES (3, '15550002222', 's.whatsapp.net', '15550002222@s.whatsapp.net');
            INSERT INTO jid VALUES (4, 'group1', 'g.us', 'group1@g.us');
            INSERT INTO jid VALUES (5, '103000000000099', 'lid', '103000000000099@lid');
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
            assert "103000000000099" not in by_id[13]["text_body"]
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
            # Most recent message in chat is message 22 ("You left")
            assert chats[0]["last_msg_type"] == "service"
            assert chats[0]["last_msg_preview"] == "You left"

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
                "text_body": "103000000000010@lid",
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

        viewer_fts._stream_fts_rows(raw_rows, cache_conn, 2000, None)

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
        assert viewer_hydration._resolve_ios_jid("", cmap) == "Someone"
        assert viewer_hydration._resolve_ios_jid("0", cmap) == "WhatsApp"
        assert viewer_hydration._resolve_ios_jid("0@s.whatsapp.net", cmap) == "WhatsApp"
        assert viewer_hydration._resolve_ios_jid("12345@s.whatsapp.net", cmap) == "Alice"
        assert viewer_hydration._resolve_ios_jid("99999@s.whatsapp.net", cmap) == "+99999"
        assert viewer_hydration._resolve_ios_jid("55555@lid", cmap) == "Bob"
        assert viewer_hydration._resolve_ios_jid("12345", cmap) == "Alice"
        assert viewer_hydration._resolve_ios_jid("77777@s.whatsapp.net", cmap) == "Charlie"
        assert viewer_hydration._resolve_ios_jid("77777@lid", cmap) == "Charlie"
        assert viewer_hydration._resolve_ios_jid("33333@s.whatsapp.net", cmap) == "+33333"
        assert viewer_hydration._resolve_ios_jid("44444@lid", cmap) == "Unknown"
        assert viewer_hydration._resolve_ios_jid("1234567890", cmap) == "+1234567890"
        assert viewer_hydration._resolve_ios_jid("random_string", cmap) == "random_string"


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

        # Preserved-only archive validation passes even if live msgstore is absent
        (tmp_path / "msgstore.db").unlink()
        (tmp_path / "preserved_messages.db").touch()
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
        conn = viewer_db_cache._open_cache_db(tmp_path)
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
        cache_conn = viewer_db_cache._open_cache_db(tmp_path)
        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        viewer_db_cache._run_db_maintenance(cache_conn, arc_conn)
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
        viewer_hydration._backfill_recent_messages(arc_conn, rows)
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
            with patch("wab_viewer.routes.messages._parse_ios_receipt_blob") as mock_parse:
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
            with patch("wab_viewer.routes.messages._parse_ios_receipt_blob") as mock_parse:
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
        val, pos = viewer_protobuf._read_varint(b"\x96\x01", 0)
        assert val == 150
        assert pos == 2

    def test_extract_ios_group_description_invalid(self):
        assert viewer_protobuf._extract_ios_group_description("") is None
        assert viewer_protobuf._extract_ios_group_description("not-valid-base64!!!") is None

    def test_extract_ios_group_description_wire_types_and_subfields(self):
        import base64

        sub = bytes([32, 5, 41]) + b"12345678" + bytes([53]) + b"1234" + bytes([26, 4]) + b"Desc"
        top = bytes([16, 2, 25]) + b"abcdefgh" + bytes([37]) + b"abcd" + bytes([10, len(sub)]) + sub
        b64 = "+" + base64.b64encode(top).decode("ascii")
        desc = viewer_protobuf._extract_ios_group_description(b64)
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

        members = viewer_hydration._parse_ios_receipt_blob(blob, _NoOpConn(), is_group=False)
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
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '103000000000008', 'lid', '103000000000008@lid')"
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
            resp = client.get("/api/chat-info/media-size?chat_id=103000000000008&chat_type=contact")
            assert resp.status_code == 200
            assert resp.get_json()["bytes"] == 10


class TestVcardViewer:
    def test_media_type_from_path_vcf(self):
        assert viewer_hydration._media_type_from_path("Contacts/Alice/Bob.vcf") == "vcard"
        assert viewer_hydration._media_type_from_path("Contacts/Alice/Bob.VCF") == "vcard"

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
            assert chats[0]["last_msg_preview"] == "Doctor Bob"

    def test_ios_chat_list_link_and_contact_terminology(self, tmp_path):
        from tests.test_wa_chat_viewer import make_archive_db

        arc_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44111111', 'Alice', 'Alice')"
        )
        arc_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('44222222', 'Bob', 'Bob')"
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
                ZMEDIAURL TEXT,
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
                ZPARENTMESSAGE INTEGER,
                ZFROMJID TEXT,
                ZSTANZAID TEXT,
                ZPUSHNAME TEXT,
                ZGROUPMEMBER INTEGER
            );
        """)
        # Chat 1: last message is a rich link preview (ZMESSAGETYPE = 7, ZMEDIALOCALPATH is NULL)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (1, '44111111@s.whatsapp.net', NULL, 'Alice')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMEDIAURL) VALUES (1, NULL, 'Example Domain', 'https://example.com')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM) VALUES (1, 1, 0, 700000000.0, 7, 'Check this: https://example.com', 1)"
        )

        # Chat 2: last message is a contact card (ZMESSAGETYPE = 4, ZMEDIALOCALPATH is NULL, ZVCARDNAME is Charlie Doctor)
        wa_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZGROUPINFO, ZPARTNERNAME) VALUES (2, '44222222@s.whatsapp.net', NULL, 'Bob')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZVCARDNAME, ZVCARDSTRING) VALUES (2, NULL, NULL, 'Charlie Doctor', 'BEGIN:VCARD\\r\\nFN:Charlie Doctor\\r\\nEND:VCARD\\r\\n')"
        )
        wa_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZTEXT, ZMEDIAITEM) VALUES (2, 2, 0, 700000010.0, 4, NULL, 2)"
        )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            chats_resp = client.get("/api/chats")
            assert chats_resp.status_code == 200
            chats_by_id = {c["id"]: c for c in chats_resp.get_json()}
            assert len(chats_by_id) == 2

            # Chat 1: Link preview must be categorized as 'link' (not 'document')
            assert chats_by_id["1"]["last_msg_type"] == "link"
            assert "https://example.com" in chats_by_id["1"]["last_msg_preview"]

            # Chat 2: Contact card must be categorized as 'vcard' and have contact name
            assert chats_by_id["2"]["last_msg_type"] == "vcard"
            assert chats_by_id["2"]["last_msg_preview"] == "Charlie Doctor"


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

    def test_android_gif_classification_and_gallery(self, tmp_path):
        """Android GIFs (message_type 13, WhatsApp Animated Gifs folder, GIF- prefix, or gif_attribution)
        are classified as 'gif', suppress raw media_name, and are correctly isolated in gallery queries."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        wa_conn.execute("ALTER TABLE message_media ADD COLUMN gif_attribution INTEGER")
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        # 1. Native message_type = 13 GIF
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (101, 10, 0, 1, 1700000010000, NULL, 13)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, gif_attribution) "
            "VALUES (101, 'Media/WhatsApp Animated Gifs/GIF-101.mp4', 'GIF-101.mp4', 0)"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/WhatsApp Animated Gifs/GIF-101.mp4', 'Contacts/Alice/2026/GIF-101.mp4')"
        )

        # 2. WhatsApp Animated Gifs folder path with standard video message_type = 3
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (102, 10, 0, 1, 1700000020000, 'Funny cat', 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, gif_attribution) "
            "VALUES (102, 'Media/WhatsApp Animated Gifs/Sent/VID-20260101-WA0002.mp4', 'VID-20260101-WA0002.mp4', 0)"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/WhatsApp Animated Gifs/Sent/VID-20260101-WA0002.mp4', 'Contacts/Alice/2026/VID-20260101-WA0002.mp4')"
        )

        # 3. Standard regular video (must NOT be classified as gif)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (103, 10, 0, 1, 1700000030000, 'Holiday video', 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, gif_attribution) "
            "VALUES (103, 'Media/WhatsApp Video/VID-20260101-WA0003.mp4', 'VID-20260101-WA0003.mp4', 0)"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/WhatsApp Video/VID-20260101-WA0003.mp4', 'Contacts/Alice/2026/VID-20260101-WA0003.mp4')"
        )

        # 4. GIF identified via gif_attribution > 0 (even with message_type = 3 and generic path)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (104, 10, 0, 1, 1700000040000, NULL, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, gif_attribution) "
            "VALUES (104, 'Media/WhatsApp Video/VID-from-tenor.mp4', 'VID-from-tenor.mp4', 1)"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/WhatsApp Video/VID-from-tenor.mp4', 'Contacts/Alice/2026/VID-from-tenor.mp4')"
        )

        # 5. GIF filename prefix GIF-*.mp4
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (105, 10, 0, 1, 1700000050000, NULL, 3)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, gif_attribution) "
            "VALUES (105, 'Media/WhatsApp Video/GIF-2026-sample.mp4', 'GIF-2026-sample.mp4', 0)"
        )
        archive_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES ('Media/WhatsApp Video/GIF-2026-sample.mp4', 'Contacts/Alice/2026/GIF-2026-sample.mp4')"
        )

        wa_conn.commit()
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=123456789&chat_type=contact")
            assert resp.status_code == 200
            by_id = {m["msg_id"]: m for m in resp.get_json()}

            # Verify msg 101 (native type 13)
            assert by_id[101]["media_type"] == "gif"
            assert by_id[101]["media_name"] == ""

            # Verify msg 102 (WhatsApp Animated Gifs folder)
            assert by_id[102]["media_type"] == "gif"
            assert by_id[102]["media_name"] == ""
            assert by_id[102]["text_body"] == "Funny cat"

            # Verify msg 103 (regular video)
            assert by_id[103]["media_type"] == "video"
            assert by_id[103]["media_name"] == "VID-20260101-WA0003.mp4"

            # Verify msg 104 (gif_attribution > 0)
            assert by_id[104]["media_type"] == "gif"
            assert by_id[104]["media_name"] == ""

            # Verify msg 105 (GIF- prefix)
            assert by_id[105]["media_type"] == "gif"
            assert by_id[105]["media_name"] == ""

            # Gallery: types=gif returns all 4 GIFs, excludes regular video
            gif_resp = client.get("/api/media?chat_id=123456789&chat_type=contact&types=gif")
            assert gif_resp.status_code == 200
            gif_ids = {m["msg_id"] for m in gif_resp.get_json()}
            assert {101, 102, 104, 105}.issubset(gif_ids)
            assert 103 not in gif_ids

            # Gallery: types=video returns msg 103, excludes all GIFs
            vid_resp = client.get("/api/media?chat_id=123456789&chat_type=contact&types=video")
            assert vid_resp.status_code == 200
            vid_ids = {m["msg_id"] for m in vid_resp.get_json()}
            assert 103 in vid_ids
            assert not any(gid in vid_ids for gid in (101, 102, 104, 105))

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
            "(13, 20, 0, 1, 1700000002500, 'Nice photo', 0),"
            "(12, 20, 0, 1, 1700000003000, NULL, 2)"
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
        viewer_hydration._backfill_recent_messages(archive_conn, rows)

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
        parsed = viewer_protobuf._parse_ios_poll_blob(blob)
        assert parsed is not None
        assert parsed["question"] == "Weekend plan?"
        assert parsed["options"] == ["Option A", "Option B"]
        assert parsed["multiple_answers"] is True
        assert len(parsed["votes"]) == 2
        assert parsed["votes"][0]["voter_jid"] == "15551112222@s.whatsapp.net"
        assert parsed["votes"][1]["option_indices"] == [0, 1]

        # Empty/corrupt blob
        assert viewer_protobuf._parse_ios_poll_blob(b"") is None
        assert viewer_protobuf._parse_ios_poll_blob(b"random bytes") is None

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

    def test_live_poll_not_misidentified_as_preserved(self, tmp_path):
        """Verify that a live poll present in msgstore.db is NEVER misidentified as preserved

        or duplicated even if preserved_messages.db has an entry, and results are never lost.
        """
        archive_path = tmp_path / ".wa_media_archiver.db"
        archive_conn = make_archive_db(archive_path)

        wa_path = tmp_path / "msgstore.db"
        wa_conn = sqlite3.connect(str(wa_path))
        wa_conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, key_id TEXT, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, file_path TEXT, media_name TEXT);
            CREATE TABLE message_quoted (message_row_id INTEGER PRIMARY KEY, text_data TEXT, from_me INTEGER DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER DEFAULT 0);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE message_poll (message_row_id INTEGER PRIMARY KEY, selectable_options_count INTEGER);
            CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT, vote_total INTEGER);
            CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id INTEGER, message_add_on_type INTEGER, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_add_on_poll_vote_selected_option (message_add_on_row_id INTEGER, message_poll_option_id INTEGER);
        """)

        # Group Chat
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (1, '15551111111')")
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '15552222222')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) VALUES (1, 1, 'Group Chat', 0, 1700000000000, 10)"
        )

        # Live poll in database with key_id
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, key_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) VALUES (10, 1, 'POLL_STANZA_1', 0, 2, 1700000000000, 'Lunch spot?', 66)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll (message_row_id, selectable_options_count) VALUES (10, 0)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (101, 10, 'Tacos', 2)"
        )
        wa_conn.execute(
            "INSERT INTO message_poll_option (_id, message_row_id, option_name, vote_total) VALUES (102, 10, 'Pizza', 1)"
        )
        wa_conn.commit()

        # Contacts
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15552222222', 'Bob (15552222222)', 'Bob')"
        )
        archive_conn.commit()

        # Now simulate a preserved_messages.db that also contains an entry for this poll
        pres_path = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_path))
        pres_conn.executescript("""
            CREATE TABLE preserved_messages (
                msg_id INTEGER,
                stanza_id TEXT,
                chat_id TEXT,
                chat_type TEXT,
                chat_jid TEXT,
                timestamp_ms INTEGER,
                sender TEXT,
                sender_jid TEXT,
                sender_phone TEXT,
                from_me INTEGER,
                text_body TEXT,
                media_type TEXT,
                media_name TEXT,
                archive_path TEXT,
                original_path TEXT,
                vcard_data TEXT,
                media_width INTEGER,
                media_height INTEGER,
                media_duration INTEGER,
                media_size INTEGER,
                is_view_once INTEGER,
                latitude REAL,
                longitude REAL,
                location_name TEXT,
                location_address TEXT,
                location_url TEXT,
                live_location_share_duration INTEGER,
                quoted_text TEXT,
                quoted_sender TEXT,
                quoted_ts INTEGER,
                quoted_stanza_id TEXT,
                quoted_parent_type TEXT,
                is_edited INTEGER,
                edited_ts INTEGER,
                is_starred INTEGER,
                action_type INTEGER,
                participant_name TEXT,
                preserved_at TEXT,
                preservation_reason TEXT,
                source_platform TEXT,
                reactions TEXT,
                poll_data TEXT,
                is_pinned INTEGER,
                pin_expiry_ts INTEGER,
                service_metadata TEXT
            );
        """)
        pres_conn.execute(
            """INSERT INTO preserved_messages (
                msg_id, stanza_id, chat_id, chat_type, timestamp_ms, sender, from_me,
                text_body, media_type, preservation_reason, poll_data
            ) VALUES (
                10, 'POLL_STANZA_1', '1', 'group', 1700000000000, 'Bob', 0,
                'Lunch spot?', 'poll', 'deleted_on_phone', '{"options": [{"name": "Tacos", "votes": 0}], "total_votes": 0}'
            )"""
        )
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # Initial fetch from live database (and backfill to recent_messages)
        res1 = client.get("/api/messages?chat_id=1&chat_type=group")
        assert res1.status_code == 200
        msgs1 = res1.get_json()
        assert len(msgs1) == 1, "Must NOT duplicate live poll with preserved copy"
        p1 = msgs1[0]
        assert p1["msg_id"] == 10
        assert p1.get("is_preserved") is None or p1.get("is_preserved") == 0
        assert p1["media_type"] == "poll"
        assert p1["poll"]["total_votes"] == 3
        assert len(p1["poll"]["options"]) == 2

        # Second fetch: populate from recent_messages cache directly
        # Wait for background thread backfill if running
        time.sleep(0.1)
        res2 = client.get("/api/messages?chat_id=1&chat_type=group")
        assert res2.status_code == 200
        msgs2 = res2.get_json()
        assert len(msgs2) == 1, "Recent messages cache must NOT leak preserved duplicate"
        p2 = msgs2[0]
        assert p2["msg_id"] == 10
        assert p2.get("is_preserved") is None or p2.get("is_preserved") == 0
        assert p2["poll"]["total_votes"] == 3
        assert len(p2["poll"]["options"]) == 2

        # Test negative ID lookup for poll details (preserved poll details fallback)
        res_neg = client.get("/api/poll_details/-1")
        assert res_neg.status_code == 200
        neg_data = res_neg.get_json()
        assert neg_data["available"] is True
        # Since POLL_STANZA_1 exists in live msgstore.db, it resolved to live details!
        assert len(neg_data["options"]) == 2

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

        pred, params = viewer_sql_builder._android_chat_filter("393401234567", "contact")
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

            INSERT INTO jid VALUES (1, '103000000000099', 'lid', '103000000000099@lid');
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

            INSERT INTO jid VALUES (1, '103000000000099', 'lid', '103000000000099@lid');
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

            INSERT INTO jid VALUES (1, '120363000000000001', 'g.us', '120363000000000001@g.us');
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
            INSERT INTO jid VALUES (1, '145500000000001', 'lid', '145500000000001@lid');
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
            assert chat["id"] == "145500000000001"
            # Must NOT be formatted as fake phone "+145500000000001"
            assert not chat["display_name"].startswith("+145500000000001")
            assert "Unknown Contact (5)" in chat["display_name"]

            msg_resp = client.get("/api/messages?chat_id=145500000000001&chat_type=contact")
            msgs = msg_resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["chat_id"] == "145500000000001"
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
            INSERT INTO jid VALUES (1, '390000000010', 's.whatsapp.net', '390000000010@s.whatsapp.net');
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
            INSERT INTO jid VALUES (1, '390000000010', 's.whatsapp.net', '390000000010@s.whatsapp.net');
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
            INSERT INTO jid VALUES (1, '145500000000003', 'lid', '145500000000003@lid');
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

            INSERT INTO jid VALUES (1, '390000000011', 's.whatsapp.net', '390000000011@s.whatsapp.net');
            INSERT INTO chat VALUES (1, 1, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 1, 0, 1, 1700000000000, 'Ciao', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        # display_name is empty; name is embedded in folder
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('390000000011', 'Alice Contact (00390000000011)', '')"
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
            assert chats[0]["display_name"] == "Alice Contact"

            # Check messages sender inside the chat
            m_resp = client.get("/api/messages?chat_id=390000000011&chat_type=contact")
            assert m_resp.status_code == 200
            msgs = m_resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["sender"] == "Alice Contact"

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
            INSERT INTO jid VALUES (10, '145500000000012', 'lid', '145500000000012@lid');
            INSERT INTO jid VALUES (20, '390000000012', 's.whatsapp.net', '390000000012@s.whatsapp.net');

            -- Chat where jid_row_id is the phone (20) and account_jid_row_id is the LID (10)
            INSERT INTO chat VALUES (100, 20, 10, NULL, 0, 1700000000000, 1);
            INSERT INTO message VALUES (1, 100, 0, 20, 1700000000000, 'Test message', 0);
        """)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('390000000012', 'Diana Folder', 'Diana Contact')"
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
            assert chats[0]["id"] == "390000000012"
            assert chats[0]["display_name"] == "Diana Contact"

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
                ZMEDIAURL TEXT,
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

    def test_ios_link_preview_with_message_data_item(self, tmp_path):
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
                ZMEDIAURL TEXT,
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
            CREATE TABLE ZWAMESSAGEDATAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZINDEX INTEGER,
                ZMESSAGE INTEGER,
                ZTITLE TEXT,
                ZSUMMARY TEXT,
                ZMATCHEDTEXT TEXT,
                ZTHUMBNAILPATH TEXT
            );

            INSERT INTO ZWACHATSESSION VALUES (1, 0, '15559876543@s.whatsapp.net', 'Bob');
        """)
        # Message 10: dedicated link (type 7) with ZWAMESSAGEDATAITEM thumbnail
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA) VALUES (1, NULL, 'Python Main Site', NULL)"
        )
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (10, 1, 0, 700000000.0, 'STANZA_MDI_1', 7, 1, NULL, NULL, 'Bob', 'https://www.python.org', '15559876543@s.whatsapp.net')
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGEDATAITEM (Z_PK, ZINDEX, ZMESSAGE, ZTITLE, ZSUMMARY, ZMATCHEDTEXT, ZTHUMBNAILPATH)
            VALUES (1, 0, 10, 'Python MDI Title', 'Python description from summary', 'https://www.python.org', 'Media/thumb_sample.thumb')
        """)

        # Message 11: plain text (type 0) with inline link extracted into ZWAMESSAGEDATAITEM
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (11, 1, 0, 700000100.0, 'STANZA_MDI_2', 0, NULL, NULL, NULL, 'Bob', 'Visit wa.me/12345 to chat', '15559876543@s.whatsapp.net')
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGEDATAITEM (Z_PK, ZINDEX, ZMESSAGE, ZTITLE, ZSUMMARY, ZMATCHEDTEXT, ZTHUMBNAILPATH)
            VALUES (2, 0, 11, 'WhatsApp Chat', 'Direct chat link', 'https://wa.me/12345', NULL)
        """)
        conn.commit()
        conn.close()

        # Write sample thumbnail file on disk
        media_dir = tmp_path / "Media"
        media_dir.mkdir(parents=True, exist_ok=True)
        fake_jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
            + b"\x00" * 32
            + b"\xff\xd9"
        )
        (media_dir / "thumb_sample.thumb").write_bytes(fake_jpeg)

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2

            m10 = next(m for m in msgs if m["msg_id"] == 10)
            assert m10["media_type"] == "link"
            assert m10["link_title"] == "Python Main Site"
            assert m10["link_desc"] == "Python description from summary"
            assert m10["link_url"] == "https://www.python.org"
            assert m10["has_thumbnail"] == 1

            m11 = next(m for m in msgs if m["msg_id"] == 11)
            assert m11["media_type"] == "link"
            assert m11["link_title"] == "WhatsApp Chat"
            assert m11["link_desc"] == "Direct chat link"
            assert m11["link_url"] == "https://wa.me/12345"

            # Check thumbnail endpoint
            thumb_resp = client.get("/api/thumbnail/10")
            assert thumb_resp.status_code == 200
            assert thumb_resp.mimetype == "image/jpeg"
            assert thumb_resp.data == fake_jpeg

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

    def test_ios_video_audio_classification_and_captions(self, tmp_path):
        """ZMESSAGETYPE=2 is Video with caption in ZTITLE, ZMESSAGETYPE=3 is Audio, ZMESSAGETYPE=54 is Video note."""
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

            INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID, ZPARTNERNAME)
            VALUES (1, NULL, '15559876543@s.whatsapp.net', 'Alice');

            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL)
            VALUES
                (1, 'Media/video.mp4', 'Goal celebration!', NULL, 'https://mmg.whatsapp.net/mms/video.mp4'),
                (2, 'Media/voice_note.opus', NULL, NULL, 'https://mmg.whatsapp.net/mms/voice.opus'),
                (3, 'Media/ptv_note.mp4', NULL, NULL, 'https://mmg.whatsapp.net/mms/ptv.mp4');

            -- Message 30: Video with caption (ZMESSAGETYPE=2, ZTEXT is NULL, ZTITLE is caption)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (30, 1, 0, 700000000.0, 'STANZA_VID_30', 2, 1, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Message 31: Audio voice note (ZMESSAGETYPE=3, ZTEXT is NULL, ZTITLE is NULL)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (31, 1, 0, 700000010.0, 'STANZA_AUD_31', 3, 2, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Message 32: Push-to-video circular note (ZMESSAGETYPE=54)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (32, 1, 0, 700000020.0, 'STANZA_PTV_32', 54, 3, NULL, NULL, 'Alice', NULL, '15559876543@s.whatsapp.net');

            -- Message 33: Reply quoting the video (ZPARENTMESSAGE=30)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (33, 1, 1, 700000030.0, 'STANZA_REPLY_33', 0, NULL, NULL, 30, 'Me', 'Awesome video!', NULL);

            -- Message 34: Reply quoting the audio (ZPARENTMESSAGE=31)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (34, 1, 1, 700000040.0, 'STANZA_REPLY_34', 0, NULL, NULL, 31, 'Me', 'Heard your message', NULL);
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
            assert len(msgs) == 5
            vid_msg = next(m for m in msgs if m["msg_id"] == 30)
            aud_msg = next(m for m in msgs if m["msg_id"] == 31)
            ptv_msg = next(m for m in msgs if m["msg_id"] == 32)
            reply_vid = next(m for m in msgs if m["msg_id"] == 33)
            reply_aud = next(m for m in msgs if m["msg_id"] == 34)

            # Video asserts: type is video and caption is preserved
            assert vid_msg["media_type"] == "video"
            assert vid_msg["text_body"] == "Goal celebration!"

            # Audio asserts: type is audio
            assert aud_msg["media_type"] == "audio"

            # Video note assert: type is video
            assert ptv_msg["media_type"] == "video"

            # Quoted reply previews
            assert "🎥 Video" in reply_vid["quoted_text"]
            assert "🎵 Audio" in reply_aud["quoted_text"]

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


# ---------------------------------------------------------------------------
# Tests: Live Photo & Motion Photo Pairing
# ---------------------------------------------------------------------------


class TestLivePhotoPairing:
    """Verify Android Motion Photo (type 11) and iOS Live Photo (type 12) pairing."""

    def test_android_motion_photo_pairing(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE message (
                _id INTEGER PRIMARY KEY,
                chat_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                text_data TEXT,
                message_type INTEGER
            );
            CREATE TABLE message_media (
                message_row_id INTEGER PRIMARY KEY,
                file_path TEXT
            );
            CREATE TABLE message_association (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                child_message_row_id INTEGER,
                association_type INTEGER
            );

            -- Parent image
            INSERT INTO message VALUES (10, 1, 0, 1700000000000, '', 1);
            INSERT INTO message_media VALUES (10, 'Media/WhatsApp Images/IMG_001.jpg');

            -- Child companion motion clip
            INSERT INTO message VALUES (11, 1, 0, 1700000000100, '', 3);
            INSERT INTO message_media VALUES (11, 'Media/WhatsApp Video/VID_001.mp4');

            -- Association (type 11 = Motion Photo)
            INSERT INTO message_association VALUES (1, 10, 11, 11);
        """)
        conn.commit()

        # Archive DB
        arch_conn = sqlite3.connect(str(archive_path))
        arch_conn.executescript("""
            CREATE TABLE archive_copies (
                original_path TEXT PRIMARY KEY,
                archive_path TEXT
            );
            INSERT INTO archive_copies VALUES ('Media/WhatsApp Video/VID_001.mp4', 'Media/VID_001.mp4');
        """)
        arch_conn.commit()
        arch_conn.close()

        conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path),))

        rows = [
            {"msg_id": 10, "media_type": "image", "archive_path": "Media/IMG_001.jpg"},
            {"msg_id": 11, "media_type": "video", "archive_path": "Media/VID_001.mp4"},
        ]

        viewer_hydration._hydrate_live_photos(rows, "android", conn)

        assert rows[0].get("is_live_photo") == 1
        assert rows[0].get("live_photo_path") == "Media/VID_001.mp4"
        assert rows[1].get("is_live_photo_video") == 1

        conn.close()

    def test_ios_live_photo_pairing(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        ext_path = tmp_path / "ExtChatDatabase.sqlite"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZSTANZAID TEXT,
                ZMEDIAITEM INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT
            );

            INSERT INTO ZWAMESSAGE VALUES (10, 'STANZA_IMG', 1);
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/IMG_001.jpg');

            INSERT INTO ZWAMESSAGE VALUES (11, 'STANZA_VID', 2);
            INSERT INTO ZWAMEDIAITEM VALUES (2, 'Media/VID_001.mp4');
        """)
        conn.commit()

        ext_conn = sqlite3.connect(str(ext_path))
        ext_conn.executescript("""
            CREATE TABLE message_parent_association (
                stanza_id TEXT,
                parent_stanza_id TEXT,
                type INTEGER
            );
            INSERT INTO message_parent_association VALUES ('STANZA_VID', 'STANZA_IMG', 12);
        """)
        ext_conn.commit()
        ext_conn.close()

        arch_conn = sqlite3.connect(str(archive_path))
        arch_conn.executescript("""
            CREATE TABLE archive_copies (
                original_path TEXT PRIMARY KEY,
                archive_path TEXT
            );
            INSERT INTO archive_copies VALUES ('Message/Media/VID_001.mp4', 'Media/VID_001.mp4');
        """)
        arch_conn.commit()
        arch_conn.close()

        conn.execute("ATTACH DATABASE ? AS ext", (str(ext_path),))
        conn.execute("ATTACH DATABASE ? AS arch", (str(archive_path),))

        rows = [
            {"stanza_id": "STANZA_IMG", "msg_id": 10, "media_type": "image"},
            {"stanza_id": "STANZA_VID", "msg_id": 11, "media_type": "video"},
        ]

        viewer_hydration._hydrate_live_photos(rows, "ios", conn)

        assert rows[0].get("is_live_photo") == 1
        assert rows[0].get("live_photo_path") == "Media/VID_001.mp4"
        assert rows[1].get("is_live_photo_video") == 1

        conn.close()


class TestExtendedMediaMap:
    def test_android_extended_media_data_hydration(self, tmp_path):
        """Test that Android messages referencing extended_media_data via message_media_map are hydrated."""
        db_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER, message_type INTEGER, text_data TEXT, from_me INTEGER, key_id TEXT);
            CREATE TABLE message_media (message_row_id INTEGER PRIMARY KEY, file_path TEXT, mime_type TEXT, width INTEGER, height INTEGER, file_size INTEGER, media_name TEXT, media_duration INTEGER);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, text_data TEXT, from_me INTEGER, sender_jid_row_id INTEGER, timestamp INTEGER);
            CREATE TABLE message_media_map (_id INTEGER PRIMARY KEY AUTOINCREMENT, message_row_id INTEGER NOT NULL, chat_row_id INTEGER NOT NULL, media_row_id INTEGER NOT NULL);
            CREATE TABLE extended_media_data (row_id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER NOT NULL, direct_path TEXT, file_path TEXT, file_hash TEXT, file_size INTEGER, media_key BLOB, media_key_timestamp INTEGER, enc_file_hash TEXT, width INTEGER, height INTEGER, media_caption TEXT, transferred INTEGER, preview_path TEXT, external_url TEXT, mime_type TEXT, display_type INTEGER);

            INSERT INTO chat VALUES (1, 1, 'Project Chat');
            INSERT INTO jid VALUES (1, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net');
            INSERT INTO message VALUES (100, 1, 1, 1700000000000, 1, 'Photo message', 0, 'STANZA_EXT_1');
            INSERT INTO message_media_map VALUES (1, 100, 1, 50);
            INSERT INTO extended_media_data (row_id, type, file_path, file_size, width, height, mime_type)
            VALUES (50, 1, 'Media/WhatsApp Images/carousel_item_1.jpg', 98765, 1200, 800, 'image/jpeg');
        """)
        conn.commit()
        conn.close()

        arch_conn = sqlite3.connect(archive_path)
        arch_conn.execute("CREATE TABLE archive_copies (original_path TEXT, archive_path TEXT)")
        arch_conn.execute(
            "INSERT INTO archive_copies VALUES ('Media/WhatsApp Images/carousel_item_1.jpg', 'Archived/carousel_item_1.jpg')"
        )
        arch_conn.commit()
        arch_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=group")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            m = msgs[0]
            assert m["msg_id"] == 100
            assert m["media_type"] == "image"
            assert m["media_width"] == 1200
            assert m["media_height"] == 800
            assert m["media_size"] == 98765
            assert m["archive_path"] == "Archived/carousel_item_1.jpg"

    def test_ios_quoted_extended_text_reply_and_self_reply(self, tmp_path):
        """Test iOS quoted replies referencing extendedTextMessage (URLs) and self-replies without participant tags."""
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

            INSERT INTO ZWACHATSESSION VALUES (1, 1, '120363000000000002@g.us', 'Team Project');
            INSERT INTO ZWAGROUPMEMBER VALUES (10, 1, '145500000000002@lid', 1);
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '145500000000002@lid', 'Bob Tester');
        """)

        def encode_varint(v):
            res = bytearray()
            while v > 0x7F:
                res.append((v & 0x7F) | 0x80)
                v >>= 7
            res.append(v & 0x7F)
            return bytes(res)

        stanza_orig = b"STANZA_LINK_ORIG"
        url_text = b"https://youtu.be/dQw4w9WgXcQ"

        # ExtendedTextMessage (subfield 6 in message container 19)
        # subfield 1: text
        # subfield 2: matched text
        ex_payload = (
            encode_varint((1 << 3) | 2)
            + encode_varint(len(url_text))
            + url_text
            + encode_varint((2 << 3) | 2)
            + encode_varint(len(url_text))
            + url_text
        )
        t19_sub6 = encode_varint((6 << 3) | 2) + encode_varint(len(ex_payload)) + ex_payload

        # Self-reply blob: contains wire tag 5 (q_stanza) and 19 (message), but OMITS tag 6 (participant)
        metadata_self_reply = (
            encode_varint((5 << 3) | 2)
            + encode_varint(len(stanza_orig))
            + stanza_orig
            + encode_varint((19 << 3) | 2)
            + encode_varint(len(t19_sub6))
            + t19_sub6
        )

        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (1, NULL, NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE, ZMETADATA, ZMEDIAURL) VALUES (2, NULL, NULL, ?, NULL)",
            (metadata_self_reply,),
        )

        # Message 50: Original message with YouTube link sent by Bob Tester (ZGROUPMEMBER=10)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (50, 1, 0, 700000000.0, 'STANZA_LINK_ORIG', 7, 1, 10, NULL, 'Bob Tester', 'https://youtu.be/dQw4w9WgXcQ', '120363000000000002@g.us')
        """)

        # Message 51: Self-reply sent by Bob Tester (ZGROUPMEMBER=10) referencing Message 50
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZMEDIAITEM, ZGROUPMEMBER, ZPARENTMESSAGE, ZPUSHNAME, ZTEXT, ZFROMJID)
            VALUES (51, 1, 0, 700000010.0, 'STANZA_SELF_REPLY', 0, 2, 10, NULL, 'Bob Tester', 'Check out this presentation video!', '120363000000000002@g.us')
        """)

        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=group")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 2
            reply_msg = next(m for m in msgs if m["msg_id"] == 51)

            # Quoted text must be extracted from extendedTextMessage (not empty!)
            assert reply_msg["quoted_text"] == "https://youtu.be/dQw4w9WgXcQ"
            # Quoted sender must be inferred from the message sender for self-replies
            assert reply_msg["quoted_sender"] == "Bob Tester"
            assert reply_msg["quoted_msg_id"] == 50
            assert reply_msg["quoted_ts"] == int((700000000.0 + 978307200) * 1000)

    def test_ios_resolve_quoted_msg_ids_backfill_text_and_sender(self, tmp_path):
        """Test _resolve_quoted_msg_ids backfills quoted_text and quoted_sender from live ZWAMESSAGE when empty."""
        wa_path = tmp_path / "ChatStorage.sqlite"
        conn = sqlite3.connect(str(wa_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZSTANZAID TEXT,
                ZMESSAGEDATE REAL,
                ZMESSAGETYPE INTEGER,
                ZTEXT TEXT,
                ZISFROMME INTEGER,
                ZPUSHNAME TEXT,
                ZFROMJID TEXT,
                ZMEDIAITEM INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZTITLE TEXT
            );
            INSERT INTO ZWAMEDIAITEM VALUES (1, 'Caption photo');
            INSERT INTO ZWAMESSAGE VALUES (10, 'STANZA_PARENT_1', 700000000.0, 1, NULL, 0, 'Alice', '1555123456@s.whatsapp.net', 1);
            INSERT INTO ZWAMESSAGE VALUES (20, 'STANZA_PARENT_2', 700000050.0, 0, 'Original text from You', 1, 'You', NULL, NULL);
        """)
        conn.commit()

        # Rows simulating incoming messages where ZMETADATA had quoted_stanza_id but missing text/sender
        rows = [
            {
                "msg_id": 100,
                "stanza_id": "STANZA_CHILD_100",
                "quoted_stanza_id": "STANZA_PARENT_1",
                "quoted_msg_id": None,
                "quoted_text": "",
                "quoted_sender": "",
                "from_me": 0,
                "sender": "Bob",
            },
            {
                "msg_id": 101,
                "stanza_id": "STANZA_CHILD_101",
                "quoted_stanza_id": "STANZA_PARENT_2",
                "quoted_msg_id": None,
                "quoted_text": "",
                "quoted_sender": "",
                "from_me": 1,
                "sender": "You",
            },
            # Row 102 quotes row 101 via local_map window
            {
                "msg_id": 102,
                "stanza_id": "STANZA_CHILD_102",
                "quoted_stanza_id": "STANZA_CHILD_101",
                "quoted_msg_id": None,
                "quoted_text": "",
                "quoted_sender": "",
                "from_me": 0,
                "sender": "Charlie",
                "text_body": "Replying to 101",
            },
        ]

        # Give row 101 a text_body so row 102 can backfill from local_map
        rows[1]["text_body"] = "I am message 101"

        viewer_preserved._resolve_quoted_msg_ids(rows, "ios", conn, None)

        # Row 100 resolved against ZWAMESSAGE (parent 10)
        assert rows[0]["quoted_msg_id"] == 10
        assert rows[0]["quoted_text"] == "Caption photo"
        assert rows[0]["quoted_has_thumb"] == 1
        assert rows[0]["quoted_sender"] == "Alice"

        # Row 101 resolved against ZWAMESSAGE (parent 20)
        assert rows[1]["quoted_msg_id"] == 20
        assert rows[1]["quoted_text"] == "Original text from You"
        assert rows[1]["quoted_sender"] == "You"

        # Row 102 resolved against local_map (row 101)
        assert rows[2]["quoted_msg_id"] == 101
        assert rows[2]["quoted_text"] == "I am message 101"
        assert rows[2]["quoted_sender"] == "You"


# ---------------------------------------------------------------------------
# Tests: Track 1 Navigation Redesign (Calls, Starred, Pinned)
# ---------------------------------------------------------------------------


class TestTrack1Endpoints:
    def test_api_calls_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15551234', 'Alice Whitman', 'Alice (15551234)')"
            )
            arch_conn.commit()

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER);
                CREATE TABLE call_log (
                    _id INTEGER PRIMARY KEY,
                    call_id TEXT,
                    jid_row_id INTEGER,
                    from_me INTEGER,
                    timestamp INTEGER,
                    video_call INTEGER,
                    duration INTEGER,
                    call_result INTEGER,
                    group_jid_row_id INTEGER
                );
                INSERT INTO jid VALUES (1, '15551234', '15551234@s.whatsapp.net');
                INSERT INTO jid VALUES (2, '15559999', '15559999@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, NULL);
                INSERT INTO chat VALUES (20, 2, NULL);

                INSERT INTO call_log VALUES (1, 'call-101', 1, 0, 1793972400000, 1, 765, 1, NULL);
                INSERT INTO call_log VALUES (2, 'call-102', 1, 0, 1793973000000, 0, 0, 2, NULL);
                INSERT INTO call_log VALUES (3, 'call-103', 2, 1, 1793974000000, 0, 120, 1, NULL);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. All calls
            resp = client.get("/api/calls")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"] if isinstance(data, dict) and "calls" in data else data
            assert len(calls) == 3
            c1 = next(c for c in calls if c["id"] == "call-101")
            assert c1["caller_name"] == "Alice Whitman"
            assert c1["caller_phone"] == "+15551234"
            assert c1["is_video"] is True
            assert c1["is_missed"] is False
            assert c1["duration"] == 765

            # 2. Missed calls filter
            resp_missed = client.get("/api/calls?filter=missed")
            assert resp_missed.status_code == 200
            data_missed = resp_missed.get_json()
            calls_missed = (
                data_missed["calls"]
                if isinstance(data_missed, dict) and "calls" in data_missed
                else data_missed
            )
            assert len(calls_missed) == 1
            assert calls_missed[0]["id"] == "call-102"
            assert calls_missed[0]["is_missed"] is True

            # 3. Video calls filter
            resp_video = client.get("/api/calls?filter=video")
            assert resp_video.status_code == 200
            data_video = resp_video.get_json()
            calls_video = (
                data_video["calls"]
                if isinstance(data_video, dict) and "calls" in data_video
                else data_video
            )
            assert len(calls_video) == 1
            assert calls_video[0]["id"] == "call-101"
            assert calls_video[0]["is_video"] is True

    def test_api_calls_ios(self, tmp_path):
        wa_path = tmp_path / "ChatStorage.sqlite"
        call_path = tmp_path / "CallHistory.sqlite"

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
                CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
                CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGEDATE REAL);
                INSERT INTO ZWACHATSESSION VALUES (1, '15553333@s.whatsapp.net', 'Bob iOS');
            """)
            conn.commit()

        with sqlite3.connect(str(call_path)) as conn:
            conn.executescript("""
                CREATE TABLE ZWACDCALLEVENT (
                    Z_PK INTEGER PRIMARY KEY,
                    ZCALLIDSTRING TEXT,
                    ZDATE REAL,
                    ZDURATION INTEGER,
                    ZBYTESENT INTEGER,
                    ZBYTESRECEIVED INTEGER,
                    ZOUTCOME INTEGER,
                    ZAGGREGATECALLEVENT INTEGER,
                    Z12_AGGREGATECALLEVENT INTEGER
                );
                CREATE TABLE ZWAAGGREGATECALLEVENT (
                    Z_PK INTEGER PRIMARY KEY,
                    ZVIDEO INTEGER,
                    ZINCOMING INTEGER,
                    ZCALLRESULT INTEGER
                );
                CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                    Z_PK INTEGER PRIMARY KEY,
                    Z1PARTICIPANTS INTEGER,
                    ZJIDSTRING TEXT,
                    ZOUTCOME INTEGER
                );

                -- Call 1: Incoming connected video call (ZDATE=700000000 -> 2023)
                INSERT INTO ZWACDCALLEVENT VALUES (1, 'ios-call-1', 700000000.0, 300, 1000, 2000, 1, 1, 1);
                INSERT INTO ZWAAGGREGATECALLEVENT VALUES (1, 1, 1, 1);
                INSERT INTO ZWACDCALLEVENTPARTICIPANT VALUES (1, 1, '15553333@s.whatsapp.net', 1);

                -- Call 2: Incoming missed audio call
                INSERT INTO ZWACDCALLEVENT VALUES (2, 'ios-call-2', 700001000.0, 0, 0, 0, 2, 2, 2);
                INSERT INTO ZWAAGGREGATECALLEVENT VALUES (2, 0, 1, 2);
                INSERT INTO ZWACDCALLEVENTPARTICIPANT VALUES (2, 2, '15554444@s.whatsapp.net', 2);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"] if isinstance(data, dict) and "calls" in data else data
            assert len(calls) == 2

            c1 = next(c for c in calls if c["id"] == "ios-call-1")
            assert c1["caller_name"] == "Bob iOS"
            assert c1["caller_phone"] == "+15553333"
            assert c1["is_video"] is True
            assert c1["is_missed"] is False
            assert c1["duration"] == 300
            assert c1["from_me"] is False

            # Filter missed
            resp_missed = client.get("/api/calls?filter=missed")
            assert resp_missed.status_code == 200
            data_missed = resp_missed.get_json()
            calls_missed = (
                data_missed["calls"]
                if isinstance(data_missed, dict) and "calls" in data_missed
                else data_missed
            )
            assert len(calls_missed) == 1
            assert calls_missed[0]["id"] == "ios-call-2"

    def test_api_calls_with_preserved(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        pres_path = tmp_path / "preserved_messages.db"

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER, timestamp INTEGER);
                CREATE TABLE call_log (
                    _id INTEGER PRIMARY KEY, call_id TEXT, jid_row_id INTEGER,
                    from_me INTEGER, timestamp INTEGER, video_call INTEGER,
                    duration INTEGER, call_result INTEGER, group_jid_row_id INTEGER
                );
            """)
            conn.commit()

        with sqlite3.connect(str(pres_path)) as conn:
            conn.executescript("""
                CREATE TABLE preserved_calls (
                    call_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    caller_jid TEXT,
                    caller_name TEXT,
                    timestamp_ms INTEGER,
                    duration_sec INTEGER,
                    video_call INTEGER,
                    from_me INTEGER,
                    call_result INTEGER
                );
                INSERT INTO preserved_calls VALUES (
                    'pres-call-1', '99', '15557777@s.whatsapp.net', 'Ghost Caller',
                    1793979999000, 0, 0, 0, 2
                );
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"] if isinstance(data, dict) and "calls" in data else data
            assert len(calls) == 1
            assert calls[0]["id"] == "pres-call-1"
            assert calls[0]["caller_name"] == "Ghost Caller"
            assert calls[0]["is_missed"] is True

    def test_api_starred_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT,
                    starred INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT
                );
                INSERT INTO jid VALUES (1, '15551111', '15551111@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, 'Project Alpha');
                INSERT INTO message VALUES (
                    100, 10, 0, 1, 1793970000000, 'Starred Important Note', 0, 'STANZA_STAR_1', 1
                );
                INSERT INTO message VALUES (
                    101, 10, 0, 1, 1793971000000, 'Regular Note', 0, 'STANZA_REG_1', 0
                );
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # Global starred
            resp = client.get("/api/starred")
            assert resp.status_code == 200
            data = resp.get_json()
            items = data["messages"] if isinstance(data, dict) and "messages" in data else data
            assert len(items) == 1
            assert items[0]["id"] == 100
            assert items[0]["text_body"] == "Starred Important Note"
            assert items[0]["chat_name"] == "Project Alpha"

            # Scoped chat starred
            resp_chat = client.get("/api/chat/10/starred")
            assert resp_chat.status_code == 200
            data_chat = resp_chat.get_json()
            chat_items = (
                data_chat["messages"]
                if isinstance(data_chat, dict) and "messages" in data_chat
                else data_chat
            )
            assert len(chat_items) == 1
            assert chat_items[0]["id"] == 100

            resp_empty = client.get("/api/chat/999/starred")
            assert resp_empty.status_code == 200
            data_empty = resp_empty.get_json()
            empty_items = (
                data_empty["messages"]
                if isinstance(data_empty, dict) and "messages" in data_empty
                else data_empty
            )
            assert len(empty_items) == 0

    def test_api_starred_android_contact_and_lid_chat_id_resolution(self, tmp_path):
        """Android /api/starred must emit canonical chat_id matching /api/chats for 1:1, LID, and group chats."""
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15551234', 'Alice Contact', 'Alice Contact (15551234)')"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15556789', 'Bob LID', 'Bob LID (15556789)')"
            )
            arch_conn.commit()

        with make_android_db(wa_path) as conn:
            conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
            conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
            conn.executescript("""
                INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net');
                INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '999000111222333', 'lid', '999000111222333@lid');
                INSERT INTO jid (_id, user, server, raw_string) VALUES (3, '15556789', 's.whatsapp.net', '15556789@s.whatsapp.net');
                INSERT INTO jid (_id, user, server, raw_string) VALUES (4, '120363000111', 'g.us', '120363000111@g.us');
                INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 3);

                INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (10, 1, NULL, 1793970000000, 0);
                INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (20, 2, NULL, 1793971000000, 0);
                INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp, hidden) VALUES (30, 4, 'Weekend Trip', 1793972000000, 0);

                -- 1:1 incoming starred message (sender_jid_row_id = 0, standard Android convention)
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (101, 10, 0, 0, 1793970000000, 'Alice starred note', 0, 'K1', 1);
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (102, 10, 1, 0, 1793970010000, 'Reply to Alice', 0, 'K2', 0);

                -- LID 1:1 outgoing starred message
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (201, 20, 1, 0, 1793971000000, 'Bob LID starred note', 0, 'K3', 1);

                -- Group starred message
                INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
                VALUES (301, 30, 0, 1, 1793972000000, 'Group starred note', 0, 'K4', 1);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            chats = client.get("/api/chats").get_json()
            chat_keys = {(c["id"], c["type"]): c for c in chats}

            resp = client.get("/api/starred")
            assert resp.status_code == 200
            items = resp.get_json()["messages"]
            assert len(items) == 3

            by_id = {m["id"]: m for m in items}

            # 1:1 standard contact
            m_alice = by_id[101]
            assert m_alice["chat_id"] == "15551234"
            assert m_alice["chat_type"] == "contact"
            assert m_alice["chat_name"] == "Alice Contact"
            assert m_alice["sender_name"] == "Alice Contact"
            assert (m_alice["chat_id"], m_alice["chat_type"]) in chat_keys

            # 1:1 LID contact
            m_bob = by_id[201]
            assert m_bob["chat_id"] == "15556789"
            assert m_bob["chat_type"] == "contact"
            assert m_bob["chat_name"] == "Bob LID"
            assert m_bob["sender_name"] == "You"
            assert (m_bob["chat_id"], m_bob["chat_type"]) in chat_keys

            # Group chat
            m_grp = by_id[301]
            assert m_grp["chat_id"] == "30"
            assert m_grp["chat_type"] == "group"
            assert m_grp["chat_name"] == "Weekend Trip"
            assert m_grp["sender_name"] == "Alice Contact"
            assert (m_grp["chat_id"], m_grp["chat_type"]) in chat_keys

            # Scoped /api/chat/<id>/starred works for canonical contact & LID IDs
            alice_scoped = client.get("/api/chat/15551234/starred").get_json()["messages"]
            assert [m["id"] for m in alice_scoped] == [101]
            bob_scoped = client.get("/api/chat/15556789/starred").get_json()["messages"]
            assert [m["id"] for m in bob_scoped] == [201]

            # Context jump endpoint resolves each starred message cleanly
            for item in items:
                ctx_resp = client.get(
                    f"/api/messages/at?chat_id={item['chat_id']}&chat_type={item['chat_type']}&msg_id={item['id']}&limit=50"
                )
                assert ctx_resp.status_code == 200
                ctx_msgs = ctx_resp.get_json()
                assert any(cm["msg_id"] == item["id"] for cm in ctx_msgs)

    def test_api_starred_ios_document_caption_and_sanitization(self, tmp_path):
        """iOS /api/starred projects captions over filenames, sanitizes tokens, and handles scoped chat resolution."""
        arch_path = tmp_path / ".wa_media_archiver.db"
        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.execute(
                "CREATE TABLE contacts (number TEXT PRIMARY KEY, display_name TEXT, folder TEXT)"
            )
            arch_conn.execute(
                "INSERT INTO contacts VALUES ('15551234', 'Alice Smith', 'Alice Smith (15551234)')"
            )
            arch_conn.commit()

        cs_path = tmp_path / "ChatStorage.sqlite"
        with make_ios_db(cs_path) as conn:
            conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZSTARRED INTEGER DEFAULT 0")
            conn.execute("""
                INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
                VALUES (1, '15551234@s.whatsapp.net', 'Alice Smith', 0)
            """)
            conn.execute("""
                INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZVCARDNAME)
                VALUES (10, 'Q3 Financial Report', 'Media/15551234@s.whatsapp.net/bef9135a.pdf', NULL)
            """)
            conn.execute("""
                INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZVCARDNAME)
                VALUES (11, NULL, NULL, 'Central Park\nNew York, NY')
            """)
            # Doc 1: Document with filename in ZTEXT and caption in ZTITLE
            conn.execute("""
                INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED, ZMEDIAITEM, ZPUSHNAME, ZFROMJID)
                VALUES (100, 1, 0, 720000000, 'bef9135a-843b-4519-9d49-9634f9da21bb.pdf', 8, 1, 10, 'CNDj+ZsG', '15551234@s.whatsapp.net')
            """)
            # Msg 2: Location with place name in ZVCARDNAME and base64 token in ZPUSHNAME
            conn.execute("""
                INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED, ZMEDIAITEM, ZPUSHNAME, ZFROMJID)
                VALUES (101, 1, 0, 720000010, NULL, 5, 1, 11, 'CKji9NUGIABIAZABAPABAtgCu5PzutWWlwM=', '15551234@s.whatsapp.net')
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        try:
            with app.test_client() as client:
                resp = client.get("/api/starred")
                assert resp.status_code == 200
                data = resp.get_json()
                items = data["messages"] if isinstance(data, dict) and "messages" in data else data
                assert len(items) == 2

                by_id = {m["id"]: m for m in items}
                doc_m = by_id[100]
                assert doc_m["media_type"] == "document"
                assert doc_m["text_body"] == "Q3 Financial Report"
                assert doc_m["file_name"] == "bef9135a-843b-4519-9d49-9634f9da21bb.pdf"
                # Sender name must be sanitized from CNDj+ZsG to contacts map name
                assert doc_m["sender_name"] == "Alice Smith"

                loc_m = by_id[101]
                assert loc_m["media_type"] == "location"
                assert loc_m["place_name"] == "Central Park\nNew York, NY"
                assert loc_m["sender_name"] == "Alice Smith"

                # Scoped chat query using phone number chat_id
                resp_scoped = client.get("/api/chat/15551234/starred")
                assert resp_scoped.status_code == 200
                scoped_data = resp_scoped.get_json()
                scoped_items = (
                    scoped_data["messages"]
                    if isinstance(scoped_data, dict) and "messages" in scoped_data
                    else scoped_data
                )
                assert len(scoped_items) == 2
                assert scoped_data.get("count") == 2

                # Scoped query using CoreData PK chat_id
                resp_pk = client.get("/api/chat/1/starred")
                assert resp_pk.status_code == 200
                assert len(resp_pk.get_json()["messages"]) == 2
        finally:
            ctx = app.config.get("VIEWER_CONTEXT")
            if ctx and hasattr(ctx, "close"):
                ctx.close()

    def test_api_pinned_android(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER,
                    expiry_timestamp INTEGER
                );
                CREATE TABLE message_add_on_pin_in_chat (
                    message_add_on_row_id INTEGER PRIMARY KEY,
                    pin_in_chat_state INTEGER,
                    sender_timestamp INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_duration INTEGER,
                    media_name TEXT
                );

                INSERT INTO jid VALUES (1, '15551111', '15551111@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, 'Pinned Chat');
                INSERT INTO message VALUES (
                    50, 10, 0, 1, 1793970000000, 'Pinned announcement message', 0, 'PIN_PARENT_1'
                );
                -- Add-on type 79 = Pin in Chat (Wire Tag 63)
                INSERT INTO message_add_on VALUES (
                    1, 50, 10, 0, 1, 'PIN_ADDON_1', 1793970500000, 79, 0, 9999999999000
                );
                INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned")
            assert resp.status_code == 200
            data = resp.get_json()
            items = (
                data["pinned_messages"]
                if isinstance(data, dict) and "pinned_messages" in data
                else data
            )
            assert len(items) == 1
            assert items[0]["id"] == 50
            assert items[0]["text_body"] == "Pinned announcement message"
            assert items[0]["is_pinned"] is True

    def test_api_chat_info_aggregates_calls(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT
                );
                CREATE TABLE call_log (
                    _id INTEGER PRIMARY KEY,
                    call_id TEXT,
                    jid_row_id INTEGER,
                    from_me INTEGER,
                    timestamp INTEGER,
                    video_call INTEGER,
                    duration INTEGER,
                    call_result INTEGER,
                    group_jid_row_id INTEGER
                );
                CREATE TABLE jid_map (
                    lid_row_id INTEGER,
                    jid_row_id INTEGER
                );

                INSERT INTO jid VALUES (1, '15551234', '15551234@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, NULL);
                INSERT INTO message VALUES (1, 10, 0, 1, 1793970000000, 'Test msg', 0, 'M1');
                INSERT INTO call_log VALUES (1, 'call-1', 1, 0, 1793971000000, 0, 60, 1, NULL);
                INSERT INTO call_log VALUES (2, 'call-2', 1, 1, 1793972000000, 1, 120, 1, NULL);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=15551234&chat_type=contact")
            assert resp.status_code == 200
            info = resp.get_json()
            assert info["calls"] == 2


class TestTrack2UIShell:
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

    def test_header_top_right_settings_and_platform_badge(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="header"' in html
        assert 'id="header-brand"' in html
        assert 'id="settings-btn"' in html
        assert "platform-badge" in html
        assert "Android" in html

    def test_sidebar_search_and_bottom_navbar(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="sidebar-bottom-bar"' in html
        assert 'id="tab-btn-chats"' in html
        assert 'id="tab-btn-calls"' in html
        assert 'id="tab-btn-starred"' in html
        assert 'id="tab-btn-archived"' in html
        assert 'data-tab="chats"' in html
        assert 'aria-selected="true"' in html

    def test_contact_header_avatar_and_subtitles(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-header-info"' in html
        assert 'id="chat-header-avatar"' in html
        assert 'id="chat-title"' in html
        assert 'id="chat-subtitle"' in html

    def test_msg_bubble_elevation_and_tokens(self, app_and_tmp):
        client, tmp_path = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert "--bubble-shadow" in css
        assert "box-shadow: var(--bubble-shadow);" in css
        assert "border: none;" in css
        assert "#chat-header-info" in css
        assert "#sidebar-bottom-bar" in css


class TestTrack3FeaturePanes:
    """Tests for Track 3: Interactive feature panes and mockup fidelity."""

    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
        wa_conn.execute("UPDATE message SET starred = 1 WHERE _id = 1")
        wa_conn.commit()

        # Create preserved_messages.db in tmp_path
        pres_db = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_db))
        pres_conn.execute("""
            CREATE TABLE preserved_messages (
                rowid INTEGER PRIMARY KEY,
                msg_id INTEGER,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid TEXT,
                sender_name TEXT,
                timestamp_ms INTEGER NOT NULL,
                text_body TEXT,
                media_type TEXT,
                archive_path TEXT,
                file_size INTEGER,
                is_starred INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0
            );
        """)
        pres_conn.execute("""
            INSERT INTO preserved_messages (rowid, msg_id, chat_id, chat_type, from_me, key_id, sender_jid, sender_name, timestamp_ms, text_body, media_type, archive_path, file_size, is_starred, is_pinned)
            VALUES (
                50, 50, '10', 'contact', 0, 'PRES1', '15551234567@s.whatsapp.net', 'Alice',
                1700000050000, 'Preserved secret', NULL, NULL, 0, 1, 0
            );
        """)
        pres_conn.commit()

        wa_conn.close()
        archive_conn.close()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_contact_header_no_role_button_and_css_reset(self, app_and_tmp):
        """Contact header must not have role='button' (which triggers Pico blue button styling) and has transparent background."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Verify #chat-header-info is clean without role="button"
        assert '<div id="chat-header-info" tabindex="0"' in html
        assert 'id="chat-header-info" role="button"' not in html

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert "#chat-header-info" in css
        assert "background: transparent !important;" in css

    def test_right_slide_over_drawers_inside_chat_pane(self, app_and_tmp):
        """Drawers must be docked inside #chat-pane with right-slide-over layout matching prototype mockup."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '<aside id="chat-starred-drawer" class="chat-drawer"' in html
        assert '<aside id="chat-info-panel" class="chat-drawer"' in html
        # Both drawers must be within #chat-pane (preceding </div> of #chat-pane)
        pane_idx = html.find('id="chat-pane"')
        starred_idx = html.find('id="chat-starred-drawer"')
        info_idx = html.find('id="chat-info-panel"')
        assert pane_idx != -1 and starred_idx > pane_idx and info_idx > pane_idx

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".chat-drawer" in css
        assert "position: absolute;" in css
        assert "right: 0;" in css
        assert "width: 340px;" in css

    def test_starred_messages_flyout_card_bubbles_and_tokens(self, app_and_tmp):
        """Starred flyout elements, cards, and design tokens must match prototype."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-starred-btn"' in html
        assert 'id="chat-starred-badge"' in html
        assert 'id="chat-starred-header-count"' in html
        assert 'id="chat-starred-close"' in html

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".starred-card" in css
        assert ".starred-card-top" in css
        assert ".starred-card-meta" in css
        assert ".starred-card-chevron" in css
        assert ".starred-card-snippet" in css
        assert ".chat-starred-subheader" in css

    def test_chat_info_hero_elements(self, app_and_tmp):
        """Chat info drawer hero elements must match mockup."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="chat-info-hero"' in html
        assert 'id="chat-info-avatar"' in html
        assert 'id="chat-info-title"' in html
        assert 'id="chat-info-phone"' in html
        assert 'id="chat-info-details"' in html
        assert 'id="chat-info-header-heading"' in html

        css_resp = client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert ".chat-info-hero" in css
        assert ".chat-info-avatar" in css
        assert ".chat-info-name" in css
        assert ".chat-info-phone" in css

    def test_pulsing_glow_animations(self, app_and_tmp):
        """Pulsing gold and emerald glow keyframes must be present in app.css."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert "@keyframes pulseGlowGold" in css
        assert "@keyframes pulseGlowEmerald" in css
        assert ".jump-highlight-gold" in css
        assert ".jump-highlight-emerald" in css

    def test_api_messages_at_resolution_by_msg_id(self, app_and_tmp):
        """api_messages_at resolves target message by msg_id directly without requiring ts."""
        client, _ = app_and_tmp
        # Query msg_id=1 (Android live message) with resolved chat_id=10
        resp = client.get("/api/messages/at?chat_id=10&chat_type=contact&msg_id=1")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) > 0
        assert any(m["msg_id"] == 1 for m in msgs)

    def test_api_messages_at_preserved_by_negative_msg_id(self, app_and_tmp):
        """api_messages_at resolves preserved message by negative msg_id."""
        client, _ = app_and_tmp
        resp = client.get("/api/messages/at?msg_id=-50")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) > 0
        assert any(m["msg_id"] == -50 for m in msgs)


class TestPhase1CallsFeature:
    """Tests for Phase 1: Calls Feature Completion & Inspector View."""

    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Seed call_log table in Android DB
        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT,
                jid_row_id INTEGER,
                group_jid_row_id INTEGER,
                from_me INTEGER DEFAULT 0,
                timestamp INTEGER NOT NULL,
                video_call INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                call_result INTEGER DEFAULT 1,
                bytes_transferred INTEGER DEFAULT 0
            );
        """)
        wa_conn.execute("""
            INSERT INTO call_log (_id, call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result)
            VALUES (1, 'call_abc_1', 1, 0, 1700000000000, 1, 765, 1);
        """)
        wa_conn.execute("""
            INSERT INTO call_log (_id, call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result)
            VALUES (2, 'call_abc_2', 1, 0, 1700000010000, 0, 0, 2);
        """)
        wa_conn.commit()

        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_call_inspector_elements_rendered_in_template(self, app_and_tmp):
        """Call inspector pane and elements must exist in index HTML."""
        client, _ = app_and_tmp
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="pane-call-inspector"' in html
        assert 'id="call-inspect-avatar"' in html
        assert 'id="call-inspect-name"' in html
        assert 'id="call-inspect-phone"' in html
        assert 'id="call-inspect-badge"' in html
        assert 'id="call-inspect-dir"' in html
        assert 'id="call-inspect-time"' in html
        assert 'id="call-inspect-duration"' in html
        assert 'id="call-inspect-data-row"' in html
        assert 'id="call-inspect-jump-btn"' in html

    def test_call_inspector_and_sidebar_css_classes(self, app_and_tmp):
        """CSS rules for call inspector card and call items must exist in app.css."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert ".call-inspector-pane" in css
        assert ".call-inspector-card" in css
        assert ".call-inspect-avatar" in css
        assert ".call-inspect-type-badge" in css
        assert ".call-inspect-jump-btn" in css
        assert ".call-item" in css
        assert ".call-item-avatar" in css
        assert ".call-icon-missed" in css
        assert ".call-icon-incoming" in css
        assert ".call-icon-outgoing" in css

    def test_calls_api_endpoint(self, app_and_tmp):
        """GET /api/calls returns calls with proper formatting and pagination."""
        client, _ = app_and_tmp
        resp = client.get("/api/calls?limit=10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "calls" in data
        assert "has_more" in data
        calls = data["calls"]
        assert len(calls) == 2
        # Check call properties
        video_call = next((c for c in calls if c["id"] == "call_abc_1"), None)
        assert video_call is not None
        assert video_call["is_video"] is True
        assert video_call["duration"] == 765
        assert video_call["call_type"] == "incoming"

        missed_call = next((c for c in calls if c["id"] == "call_abc_2"), None)
        assert missed_call is not None
        assert missed_call["is_video"] is False
        assert missed_call["call_type"] == "missed"

    def test_calls_contact_chat_id_matches_raw_number(self, app_and_tmp):
        """Android contact call records must project chat_id matching the contact phone/user string."""
        client, _ = app_and_tmp
        resp = client.get("/api/calls?limit=10")
        assert resp.status_code == 200
        data = resp.get_json()
        calls = data["calls"]
        assert len(calls) >= 1
        call = calls[0]
        assert call["chat_id"] == "123456789"

    def test_calls_android_privacy_lid_resolved(self, tmp_path):
        """Android call records logged with privacy LID must resolve caller phone, name, and chat_id."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")

        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '187000000000001', 'lid', '187000000000001@lid')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '15551234567', 's.whatsapp.net', '15551234567@s.whatsapp.net')"
        )
        wa_conn.execute("INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (1, 2)")

        wa_conn.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (10, 2, 'Alice')")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data) VALUES (1, 10, 0, 1700000000000, 'Hey')"
        )

        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT,
                jid_row_id INTEGER,
                group_jid_row_id INTEGER,
                from_me INTEGER DEFAULT 0,
                timestamp INTEGER NOT NULL,
                video_call INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                call_result INTEGER DEFAULT 1,
                bytes_transferred INTEGER DEFAULT 0
            );
        """)
        wa_conn.execute("""
            INSERT INTO call_log (_id, call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result)
            VALUES (1, 'call_lid_test', 1, 0, 1700000050000, 1, 300, 1)
        """)
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15551234567', '15551234567', 'Alice Whitman')"
        )
        archive_conn.commit()

        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls?limit=10")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "calls" in data
            calls = data["calls"]
            assert len(calls) == 1
            call = calls[0]
            assert call["id"] == "call_lid_test"
            assert call["caller_phone"] == "+15551234567"
            assert call["caller_name"] == "Alice Whitman"
            assert call["chat_id"] == "15551234567"

    def test_api_chat_info_calls_count_includes_lid_calls(self, tmp_path):
        """Chat info calls_count must count calls logged under companion privacy LID."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")

        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '187000000000001', 'lid', '187000000000001@lid')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '15559876543', 's.whatsapp.net', '15559876543@s.whatsapp.net')"
        )
        wa_conn.execute("INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (1, 2)")

        wa_conn.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (20, 2, NULL)")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data) VALUES (1, 20, 0, 1700000000000, 'Hello')"
        )

        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT,
                jid_row_id INTEGER,
                group_jid_row_id INTEGER,
                from_me INTEGER DEFAULT 0,
                timestamp INTEGER NOT NULL,
                video_call INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                call_result INTEGER DEFAULT 1,
                bytes_transferred INTEGER DEFAULT 0
            );
        """)
        wa_conn.execute("""
            INSERT INTO call_log (_id, call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result)
            VALUES (1, 'call_lid_info', 1, 0, 1700000060000, 0, 120, 1)
        """)
        wa_conn.commit()
        archive_conn.commit()

        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=15559876543&chat_type=contact")
            assert resp.status_code == 200
            info = resp.get_json()
            assert info["calls_count"] == 1

    def test_ios_calls_api_endpoint(self, tmp_path):
        """GET /api/calls on iOS parses CallHistory.sqlite with metrics and participants."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_conn.close()

        call_db = tmp_path / "CallHistory.sqlite"
        call_conn = sqlite3.connect(str(call_db))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                ZAGGREGATECALLEVENT INTEGER
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER,
                ZJIDSTRING TEXT,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (1, 1, 1);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, ZAGGREGATECALLEVENT)
            VALUES (1, 'ios_call_1', 345, 1, 720000000, 1);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (1, 1, '19876543210@s.whatsapp.net', 1);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls?limit=10")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "calls" in data
            calls = data["calls"]
            assert len(calls) == 1
            c = calls[0]
            assert c["id"] == "ios_call_1"
            assert c["is_video"] is True
            assert c["duration"] == 345
            assert c["call_type"] == "incoming"
            assert "19876543210" in c["caller_phone"]

    def test_ios_calls_coredata_z1callevents_and_z1participants_join(self, tmp_path):
        """GET /api/calls on iOS parses authentic CoreData Z1CALLEVENTS and Z1PARTICIPANTS."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_conn.close()

        call_db = tmp_path / "CallHistory.sqlite"
        call_conn = sqlite3.connect(str(call_db))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                Z1CALLEVENTS INTEGER
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 0
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER,
                ZJIDSTRING TEXT,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (1, 1, 0);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS)
            VALUES (84, 'ios_coredata_call_1', 120, 0, 720000000, 1);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (1, 1, '15551234567@s.whatsapp.net', 0);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls?limit=10")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"]
            assert len(calls) == 1
            c = calls[0]
            assert c["id"] == "ios_coredata_call_1"
            assert c["is_video"] is True
            assert c["from_me"] is True
            assert c["call_type"] == "outgoing"
            assert c["caller_phone"] == "+15551234567"

    def test_ios_calls_privacy_lid_resolution_via_chat_identifier(self, tmp_path):
        """GET /api/calls on iOS resolves Privacy LID participant via ZCONTACTIDENTIFIER."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZCONTACTIDENTIFIER" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZCONTACTIDENTIFIER TEXT")
        cs_conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZHIDDEN)
            VALUES (50, '15559876543@s.whatsapp.net', '145500000000001@lid', 'Alice Test', 0)
        """)
        cs_conn.commit()
        cs_conn.close()

        call_db = tmp_path / "CallHistory.sqlite"
        call_conn = sqlite3.connect(str(call_db))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                Z1CALLEVENTS INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1,
                ZMISSED INTEGER DEFAULT 0
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER NOT NULL,
                ZJIDSTRING TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (1, 0, 1);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS)
            VALUES (1, 'ios_lid_call', 60, 0, 720000000, 1);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (1, 1, '145500000000001@lid', 0);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/calls?limit=10")
            assert resp.status_code == 200
            data = resp.get_json()
            calls = data["calls"]
            assert len(calls) == 1
            c = calls[0]
            assert c["id"] == "ios_lid_call"
            assert c["caller_phone"] == "+15559876543"
            assert c["caller_name"] == "Alice Test"
            assert c["chat_id"] == "50"

    def test_api_chat_info_ios_calls_count_with_companion_lid(self, tmp_path):
        """GET /api/chat-info on iOS accurately counts calls logged under companion Privacy LID."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZCONTACTIDENTIFIER" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZCONTACTIDENTIFIER TEXT")
        cs_conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZHIDDEN)
            VALUES (50, '15559876543@s.whatsapp.net', '145500000000001@lid', 'Alice Test', 0)
        """)
        cs_conn.commit()
        cs_conn.close()

        call_db = tmp_path / "CallHistory.sqlite"
        call_conn = sqlite3.connect(str(call_db))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                Z1CALLEVENTS INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1,
                ZMISSED INTEGER DEFAULT 0
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER NOT NULL,
                ZJIDSTRING TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (1, 0, 1);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS)
            VALUES (1, 'ios_lid_call_1', 60, 0, 720000000, 1);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (1, 1, '145500000000001@lid', 0);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat-info?chat_id=50&chat_type=contact")
            assert resp.status_code == 200
            info = resp.get_json()
            assert info["calls_count"] == 1

    def test_calls_js_functions_present(self, app_and_tmp):
        """Helper functions for calls and duration must exist in app.js."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "function fmtDuration(seconds)" in js
        assert "function fmtDurationLong(seconds)" in js
        assert "function checkCallsAvailability()" in js
        assert "function loadCallsList(reset)" in js
        assert "function renderCallItem(call)" in js
        assert "function selectCallItem(call, el)" in js
        assert "function fmtTimeGap(diffMs, isPrior)" in js
        assert "Nearest message:" in js
        assert "function jumpFromCallToChat(call, matchedChat)" in js
        assert "async function openChatAtTimestamp(chatId, timestampMs)" in js
        assert "function showContextJumpBanner(icon, text, onSwitch)" in js


class TestPhase2ArchivedAndSearch:
    """Tests for Phase 2: Archived Tab Live State & Tab-Aware Search."""

    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Ensure chat table has archived column and set one chat as archived
        chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
        if "archived" not in chat_cols:
            wa_conn.execute("ALTER TABLE chat ADD COLUMN archived INTEGER DEFAULT 0")
        wa_conn.execute("UPDATE chat SET archived = 1 WHERE _id = 10")
        wa_conn.commit()

        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_api_chats_projection_includes_is_archived(self, app_and_tmp):
        """GET /api/chats returns is_archived integer flag for each chat."""
        client, _ = app_and_tmp
        resp = client.get("/api/chats")
        assert resp.status_code == 200
        chats = resp.get_json()
        assert len(chats) > 0
        # Check that is_archived exists on every chat
        for c in chats:
            assert "is_archived" in c
        # At least one chat is archived
        archived_chats = [c for c in chats if c["is_archived"] == 1]
        assert len(archived_chats) >= 1

    def test_main_chat_list_excludes_archived(self, app_and_tmp):
        """Archived chats must be filtered out from main renderChatList() logic."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "allChats.filter(c => !c.is_archived)" in js

    def test_ios_archived_chats_api(self, tmp_path):
        """GET /api/chats returns is_archived = 1 from ZWACHATSESSION.ZARCHIVED on iOS."""
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZARCHIVED" not in cs_cols:
            conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZARCHIVED INTEGER DEFAULT 0")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZARCHIVED)
            VALUES (1, '123456789@s.whatsapp.net', 'Archived User', 0, 1)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE)
            VALUES (1, 1, 0, 720000000, 'Archived chat message', 0)
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) >= 1
            archived = next((c for c in chats if c["id"] == "1"), None)
            assert archived is not None
            assert archived["is_archived"] == 1

    def test_android_archived_chats_companion_consolidation(self, tmp_path):
        """Companion phone and LID chats consolidate without losing archived status."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")

        chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
        if "archived" not in chat_cols:
            wa_conn.execute("ALTER TABLE chat ADD COLUMN archived INTEGER DEFAULT 0")

        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15550001111', 's.whatsapp.net', '15550001111@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '187000000000002', 'lid', '187000000000002@lid')"
        )
        wa_conn.execute("INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 1)")

        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, archived, sort_timestamp) VALUES (10, 1, NULL, 1, 1000)"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, archived, sort_timestamp) VALUES (20, 2, NULL, 0, 2000)"
        )

        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data) VALUES (1, 20, 0, 1700000000000, 'Hey there')"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550001111', '15550001111', 'Alice Dual')"
        )
        archive_conn.commit()

        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            matching = [c for c in chats if c["id"] == "15550001111"]
            assert len(matching) == 1
            assert matching[0]["is_archived"] == 1

    def test_android_archived_chat_without_messages_not_pruned(self, tmp_path):
        """Archived Android chat with zero messages must not be pruned from /api/chats."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")

        chat_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(chat)").fetchall()}
        if "archived" not in chat_cols:
            wa_conn.execute("ALTER TABLE chat ADD COLUMN archived INTEGER DEFAULT 0")

        wa_conn.execute(
            "INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15550002222', 's.whatsapp.net', '15550002222@s.whatsapp.net')"
        )
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, archived, sort_timestamp) VALUES (30, 1, NULL, 1, 1000)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15550002222', '15550002222', 'Empty Archived')"
        )
        archive_conn.commit()

        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            matching = [c for c in chats if c["id"] == "15550002222"]
            assert len(matching) == 1
            assert matching[0]["is_archived"] == 1

    def test_ios_archived_chat_without_messages_not_pruned(self, tmp_path):
        """Archived iOS chat with zero messages must not be pruned from /api/chats."""
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZARCHIVED" not in cs_cols:
            conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZARCHIVED INTEGER DEFAULT 0")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZARCHIVED)
            VALUES (10, '15553334444@s.whatsapp.net', 'Empty Archived iOS', 0, 1)
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            matching = [c for c in chats if c["id"] == "10"]
            assert len(matching) == 1
            assert matching[0]["is_archived"] == 1

    def test_archived_and_search_js_functions_present(self, app_and_tmp):
        """Helper functions for archived rendering, client-side filters, and keyboard shortcuts must exist in app.js."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "function renderArchivedList(searchQuery)" in js
        assert "function filterCallsList(searchQuery)" in js
        assert "function filterStarredList(searchQuery)" in js
        assert "function loadStarredList(reset)" in js
        assert "function renderStarredItem(m)" in js
        assert "Search messages & chats…" in js
        assert "Search archived chats…" in js
        assert "Search starred bookmarks…" in js
        assert "Search call history…" in js
        # Verify keyboard shortcuts
        assert "e.key === '/'" in js
        assert "e.key.toLowerCase() === 'k'" in js


class TestPhase3InBubbleIndicators:
    """Tests for Phase 3: In-Bubble Pinned & Starred Indicators."""

    @pytest.fixture
    def app_and_tmp(self, tmp_path):
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)
        seed_android_db(wa_conn, archive_conn)

        # Ensure message table has starred column
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
        if "starred" not in msg_cols:
            wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")

        # Insert message 2; message 1 will be starred, message 2 will be pinned
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, starred) "
            "VALUES (2, 10, 0, 1700000001000, 'Second message pinned', 0, 0)"
        )
        wa_conn.execute("UPDATE message SET starred = 1 WHERE _id = 1")

        # Create message_add_on and insert pin for message 2
        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            )
        """)
        wa_conn.execute("""
            INSERT INTO message_add_on (parent_message_row_id, message_add_on_type, expiry_timestamp, timestamp)
            VALUES (2, 79, 9999999999999, 1700000001000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, tmp_path

    def test_android_messages_projection_includes_starred_and_pinned(self, app_and_tmp):
        """GET /api/messages returns is_starred and is_pinned integer flags."""
        client, _ = app_and_tmp
        resp = client.get("/api/messages?chat_id=10&chat_type=contact")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) >= 2
        for m in msgs:
            assert "is_starred" in m
            assert "is_pinned" in m
        msg1 = next((m for m in msgs if m["msg_id"] == 1), None)
        assert msg1 is not None
        assert msg1["is_starred"] == 1
        assert msg1["is_pinned"] == 0
        msg2 = next((m for m in msgs if m["msg_id"] == 2), None)
        assert msg2 is not None
        assert msg2["is_pinned"] == 1
        assert msg2["is_starred"] == 0

    def test_messages_at_projection_includes_starred_and_pinned(self, app_and_tmp):
        """GET /api/messages/at returns is_starred and is_pinned."""
        client, _ = app_and_tmp
        resp = client.get("/api/messages/at?chat_id=10&chat_type=contact&ts=1700000000000")
        assert resp.status_code == 200
        msgs = resp.get_json()
        assert len(msgs) >= 1
        for m in msgs:
            assert "is_starred" in m
            assert "is_pinned" in m

    def test_preserved_messages_projection_includes_starred_and_pinned(self, tmp_path):
        """GET /api/messages correctly passes is_starred and is_pinned from preserved_messages.db."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()

        pres_db = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_db))
        pres_conn.execute("""
            CREATE TABLE preserved_messages (
                rowid INTEGER PRIMARY KEY,
                msg_id INTEGER,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                from_me INTEGER NOT NULL,
                key_id TEXT NOT NULL,
                sender_jid TEXT,
                sender_name TEXT,
                timestamp_ms INTEGER NOT NULL,
                text_body TEXT,
                media_type TEXT,
                archive_path TEXT,
                file_size INTEGER,
                is_starred INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0
            );
        """)
        pres_conn.execute(
            """INSERT INTO preserved_messages (
                rowid, msg_id, chat_id, chat_type, from_me, key_id,
                sender_name, timestamp_ms, text_body, media_type, is_starred, is_pinned
            ) VALUES (
                1, 101, 'test_chat', 'contact', 0, 'st_pres_1',
                'Alice', 1700000005000, 'Preserved starred pin', 'text', 1, 1
            )"""
        )
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=test_chat&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_starred"] == 1
            assert msgs[0]["is_pinned"] == 1
            assert msgs[0]["is_preserved"] == 1

    def test_js_in_bubble_indicator_markup(self, app_and_tmp):
        """Verify helper functions and classes for in-bubble indicators in app.js."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "function createMetaPinIcon()" in js
        assert "function createMetaStarIcon()" in js
        assert "msg-pinned-icon" in js
        assert "msg-starred-icon" in js
        assert "msg-meta-icon" in js
        assert "Pinned message" in js
        assert "Starred message" in js

    def test_android_pinned_unpinned_state_exclusion(self, tmp_path):
        """Android message with pin_in_chat_state = 2 (unpinned) must have is_pinned = 0."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            );
        """)
        wa_conn.execute("""
            CREATE TABLE message_add_on_pin_in_chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_add_on_row_id INTEGER NOT NULL,
                pin_in_chat_state INTEGER NOT NULL
            );
        """)
        wa_conn.execute(
            "INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type) VALUES (1, 1, 79)"
        )
        wa_conn.execute(
            "INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id, pin_in_chat_state) VALUES (1, 2)"
        )
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) >= 1
            msg1 = next(m for m in msgs if m["msg_id"] == 1)
            assert msg1["is_pinned"] == 0

    def test_historical_backup_pin_not_expired(self, tmp_path):
        """Pinned messages in older backups must not be expired by host computer clock."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            );
        """)
        wa_conn.execute("""
            INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type, expiry_timestamp, timestamp)
            VALUES (1, 1, 79, 1580000000000, 1570000000000)
        """)
        wa_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=10&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) >= 1
            msg1 = next(m for m in msgs if m["msg_id"] == 1)
            assert msg1["is_pinned"] == 1

    def test_ios_messages_starred_and_pinned(self, tmp_path):
        """GET /api/messages on iOS projects is_starred and is_pinned."""
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZSTARRED INTEGER DEFAULT 0")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '123456789@s.whatsapp.net', 'Alice', 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED)
            VALUES (1, 1, 0, 720000000, 'Starred msg', 0, 1)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTARRED)
            VALUES (2, 1, 0, 720000010, 'Pinned target', 0, 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZPARENTMESSAGE)
            VALUES (3, 1, 0, 720000020, 66, 2)
        """)
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) >= 2
            msg1 = next(m for m in msgs if m["msg_id"] == 1)
            assert msg1["is_starred"] == 1
            assert msg1["is_pinned"] == 0

            msg2 = next(m for m in msgs if m["msg_id"] == 2)
            assert msg2["is_starred"] == 0
            assert msg2["is_pinned"] == 1

    def test_ios_ext_chat_pinned_messages(self, tmp_path):
        """GET /api/messages on iOS projects is_pinned when pinned in ExtChatDatabase.sqlite."""
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '123456789@s.whatsapp.net', 'Alice', 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (10, 1, 0, 720000000, 'Ext pinned msg', 0, 'ext_stanza_pin_1')
        """)
        conn.commit()
        conn.close()

        ext_db = tmp_path / "ExtChatDatabase.sqlite"
        ext_conn = sqlite3.connect(str(ext_db))
        ext_conn.executescript("""
            CREATE TABLE thread_messages (
                rowid INTEGER PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                key_id TEXT NOT NULL,
                pin_timestamp INTEGER,
                thread_type INTEGER
            );
            INSERT INTO thread_messages (rowid, chat_jid, key_id, pin_timestamp, thread_type)
            VALUES (1, '123456789@s.whatsapp.net', 'ext_stanza_pin_1', 1700000000, 0);
        """)
        ext_conn.commit()
        ext_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            msgs = resp.get_json()
            assert len(msgs) == 1
            assert msgs[0]["msg_id"] == 10
            assert msgs[0]["is_pinned"] == 1

    def test_context_jump_banner_reset_helper(self, app_and_tmp):
        """Verify showContextJumpBanner helper resets banner icon and text and preserves active nav tab."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "function showContextJumpBanner(icon, text, onSwitch)" in js
        assert "showContextJumpBanner('⭐', 'Viewing message in context'" in js
        assert "showContextJumpBanner('📞', `Viewing Call Moment in context" in js
        assert "if (currentNavTab === 'chats')" in js
        assert "el.dataset.msgId = String(m.id);" in js
        # openChatAtMessage must not prematurely reset filter/render chats while in Starred tab
        fn_start = js.index("async function openChatAtMessage(")
        fn_end = js.index("async function openChatAtTimestamp(")
        open_msg_body = js[fn_start:fn_end]
        banner_idx = open_msg_body.index("showContextJumpBanner(")
        assert "resetFilterTo('all')" not in open_msg_body[:banner_idx]

    def test_css_indicator_styles(self, app_and_tmp):
        """Verify CSS classes for meta indicator icons in app.css."""
        client, _ = app_and_tmp
        resp = client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)
        assert ".msg-meta-icon" in css
        assert ".msg-pinned-icon" in css
        assert ".msg-starred-icon" in css
        assert "var(--star-color" in css


class TestInChatCallBubblesAndUI:
    """Tests for chronological in-chat call bubbles, pagination, dedup, and UI refinements."""

    def test_android_in_chat_call_bubbles_and_group_isolation(self, tmp_path):
        """Android /api/messages returns 1:1 & @lid calls, isolates group calls, and keeps recent_messages clean."""
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        arch_conn = make_archive_db(arch_path)
        arch_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15551234', 'Alice (15551234)', 'Alice')"
        )
        arch_conn.commit()
        arch_conn.close()

        wa_conn = make_android_db(wa_path)
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")

        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS jid_map (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net');
            INSERT INTO jid (_id, user, server, raw_string) VALUES (2, '9988776655', 'lid', '9988776655@lid');
            INSERT INTO jid (_id, user, server, raw_string) VALUES (3, '120363000111', 'g.us', '120363000111@g.us');
            INSERT INTO jid_map (lid_row_id, jid_row_id) VALUES (2, 1);

            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000050000);
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (20, 3, 'Project Team', 1700000050000);

            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000010000, 'Before call', 0, 'M1');
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (2, 10, 1, 0, 1700000040000, 'After call', 0, 'M2');
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (3, 20, 0, 1, 1700000015000, 'Group msg', 0, 'M3');

            -- 1:1 outgoing answered video call directly by jid 1
            INSERT INTO call_log VALUES (101, 'call_direct_1', 1, 1, 1700000020000, 1, 185, 1, NULL);
            -- 1:1 incoming missed voice call via privacy @lid jid 2
            INSERT INTO call_log VALUES (102, 'call_lid_2', 2, 0, 1700000030000, 0, 0, 2, NULL);
            -- Group video call initiated by jid 1 in group 3 (must NOT leak into contact 10)
            INSERT INTO call_log VALUES (103, 'call_grp_3', 1, 0, 1700000025000, 1, 420, 1, 3);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234&chat_type=contact&limit=50")
            assert resp.status_code == 200
            items = resp.get_json()

            call_items = [m for m in items if m["media_type"] == "call"]
            assert [c["msg_id"] for c in call_items] == ["call:call_lid_2", "call:call_direct_1"]
            assert all(c["sort_id"] == 0 for c in call_items)
            assert call_items[0]["call"]["call_type"] == "missed"
            assert call_items[0]["call"]["outcome"] == "missed"
            assert call_items[0]["call"]["video"] is False
            assert call_items[1]["call"]["video"] is True
            assert call_items[1]["call"]["duration_sec"] == 185
            assert call_items[1]["from_me"] == 1
            # Group call must not leak into 1:1 chat
            assert all(c["msg_id"] != "call:call_grp_3" for c in call_items)

            # Group chat contains only the group call with resolved sender
            g_resp = client.get("/api/messages?chat_id=20&chat_type=group&limit=50")
            assert g_resp.status_code == 200
            g_calls = [m for m in g_resp.get_json() if m["media_type"] == "call"]
            assert len(g_calls) == 1
            assert g_calls[0]["msg_id"] == "call:call_grp_3"
            assert g_calls[0]["call"]["is_group"] is True
            assert g_calls[0]["sender"] == "Alice"

            # Wait deterministically for background _backfill_recent_messages thread and verify calls are NOT cached in recent_messages
            cached = []
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with sqlite3.connect(str(arch_path)) as ac:
                    cached = ac.execute(
                        "SELECT msg_id, media_type FROM recent_messages WHERE chat_id = '15551234'"
                    ).fetchall()
                if cached:
                    break
                time.sleep(0.05)
            assert len(cached) > 0
            assert all(row[1] != "call" and isinstance(row[0], int) for row in cached)

            # Second request exercises recent_messages fast-path and must still return the call bubbles
            resp2 = client.get("/api/messages?chat_id=15551234&chat_type=contact&limit=50")
            assert resp2.status_code == 200
            calls2 = [m["msg_id"] for m in resp2.get_json() if m["media_type"] == "call"]
            assert calls2 == ["call:call_lid_2", "call:call_direct_1"]

    def test_ios_in_chat_call_bubbles_and_messages_at(self, tmp_path):
        """iOS /api/messages and /api/messages/at merge ZWACDCALLEVENT entries including @lid links."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZCONTACTIDENTIFIER" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZCONTACTIDENTIFIER TEXT")
        cs_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZHIDDEN) "
            "VALUES (5, '15559876543@s.whatsapp.net', '145500000000001@lid', 'Alice', 0)"
        )
        cs_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID) "
            "VALUES (1, 5, 0, 720000000, 'Before iOS call', 0, 'IOS_M1')"
        )
        cs_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID) "
            "VALUES (2, 5, 1, 720000120, 'After iOS call', 0, 'IOS_M2')"
        )
        cs_conn.commit()
        cs_conn.close()

        call_conn = sqlite3.connect(str(tmp_path / "CallHistory.sqlite"))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                Z1CALLEVENTS INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1,
                ZMISSED INTEGER DEFAULT 0
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER NOT NULL,
                ZJIDSTRING TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (10, 1, 0);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS)
            VALUES (1, 'ios_chat_call_1', 95, 0, 720000060, 10);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (1, 10, '145500000000001@lid', 0);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=5&chat_type=contact&limit=20")
            assert resp.status_code == 200
            items = resp.get_json()
            assert [m["msg_id"] for m in items] == [2, "call:ios_chat_call_1", 1]
            call_bubble = items[1]
            assert call_bubble["media_type"] == "call"
            assert call_bubble["call"]["video"] is True
            assert call_bubble["call"]["duration_sec"] == 95
            assert call_bubble["call"]["call_type"] == "outgoing"

            target_ts = call_bubble["timestamp_ms"]
            at_resp = client.get(
                f"/api/messages/at?chat_id=5&chat_type=contact&ts={target_ts}&msg_id=call:ios_chat_call_1&limit=10"
            )
            assert at_resp.status_code == 200
            at_items = at_resp.get_json()
            assert [m["msg_id"] for m in at_items] == [1, "call:ios_chat_call_1", 2]

    def test_preserved_calls_dedup_and_preserved_only_chat(self, tmp_path):
        """Preserved calls deduplicate against live call_log and render in preserved-only chats."""
        from wab_archiver.preservation import PRESERVED_SCHEMA

        wa_path = tmp_path / "msgstore.db"
        wa_conn = make_android_db(wa_path)
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS jid_map (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000050000);
            INSERT INTO call_log VALUES (1, 'live_and_pres_call', 1, 1, 1700000020000, 0, 45, 1, NULL);
        """)
        wa_conn.commit()
        wa_conn.close()

        pres_path = tmp_path / "preserved_messages.db"
        pres_conn = sqlite3.connect(str(pres_path))
        pres_conn.executescript(PRESERVED_SCHEMA)
        pres_conn.executescript("""
            INSERT INTO preserved_chats (chat_id, chat_type, subject, description, created_ts, oldest_ts, newest_ts, msg_count, members_json)
            VALUES ('15559999', 'contact', 'Deleted Contact', NULL, NULL, 1700000000000, 1700000030000, 0, NULL);

            -- Exact overlap with live call (must deduplicate in favor of live)
            INSERT INTO preserved_calls (call_id, chat_id, chat_jid, caller_jid, caller_name, from_me, timestamp_ms, duration_seconds, is_video, call_result, is_group_call, participants, bytes_transferred, preserved_at, source_platform)
            VALUES ('live_and_pres_call', '15551234', '15551234@s.whatsapp.net', '15551234@s.whatsapp.net', 'Alice', 1, 1700000020000, 45, 0, 1, 0, NULL, NULL, '2026-01-01T00:00:00Z', 'android');

            -- Preserved-only historical call for live chat
            INSERT INTO preserved_calls (call_id, chat_id, chat_jid, caller_jid, caller_name, from_me, timestamp_ms, duration_seconds, is_video, call_result, is_group_call, participants, bytes_transferred, preserved_at, source_platform)
            VALUES ('pres_only_call', '15551234', '15551234@s.whatsapp.net', '15551234@s.whatsapp.net', 'Alice', 0, 1700000010000, 0, 1, 2, 0, NULL, NULL, '2026-01-01T00:00:00Z', 'android');

            -- Call in preserved-only deleted chat (no live rows)
            INSERT INTO preserved_calls (call_id, chat_id, chat_jid, caller_jid, caller_name, from_me, timestamp_ms, duration_seconds, is_video, call_result, is_group_call, participants, bytes_transferred, preserved_at, source_platform)
            VALUES ('ghost_chat_call', '15559999', '15559999@s.whatsapp.net', '15559999@s.whatsapp.net', 'Ghost', 0, 1700000025000, 60, 0, 1, 0, NULL, NULL, '2026-01-01T00:00:00Z', 'android');
        """)
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234&chat_type=contact&limit=20")
            assert resp.status_code == 200
            items = resp.get_json()
            assert [m["msg_id"] for m in items] == [
                "call:live_and_pres_call",
                "call:pres_only_call",
            ]
            assert items[0]["is_preserved"] == 0
            assert items[1]["is_preserved"] == 1
            assert items[1]["call"]["source"] == "preserved"

            ghost_resp = client.get("/api/messages?chat_id=15559999&chat_type=contact&limit=20")
            assert ghost_resp.status_code == 200
            ghost_items = ghost_resp.get_json()
            assert [m["msg_id"] for m in ghost_items] == ["call:ghost_chat_call"]
            assert ghost_items[0]["is_preserved"] == 1

    def test_in_chat_call_pagination_and_calls_timestamp_ms(self, tmp_path):
        """Cursor pagination (before/after) cleanly returns interleaved calls, and /api/calls includes timestamp_ms."""
        wa_path = tmp_path / "msgstore.db"
        wa_conn = make_android_db(wa_path)
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS jid_map (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15554321', 's.whatsapp.net', '15554321@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000005000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000001000, 'Msg 1', 0, 'M1');
            INSERT INTO call_log VALUES (10, 'c_1', 1, 1, 1700000002456, 0, 30, 1, NULL);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (2, 10, 1, 0, 1700000003000, 'Msg 2', 0, 'M2');
            INSERT INTO call_log VALUES (11, 'c_2', 1, 0, 1700000004789, 1, 0, 2, NULL);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            calls_resp = client.get("/api/calls?limit=10")
            assert calls_resp.status_code == 200
            calls = calls_resp.get_json()["calls"]
            assert [c["timestamp_ms"] for c in calls] == [1700000004789, 1700000002456]

            page1 = client.get(
                "/api/messages?chat_id=15554321&chat_type=contact&limit=2"
            ).get_json()
            assert [m["msg_id"] for m in page1] == ["call:c_2", 2]

            before_ts = page1[-1]["timestamp_ms"]
            page2 = client.get(
                f"/api/messages?chat_id=15554321&chat_type=contact&limit=2&before={before_ts}"
            ).get_json()
            assert [m["msg_id"] for m in page2] == ["call:c_1", 1]

            after_ts = page2[-1]["timestamp_ms"]
            newer = client.get(
                f"/api/messages?chat_id=15554321&chat_type=contact&limit=2&after={after_ts}"
            ).get_json()
            assert [m["msg_id"] for m in newer] == ["call:c_1", 2]

    def test_ui_refinements_css_and_js_contracts(self, tmp_path):
        """Verify responsive sidebar width tokens, two-line call cards, call bubble renderer, and 'Calls' label."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            css = client.get("/static/app.css").get_data(as_text=True)
            js = client.get("/static/app.js").get_data(as_text=True)

        assert "--sidebar-scale:" in css
        assert "clamp(300px, 24vw, 460px)" in css
        assert "max-width: 42vw;" in css
        assert ".call-item-row-1" in css
        assert ".call-item-row-2" in css
        assert ".msg-bubble.call-event" in css
        assert ".msg-call-card" in css

        assert "function renderCallCard(msg)" in js
        assert "function showCallDetails(msg, anchorEl)" in js
        assert "_infoRow('Calls'" in js
        assert "'Preserved calls'" not in js
        assert "async function openChatAtTimestamp(chatId, timestampMs)" in js


class TestReviewFixRegressions:
    """Regression suite covering critical fixes and optimizations from review."""

    def test_ios_pinned_cross_chat_scoping(self, tmp_path):
        """iOS /api/chat/<id>/pinned scopes ExtChatDatabase thread_messages and ZWAMESSAGE pins to the target chat."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '1110001@s.whatsapp.net', 'Alice', 0),
                   (2, '2220002@s.whatsapp.net', 'Bob', 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (10, 1, 0, 720000000, 'Alice pinned msg', 0, 'pin_chat_1'),
                   (20, 2, 0, 720000010, 'Bob ext pinned msg', 0, 'pin_chat_2'),
                   (21, 2, 0, 720000020, 'Bob legacy pinned msg', 0, 'pin_chat_2_legacy'),
                   (22, 2, 0, 720000030, NULL, 66, 'pin_event_chat_2');
            UPDATE ZWAMESSAGE SET ZPARENTMESSAGE = 21 WHERE Z_PK = 22;
        """)
        cs_conn.commit()
        cs_conn.close()

        ext_conn = sqlite3.connect(str(tmp_path / "ExtChatDatabase.sqlite"))
        ext_conn.executescript("""
            CREATE TABLE thread_messages (
                rowid INTEGER PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                key_id TEXT NOT NULL,
                pin_timestamp INTEGER,
                thread_type INTEGER
            );
            INSERT INTO thread_messages (rowid, chat_jid, key_id, pin_timestamp, thread_type)
            VALUES (1, '1110001@s.whatsapp.net', 'pin_chat_1', 1700000000, 0),
                   (2, '2220002@s.whatsapp.net', 'pin_chat_2', 1700000010, 0);
        """)
        ext_conn.commit()
        ext_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            r1 = client.get("/api/chat/1/pinned?chat_type=contact")
            assert r1.status_code == 200
            pins1 = r1.get_json()["pinned_messages"]
            assert [m["id"] for m in pins1] == [10]

            r2 = client.get("/api/chat/2/pinned?chat_type=contact")
            assert r2.status_code == 200
            pins2 = r2.get_json()["pinned_messages"]
            assert {m["id"] for m in pins2} == {20, 21}

    def test_api_chat_info_ios_legacy_call_schema(self, tmp_path):
        """iOS /api/chat-info supports legacy ZAGGREGATECALLEVENT/ZCALLEVENT/ZJID schemas and isolates group calls."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()}
        if "ZCONTACTIDENTIFIER" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZCONTACTIDENTIFIER TEXT")
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZHIDDEN)
            VALUES (10, '15551112222@s.whatsapp.net', '9990001@lid', 'Alice', 0),
                   (20, '120363999@g.us', NULL, 'Team Group', 0);
            UPDATE ZWACHATSESSION SET ZGROUPINFO = 1 WHERE Z_PK = 20;
        """)
        cs_conn.commit()
        cs_conn.close()

        call_conn = sqlite3.connect(str(tmp_path / "CallHistory.sqlite"))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                ZAGGREGATECALLEVENT INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLEVENT INTEGER NOT NULL,
                ZJID TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (1, 0, 1), (2, 1, 0);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZGROUPJIDSTRING, ZDURATION, ZOUTCOME, ZDATE, ZAGGREGATECALLEVENT)
            VALUES (1, 'legacy_1to1', NULL, 42, 0, 720000000, 1),
                   (2, 'legacy_grp', '120363999@g.us', 120, 0, 720000100, 2);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, ZCALLEVENT, ZJID, ZOUTCOME)
            VALUES (1, 1, '9990001@lid', 0),
                   (2, 2, '9990001@lid', 0);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            c_info = client.get("/api/chat-info?chat_id=10&chat_type=contact").get_json()
            assert c_info["calls_count"] == 1
            g_info = client.get("/api/chat-info?chat_id=20&chat_type=group").get_json()
            assert g_info["calls_count"] == 1

    def test_api_chats_ios_without_zlastmessagedate_and_empty_chat(self, tmp_path):
        """iOS /api/chats works when ZWACHATSESSION lacks ZLASTMESSAGEDATE and yields newest_ts=0 for empty chats."""
        cs_conn = sqlite3.connect(str(tmp_path / "ChatStorage.sqlite"))
        cs_conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZGROUPINFO INTEGER,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZHIDDEN INTEGER DEFAULT 0,
                ZARCHIVED INTEGER DEFAULT 0,
                ZCONTACTABID INTEGER
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMEDIALOCALPATH TEXT,
                ZMEDIAURL TEXT,
                ZTITLE TEXT,
                ZXMPPTHUMBPATH TEXT,
                ZVCARDNAME TEXT,
                ZVCARDSTRING TEXT,
                ZMETADATA BLOB
            );
            CREATE TABLE ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZMEMBERJID TEXT,
                ZCONTACTNAME TEXT,
                ZISACTIVE INTEGER DEFAULT 1
            );
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZCREATORJID TEXT);
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER NOT NULL,
                ZISFROMME INTEGER NOT NULL DEFAULT 0,
                ZMESSAGEDATE REAL,
                ZSTANZAID TEXT,
                ZMESSAGETYPE INTEGER DEFAULT 0,
                ZGROUPEVENTTYPE INTEGER DEFAULT 0,
                ZMEDIAITEM INTEGER,
                ZGROUPMEMBER INTEGER,
                ZPARENTMESSAGE INTEGER,
                ZPUSHNAME TEXT,
                ZTEXT TEXT,
                ZFROMJID TEXT,
                ZTOJID TEXT,
                ZRECEIPTINFO BLOB,
                ZMESSAGESTATUS INTEGER DEFAULT 0
            );
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZARCHIVED)
            VALUES (1, '15551110001@s.whatsapp.net', 'Has Msg', 0, 0),
                   (2, '15551110002@s.whatsapp.net', 'Empty Chat', 0, 1);
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE)
            VALUES (10, 1, 0, 720000000, 'Hello', 0);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = {c["id"]: c for c in resp.get_json()}
            assert chats["1"]["newest_ts"] > 0
            assert chats["2"]["newest_ts"] == 0

    def test_ios_calls_missed_fallback_and_filter_limit(self, tmp_path):
        """iOS /api/calls detects missed calls via ZOUTCOME=4 without ZMISSED and does not truncate filtered queries."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN) "
            "VALUES (1, '15557778888@s.whatsapp.net', NULL, 0)"
        )
        cs_conn.commit()
        cs_conn.close()

        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.commit()
        arch_conn.close()

        cv_conn = sqlite3.connect(str(tmp_path / "ContactsV2.sqlite"))
        cv_conn.execute(
            "CREATE TABLE ZWAADDRESSBOOKCONTACT ("
            "Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID TEXT, ZLID TEXT, ZFULLNAME TEXT, ZPHONESTATUS TEXT)"
        )
        cv_conn.execute(
            "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZWHATSAPPID, ZLID, ZFULLNAME, ZPHONESTATUS) "
            "VALUES (1, '15557778888@s.whatsapp.net', NULL, 'Saved Caller Name', NULL)"
        )
        cv_conn.commit()
        cv_conn.close()

        call_conn = sqlite3.connect(str(tmp_path / "CallHistory.sqlite"))
        call_conn.executescript("""
            CREATE TABLE ZWACDCALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZCALLIDSTRING TEXT NOT NULL,
                ZGROUPJIDSTRING TEXT,
                ZDURATION REAL DEFAULT 0,
                ZOUTCOME INTEGER DEFAULT 0,
                ZDATE REAL NOT NULL,
                Z1CALLEVENTS INTEGER NOT NULL
            );
            CREATE TABLE ZWAAGGREGATECALLEVENT (
                Z_PK INTEGER PRIMARY KEY,
                ZVIDEO INTEGER DEFAULT 0,
                ZINCOMING INTEGER DEFAULT 1
            );
            CREATE TABLE ZWACDCALLEVENTPARTICIPANT (
                Z_PK INTEGER PRIMARY KEY,
                Z1PARTICIPANTS INTEGER NOT NULL,
                ZJIDSTRING TEXT NOT NULL,
                ZOUTCOME INTEGER DEFAULT 0
            );
        """)
        # 55 newer connected audio outgoing calls + 5 older missed incoming + 2 older video calls
        for i in range(1, 56):
            call_conn.execute(
                "INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (?, 0, 0)",
                (i,),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS) "
                "VALUES (?, ?, 30, 0, ?, ?)",
                (i, f"conn_{i}", 720001000 + i, i),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME) "
                "VALUES (?, ?, '15557778888@s.whatsapp.net', 0)",
                (i, i),
            )
        for i in range(56, 61):
            call_conn.execute(
                "INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (?, 0, 1)",
                (i,),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS) "
                "VALUES (?, ?, 0, 4, ?, ?)",
                (i, f"missed_{i}", 720000000 + i, i),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME) "
                "VALUES (?, ?, '15557778888@s.whatsapp.net', 4)",
                (i, i),
            )
        for i in range(61, 63):
            call_conn.execute(
                "INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING) VALUES (?, 1, 0)",
                (i,),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS) "
                "VALUES (?, ?, 45, 0, ?, ?)",
                (i, f"vid_{i}", 720000100 + i, i),
            )
            call_conn.execute(
                "INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME) "
                "VALUES (?, ?, '15557778888@s.whatsapp.net', 0)",
                (i, i),
            )
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            missed_resp = client.get("/api/calls?filter=missed&limit=10").get_json()
            assert len(missed_resp["calls"]) == 5
            assert all(c["call_type"] == "missed" for c in missed_resp["calls"])
            assert missed_resp["calls"][0]["caller_name"] == "Saved Caller Name"

            video_resp = client.get("/api/calls?filter=video&limit=10").get_json()
            assert len(video_resp["calls"]) == 2
            assert all(c["is_video"] is True for c in video_resp["calls"])

    def test_android_calls_group_preserves_subject(self, tmp_path):
        """Android /api/calls preserves group chat subject instead of overwriting with caller's contact name."""
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15551234', 'Alice (15551234)', 'Alice')"
        )
        arch_conn.commit()
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string)
            VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net'),
                   (2, '120363000111', 'g.us', '120363000111@g.us');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (20, 2, 'Weekend Crew', 1700000050000);
            INSERT INTO call_log VALUES (1, 'grp_call_1', 1, 0, 1700000040000, 1, 90, 1, 2);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            calls = client.get("/api/calls?limit=10").get_json()["calls"]
            assert len(calls) == 1
            assert calls[0]["chat_id"] == "20"
            assert calls[0]["caller_name"] == "Weekend Crew"

    def test_api_chat_starred_exact_total_and_pagination_beyond_100(self, tmp_path):
        """GET /api/chat/<id>/starred returns exact count regardless of limit and paginates past 100."""
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
        if "starred" not in msg_cols:
            wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
        wa_conn.executescript("""
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '15551234', '15551234@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000200000);
        """)
        for i in range(1, 106):
            wa_conn.execute(
                "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred) "
                "VALUES (?, 10, 0, 1, ?, ?, 0, ?, 1)",
                (i, 1700000000000 + i * 1000, f"Starred {i}", f"STAR_{i}"),
            )
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            r1 = client.get(
                "/api/chat/15551234/starred?chat_type=contact&limit=5&offset=0"
            ).get_json()
            assert r1["count"] == 105
            assert len(r1["messages"]) == 5

            r2 = client.get(
                "/api/chat/15551234/starred?chat_type=contact&limit=10&offset=100"
            ).get_json()
            assert r2["count"] == 105
            assert len(r2["messages"]) == 5

    def test_recent_messages_user_version_3_migration(self, tmp_path):
        """Upgrading archive DB from user_version=3 clears stale recent_messages and bumps user_version to 4."""
        arch_path = tmp_path / ".wa_media_archiver.db"
        arch_conn = make_archive_db(arch_path)
        arch_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN is_starred INTEGER NOT NULL DEFAULT 0"
        )
        arch_conn.execute(
            "ALTER TABLE recent_messages ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0"
        )
        arch_conn.execute("""
            INSERT INTO recent_messages (
                chat_id, chat_type, msg_id, stanza_id, timestamp_ms, sender,
                from_me, media_type, text_body, is_starred, is_pinned
            ) VALUES (
                '15551234', 'contact', 1, 'M_STAR_PIN', 1700000010000, 'Alice',
                0, 'text', 'Important msg', 0, 0
            )
        """)
        arch_conn.execute("PRAGMA user_version = 3")
        arch_conn.commit()
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        msg_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(message)").fetchall()}
        if "starred" not in msg_cols:
            wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER DEFAULT 0")
        wa_conn.executescript("""
            CREATE TABLE message_add_on (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_message_row_id INTEGER NOT NULL,
                message_add_on_type INTEGER NOT NULL,
                expiry_timestamp INTEGER,
                timestamp INTEGER
            );
            CREATE TABLE message_add_on_pin_in_chat (
                _id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_add_on_row_id INTEGER NOT NULL,
                pin_in_chat_state INTEGER NOT NULL
            );
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '15551234', '15551234@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id, starred)
            VALUES (1, 10, 0, 1, 1700000010000, 'Important msg', 0, 'M_STAR_PIN', 1);
            INSERT INTO message_add_on (_id, parent_message_row_id, message_add_on_type) VALUES (1, 1, 79);
            INSERT INTO message_add_on_pin_in_chat (message_add_on_row_id, pin_in_chat_state) VALUES (1, 1);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with sqlite3.connect(str(arch_path)) as ac:
            uv = ac.execute("PRAGMA user_version").fetchone()[0]
            assert uv == 6
            cnt = ac.execute("SELECT COUNT(*) FROM recent_messages").fetchone()[0]
            assert cnt == 0

        with app.test_client() as client:
            msgs = client.get(
                "/api/messages?chat_id=15551234&chat_type=contact&limit=10"
            ).get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_starred"] == 1
            assert msgs[0]["is_pinned"] == 1

    def test_messages_inclusive_pagination_and_preserved_pin_jump(self, tmp_path):
        """inclusive=1 includes boundary ties, api/messages/at keeps target with dense calls, and preserved pin jumps by -rowid."""
        from wab_archiver.preservation import PRESERVED_SCHEMA

        wa_conn = make_android_db(tmp_path / "msgstore.db")
        jid_cols = {r[1] for r in wa_conn.execute("PRAGMA table_info(jid)").fetchall()}
        if "server" not in jid_cols:
            wa_conn.execute("ALTER TABLE jid ADD COLUMN server TEXT")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT NOT NULL,
                jid_row_id INTEGER NOT NULL,
                from_me INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            );
            INSERT INTO jid (_id, user, server, raw_string) VALUES (1, '15556789', 's.whatsapp.net', '15556789@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000001000, 'Oldest', 0, 'T_OLD'),
                   (2, 10, 0, 1, 1700000005000, 'Tie 1', 0, 'T_1'),
                   (3, 10, 0, 1, 1700000005000, 'Tie 2', 0, 'T_2'),
                   (4, 10, 0, 1, 1700000005000, 'Tie 3', 0, 'T_3');
        """)
        for idx in range(1, 6):
            wa_conn.execute(
                "INSERT INTO call_log VALUES (?, ?, 1, 0, 1700000005000, 0, 10, 1, NULL)",
                (idx, f"tie_call_{idx}"),
            )
        wa_conn.commit()
        wa_conn.close()

        pres_conn = sqlite3.connect(str(tmp_path / "preserved_messages.db"))
        pres_conn.executescript(PRESERVED_SCHEMA)
        pres_conn.executescript("""
            INSERT INTO preserved_chats (chat_id, chat_type, subject, description, created_ts, oldest_ts, newest_ts, msg_count, members_json)
            VALUES ('15556789', 'contact', 'Alice', NULL, NULL, 1700000000000, 1700000009000, 1, NULL);
            INSERT INTO preserved_messages (
                rowid, chat_id, chat_type, msg_id, stanza_id, timestamp_ms,
                sender, from_me, media_type, text_body, is_pinned, preserved_at, preservation_reason, source_platform
            ) VALUES (
                42, '15556789', 'contact', '999', 'PRES_PIN_1', 1700000009000,
                'Alice', 0, 'text', 'Preserved Pin Target', 1, '2026-01-01T00:00:00Z', 'deleted', 'android'
            );
        """)
        pres_conn.commit()
        pres_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            # 1. inclusive=1 includes tied timestamp_ms rows on upward pagination
            inc_page = client.get(
                "/api/messages?chat_id=15556789&chat_type=contact&limit=10&before=1700000005000&inclusive=1"
            ).get_json()
            inc_ids = [m["msg_id"] for m in inc_page]
            assert 2 in inc_ids and 3 in inc_ids and 4 in inc_ids and 1 in inc_ids

            # 2. api/messages/at keeps requested target even with dense same-timestamp calls
            at_tie = client.get(
                "/api/messages/at?chat_id=15556789&chat_type=contact&ts=1700000005000&msg_id=2&limit=4"
            ).get_json()
            assert any(m["msg_id"] == 2 for m in at_tie)

            # 3. Preserved pin uses negative -rowid and jumps accurately via api/messages/at
            pins = client.get("/api/chat/15556789/pinned?chat_type=contact").get_json()[
                "pinned_messages"
            ]
            assert any(p["id"] == -42 for p in pins)
            jump_pin = client.get(
                "/api/messages/at?chat_id=15556789&chat_type=contact&msg_id=-42&limit=10"
            ).get_json()
            assert any(
                m["msg_id"] == -42 and m["text_body"] == "Preserved Pin Target" for m in jump_pin
            )

    def test_multi_app_context_isolation(self, tmp_path):
        """Multiple create_app() instances retain independent ViewerContext and QuerySet state."""
        rich_dir = tmp_path / "android_rich"
        plain_dir = tmp_path / "android_plain"
        ios_dir = tmp_path / "ios_workspace"
        for d in (rich_dir, plain_dir, ios_dir):
            d.mkdir()

        # 1. Android workspace WITH message_edit_info
        make_archive_db(rich_dir / ".wa_media_archiver.db").close()
        rich_conn = make_android_db(rich_dir / "msgstore.db")
        rich_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_edit_info (
                message_row_id INTEGER PRIMARY KEY,
                original_key_id TEXT,
                edited_timestamp INTEGER DEFAULT 0,
                sender_timestamp INTEGER DEFAULT 0
            );
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15551110001', '15551110001@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (10, 1, 'Rich Chat', 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000010000, 'Edited Hello', 0, 'RICH_1');
            INSERT INTO message_edit_info (message_row_id, original_key_id, edited_timestamp, sender_timestamp)
            VALUES (1, 'RICH_1', 1700000015000, 1700000010000);
        """)
        rich_conn.commit()
        rich_conn.close()

        # 2. Android workspace WITHOUT message_edit_info
        make_archive_db(plain_dir / ".wa_media_archiver.db").close()
        plain_conn = make_android_db(plain_dir / "msgstore.db")
        plain_conn.executescript("""
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15551110002', '15551110002@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (10, 1, 'Plain Chat', 1700000020000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000020000, 'Plain Hello', 0, 'PLAIN_1');
        """)
        plain_conn.commit()
        plain_conn.close()

        # 3. iOS workspace
        make_archive_db(ios_dir / ".wa_media_archiver.db").close()
        ios_conn = make_ios_db(ios_dir / "ChatStorage.sqlite")
        ios_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZLASTMESSAGEDATE)
            VALUES (1, '15551110003@s.whatsapp.net', 'iOS Contact', 0, 720000000.0);
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT)
            VALUES (1, 1, 0, 720000000.0, 'IOS_1', 0, 'iOS Hello');
        """)
        ios_conn.commit()
        ios_conn.close()

        rich_app = viewer.create_app(rich_dir, rescan=False)
        plain_app = viewer.create_app(plain_dir, rescan=False)
        ios_app = viewer.create_app(ios_dir, rescan=False)
        for a in (rich_app, plain_app, ios_app):
            a.config["TESTING"] = True

        rich_ctx = rich_app.extensions["viewer"]
        plain_ctx = plain_app.extensions["viewer"]
        ios_ctx = ios_app.extensions["viewer"]

        assert rich_ctx.source_type == "android"
        assert rich_ctx.caps.has_android_edit_info is True
        assert plain_ctx.source_type == "android"
        assert plain_ctx.caps.has_android_edit_info is False
        assert ios_ctx.source_type == "ios"

        # Query all three apps interleaved in the same process after all apps are built
        with (
            rich_app.test_client() as rc,
            plain_app.test_client() as pc,
            ios_app.test_client() as ic,
        ):
            r_msgs = rc.get("/api/messages?chat_id=15551110001&chat_type=contact").get_json()
            p_msgs = pc.get("/api/messages?chat_id=15551110002&chat_type=contact").get_json()
            i_msgs = ic.get("/api/messages?chat_id=1&chat_type=contact").get_json()

            assert len(r_msgs) == 1
            assert r_msgs[0]["text_body"] == "Edited Hello"
            assert r_msgs[0]["is_edited"] == 1

            assert len(p_msgs) == 1
            assert p_msgs[0]["text_body"] == "Plain Hello"
            assert p_msgs[0]["is_edited"] == 0

            assert len(i_msgs) == 1
            assert i_msgs[0]["text_body"] == "iOS Hello"

    def test_multi_app_android_chat_row_cache_isolation(self, tmp_path):
        """Two Android apps sharing the same contact/group chat_id must not cross-contaminate row ID caches."""
        dir_a = tmp_path / "android_a"
        dir_b = tmp_path / "android_b"
        dir_a.mkdir()
        dir_b.mkdir()

        make_archive_db(dir_a / ".wa_media_archiver.db").close()
        conn_a = make_android_db(dir_a / "msgstore.db")
        conn_a.executescript("""
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15559990001', '15559990001@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (10, 1, NULL, 1700000010000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 10, 0, 1, 1700000010000, 'From App A', 0, 'A_1');
        """)
        conn_a.commit()
        conn_a.close()

        make_archive_db(dir_b / ".wa_media_archiver.db").close()
        conn_b = make_android_db(dir_b / "msgstore.db")
        conn_b.executescript("""
            INSERT INTO jid (_id, user, raw_string)
            VALUES (1, '15559990001', '15559990001@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp)
            VALUES (99, 1, NULL, 1700000020000);
            INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id)
            VALUES (1, 99, 0, 1, 1700000020000, 'From App B', 0, 'B_1');
        """)
        conn_b.commit()
        conn_b.close()

        app_a = viewer.create_app(dir_a, rescan=False)
        app_b = viewer.create_app(dir_b, rescan=False)
        app_a.config["TESTING"] = True
        app_b.config["TESTING"] = True

        with app_a.test_client() as ca, app_b.test_client() as cb:
            assert len(ca.get("/api/chats").get_json()) == 1
            msgs_a = ca.get("/api/messages?chat_id=15559990001&chat_type=contact").get_json()
            msgs_b = cb.get("/api/messages?chat_id=15559990001&chat_type=contact").get_json()

            assert len(msgs_a) == 1
            assert msgs_a[0]["text_body"] == "From App A"
            assert len(msgs_b) == 1
            assert msgs_b[0]["text_body"] == "From App B"

    def test_indexing_routes_not_duplicated_and_ios_location_chat_preview(self, tmp_path):
        """Indexing blueprint must register each route once and iOS location preview must not fail on sqlite3.Row."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        ios_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        ios_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZLASTMESSAGEDATE)
            VALUES (1, '15559990002@s.whatsapp.net', 'Location Contact', 0, 720000000.0);
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT)
            VALUES (1, 1, 0, 720000000.0, 'LOC_1', 5, 'Cafe Spot');
        """)
        ios_conn.commit()
        ios_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        rule_counts: dict[str, int] = {}
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith("/api/"):
                rule_counts[rule.rule] = rule_counts.get(rule.rule, 0) + 1
        assert rule_counts["/api/index-status"] == 1
        assert rule_counts["/api/preferences"] == 2  # GET and POST

        with app.test_client() as client:
            chats = client.get("/api/chats").get_json()
            assert len(chats) == 1
            assert chats[0]["last_msg_type"] == "location"

    def test_long_message_collapse_assets_present(self, tmp_path):
        """app.css and app.js must include dual-theme Read More tokens, Pico resets, and collapse helpers."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            css = client.get("/static/app.css").get_data(as_text=True)
            js = client.get("/static/app.js").get_data(as_text=True)

        assert "--bubble-action:" in css
        assert "--bubble-action-sent:" in css
        assert ".msg-text .msg-read-more" in css
        assert "COLLAPSE_CHAR_LIMIT = 750" in js
        assert "COLLAPSE_LINE_LIMIT = 15" in js
        assert "COLLAPSE_MIN_REMAINDER = 60" in js
        assert "function renderCollapsibleMessageText(" in js
        assert "expandedMsgIds.clear()" in js

    def test_grouped_media_caption_uses_first_non_empty_member(self, tmp_path):
        """Grouped media bubbles must render captions stored on any album member (e.g. iOS group[0])."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        make_android_db(tmp_path / "msgstore.db").close()
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True

        with app.test_client() as client:
            js = client.get("/static/app.js").get_data(as_text=True)

        assert "const captionMsg = group.find(m => m.text_body && m.text_body.trim());" in js

    def test_ios_large_group_query_plan_and_pagination(self, tmp_path):
        """iOS /api/messages and /api/messages/at must use index scans without temp B-tree sort."""
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        ios_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        ios_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZGROUPINFO, ZHIDDEN, ZARCHIVED, ZLASTMESSAGEDATE)
            VALUES (1, '120363000000000001@g.us', 'Large Archived Group', 1, 0, 1, 720005000.0);
        """)
        rows = [(i, 1, 0, 720000000.0 + i, f"MSG_{i}", 0, f"Message {i}") for i in range(1, 2001)]
        ios_conn.executemany(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZSTANZAID, ZMESSAGETYPE, ZTEXT) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        ios_conn.commit()
        ios_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        ctx = app.extensions["viewer"]
        conn = ctx.get_wa()

        indexes = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_zwamessage_chat_ts" in indexes
        assert "idx_zwamessage_pin_lookup" in indexes
        assert "idx_zwamessage_stanza" in indexes

        plan_rows = conn.execute(
            f"{ctx.queries.ios_select} WHERE m.ZCHATSESSION = CAST(? AS INTEGER) "
            f"{ctx.queries.ios_filter} ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT ?",
            ["1", 50],
        ).fetchall()
        assert len(plan_rows) == 50

        eqp = conn.execute(
            f"EXPLAIN QUERY PLAN {ctx.queries.ios_select} WHERE m.ZCHATSESSION = CAST(? AS INTEGER) "
            f"{ctx.queries.ios_filter} ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT ?",
            ["1", 50],
        ).fetchall()
        plan_text = "\n".join(str(r[-1]) for r in eqp)
        assert "idx_zwamessage_chat_ts" in plan_text
        assert "USE TEMP B-TREE FOR ORDER BY" not in plan_text
        assert "idx_zwamessage_pin_lookup" in plan_text

        with app.test_client() as client:
            latest = client.get("/api/messages?chat_id=1&chat_type=group&limit=50").get_json()
            assert len(latest) == 50
            assert latest[0]["text_body"] == "Message 2000"
            assert latest[-1]["text_body"] == "Message 1951"

            older = client.get(
                f"/api/messages?chat_id=1&chat_type=group&limit=50&before={latest[-1]['timestamp_ms']}"
            ).get_json()
            assert len(older) == 50
            assert older[0]["text_body"] == "Message 1950"

            around = client.get(
                f"/api/messages/at?chat_id=1&chat_type=group&ts={older[0]['timestamp_ms']}&limit=10"
            ).get_json()
            assert any(m["text_body"] == "Message 1950" for m in around)

    def test_collections_server_search_and_preview_richness(self, tmp_path):
        """Verify /api/calls and /api/starred server search (?q=) and preview metadata."""
        wa_path = tmp_path / "msgstore.db"
        archive_path_db = tmp_path / ".wa_media_archiver.db"
        pres_path = tmp_path / "preserved_messages.db"

        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path_db)
        seed_android_db(wa_conn, archive_conn)

        wa_conn.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                _id INTEGER PRIMARY KEY,
                call_id TEXT,
                jid_row_id INTEGER,
                from_me INTEGER,
                timestamp INTEGER,
                video_call INTEGER,
                duration INTEGER,
                call_result INTEGER,
                group_jid_row_id INTEGER
            )
        """)
        wa_conn.execute("ALTER TABLE message ADD COLUMN starred INTEGER")
        wa_conn.execute("ALTER TABLE message_media ADD COLUMN media_duration INTEGER")

        # Insert live call and live starred message with rich preview columns
        wa_conn.execute(
            "INSERT INTO call_log (_id, call_id, jid_row_id, from_me, timestamp, video_call, duration, call_result) "
            "VALUES (10, 'call_abc_1', 1, 0, 1700000000000, 1, 120, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, starred) "
            "VALUES (201, 10, 0, 1, 1700000000000, 'Project Roadmap', 9, 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path, media_name, media_duration) "
            "VALUES (201, 'Media/WhatsApp Documents/Roadmap.pdf', 'Roadmap.pdf', 0)"
        )
        wa_conn.commit()

        # Insert preserved call and preserved starred message
        with sqlite3.connect(str(pres_path)) as pres_conn:
            pres_conn.executescript("""
                CREATE TABLE preserved_calls (
                    call_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    caller_jid TEXT,
                    caller_name TEXT,
                    timestamp_ms INTEGER,
                    duration_seconds INTEGER,
                    is_video INTEGER,
                    from_me INTEGER,
                    call_result INTEGER
                );
                CREATE TABLE preserved_messages (
                    stanza_id TEXT,
                    chat_id TEXT,
                    chat_type TEXT,
                    sender_jid TEXT,
                    sender_name TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    media_type TEXT,
                    from_me INTEGER,
                    is_starred INTEGER,
                    media_duration INTEGER,
                    original_path TEXT
                );
            """)
            pres_conn.execute(
                "INSERT INTO preserved_calls (call_id, chat_id, caller_jid, caller_name, from_me, timestamp_ms, duration_seconds, is_video, call_result) "
                "VALUES ('pres_call_1', '15551234567', '15551234567@s.whatsapp.net', 'Bob Architect', 0, 1700000050000, 45, 0, 1)"
            )
            pres_conn.execute(
                "INSERT INTO preserved_messages (stanza_id, chat_id, chat_type, sender_jid, sender_name, timestamp_ms, text_body, media_type, from_me, is_starred, media_duration, original_path) "
                "VALUES ('pres_msg_1', '15551234567', 'contact', '15551234567@s.whatsapp.net', 'Bob Architect', 1700000050000, 'Audio note', 'audio', 0, 1, 65, 'Media/WhatsApp Voice/voice.opus')"
            )
            pres_conn.commit()

        app = viewer.create_app(tmp_path)
        client = app.test_client()

        # 1. Test /api/calls server search (?q=)
        res_calls_all = client.get("/api/calls")
        assert res_calls_all.status_code == 200
        calls_data = res_calls_all.get_json()
        assert len(calls_data["calls"]) == 2
        # Check preserved and group flags
        pres_call_item = next(c for c in calls_data["calls"] if c["id"] == "pres_call_1")
        assert pres_call_item["is_preserved"] is True

        res_calls_q = client.get("/api/calls?q=Architect")
        assert res_calls_q.status_code == 200
        q_calls = res_calls_q.get_json()["calls"]
        assert len(q_calls) == 1
        assert q_calls[0]["id"] == "pres_call_1"

        # 2. Test /api/starred server search (?q=) and preview metadata
        res_starred_all = client.get("/api/starred")
        assert res_starred_all.status_code == 200
        starred_data = res_starred_all.get_json()
        assert len(starred_data["messages"]) == 2

        doc_msg = next(m for m in starred_data["messages"] if m["id"] == 201)
        assert doc_msg["media_type"] == "document"
        assert doc_msg["file_name"] == "Roadmap.pdf"

        audio_msg = next(m for m in starred_data["messages"] if m["id"] < 0)
        assert audio_msg["media_type"] == "audio"
        assert audio_msg["duration"] == 65

        res_starred_q = client.get("/api/starred?q=Roadmap")
        assert res_starred_q.status_code == 200
        q_starred = res_starred_q.get_json()["messages"]
        assert len(q_starred) == 1
        assert q_starred[0]["id"] == 201


class TestPinnedMessagesRichPreviews:
    """Tests for pinned message resolution, anti-stanza-ID protection, and preview richness."""

    def test_ios_pinned_resolves_type_66_stanza_id_when_parent_is_null(self, tmp_path):
        """iOS ZMESSAGETYPE=66 with ZPARENTMESSAGE=NULL resolves to target message via ZSTANZAID."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            -- Target message: text
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (10, 1, 0, 720000000, 'Important meeting agenda', 0, '3EB0TARGETSTANZAPIN1');

            -- Announcement message (type 66): ZTEXT contains target stanza ID, ZPARENTMESSAGE is NULL
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPARENTMESSAGE)
            VALUES (11, 1, 0, 720000050, '3EB0TARGETSTANZAPIN1', 66, '3EB0ANNOUNCESTANZAPIN1', NULL);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            # Must resolve to target message PK 10, NOT the announcement PK 11
            assert pin["id"] == 10
            # Must show target text, NOT the raw stanza ID
            assert pin["text_body"] == "Important meeting agenda"
            assert pin["is_pinned"] is True

    def test_ios_pinned_media_richness(self, tmp_path):
        """iOS pinned media message projects media_type, caption, and filename via ZWAMEDIAITEM."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_cols = {r[1] for r in cs_conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()}
        if "ZMOVIEDURATION" not in cs_cols:
            cs_conn.execute("ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZMOVIEDURATION INTEGER")
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH, ZMOVIEDURATION)
            VALUES (101, 'Sunset at the lake', 'Media/Photos/sunset.jpg', 0),
                   (102, 'Sprint Demo Recording', 'Media/Videos/demo.mp4', 125);

            -- Photo message with caption in media item
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZMEDIAITEM)
            VALUES (20, 1, 0, 720000100, NULL, 1, '3EB0PHOTOSUNSET1', 101);

            -- Video message with caption and duration
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZMEDIAITEM)
            VALUES (21, 1, 0, 720000200, NULL, 2, '3EB0VIDEODEMO1', 102);

            -- Pin announcements pointing to the media messages
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPARENTMESSAGE)
            VALUES (22, 1, 0, 720000150, '3EB0PHOTOSUNSET1', 66, '3EB0PINANNOUNCE1', NULL),
                   (23, 1, 0, 720000250, '3EB0VIDEODEMO1', 66, '3EB0PINANNOUNCE2', NULL);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 2

            video_pin = next(p for p in pins if p["id"] == 21)
            assert video_pin["media_type"] == "video"
            assert video_pin["text_body"] == "Sprint Demo Recording"
            assert video_pin["duration"] == 125
            assert video_pin["file_name"] == "demo.mp4"

            photo_pin = next(p for p in pins if p["id"] == 20)
            assert photo_pin["media_type"] == "photo"
            assert photo_pin["text_body"] == "Sunset at the lake"
            assert photo_pin["file_name"] == "sunset.jpg"

    def test_ios_pinned_unresolved_never_exposes_stanza_id(self, tmp_path):
        """When an iOS type 66 pin targets a missing message, raw stanza ID is suppressed."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            -- Announcement pointing to a purged message
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPARENTMESSAGE)
            VALUES (30, 1, 0, 720000050, '3EB0DELETEDSTANZAPIN999', 66, '3EB0ANNOUNCE30', NULL);
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            # Raw stanza ID must NOT be leaked
            assert "3EB0" not in pin["text_body"]
            assert pin["text_body"] == "Pinned message"

    def test_android_pinned_media_richness(self, tmp_path):
        """Android pinned messages project media_type, duration, file_name, and place_name."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER,
                    expiry_timestamp INTEGER
                );
                CREATE TABLE message_add_on_pin_in_chat (
                    message_add_on_row_id INTEGER PRIMARY KEY,
                    pin_in_chat_state INTEGER,
                    sender_timestamp INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_duration INTEGER,
                    media_name TEXT
                );
                CREATE TABLE message_location (
                    message_row_id INTEGER PRIMARY KEY,
                    latitude REAL,
                    longitude REAL,
                    place_name TEXT,
                    place_address TEXT
                );

                INSERT INTO jid VALUES (1, '15551111', '15551111@s.whatsapp.net');
                INSERT INTO chat VALUES (10, 1, 'Pinned Chat');

                -- Message 50: Photo with file_path and text caption
                INSERT INTO message VALUES (50, 10, 0, 1, 1793970000000, 'Mountain peak', 1, 'KEY_PHOTO_50');
                INSERT INTO message_media VALUES (50, '/storage/emulated/0/WhatsApp/Media/peak.jpg', 'image/jpeg', 0, 'peak.jpg');
                INSERT INTO message_add_on VALUES (1, 50, 10, 0, 1, 'ADDON_PIN_50', 1793970500000, 79, 0, 9999999999000);
                INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);

                -- Message 51: Audio with media_duration
                INSERT INTO message VALUES (51, 10, 0, 1, 1793970100000, '', 3, 'KEY_AUDIO_51');
                INSERT INTO message_media VALUES (51, '/storage/emulated/0/WhatsApp/Media/voice.opus', 'audio/ogg', 82, 'voice.opus');
                INSERT INTO message_add_on VALUES (2, 51, 10, 0, 1, 'ADDON_PIN_51', 1793970600000, 79, 0, 9999999999000);
                INSERT INTO message_add_on_pin_in_chat VALUES (2, 1, 1793970600000);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 2

            photo_pin = next(p for p in pins if p["id"] == 50)
            assert photo_pin["media_type"] == "photo"
            assert photo_pin["text_body"] == "Mountain peak"
            assert photo_pin["file_name"] == "peak.jpg"

            audio_pin = next(p for p in pins if p["id"] == 51)
            assert audio_pin["media_type"] == "audio"
            assert audio_pin["duration"] == 82

    def test_preserved_pinned_media_richness(self, tmp_path):
        """Preserved pinned messages project media_type, duration, file_name, and place_name."""
        make_ios_db(tmp_path / "ChatStorage.sqlite")
        pres_db = tmp_path / "preserved_messages.db"
        with sqlite3.connect(str(pres_db)) as conn:
            conn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER,
                    chat_id TEXT,
                    chat_type TEXT,
                    from_me INTEGER,
                    sender_name TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    media_type TEXT,
                    media_duration INTEGER,
                    original_path TEXT,
                    place_name TEXT,
                    is_pinned INTEGER DEFAULT 0,
                    pin_expiry_ts INTEGER
                );
                INSERT INTO preserved_messages (
                    msg_id, chat_id, chat_type, from_me, sender_name, timestamp_ms,
                    text_body, media_type, media_duration, original_path, place_name, is_pinned, pin_expiry_ts
                ) VALUES (
                    88, '1001', 'contact', 0, 'Alice', 1700000000000,
                    'Check out this document', 'document', 0, 'Media/Documents/Specs.pdf', '', 1, 9999999999000
                );
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1001/pinned")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            doc_pin = pins[0]
            assert doc_pin["media_type"] == "document"
            assert doc_pin["file_name"] == "Specs.pdf"
            assert doc_pin["text_body"] == "Check out this document"
            assert doc_pin["is_pinned"] is True

    def test_frontend_message_preview_contracts(self, tmp_path):
        """app.js contains _formatMessagePreview, stanza ID sanitization, and shared formatting."""
        make_ios_db(tmp_path / "ChatStorage.sqlite")
        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/static/app.js")
            assert resp.status_code == 200
            js = resp.get_data(as_text=True)
            assert "function _formatMessagePreview(m)" in js
            assert "_isRawStanzaId" in js
            assert "_isUnreadableToken" in js
            assert "snippetEl.textContent = _formatMessagePreview(pin);" in js

    def test_ios_pinned_album_container_never_treated_as_pin(self, tmp_path):
        """iOS type 66 media album containers with ZMEDIAITEM must NEVER be treated as pins."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            INSERT INTO ZWAMEDIAITEM (Z_PK, ZTITLE, ZMEDIALOCALPATH)
            VALUES (101, NULL, NULL);

            -- Media album container (type 66 with media item and base64 push name)
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZMEDIAITEM, ZPUSHNAME)
            VALUES (50, 1, 0, 720000000, NULL, 66, '3A86D5BC08A513A4AEE7', 101, 'CKji9NUGIABIAZABAPABAtgCu5PzutWWlwM=');

            -- Normal message in same chat
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (51, 1, 0, 720000010, 'Hello there', 0, '3EB0NORMALMSG51');
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 0
            assert resp.get_json()["pinned_message"] is None

            # Verify GET /api/messages does not mark messages as is_pinned = 1
            resp_m = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp_m.status_code == 200
            for m in resp_m.get_json():
                assert m.get("is_pinned", 0) == 0

    def test_ios_pinned_deleted_in_extchat_thread_id_excluded(self, tmp_path):
        """In ExtChatDatabase.sqlite, thread_id with deleted = 1 is excluded from pins."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (60, 1, 0, 720000000, 'Unpinned message', 0, '3EB0UNPINNED60');
        """)
        cs_conn.commit()
        cs_conn.close()

        ext_db = tmp_path / "ExtChatDatabase.sqlite"
        ext_conn = sqlite3.connect(str(ext_db))
        ext_conn.executescript("""
            CREATE TABLE thread_id (
                id INTEGER PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                key_id TEXT NOT NULL,
                pin_timestamp INTEGER,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE thread_messages (
                id INTEGER PRIMARY KEY,
                stanza_id TEXT NOT NULL,
                thread_key_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL
            );
            -- An unpinned message (deleted = 1)
            INSERT INTO thread_id (id, chat_jid, key_id, pin_timestamp, deleted)
            VALUES (1, '12345678@s.whatsapp.net', '3EB0UNPINNED60', 1700000000, 1);
            INSERT INTO thread_messages (id, stanza_id, thread_key_id, chat_jid)
            VALUES (1, '3EB0UNPINNED60', '3EB0UNPINNED60', '12345678@s.whatsapp.net');
        """)
        ext_conn.commit()
        ext_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 0

            resp_m = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp_m.status_code == 200
            msgs = resp_m.get_json()
            assert len(msgs) == 1
            assert msgs[0]["is_pinned"] == 0

    def test_ios_pinned_unreadable_token_push_name_sanitized(self, tmp_path):
        """Base64 push name or LID token is sanitized and not returned as sender_name."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEERRORSTATUS INTEGER")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '12345678@s.whatsapp.net', 'Alice', 0);

            -- Target message with base64 binary push name
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZPUSHNAME, ZFROMJID)
            VALUES (70, 1, 0, 720000000, 'Important pinned note', 0, '3EB0VALIDPIN70', 'CKji9NUGIABIAZABAPABAtgCu5PzutWWlwM=', '393481234567@s.whatsapp.net');

            -- Valid pin announcement targeting message 70
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID)
            VALUES (71, 1, 0, 720000050, '3EB0VALIDPIN70', 66, '3EB0PINANNOUNCE71');
        """)
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            # Must NOT expose base64 token
            assert "CKji9NUG" not in pin["sender_name"]
            # Must resolve to phone number or Unknown
            assert pin["sender_name"] in ("+393481234567", "393481234567", "Unknown")

    def test_ios_pinned_chat_session_metadata_resolution(self, tmp_path):
        """Modern iOS pin resolution via ZWACHATSESSION.ZLASTMESSAGETEXT protobuf."""

        def encode_varint(v: int) -> bytes:
            b = bytearray()
            while True:
                part = v & 0x7F
                v >>= 7
                if v:
                    b.append(part | 0x80)
                else:
                    b.append(part)
                    break
            return bytes(b)

        # Build submessage tag 4 (duration and status)
        sub4 = bytearray()
        sub4.extend(encode_varint((1 << 3) | 0))  # tag 1: duration
        sub4.extend(encode_varint(2592000))
        sub4.extend(encode_varint((2 << 3) | 0))  # tag 2: status (1 = active)
        sub4.extend(encode_varint(1))

        # Build tag 31 submessage
        target_key = b"120363000000000005@g.us_3EB0TARGETPIN10_0_145500000000005@lid"
        sub31 = bytearray()
        sub31.extend(encode_varint((1 << 3) | 2))  # tag 1: action stanza ID
        sub31.extend(encode_varint(len(b"3EB0PINACT10")))
        sub31.extend(b"3EB0PINACT10")
        sub31.extend(encode_varint((2 << 3) | 2))  # tag 2: target key
        sub31.extend(encode_varint(len(target_key)))
        sub31.extend(target_key)
        sub31.extend(encode_varint((3 << 3) | 2))  # tag 3: pinner LID
        sub31.extend(encode_varint(len(b"145500000000005@lid")))
        sub31.extend(b"145500000000005@lid")
        sub31.extend(encode_varint((4 << 3) | 2))  # tag 4: meta
        sub31.extend(encode_varint(len(sub4)))
        sub31.extend(sub4)
        sub31.extend(encode_varint((5 << 3) | 0))  # tag 5: pin ts
        sub31.extend(encode_varint(1728307200000))

        # Root message
        root = bytearray()
        root.extend(encode_varint((31 << 3) | 2))
        root.extend(encode_varint(len(sub31)))
        root.extend(sub31)
        b64_payload = base64.b64encode(bytes(root)).decode("ascii")

        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        try:
            cs_conn.execute("ALTER TABLE ZWACHATSESSION ADD COLUMN ZLASTMESSAGETEXT TEXT")
        except sqlite3.OperationalError:
            pass
        cs_conn.executescript("""
            CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
                Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER,
                Z_OPT INTEGER,
                ZCONTACTABID INTEGER,
                ZISACTIVE INTEGER,
                ZISADMIN INTEGER,
                ZSENDERKEYSENT INTEGER,
                ZCHATSESSION INTEGER,
                ZRECENTGROUPCHAT INTEGER,
                ZCONTACTIDENTIFIER TEXT,
                ZCONTACTNAME TEXT,
                ZFIRSTNAME TEXT,
                ZMEMBERJID TEXT
            );

            CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
                Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER,
                Z_OPT INTEGER,
                ZJID TEXT,
                ZPUSHNAME TEXT
            );

            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (1, '120363000000000005@g.us', 'Project Team', 0);

            INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID)
            VALUES (100, 1, '145500000000005@lid');

            INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME)
            VALUES (200, '145500000000005@lid', 'Alice Smith');

            -- Target message
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZSTANZAID, ZGROUPMEMBER, ZFROMJID)
            VALUES (10, 1, 0, 750000000, 'Meeting notes and roadmap', 0, '3EB0TARGETPIN10', 100, '120363000000000005@g.us');
        """)
        cs_conn.execute(
            "UPDATE ZWACHATSESSION SET ZLASTMESSAGETEXT = ? WHERE Z_PK = 1",
            (b64_payload,),
        )
        cs_conn.commit()
        cs_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/1/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            pin = pins[0]
            assert pin["id"] == 10
            assert pin["sender_name"] == "Alice Smith"
            assert pin["text"] == "Meeting notes and roadmap"
            assert pin["is_pinned"] is True
            assert pin["text"] == "Meeting notes and roadmap"
            assert pin["is_pinned"] is True

    def test_android_and_ios_video_note_contracts(self, tmp_path):
        """Android (message_type=28) and iOS (ZMESSAGETYPE=54) video notes expose is_video_note and raw_msg_type."""
        android_dir = tmp_path / "android"
        android_dir.mkdir()
        wa_conn = make_android_db(android_dir / "msgstore.db")
        arc_conn = make_archive_db(android_dir / ".wa_media_archiver.db")

        wa_conn.execute("INSERT INTO jid VALUES (1, '15551234567', '15551234567@s.whatsapp.net')")
        wa_conn.execute("INSERT INTO chat VALUES (10, 1, 'Alice Chat', 0, 1700000000000, 102)")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
            "VALUES (101, 10, 0, 1, 1700000000000, '', 28, 'STANZA_VN_ANDROID')"
        )
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type, key_id) "
            "VALUES (102, 10, 0, 1, 1700000001000, '', 81, 'STANZA_VN_ANDROID_81')"
        )
        wa_conn.execute(
            "INSERT INTO message_media (_id, message_row_id, chat_row_id, file_path, media_name, mime_type, message_url) "
            "VALUES (101, 101, 10, 'Media/WhatsApp Video Notes/202601/VID-101.mp4', 'VID-101.mp4', 'video/mp4', '')"
        )
        wa_conn.execute(
            "INSERT INTO message_media (_id, message_row_id, chat_row_id, file_path, media_name, mime_type, message_url) "
            "VALUES (102, 102, 10, 'Media/WhatsApp Video Notes/202501/PTV-20250101-WA0010.mp4', 'PTV-20250101-WA0010.mp4', 'video/mp4', '')"
        )
        wa_conn.commit()

        arc_conn.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Video Notes/202601/VID-101.mp4', x'01020304')"
        )
        arc_conn.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Media/WhatsApp Video Notes/202501/PTV-20250101-WA0010.mp4', x'05060708')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/WhatsApp Video Notes/202601/VID-101.mp4', 'Contacts/Alice/2026/Received/VID-101.mp4')"
        )
        arc_conn.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Media/WhatsApp Video Notes/202501/PTV-20250101-WA0010.mp4', 'Contacts/Alice/2025/Received/PTV-20250101-WA0010.mp4')"
        )
        arc_conn.commit()

        app_android = viewer.create_app(android_dir, rescan=False)
        app_android.config["TESTING"] = True
        with app_android.test_client() as client:
            resp = client.get("/api/messages?chat_id=15551234567&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 2
            by_id = {m["msg_id"]: m for m in data}
            assert 101 in by_id
            assert 102 in by_id
            assert by_id[101]["is_video_note"] == 1
            assert by_id[101]["media_type"] == "video"
            assert by_id[101]["archive_path"] == "Contacts/Alice/2026/Received/VID-101.mp4"
            assert by_id[102]["is_video_note"] == 1
            assert by_id[102]["media_type"] == "video"
            assert (
                by_id[102]["archive_path"] == "Contacts/Alice/2025/Received/PTV-20250101-WA0010.mp4"
            )

            # Check chat snippet
            chat_resp = client.get("/api/chats")
            assert chat_resp.status_code == 200
            chats = chat_resp.get_json()
            assert len(chats) >= 1
            c = next(x for x in chats if str(x["id"]) in ("15551234567", "10"))
            assert c["last_msg_type"] == "video"

        # Now verify iOS ZMESSAGETYPE = 54
        ios_dir = tmp_path / "ios"
        ios_dir.mkdir()
        ios_conn = make_ios_db(ios_dir / "ChatStorage.sqlite")
        ios_arc = make_archive_db(ios_dir / ".wa_media_archiver.db")

        ios_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME) VALUES (1, '15559876543@s.whatsapp.net', 'Bob')"
        )
        ios_conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH) VALUES (501, 'Media/VideoNotes/vn_501.mp4')"
        )
        ios_conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZFROMJID, ZTOJID, ZMESSAGEDATE, ZMESSAGETYPE, ZMEDIAITEM, ZISFROMME, ZSTANZAID) "
            "VALUES (501, 1, '15559876543@s.whatsapp.net', 'me@s.whatsapp.net', 700000000, 54, 501, 0, 'STANZA_VN_IOS')"
        )
        ios_conn.commit()

        ios_arc.execute(
            "INSERT INTO files (original_path, md5) VALUES ('Message/Media/VideoNotes/vn_501.mp4', x'05060708')"
        )
        ios_arc.execute(
            "INSERT INTO archive_copies (original_path, archive_path) VALUES "
            "('Message/Media/VideoNotes/vn_501.mp4', 'Contacts/Bob/2026/Received/vn_501.mp4')"
        )
        ios_arc.commit()

        app_ios = viewer.create_app(ios_dir, rescan=False)
        app_ios.config["TESTING"] = True
        with app_ios.test_client() as client:
            resp = client.get("/api/messages?chat_id=1&chat_type=contact")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            msg = data[0]
            assert msg["is_video_note"] == 1
            assert msg["media_type"] == "video"
            assert msg["archive_path"] == "Contacts/Bob/2026/Received/vn_501.mp4"


class TestChatJumpHudPlacement:
    """Tests for chat jump HUD positioning aware of header height and pinned banners."""

    @pytest.fixture
    def app_client(self, tmp_path):
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
            yield client

    def test_jump_hud_css_rules_and_transition(self, app_client):
        """CSS must define variable-backed top offset and smooth vertical transition."""
        resp = app_client.get("/static/app.css")
        assert resp.status_code == 200
        css = resp.get_data(as_text=True)

        assert ".chat-jump-hud" in css
        assert "top: var(--jump-hud-top, 72px);" in css
        assert "transition: opacity 0.2s ease, transform 0.2s ease;" in css
        assert "z-index: 25;" in css

    def test_jump_hud_dom_and_js_engine(self, app_client):
        """DOM and app.js must wire dynamic chat space offset calculation."""
        resp = app_client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

        assert 'id="chat-jump-hud"' in html
        assert 'id="pinned-banner"' in html
        assert 'id="message-scroll"' in html

        js_resp = app_client.get("/static/app.js")
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)

        assert "function updateJumpHudPosition()" in js
        assert "scroll.offsetTop" in js
        assert "topOffset + 12" in js
        # Ensure wired into showJumpHud, showJumpHudError, renderPinnedBanner, and showContextJumpBanner
        assert "updateJumpHudPosition();" in js

    def test_jump_to_message_and_pinned_banner_resilience(self, app_client):
        """Verify jumpToMessage and jumpToTimestamp anchor messages and prevent bottom-snap."""
        js_resp = app_client.get("/static/app.js")
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)

        # jumpToMessage must invalidate _loadGen, cancel bottom-anchor, and call _pinToMsg
        assert "async function jumpToMessage(msg, highlightType)" in js
        assert "await _pinToMsg(inWin, gen);" in js
        assert "isUserAtBottom = false;" in js

        # jumpToTimestamp must ensure isUserAtBottom is false and chat-loading is hidden
        assert "async function jumpToTimestamp(ts, exactMsgId, isExactTarget, highlightType)" in js
        assert "await _pinToMsg(existing, gen);" in js

        # pinned banner must have z-index: 12 so it remains interactive and unblocked
        css_resp = app_client.get("/static/app.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert "#pinned-banner" in css
        assert "z-index: 12;" in css

    def test_android_pinned_without_expiry_timestamp_column(self, tmp_path):
        """Android pinned query succeeds on schemas lacking message_add_on.expiry_timestamp."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );
                CREATE TABLE message_add_on_pin_in_chat (
                    message_add_on_row_id INTEGER PRIMARY KEY,
                    pin_in_chat_state INTEGER,
                    sender_timestamp INTEGER
                );
                CREATE TABLE message_media (
                    message_row_id INTEGER PRIMARY KEY,
                    file_path TEXT,
                    mime_type TEXT,
                    media_duration INTEGER,
                    media_name TEXT
                );

                INSERT INTO jid VALUES (1, '1203630000000001', 'g.us', '1203630000000001@g.us');
                INSERT INTO chat VALUES (10, 1, 'Group Without Expiry Col');
                INSERT INTO message VALUES (50, 10, 0, NULL, 1793970000000, 'Legacy schema pin', 0, 'PIN_1');
                INSERT INTO message_add_on VALUES (1, 50, 10, 0, NULL, 'ADDON_1', 1793970500000, 79, 0);
                INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["id"] == 50
            assert pins[0]["text_body"] == "Legacy schema pin"

    def test_android_pinned_privacy_lid_contact_resolution(self, tmp_path):
        """1-to-1 Privacy LID contact chats resolve pinned messages queried by phone number."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE jid_map (lid_row_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );

                -- Phone JID 1, LID JID 2
                INSERT INTO jid VALUES (1, '15551234', 's.whatsapp.net', '15551234@s.whatsapp.net');
                INSERT INTO jid VALUES (2, '999888777', 'lid', '999888777@lid');
                INSERT INTO jid_map VALUES (2, 1);
                -- Chat uses Privacy LID JID 2
                INSERT INTO chat VALUES (5, 2, NULL);
                INSERT INTO message VALUES (60, 5, 0, NULL, 1793970000000, 'Pinned in LID chat', 0, 'PIN_LID');
                INSERT INTO message_add_on VALUES (1, 60, 5, 0, NULL, 'ADDON_LID', 1793970500000, 79, 0);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/15551234/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["id"] == 60
            assert pins[0]["text_body"] == "Pinned in LID chat"

    def test_android_pinned_1to1_sender_name_resolved(self, tmp_path):
        """Incoming pinned messages in 1-to-1 chats resolve sender name from chat contact."""
        wa_path = tmp_path / "msgstore.db"
        arch_path = tmp_path / ".wa_media_archiver.db"

        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );

                INSERT INTO jid VALUES (1, '15559876', 's.whatsapp.net', '15559876@s.whatsapp.net');
                INSERT INTO chat VALUES (3, 1, NULL);
                -- Incoming message (from_me = 0, sender_jid_row_id = NULL)
                INSERT INTO message VALUES (70, 3, 0, NULL, 1793970000000, '1-to-1 note', 0, 'PIN_1TO1');
                INSERT INTO message_add_on VALUES (1, 70, 3, 0, NULL, 'ADDON_70', 1793970500000, 79, 0);
            """)
            conn.commit()

        with sqlite3.connect(str(arch_path)) as arch_conn:
            arch_conn.executescript("""
                CREATE TABLE contacts (
                    number TEXT PRIMARY KEY,
                    folder TEXT,
                    display_name TEXT
                );
                INSERT INTO contacts VALUES ('15559876', 'Dr. Vance', 'Dr. Vance');
            """)
            arch_conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/15559876/pinned?chat_type=contact")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["sender_name"] == "Dr. Vance"

    def test_android_pinned_group_lid_sender_name_resolved(self, tmp_path):
        """Group chat pinned messages from Privacy LID senders resolve via lid_display_name."""
        wa_path = tmp_path / "msgstore.db"
        with sqlite3.connect(str(wa_path)) as conn:
            conn.executescript("""
                CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
                CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
                CREATE TABLE lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT, username TEXT);
                CREATE TABLE message (
                    _id INTEGER PRIMARY KEY,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    timestamp INTEGER,
                    text_data TEXT,
                    message_type INTEGER,
                    key_id TEXT
                );
                CREATE TABLE message_add_on (
                    _id INTEGER PRIMARY KEY,
                    parent_message_row_id INTEGER,
                    chat_row_id INTEGER,
                    from_me INTEGER,
                    sender_jid_row_id INTEGER,
                    key_id TEXT,
                    timestamp INTEGER,
                    message_add_on_type INTEGER,
                    status INTEGER
                );

                INSERT INTO jid VALUES (1, '1203630000000001', 'g.us', '1203630000000001@g.us');
                INSERT INTO jid VALUES (2, '999111222', 'lid', '999111222@lid');
                INSERT INTO chat VALUES (10, 1, 'Alpine Trail');
                INSERT INTO lid_display_name VALUES (2, 'Maya Lin', 'mayalin');
                INSERT INTO message VALUES (80, 10, 0, 2, 1793970000000, 'Group pin from LID', 0, 'PIN_GRP_LID');
                INSERT INTO message_add_on VALUES (1, 80, 10, 0, 2, 'ADDON_80', 1793970500000, 79, 0);
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["sender_name"] == "Maya Lin"

    def test_android_pinned_unpinned_by_subsequent_addon_excluded(self, tmp_path):
        """A message pinned and subsequently unpinned in a later add-on row is excluded."""
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.executescript("""
            CREATE TABLE IF NOT EXISTS message_add_on (
                _id INTEGER PRIMARY KEY,
                parent_message_row_id INTEGER,
                chat_row_id INTEGER,
                from_me INTEGER,
                sender_jid_row_id INTEGER,
                key_id TEXT,
                timestamp INTEGER,
                message_add_on_type INTEGER,
                status INTEGER
            );
            CREATE TABLE IF NOT EXISTS message_add_on_pin_in_chat (
                message_add_on_row_id INTEGER PRIMARY KEY,
                pin_in_chat_state INTEGER,
                sender_timestamp INTEGER
            );

            INSERT INTO jid (_id, user, raw_string) VALUES (10, '1203630000000001', '1203630000000001@g.us');
            INSERT INTO chat (_id, jid_row_id, subject) VALUES (10, 10, 'Lifecycle Chat');
            INSERT INTO message (_id, chat_row_id, from_me, timestamp, text_data, message_type, key_id)
            VALUES (90, 10, 0, 1793970000000, 'Unpinned message', 0, 'PIN_90');

            -- Event 1: Pinned (state = 1)
            INSERT INTO message_add_on (_id, parent_message_row_id, chat_row_id, from_me, message_add_on_type, timestamp)
            VALUES (1, 90, 10, 0, 79, 1793970500000);
            INSERT INTO message_add_on_pin_in_chat VALUES (1, 1, 1793970500000);

            -- Event 2: Unpinned (state = 2, higher _id)
            INSERT INTO message_add_on (_id, parent_message_row_id, chat_row_id, from_me, message_add_on_type, timestamp)
            VALUES (2, 90, 10, 0, 79, 1793970600000);
            INSERT INTO message_add_on_pin_in_chat VALUES (2, 2, 1793970600000);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 0

            # Also verify /api/messages projection
            m_resp = client.get("/api/messages?chat_id=10&chat_type=group")
            assert m_resp.status_code == 200
            msgs = m_resp.get_json()
            assert msgs[0]["is_pinned"] == 0

    def test_preserved_pinned_historical_backup_not_wallclock_expired(self, tmp_path):
        """Preserved pinned messages older than 30 days are retained without wall-clock expiry."""
        make_ios_db(tmp_path / "ChatStorage.sqlite")
        pres_db = tmp_path / "preserved_messages.db"
        with sqlite3.connect(str(pres_db)) as conn:
            conn.executescript("""
                CREATE TABLE preserved_messages (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER,
                    chat_id TEXT,
                    chat_type TEXT,
                    from_me INTEGER,
                    sender_name TEXT,
                    timestamp_ms INTEGER,
                    text_body TEXT,
                    is_pinned INTEGER DEFAULT 1,
                    pin_expiry_ts INTEGER,
                    media_type TEXT
                );

                -- Preserved pin with pin_expiry_ts from the past (2020)
                INSERT INTO preserved_messages (msg_id, chat_id, chat_type, from_me, sender_name, timestamp_ms, text_body, is_pinned, pin_expiry_ts, media_type)
                VALUES (99, '10', 'group', 0, 'Alice', 1580000000000, 'Old preserved pin', 1, 1582592000000, 'text');
            """)
            conn.commit()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chat/10/pinned?chat_type=group")
            assert resp.status_code == 200
            pins = resp.get_json()["pinned_messages"]
            assert len(pins) == 1
            assert pins[0]["text_body"] == "Old preserved pin"
