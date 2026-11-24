"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import sqlite3
import sys
import time

import pytest
from tests.conftest import (
    make_android_db,
    make_archive_db,
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
    _seed_android_db_large,
    make_cache_db,
)
from wab_viewer import db_cache as viewer_db_cache
from wab_viewer import fts as viewer_fts
from wab_viewer import hydration as viewer_hydration
from wab_viewer import main as viewer

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


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

    def test_main_start_header(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        archive_dir = str(tmp_path)
        mock_app = MagicMock()

        with (
            patch.object(sys, "argv", ["wab-viewer", archive_dir]),
            patch("wab_viewer.main.validate_output_root"),
            patch("wab_viewer.main.create_app", return_value=mock_app),
            patch("wab_viewer.main.webbrowser.open"),
        ):
            viewer.main()
            captured = capsys.readouterr()
            expected_header = f"=== WhatsApp Backup Tools - Viewer v{viewer.get_version()} ==="
            assert expected_header in captured.out
            assert captured.out.strip().startswith(expected_header)
