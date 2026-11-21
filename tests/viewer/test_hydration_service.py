"""Tests for wab_viewer."""

from __future__ import annotations

import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

import os
import sqlite3

from tests.conftest import (
    make_archive_db,
)
from tests.fixtures.protobuf_factory import (
    encode_varint,
    lid_field1_bytes,
    make_compact_receipt_blob,
    make_ios_reaction_blob,
    make_receipt_blob,
    make_zreceipt_blob_with_reactions,
)
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


class TestVcardViewer:
    def test_media_type_from_path_vcf(self):
        assert viewer_hydration._media_type_from_path("Contacts/Alice/Bob.vcf") == "vcard"
        assert viewer_hydration._media_type_from_path("Contacts/Alice/Bob.VCF") == "vcard"

    def test_android_vcard_messages_endpoint(self, tmp_path):
        from tests.conftest import make_archive_db

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
        from tests.conftest import make_archive_db

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
        from tests.conftest import make_archive_db

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
