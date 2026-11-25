"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import sqlite3
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
    _get_app_js,
    make_cache_db,
)
from wab_viewer import fts as viewer_fts
from wab_viewer import main as viewer
from wab_viewer.queries import sql_builder as viewer_sql_builder

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


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
        assert "sidebar.search_chats_placeholder" in js
        assert "sidebar.search_archived_placeholder" in js
        assert "sidebar.search_starred_placeholder" in js
        assert "sidebar.search_calls_placeholder" in js
        # Verify keyboard shortcuts
        assert "e.key === '/'" in js
        assert "e.key.toLowerCase() === 'k'" in js


class TestChatListSearchAndDescription:
    """Tests for chat list search subtitles and group description propagation."""

    def test_app_js_bundle_includes_updated_search_subtitles_and_section_header(self):
        js = _get_app_js()
        # Section header renamed to Chats (via i18n key)
        assert "makeSectionLabel(t('search.chats_section'))" in js
        # Subtitle logic contains Group chat fallback (via i18n key)
        assert "t('chat.group_conversation')" in js
        # Subtitle logic formats contact phone number
        assert "formatPhoneNumber(chat.phone_number || chat.id)" in js
        # Client-side search filters include description and phone_number
        assert "c.description" in js
        assert "c.phone_number" in js

    def test_android_api_chats_includes_group_description(self, tmp_path):
        conn = make_android_db(tmp_path / "msgstore.db")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS message_system (message_row_id INTEGER PRIMARY KEY, action_type INTEGER)"
        )
        # 1. Individual contact chat
        conn.execute(
            "INSERT INTO jid (_id, user, raw_string) VALUES (1, '123456789', '123456789@s.whatsapp.net')"
        )
        conn.execute("INSERT INTO chat (_id, jid_row_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (1, 1, 0, 'k1', 1, 1700000001000, 'Hello contact', 0)"
        )

        # 2. Group chat with system description (action 27)
        conn.execute("INSERT INTO jid (_id, user, raw_string) VALUES (2, 'group1', 'group1@g.us')")
        conn.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (2, 2, 'Hiking Club')")
        conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (2, 2, 0, 'k2', 1, 1700000002000, 'Hiking club announcements', 7)"
        )
        conn.execute("INSERT INTO message_system (message_row_id, action_type) VALUES (2, 27)")

        # 3. Group chat where description was changed then deleted (action 28)
        conn.execute("INSERT INTO jid (_id, user, raw_string) VALUES (3, 'group2', 'group2@g.us')")
        conn.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (3, 3, 'Old Group')")
        conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (3, 3, 0, 'k3', 1, 1700000003000, 'Old desc', 7)"
        )
        conn.execute("INSERT INTO message_system (message_row_id, action_type) VALUES (3, 27)")
        conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (4, 3, 0, 'k4', 1, 1700000004000, '', 7)"
        )
        conn.execute("INSERT INTO message_system (message_row_id, action_type) VALUES (4, 28)")

        # 4. Group chat with fallback to arch.groups description
        conn.execute("INSERT INTO jid (_id, user, raw_string) VALUES (4, 'group3', 'group3@g.us')")
        conn.execute("INSERT INTO chat (_id, jid_row_id, subject) VALUES (4, 4, 'Book Club')")
        conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp, text_data, message_type) "
            "VALUES (5, 4, 0, 'k5', 1, 1700000005000, 'Welcome', 0)"
        )
        conn.commit()
        conn.close()

        # Archive DB with groups description
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_cols = {c[1] for c in arch_conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "description" not in arch_cols:
            arch_conn.execute("ALTER TABLE groups ADD COLUMN description TEXT")
        arch_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject, description) VALUES ('4', 'Book Club', 'Book Club', 'Weekly reading list')"
        )
        arch_conn.commit()
        arch_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            chat_map = {c["id"]: c for c in chats}

            # Contact chat has description None and phone number
            c1 = chat_map["123456789"]
            assert c1["type"] == "contact"
            assert c1["description"] is None
            assert c1["phone_number"] == "123456789"

            # Group 2 has description from message_system 27
            c2 = chat_map["2"]
            assert c2["type"] == "group"
            assert c2["description"] == "Hiking club announcements"

            # Group 3 had description deleted by action 28
            c3 = chat_map["3"]
            assert c3["type"] == "group"
            assert c3["description"] is None

            # Group 4 fell back to arch.groups.description
            c4 = chat_map["4"]
            assert c4["type"] == "group"
            assert c4["description"] == "Weekly reading list"

    def test_ios_api_chats_includes_group_description(self, tmp_path):
        conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        gi_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAGROUPINFO)").fetchall()}
        if "ZDESCRIPTION" not in gi_cols:
            conn.execute("ALTER TABLE ZWAGROUPINFO ADD COLUMN ZDESCRIPTION TEXT")
        if "ZPICTUREID" not in gi_cols:
            conn.execute("ALTER TABLE ZWAGROUPINFO ADD COLUMN ZPICTUREID TEXT")

        # 1. Group with direct ZDESCRIPTION
        conn.execute(
            "INSERT INTO ZWAGROUPINFO (Z_PK, ZDESCRIPTION) VALUES (1, 'Cooking and recipes')"
        )
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZGROUPINFO)
            VALUES (1, 'group1@g.us', 'Foodies', 0, 1)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE)
            VALUES (1, 1, 0, 720000000, 'Welcome foodies', 0)
        """)

        # 2. Contact chat
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN)
            VALUES (2, '987654321@s.whatsapp.net', 'Alice', 0)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE)
            VALUES (2, 2, 0, 720000001, 'Hello', 0)
        """)

        # 3. Group with fallback to arch.groups description
        conn.execute("INSERT INTO ZWAGROUPINFO (Z_PK) VALUES (3)")
        conn.execute("""
            INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN, ZGROUPINFO)
            VALUES (3, 'group3@g.us', 'Tech Talks', 0, 3)
        """)
        conn.execute("""
            INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE)
            VALUES (3, 3, 0, 720000002, 'Tech discussion', 0)
        """)
        conn.commit()
        conn.close()

        # Archive DB
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_cols = {c[1] for c in arch_conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "description" not in arch_cols:
            arch_conn.execute("ALTER TABLE groups ADD COLUMN description TEXT")
        arch_conn.execute(
            "INSERT INTO groups (chat_row_id, folder, subject, description) VALUES ('3', 'Tech Talks', 'Tech Talks', 'Python & Rust talks')"
        )
        arch_conn.commit()
        arch_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            chat_map = {c["id"]: c for c in chats}

            # Group 1 has direct description
            c1 = chat_map["1"]
            assert c1["type"] == "group"
            assert c1["description"] == "Cooking and recipes"

            # Contact 2 has description None and phone number
            c2 = chat_map["2"]
            assert c2["type"] == "contact"
            assert c2["description"] is None
            assert c2["phone_number"] == "987654321"

            # Group 3 fell back to arch.groups
            c3 = chat_map["3"]
            assert c3["type"] == "group"
            assert c3["description"] == "Python & Rust talks"

    def test_preserved_chats_includes_description(self, tmp_path):
        wa_conn = make_android_db(tmp_path / "msgstore.db")
        wa_conn.close()

        pres_db = tmp_path / "preserved_messages.db"
        conn = sqlite3.connect(str(pres_db))
        conn.execute("""
            CREATE TABLE preserved_chats (
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                newest_ts INTEGER,
                subject TEXT,
                description TEXT,
                PRIMARY KEY (chat_id, chat_type)
            )
        """)
        conn.execute("""
            CREATE TABLE preserved_messages (
                msg_id INTEGER PRIMARY KEY,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                sender TEXT NOT NULL,
                from_me INTEGER NOT NULL,
                text_body TEXT,
                media_type TEXT DEFAULT 'text',
                media_name TEXT,
                is_view_once INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO preserved_chats (chat_id, chat_type, newest_ts, subject, description) "
            "VALUES ('grp_pres_1', 'group', 1700000000000, 'Preserved Group', 'Preserved group description')"
        )
        conn.execute(
            "INSERT INTO preserved_chats (chat_id, chat_type, newest_ts, subject, description) "
            "VALUES ('123456789', 'contact', 1700000001000, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO preserved_messages (msg_id, chat_id, chat_type, timestamp_ms, sender, from_me, text_body) "
            "VALUES (1, 'grp_pres_1', 'group', 1700000000000, 'user', 0, 'Preserved msg')"
        )
        conn.execute(
            "INSERT INTO preserved_messages (msg_id, chat_id, chat_type, timestamp_ms, sender, from_me, text_body) "
            "VALUES (2, '123456789', 'contact', 1700000001000, 'user', 0, 'Preserved 1:1 msg')"
        )
        conn.commit()
        conn.close()

        # Archive DB
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/chats")
            assert resp.status_code == 200
            chats = resp.get_json()
            chat_map = {c["id"]: c for c in chats}

            g = chat_map["grp_pres_1"]
            assert g["type"] == "group"
            assert g["is_preserved_only"] is True
            assert g["description"] == "Preserved group description"

            c = chat_map["123456789"]
            assert c["type"] == "contact"
            assert c["is_preserved_only"] is True
            assert c["description"] is None


class TestLocalizedMentionSearch:
    """Tests for localized @all mention search (@tutti, @alle, @tous, @todos, @all)."""

    def test_search_localized_all_mentions_and_at_prefix(self, tmp_path):
        make_archive_db(tmp_path / ".wa_media_archiver.db").close()
        conn = make_android_db(tmp_path / "msgstore.db")
        conn.execute(
            "INSERT INTO jid (_id, user, raw_string) "
            "VALUES (1, '15550001111', '15550001111@s.whatsapp.net')"
        )
        conn.execute(
            "INSERT INTO jid (_id, user, raw_string) "
            "VALUES (2, '120363000000000999', '120363000000000999@g.us')"
        )
        conn.execute(
            "INSERT INTO chat (_id, jid_row_id, subject) VALUES (1, 1, NULL), (2, 2, 'Team Group')"
        )
        conn.execute(
            "INSERT INTO message (_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data) "
            "VALUES "
            "(1, 1, 0, 'C1', 1700000001000, 0, 'Hey @all in 1-to-1'), "
            "(2, 2, 0, 'G1', 1700000002000, 0, 'Hey @all, please check this'), "
            "(3, 2, 0, 'G2', 1700000003000, 0, 'All good in this group'), "
            "(4, 2, 0, 'G3', 1700000004000, 0, 'Hello @allen')"
        )
        conn.commit()
        conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            client.get("/api/messages?chat_id=15550001111&chat_type=contact")
            client.get("/api/messages?chat_id=2&chat_type=group")

            deadline = time.time() + 5
            while time.time() < deadline:
                s1 = client.get(
                    "/api/chat-index-status?chat_id=15550001111&chat_type=contact"
                ).get_json()["status"]
                s2 = client.get("/api/chat-index-status?chat_id=2&chat_type=group").get_json()[
                    "status"
                ]
                if s1 == "done" and s2 == "done":
                    break
                time.sleep(0.05)

            for tok in ("@tutti", "@alle", "@tous", "@todos", "@all", "@TUTTI"):
                resp = client.get(f"/api/search?q={tok}")
                assert resp.status_code == 200
                results = resp.get_json()["results"]
                assert [r["msg_id"] for r in results] == [2], f"Failed for {tok}"

            # In-chat group search matches @all; 1-to-1 chat search returns empty
            grp_resp = client.get("/api/search?q=@tutti&chat_id=2&chat_type=group")
            assert grp_resp.status_code == 200
            assert [r["msg_id"] for r in grp_resp.get_json()["results"]] == [2]

            ctc_resp = client.get("/api/search?q=@tutti&chat_id=15550001111&chat_type=contact")
            assert ctc_resp.status_code == 200
            assert ctc_resp.get_json()["results"] == []

            # Bare @mention search (e.g. @allen) does not cause FTS5 syntax error
            allen_resp = client.get("/api/search?q=@allen")
            assert allen_resp.status_code == 200
            assert [r["msg_id"] for r in allen_resp.get_json()["results"]] == [4]
