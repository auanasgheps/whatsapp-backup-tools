"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import sqlite3
import time
from pathlib import Path

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
from wab_viewer import main as viewer
from wab_viewer.queries.calls import _map_android_call_outcome, _map_ios_call_outcome

# Protobuf wire builder aliases for compatibility
_encode_varint = encode_varint
_lid_field1_bytes = lid_field1_bytes
_make_receipt_blob = make_receipt_blob
_make_compact_receipt_blob = make_compact_receipt_blob
_make_zreceipt_blob_with_reactions = make_zreceipt_blob_with_reactions
_make_ios_reaction_blob = make_ios_reaction_blob


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
        assert "time.nearest_prior" in js
        assert "function jumpFromCallToChat(call, matchedChat)" in js
        assert "async function openChatAtTimestamp(chatId, timestampMs)" in js
        assert "function showContextJumpBanner(icon, text, onSwitch)" in js


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
        assert "_infoRow(t('nav.calls')" in js
        assert "'Preserved calls'" not in js
        assert "async function openChatAtTimestamp(chatId, timestampMs)" in js


class TestCallOutcomeHelpers:
    """Validate deterministic mapping of (call_type, outcome) for Android and iOS."""

    def test_android_duration_always_answered(self):
        """Any call with duration > 0 is answered and never missed, regardless of call_result."""
        assert _map_android_call_outcome(0, 2, 188) == ("incoming", "answered")
        assert _map_android_call_outcome(0, 3, 45) == ("incoming", "answered")
        assert _map_android_call_outcome(0, 5, 10) == ("incoming", "answered")
        assert _map_android_call_outcome(0, 1, 60) == ("incoming", "answered")
        assert _map_android_call_outcome(1, 2, 188) == ("outgoing", "answered")
        assert _map_android_call_outcome(1, 1, 60) == ("outgoing", "answered")

    def test_android_zero_duration_missed_or_declined(self):
        """Calls with duration == 0 map correctly to missed, declined, or unanswered."""
        assert _map_android_call_outcome(0, 2, 0) == ("missed", "missed")
        assert _map_android_call_outcome(0, 3, 0) == ("missed", "declined")
        assert _map_android_call_outcome(0, 5, 0) == ("missed", "missed")
        assert _map_android_call_outcome(1, 3, 0) == ("outgoing", "declined")
        assert _map_android_call_outcome(1, 1, 0) == ("outgoing", "unanswered")

    def test_ios_duration_always_answered(self):
        """Any iOS call with duration > 0 is answered and never missed, regardless of outcome_code."""
        assert _map_ios_call_outcome(0, 2, 188) == ("incoming", "answered")
        assert _map_ios_call_outcome(0, 3, 188) == ("incoming", "answered")
        assert _map_ios_call_outcome(0, 4, 188) == ("incoming", "answered")
        assert _map_ios_call_outcome(0, 0, 188) == ("incoming", "answered")
        assert _map_ios_call_outcome(1, 2, 188) == ("outgoing", "answered")
        assert _map_ios_call_outcome(1, 0, 188) == ("outgoing", "answered")

    def test_ios_zero_duration_missed_or_declined(self):
        """Calls with duration == 0 map correctly to missed, declined, or canceled."""
        assert _map_ios_call_outcome(0, 4, 0) == ("missed", "missed")
        assert _map_ios_call_outcome(0, 3, 0) == ("missed", "declined")
        assert _map_ios_call_outcome(0, 1, 0) == ("missed", "canceled")
        assert _map_ios_call_outcome(1, 1, 0) == ("outgoing", "canceled")
        assert _map_ios_call_outcome(1, 3, 0) == ("outgoing", "declined")
        assert _map_ios_call_outcome(1, 0, 0) == ("outgoing", "unanswered")


