"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import base64
import os
import sqlite3
import sys
import time

import pytest
from tests.conftest import (
    make_android_db,
    make_archive_db,
    make_ios_db,
    seed_android_db,
)
from tests.fixtures.protobuf_factory import (
    encode_varint,
    lid_field1_bytes,
    make_compact_receipt_blob,
    make_ios_reaction_blob,
    make_receipt_blob,
    make_zreceipt_blob_with_reactions,
)
from tests.viewer.conftest import (
    _make_android_app,
    make_cache_db,
    seed_android_db_with_media,
)
from wab_viewer import main as viewer

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


def _link_dir(src, dst):
    """Create a directory symlink or junction from dst pointing to src.

    Returns True if successfully created, False if platform privileges prevent it.
    """
    try:
        if sys.platform == "win32":
            import _winapi

            try:
                _winapi.CreateJunction(str(src), str(dst))
                return True
            except OSError:
                pass
        dst.symlink_to(src, target_is_directory=True)
        return True
    except OSError:
        return False


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

    def test_media_served_from_symlinked_folder(self, app_and_tmp, tmp_path_factory):
        # Contacts/ kept on another disk and linked into the archive: its files must be served.
        client, tmp = app_and_tmp
        elsewhere = tmp_path_factory.mktemp("other_disk")
        (elsewhere / "Alice").mkdir()
        (elsewhere / "Alice" / "IMG.jpg").write_bytes(b"photo")
        if not _link_dir(elsewhere, tmp / "Contacts"):
            pytest.skip("Cannot create directory symlink or junction on this environment")
        resp = client.get("/media/Contacts/Alice/IMG.jpg")
        assert resp.status_code == 200
        assert resp.data == b"photo"

    def test_media_served_from_symlinked_groups_folder(self, app_and_tmp, tmp_path_factory):
        # Groups/ kept on another disk and linked into the archive: its files must be served.
        client, tmp = app_and_tmp
        elsewhere = tmp_path_factory.mktemp("other_disk_groups")
        (elsewhere / "Family").mkdir()
        (elsewhere / "Family" / "VID.mp4").write_bytes(b"video_bytes")
        if not _link_dir(elsewhere, tmp / "Groups"):
            pytest.skip("Cannot create directory symlink or junction on this environment")
        resp = client.get("/media/Groups/Family/VID.mp4")
        assert resp.status_code == 200
        assert resp.data == b"video_bytes"

    def test_media_403_for_traversal_next_to_symlinked_folder(self, app_and_tmp, tmp_path_factory):
        client, tmp = app_and_tmp
        elsewhere = tmp_path_factory.mktemp("other_disk")
        (elsewhere / "secret.txt").write_bytes(b"secret")
        if not _link_dir(elsewhere, tmp / "Contacts"):
            pytest.skip("Cannot create directory symlink or junction on this environment")
        assert client.get("/media/Contacts/../../secret.txt").status_code == 403

    def test_media_403_for_arbitrary_unwhitelisted_symlink(self, app_and_tmp, tmp_path_factory):
        # Arbitrary symlink pointing outside archive (not Contacts or Groups) must be rejected
        client, tmp = app_and_tmp
        elsewhere = tmp_path_factory.mktemp("other_disk")
        (elsewhere / "secret.txt").write_bytes(b"secret")
        if not _link_dir(elsewhere, tmp / "evil_folder"):
            pytest.skip("Cannot create directory symlink or junction on this environment")
        assert client.get("/media/evil_folder/secret.txt").status_code == 403

    def test_media_404_for_directory_path(self, app_and_tmp, tmp_path_factory):
        # Requesting a directory path instead of a file must return 404, not unhandled 500
        client, tmp = app_and_tmp
        elsewhere = tmp_path_factory.mktemp("other_disk")
        (elsewhere / "Alice").mkdir()
        if not _link_dir(elsewhere, tmp / "Contacts"):
            pytest.skip("Cannot create directory symlink or junction on this environment")
        assert client.get("/media/Contacts").status_code == 404
        assert client.get("/media/Contacts/Alice").status_code == 404

    def test_media_range_request_through_symlink(self, app_and_tmp, tmp_path_factory):
        client, tmp = app_and_tmp
        elsewhere = tmp_path_factory.mktemp("other_disk")
        (elsewhere / "test.mp4").write_bytes(b"0123456789ABCDEF")
        if not _link_dir(elsewhere, tmp / "Contacts"):
            pytest.skip("Cannot create directory symlink or junction on this environment")
        resp = client.get("/media/Contacts/test.mp4", headers={"Range": "bytes=4-9"})
        assert resp.status_code == 206
        assert resp.data == b"456789"

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

    def test_android_chat_info_excludes_service_messages_from_first_and_last_ts(self, tmp_path):
        """Android /api/chat-info first_ts and last_ts exclude system/service messages."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000001')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (30, 2, 'Project Chat', 0, 1700000000000, 4)"
        )
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (3, '987654321')")

        # Msg 1: Leading service message (group created / encryption notice)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (1, 30, 0, 3, 1600000000000, 'Group created', 7)"
        )
        # Msg 2: First conversational message (text)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (2, 30, 0, 3, 1650000000000, 'First real message', 0)"
        )
        # Msg 3: Last conversational message (image)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (3, 30, 1, NULL, 1680000000000, '', 1)"
        )
        wa_conn.execute(
            "INSERT INTO message_media (message_row_id, file_path) VALUES (3, 'WhatsApp/Media/WhatsApp Images/photo.jpg')"
        )
        # Msg 4: Trailing service message (e.g. member left)
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (4, 30, 0, 3, 1700000000000, 'Alice left', 7)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('30', 'Project Chat', 'Project Chat')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=30&chat_type=group").get_json()

        assert data["first_ts"] == 1650000000000
        assert data["last_ts"] == 1680000000000
        assert data["total"] == 2
        assert data["sent"] == 1
        assert data["received"] == 1

    def test_android_chat_info_only_service_messages_returns_none_timestamps(self, tmp_path):
        """Android /api/chat-info with only service messages returns None first_ts/last_ts."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"
        wa_conn = make_android_db(wa_path)
        archive_conn = make_archive_db(archive_path)

        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (2, '120363000000002')")
        wa_conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject, hidden, sort_timestamp, display_message_row_id) "
            "VALUES (31, 2, 'Empty Group', 0, 1600000000000, 1)"
        )
        wa_conn.execute("INSERT INTO jid (_id, user) VALUES (3, '987654321')")
        wa_conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (1, 31, 0, 3, 1600000000000, 'Group created', 7)"
        )
        wa_conn.commit()

        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('31', 'Empty Group', 'Empty Group')"
        )
        archive_conn.commit()
        wa_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=31&chat_type=group").get_json()

        assert data["first_ts"] is None
        assert data["last_ts"] is None
        assert data["total"] == 0

    def test_ios_chat_info_excludes_service_messages_from_first_and_last_ts(self, tmp_path):
        """iOS /api/chat-info first_ts and last_ts exclude ZMESSAGETYPE in (6, 10)."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")

        cs_conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (40, 1, '120363000000040@g.us', 'iOS Project', 0)
        """)
        cs_conn.execute("""
            INSERT INTO ZWAGROUPINFO (Z_PK, ZCREATIONDATE, ZCREATORJID)
            VALUES (1, 100.0, '15550001111@s.whatsapp.net')
        """)
        cs_conn.execute("""
            INSERT INTO ZWAMEDIAITEM (Z_PK, ZMEDIALOCALPATH, ZTITLE)
            VALUES (1, 'Media/photo.jpg', NULL)
        """)

        # Msg 1: Group event (type 10: created/joined)
        cs_conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZGROUPEVENTTYPE, ZTEXT)
            VALUES (1, 40, 0, 100.0, 10, 1, 'Group created')
        """)
        # Msg 2: First conversational message (text)
        cs_conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZGROUPEVENTTYPE, ZTEXT)
            VALUES (2, 40, 0, 200.0, 0, NULL, 'Hello team')
        """)
        # Msg 3: Last conversational message (photo)
        cs_conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZGROUPEVENTTYPE, ZMEDIAITEM, ZTEXT)
            VALUES (3, 40, 1, 300.0, 1, NULL, 1, NULL)
        """)
        # Msg 4: Trailing service message (type 6: subject changed)
        cs_conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZGROUPEVENTTYPE, ZTEXT)
            VALUES (4, 40, 0, 400.0, 6, NULL, 'Subject changed')
        """)
        cs_conn.commit()

        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('40', 'iOS Project', 'iOS Project')"
        )
        archive_conn.commit()
        cs_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=40&chat_type=group").get_json()

        expected_first = int((200.0 + 978307200) * 1000)
        expected_last = int((300.0 + 978307200) * 1000)
        assert data["first_ts"] == expected_first
        assert data["last_ts"] == expected_last
        assert data["total"] == 2
        assert data["sent"] == 1
        assert data["received"] == 1

    def test_ios_chat_info_only_service_messages_returns_none_timestamps(self, tmp_path):
        """iOS /api/chat-info with only service messages returns None first_ts/last_ts."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        archive_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")

        cs_conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZGROUPINFO, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (41, 1, '120363000000041@g.us', 'Empty iOS Group', 0)
        """)
        cs_conn.execute("""
            INSERT INTO ZWAGROUPINFO (Z_PK, ZCREATIONDATE, ZCREATORJID)
            VALUES (1, 100.0, '15550001111@s.whatsapp.net')
        """)
        cs_conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZMESSAGETYPE, ZGROUPEVENTTYPE, ZTEXT)
            VALUES (1, 41, 0, 100.0, 10, 1, 'Group created')
        """)
        cs_conn.commit()

        archive_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject) VALUES ('41', 'Empty iOS Group', 'Empty iOS Group')"
        )
        archive_conn.commit()
        cs_conn.close()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            data = client.get("/api/chat-info?chat_id=41&chat_type=group").get_json()

        assert data["first_ts"] is None
        assert data["last_ts"] is None
        assert data["total"] == 0

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


class TestChatInfoMediaSizeAndStatus:
    def test_chat_info_media_size_contact_and_group(self, tmp_path):
        from tests.conftest import make_android_db, make_archive_db

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
        from tests.conftest import make_archive_db

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
        from tests.conftest import make_archive_db

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

    def test_api_chats_chunking_handles_large_contact_set(self, tmp_path):
        """Verify that more than 900 contact JIDs/messages in a chat list are safely chunked without SQL variable limits."""
        wa_path = tmp_path / "msgstore.db"
        archive_path = tmp_path / ".wa_media_archiver.db"

        conn = sqlite3.connect(str(wa_path))
        conn.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, hidden INTEGER DEFAULT 0, sort_timestamp INTEGER, display_message_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER NOT NULL, from_me INTEGER NOT NULL DEFAULT 0, sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER DEFAULT 0);
            CREATE TABLE message_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER, chat_row_id INTEGER, file_path TEXT, file_size INTEGER, media_name TEXT, message_url TEXT, mime_type TEXT, file_hash TEXT, transferred INTEGER);
            CREATE TABLE message_quoted (_id INTEGER PRIMARY KEY, message_row_id INTEGER, chat_row_id INTEGER, parent_message_chat_row_id INTEGER, from_me INTEGER, key_id TEXT, text_data TEXT);
            CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
            CREATE TABLE lid_display_name (lid_row_id INTEGER PRIMARY KEY, display_name TEXT, username TEXT);
            CREATE TABLE message_poll (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL);
            CREATE TABLE message_view_once_media (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL);
            CREATE TABLE message_revoked (_id INTEGER PRIMARY KEY, message_row_id INTEGER NOT NULL);
        """)

        # Insert 950 chats and messages (exceeding standard SQLite 900 batch size)
        chat_rows = []
        jid_rows = []
        msg_rows = []
        for i in range(1, 951):
            jid_rows.append(
                (i, f"1555000{i:04d}", "s.whatsapp.net", f"1555000{i:04d}@s.whatsapp.net")
            )
            chat_rows.append((i, i, None, 0, 1700000000000 + i, i))
            msg_rows.append((i, i, 0, i, 1700000000000 + i, f"Message {i}", 0))

        conn.executemany("INSERT INTO jid VALUES (?, ?, ?, ?)", jid_rows)
        conn.executemany("INSERT INTO chat VALUES (?, ?, ?, ?, ?, ?)", chat_rows)
        conn.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?)", msg_rows)
        conn.commit()
        conn.close()

        archive_conn = make_archive_db(archive_path)
        archive_conn.commit()
        archive_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            assert len(chats) == 950
