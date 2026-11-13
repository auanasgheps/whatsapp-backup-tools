"""Unit and integration tests for call outcome resolution and consistency across iOS and Android."""

import sqlite3
from pathlib import Path

from tests.conftest import make_android_db, make_archive_db, make_ios_db
from wab_viewer import main as viewer
from wab_viewer.queries.calls import _map_android_call_outcome, _map_ios_call_outcome


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