class TestCallOutcomeEndToEnd:
    """Validate end-to-end API and timeline behavior for calls."""

    def test_android_answered_call_never_marked_missed(self, tmp_path: Path):
        """Android incoming call with duration > 0 and call_result in (2, 3, 5) is incoming/answered, not missed."""
        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.execute(
            "INSERT INTO contacts (number, folder, display_name) VALUES ('15551111', 'Bob (15551111)', 'Bob')"
        )
        arch_conn.commit()
        arch_conn.close()

        wa_conn = make_android_db(tmp_path / "msgstore.db")
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
            INSERT INTO jid (_id, user, raw_string) VALUES (1, '15551111', '15551111@s.whatsapp.net');
            INSERT INTO chat (_id, jid_row_id, subject, sort_timestamp) VALUES (10, 1, NULL, 1700000050000);

            -- Incoming video call lasting 188s (3m 8s) with call_result = 2 (e.g. ended by partner/busy)
            INSERT INTO call_log VALUES (1, 'call_android_ans_1', 1, 0, 1700000020000, 1, 188, 2, NULL);
            -- Real incoming missed call with duration = 0 and call_result = 2
            INSERT INTO call_log VALUES (2, 'call_android_miss_2', 1, 0, 1700000030000, 0, 0, 2, NULL);
        """)
        wa_conn.commit()
        wa_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            all_calls = client.get("/api/calls").get_json()["calls"]
            assert len(all_calls) == 2
            ans_call = next(c for c in all_calls if c["id"] == "call_android_ans_1")
            assert ans_call["call_type"] == "incoming"
            assert ans_call["is_missed"] is False
            assert ans_call["duration"] == 188

            miss_call = next(c for c in all_calls if c["id"] == "call_android_miss_2")
            assert miss_call["call_type"] == "missed"
            assert miss_call["is_missed"] is True

            # Filter missed calls: only the real missed call with duration=0 is returned
            missed_resp = client.get("/api/calls?filter=missed").get_json()["calls"]
            assert len(missed_resp) == 1
            assert missed_resp[0]["id"] == "call_android_miss_2"

            # In chat timeline, call bubble must be incoming and answered
            msgs = client.get(
                "/api/messages?chat_id=15551111&chat_type=contact&limit=10"
            ).get_json()
            call_bubbles = [m for m in msgs if m["media_type"] == "call"]
            assert len(call_bubbles) == 2
            ans_bubble = next(b for b in call_bubbles if b["msg_id"] == "call:call_android_ans_1")
            assert ans_bubble["call"]["call_type"] == "incoming"
            assert ans_bubble["call"]["outcome"] == "answered"
            assert ans_bubble["call"]["duration_sec"] == 188

    def test_ios_answered_call_never_marked_missed(self, tmp_path: Path):
        """iOS incoming call with duration > 0 and ZOUTCOME in (2, 3) is incoming/answered, not missed."""
        cs_conn = make_ios_db(tmp_path / "ChatStorage.sqlite")
        cs_conn.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZHIDDEN) "
            "VALUES (1, '15552222@s.whatsapp.net', 'Carol', 0)"
        )
        cs_conn.commit()
        cs_conn.close()

        arch_conn = make_archive_db(tmp_path / ".wa_media_archiver.db")
        arch_conn.commit()
        arch_conn.close()

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
            -- Aggregate 1: Incoming video call, duration 188s, ZOUTCOME = 2 (remote ended), ZMISSED = 0
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING, ZMISSED) VALUES (1, 1, 1, 0);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS)
            VALUES (1, 'call_ios_ans_1', 188, 2, 720000000, 1);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (1, 1, '15552222@s.whatsapp.net', 2);

            -- Aggregate 2: Real incoming missed call, duration 0, ZOUTCOME = 4, ZMISSED = 1
            INSERT INTO ZWAAGGREGATECALLEVENT (Z_PK, ZVIDEO, ZINCOMING, ZMISSED) VALUES (2, 0, 1, 1);
            INSERT INTO ZWACDCALLEVENT (Z_PK, ZCALLIDSTRING, ZDURATION, ZOUTCOME, ZDATE, Z1CALLEVENTS)
            VALUES (2, 'call_ios_miss_2', 0, 4, 720000100, 2);
            INSERT INTO ZWACDCALLEVENTPARTICIPANT (Z_PK, Z1PARTICIPANTS, ZJIDSTRING, ZOUTCOME)
            VALUES (2, 2, '15552222@s.whatsapp.net', 4);
        """)
        call_conn.commit()
        call_conn.close()

        app = viewer.create_app(tmp_path, rescan=False)
        app.config["TESTING"] = True
        with app.test_client() as client:
            all_calls = client.get("/api/calls").get_json()["calls"]
            assert len(all_calls) == 2
            ans_call = next(c for c in all_calls if c["id"] == "call_ios_ans_1")
            assert ans_call["call_type"] == "incoming"
            assert ans_call["is_missed"] is False
            assert ans_call["duration"] == 188

            miss_call = next(c for c in all_calls if c["id"] == "call_ios_miss_2")
            assert miss_call["call_type"] == "missed"
            assert miss_call["is_missed"] is True

            # Filter missed calls: only the real missed call with duration=0 is returned
            missed_resp = client.get("/api/calls?filter=missed").get_json()["calls"]
            assert len(missed_resp) == 1
            assert missed_resp[0]["id"] == "call_ios_miss_2"

            # In chat timeline, call bubble must be incoming and answered
            msgs = client.get("/api/messages?chat_id=1&chat_type=contact&limit=10").get_json()
            call_bubbles = [m for m in msgs if m["media_type"] == "call"]
            assert len(call_bubbles) == 2
            ans_bubble = next(b for b in call_bubbles if b["msg_id"] == "call:call_ios_ans_1")
            assert ans_bubble["call"]["call_type"] == "incoming"
            assert ans_bubble["call"]["outcome"] == "answered"
            assert ans_bubble["call"]["duration_sec"] == 188
